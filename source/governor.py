"""governor: the main loop. While a game runs it watches the frame timing and lowers the clock of whichever chip
(CPU or GPU) is NOT holding the frame rate back, so the shared cooler's heat budget goes to the one that is.

How it works, in one pass per second (see session()):
  1. PresentMon gives per-frame timings; classify() decides if the game is GPU-bound, CPU-bound or capped.
  2. Policy turns that into a target clock limit for each chip, using the profile (how fast each chip is at each
     limit). It lowers one step at a time, checks the frame rate didn't drop, and reverts if it did.
  3. Actuator applies the limit (nvidia-smi for the GPU, the power plan for the CPU) and everything is logged.
When the game closes, both limits are removed.
"""
import argparse
import csv
import shlex
import time
from collections import deque
from statistics import median

from hwctl import (AB_CPU_CLOCK, AB_CPU_POWER, AB_CPU_TEMP, HERE, Afterburner, GpuSensor, PresentMonReader,
                   cpu_cap, display_refresh_hz, ensure_presentmon, fnum, foreground_app, frame_stats, gpu_lock,
                   gpu_reset, install_close_handler, load_map, pick_index, process_names, require_windows_admin,
                   restore_all, run_app, save_restore)

LOG = HERE / "gamegov_log.csv"                  # one row per second while governing (thermalshare.py repoints this)
LOG_COLS = ["time", "app", "mode", "action", "fps", "low1", "low5", "ft", "fps_cap", "cpu_busy", "gpu_busy",
            "cpu_src", "gpu_cap", "cpu_cap", "gpu_clock", "gpu_power", "gpu_temp", "gpu_thermal", "cpu_clock",
            "cpu_power", "cpu_temp"]
# Tunable settings (each is also a --option on the command line, e.g. --gpu-thr 0.9):
#   tick       seconds between decisions            window    seconds of frames each decision looks at
#   gpu_thr    GPU busy >= this share of the frame = GPU-bound     cpu_thr   same for the CPU
#   margin     keep the trimmed chip this far under the frame time (0.85 = 15% headroom)
#   eval_s     seconds to wait after a trim before judging it      hold_s    wait before retrying after a revert
#   tol_fps    revert a trim if median FPS falls by more than this fraction;  tol_low: same for the 5% low FPS
#   fps_cap    fixed frame limit (0 = none; unset = find it)        cap_tol   how close to a cap counts as "at the cap"
#   cpu_trust  CPU busy >= this share of the frame while not CPU-bound = busy time is really waiting (see Policy.vote)
#   cpu_min    never limit the CPU below this many MHz
DEFAULTS = {"tick": 1.0, "window": 2.0, "gpu_thr": 0.90, "cpu_thr": 0.85, "margin": 0.85,
            "eval_s": 2.5, "hold_s": 20.0, "tol_fps": 0.03, "tol_low": 0.06,
            "fps_cap": 0.0, "cap_tol": 0.03, "cpu_trust": 0.90, "cpu_min": 2400.0}
GAMES_FILE = HERE / "gamegov_games.txt"         # the user's game list (thermalshare.py repoints this)
GAMES_TEMPLATE = """\
# Games for governor.exe
#
# Listed games are governed whenever they run, windowed or fullscreen.
# Any other app is governed automatically once it has been fullscreen in the foreground for a few seconds,
# so most games need no entry here. Add the ones you play windowed.
#
# The governor finds each game's frame limit on its own. If it can't (the log's fps_cap column stays at your
# refresh rate while the game sits on another limit), write the limit after the name:   game.exe 60
# 0 means the game has no limit:                                                         game.exe 0
# A minus in front keeps an app from ever being governed:                                -someapp.exe
# Names with spaces go in quotes:                                                        "My Game.exe"

"""
# Fullscreen apps that aren't games: the shell, browsers, video players, launchers, chat, and TuneKit's own tools.
NOT_GAMES = {
    "explorer.exe", "lockapp.exe", "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "applicationframehost.exe", "textinputhost.exe", "dwm.exe", "taskmgr.exe",
    "windowsterminal.exe", "conhost.exe", "openconsole.exe", "cmd.exe", "powershell.exe", "mstsc.exe",
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe", "vivaldi.exe", "iexplore.exe",
    "msedgewebview2.exe", "vlc.exe", "mpv.exe", "mpc-hc.exe", "mpc-hc64.exe", "mpc-be.exe", "mpc-be64.exe",
    "potplayermini.exe", "potplayermini64.exe", "wmplayer.exe", "video.ui.exe", "microsoft.media.player.exe",
    "spotify.exe", "discord.exe", "slack.exe", "teams.exe", "ms-teams.exe", "zoom.exe", "whatsapp.exe",
    "telegram.exe", "claude.exe", "code.exe", "powerpnt.exe", "obs64.exe", "steam.exe", "steamwebhelper.exe",
    "epicgameslauncher.exe", "galaxyclient.exe", "battle.net.exe", "eadesktop.exe", "origin.exe", "upc.exe",
    "msiafterburner.exe", "rtss.exe", "throttlestop.exe", "governor.exe", "mapper.exe", "bench.exe", "synth.exe",
    "gpu_ocscan.exe", "uvtuner.exe", "synthgame.exe",
}
HELP = {"fps_cap": "frame limit in fps for every game (default: found per game; 0 = no limit)",
        "cap_tol": "fps within this fraction of --fps-cap counts as capped",
        "cpu_trust": "CPU busy >= this fraction of the frame while GPU-bound = busy time is really waiting",
        "cpu_min": "never cap the CPU below this many MHz",
        "tol_fps": "revert a trim if median fps drops more than this fraction",
        "tol_low": "revert a trim if 5%% low fps drops more than this fraction"}
NAME = {"g": "gpu", "c": "cpu"}      # the policy's short chip keys -> the names shown in the log's action column
PM_SESSION = "GameGovGovernor"   # not "GameGov": bench.exe and mapper.exe use that one and would stop ours


def ratio(st, key):
    """A busy time (ms) as a share of the frame time, e.g. 0.92 = busy 92% of the frame; None if not measured."""
    v = st.get(key)
    return v / st["ft"] if v is not None else None


def caps_of(cfg):
    """The frame-rate limits to compare against: the live list if there is one, else the fixed --fps-cap."""
    if "caps" in cfg:                     # set each tick by the session's CapTracker
        return cfg["caps"]
    cap = cfg.get("fps_cap") or 0
    return [cap] if cap > 0 else []


def at_cap(st, cfg):
    """True if the measured FPS sits within cap_tol of a frame limit: the game is waiting on its limiter, not
    working flat out."""
    return any(abs(st["fps"] - c) <= c * cfg["cap_tol"] for c in caps_of(cfg))


def find_ceiling(vals, tol=0.015, min_n=20, share=0.4, over=0.03, max_over=0.05):
    """A frame rate the game keeps sitting on and doesn't go above: its frame limiter or V-Sync.

    GPU- or CPU-bound frame rates wander with the scene; a limited one piles up at one value. Needs min_n
    samples, share of them within tol of one value, and at most max_over of them more than over above it."""
    xs = [v for v in vals if v]
    if len(xs) < min_n:                    # too little history to say
        return None
    best, best_n = None, 0
    for v in xs:                           # find the value that the most samples cluster around (ties: the higher)
        n = sum(1 for x in xs if abs(x - v) <= v * tol)
        if n > best_n or (n == best_n and v > best):
            best, best_n = v, n
    if best_n < share * len(xs):           # no value attracts enough samples: the frame rate just wanders
        return None
    c = median(x for x in xs if abs(x - best) <= best * tol)    # the cluster's typical value is the ceiling
    if sum(1 for x in xs if x > c * (1 + over)) > max_over * len(xs):
        return None                        # the frame rate often goes above it, so it isn't really a ceiling
    return round(c, 1)


class CapTracker:
    """The frame limits to check against. A fixed limit when one is given (0 = the game has none); otherwise
    the display refresh rate (V-Sync, or a limiter set to it) plus whatever ceiling the frame rate keeps
    sitting on over the last ~90 s, so a game limited to 60 on a 144 Hz panel counts as capped at 60."""

    def __init__(self, fixed=None, refresh=None, keep=90):
        self.fixed, self.refresh = fixed, refresh      # fixed: set by the user (None = find it); refresh: display Hz
        self.hist = deque(maxlen=keep)                 # recent FPS values, about one per second
        self.learned = None                            # the ceiling found in them, if any

    def update(self, fps):
        """Feed in the latest FPS and re-check for a ceiling (skipped when the limit was set by the user)."""
        if self.fixed is None and fps:
            self.hist.append(fps)
            self.learned = find_ceiling(self.hist)

    def caps(self):
        """The current list of frame limits (may be empty = no known limit)."""
        if self.fixed is not None:
            return [self.fixed] if self.fixed > 0 else []
        return [c for c in (self.refresh, self.learned) if c]

    def describe(self):
        """The frame limit in words, for the start-of-session message."""
        if self.fixed is not None:
            return f"{self.fixed:.0f} fps (set)" if self.fixed > 0 else "none (set)"
        if self.learned:
            return f"{self.learned:.0f} fps (found)"
        return f"{self.refresh:.0f} fps (display) until the game's own limit shows" if self.refresh else "finding"


def classify(st, cfg):
    """Name what is limiting the frame rate right now: "gpu" (GPU busy most of the frame), "cpu", "capped" (the
    game is waiting on its frame limiter or V-Sync, so both chips have slack), "unknown" (no busy times), or
    "idle" (no frames). `st` is the frame_stats() summary of the last window."""
    if not st:
        return "idle"
    g, c = ratio(st, "gpu"), ratio(st, "cpu")
    if g is not None and g >= cfg["gpu_thr"]:
        return "gpu"
    # A frame limiter's sleep is counted as CPU busy, so check the cap before trusting the CPU ratio.
    if at_cap(st, cfg):
        return "capped"
    if c is not None and c >= cfg["cpu_thr"]:
        return "cpu"
    if g is None and c is None:
        return "unknown"
    return "capped"


def worse(st, base, cfg):
    """Why the trial step lost performance, or None. Uses the 5% low: over a 2 s window the 1% low is the
    3rd-worst frame, so one streaming hitch swings it 5x and says nothing about the trim."""
    if st["fps"] < base["fps"] * (1 - cfg["tol_fps"]):
        return "fps"
    lo, blo = st.get("low5", st.get("low1")), base.get("low5", base.get("low1"))
    if lo and blo and lo < blo * (1 - cfg["tol_low"]):
        return "low"
    return None


def floor_index(points, mhz):
    """Index of the lowest profile point whose limit is at least `mhz`: how low the policy may go. With no
    minimum, index 0 (the lowest limit measured)."""
    if not mhz or mhz <= 0:
        return 0
    return next((i for i, p in enumerate(points) if p.get("unlimited") or p["cap"] >= mhz), len(points) - 1)


class Policy:
    """Decides, once per tick, what limit each chip should have. Pure logic: it never touches the hardware, it
    only changes self.g / self.c, the current index into the GPU and CPU profile points (lowest limit = 0, no
    limit = the last index). Lowering is cautious: one step at a time, then wait and check (self.pending) that the
    frame rate held, and revert and back off (self.hold / self.reverts) if it didn't. Raising is immediate."""

    def __init__(self, gpts, cpts, cfg):
        self.gp, self.cp, self.cfg = gpts, cpts, cfg
        self.gtop, self.ctop = len(gpts) - 1, len(cpts) - 1        # index of "no limit" for each chip
        self.g, self.c = self.gtop, self.ctop                      # start with both chips unlimited
        self.cfloor = min(floor_index(cpts, cfg.get("cpu_min")), self.ctop)     # lowest index the CPU may use
        self.hold = {"g": 0.0, "c": 0.0}         # don't try lowering this chip again before this time
        self.reverts = {"g": 0, "c": 0}          # consecutive failed trims, which lengthens the hold
        self.pending = None                      # a trim being judged: (chip, previous index, frame stats before, start time)
        self.mode = "idle"
        self.votes = deque(maxlen=10)            # recent "CPU busy time looks like waiting" votes (see vote)
        self.cpu_probe = cfg.get("cpu_busy") == "ignore"    # True = trim the CPU by trial, not by its busy time

    def vote(self, mode, st):
        """Decide whether PresentMon's CPU busy time can be planned with.

        Some games (War Thunder with Reflex, anything with an in-engine limiter) wait for the GPU or the
        frame cap inside their own frame, and PresentMon counts that wait as CPU busy. Tell-tale: with the
        CPU uncapped and the GPU or the cap setting the pace, CPU busy still reads ~the whole frame. Then the
        CPU is trimmed by trial (one step, check FPS, keep or revert) instead of by its busy time."""
        how = self.cfg.get("cpu_busy", "auto")
        if how != "auto":                        # the user forced it with --cpu-busy trust / ignore
            self.cpu_probe = how == "ignore"
            return
        # Only vote when the CPU is unlimited and something else sets the pace, so its busy time should be low.
        if mode not in ("gpu", "capped") or self.c != self.ctop or not st or st.get("cpu") is None:
            return
        self.votes.append(ratio(st, "cpu") >= self.cfg["cpu_trust"])
        if len(self.votes) >= 5:                 # need a few votes; a majority of the last 10 decides
            self.cpu_probe = sum(self.votes) * 2 > len(self.votes)

    def targets(self, mode, st):
        """The (GPU index, CPU index) this policy would like, given what is limiting the frame rate. Lower than
        the current index means "can be trimmed"; higher means "give speed back". Times are in ms per frame, and
        `m` (margin) keeps the trimmed chip safely under the time it has available."""
        m = self.cfg["margin"]
        if mode in ("idle", "unknown"):          # nothing useful to go on: run both chips at full speed
            return self.gtop, self.ctop
        cpu = None if self.cpu_probe else st.get("cpu")      # CPU busy time, unless it can't be trusted
        if mode == "gpu":
            # GPU-bound: keep the GPU unlimited; slow the CPU as far as its work still fits inside the GPU's time.
            tc = pick_index(self.cp, self.c, cpu, m * st["gpu"]) if cpu else 0
            return self.gtop, max(tc, self.cfloor)
        if mode == "cpu":
            # CPU-bound: keep the CPU unlimited; slow the GPU as far as its work fits inside the CPU's time.
            tg = pick_index(self.gp, self.g, st["gpu"], m * (cpu or st["ft"])) if st.get("gpu") else 0
            return tg, self.ctop
        # Capped (or no clear bottleneck): both chips have slack, so each may slow until its work just fits in
        # one frame.
        tg = pick_index(self.gp, self.g, st["gpu"], m * st["ft"]) if st.get("gpu") else self.g
        if cpu:
            tc = pick_index(self.cp, self.c, cpu, m * st["ft"])
        elif self.cpu_probe and at_cap(st, self.cfg):
            tc = 0                               # busy time unusable: aim as low as allowed and let the trial find out
        else:
            tc = self.c
        return tg, max(tc, self.cfloor)

    def penalize(self, comp, now):
        """A trim failed: don't try that chip again for hold_s seconds, doubling each time in a row (up to 5 min)."""
        self.hold[comp] = now + min(self.cfg["hold_s"] * 2 ** self.reverts[comp], 300)
        self.reverts[comp] += 1

    def step(self, now, st):
        """Run one decision. `now` is the time, `st` the frame stats of the last window (None = no frames).
        Returns (mode, action): the classification and a short note for the log ("gpu-", "cpu+", "eval",
        "revert-gpu:fps", "hold", ...)."""
        mode = self.mode = classify(st, self.cfg)
        self.vote(mode, st)
        tg, tc = self.targets(mode, st)
        act = []
        # 1) Raising a limit (giving speed back) happens right away, even in the middle of a trial.
        for comp, tgt in (("g", tg), ("c", tc)):
            if tgt <= getattr(self, comp):
                continue
            probing = comp == "c" and self.cpu_probe          # the CPU is being trimmed by trial
            on_trial = self.pending is not None and self.pending[0] == comp
            prev = self.pending[1] if on_trial else None      # the index before the step under trial
            if on_trial:
                self.pending = None                           # raising ends the trial
            if probing and on_trial and mode == "cpu":
                # The trial step made the CPU the bottleneck: back to the last good step, not all the way up.
                self.c = min(prev, tgt)
                self.penalize("c", now)
                act.append("revert-cpu:bneck")
                continue
            setattr(self, comp, tgt)
            if probing:
                # The bottleneck moved to the CPU after a trial was accepted; don't re-probe straight away.
                self.hold["c"] = max(self.hold["c"], now + self.cfg["hold_s"])
            else:
                self.reverts[comp] = 0
            act.append(f"{NAME[comp]}+")
        if act:
            return mode, " ".join(act)
        # 2) A trim is being judged: wait out eval_s, then keep it if FPS held or revert and back off if it didn't.
        if self.pending:
            comp, prev, base, t0 = self.pending
            if now - t0 < self.cfg["eval_s"]:
                return mode, "eval"
            self.pending = None
            why = worse(st, base, self.cfg) if st else None
            if why:
                setattr(self, comp, prev)
                self.penalize(comp, now)
                return mode, f"revert-{NAME[comp]}:{why}"
            self.reverts[comp] = 0                   # the trim worked: forget earlier failures
        if st is None:
            return mode, ""
        # 3) Start at most one new trim per tick, a single step down, GPU first. `base` (st) is what FPS must hold to.
        if tg < self.g and now >= self.hold["g"]:
            self.pending = ("g", self.g, st, now)
            self.g -= 1
            return mode, "gpu-"
        if tc < self.c and now >= self.hold["c"]:
            self.pending = ("c", self.c, st, now)
            self.c -= 1
            return mode, "cpu-"
        return mode, "hold"


class Actuator:
    """Turns the policy's chosen indexes into real limits, only touching the hardware when an index changed.
    With dry=True (--observe) it just remembers the indexes and changes nothing."""

    def __init__(self, gpts, cpts, dry=False):
        self.gp, self.cp, self.dry = gpts, cpts, dry
        self.g = self.c = None                   # the indexes currently applied (None = nothing applied yet)

    def gcap(self, i):
        """The GPU clock limit (MHz) for profile point i, or None for "no limit" (the unlimited/top point)."""
        p = self.gp[i]
        return None if p.get("unlimited") or i == len(self.gp) - 1 else p["cap"]

    def ccap(self, i):
        """The CPU frequency limit (MHz) for profile point i, or 0 for "no limit"."""
        p = self.cp[i]
        return 0 if p.get("unlimited") or i == len(self.cp) - 1 else p["cap"]

    def apply(self, gi, ci):
        """Make GPU index gi and CPU index ci the active limits."""
        if self.dry:
            self.g, self.c = gi, ci
            return
        if gi != self.g:
            cap = self.gcap(gi)
            gpu_reset() if cap is None else gpu_lock(cap)       # no limit = clear the lock, else lock to the cap
            self.g = gi
        if ci != self.c:
            cpu_cap(self.ccap(ci))
            self.c = ci


def parse_games(text):
    """gamegov_games.txt -> ({exe lowercase: frame cap or None to find it}, {excluded exe lowercase})."""
    listed, excluded = {}, set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()      # drop comments and blank lines
        if not line:
            continue
        try:
            parts = shlex.split(line)            # understands "quoted names with spaces"
        except ValueError:                       # unbalanced quote: fall back to plain whitespace splitting
            parts = line.split()
        if not parts:
            continue
        if parts[0].startswith("-"):             # "-app.exe" = never govern this app
            excluded.add(parts[0][1:].strip().lower())
            continue
        cap = fnum(parts[1]) if len(parts) > 1 else None    # optional frame limit after the name
        listed[parts[0].lower()] = cap if cap is not None and cap >= 0 else None
    return listed, excluded


def load_games(path=None):
    """Read the game list, creating the file from GAMES_TEMPLATE the first time (or using the template in memory
    if the folder isn't writable)."""
    path = path or GAMES_FILE
    if not path.exists():
        try:
            path.write_text(GAMES_TEMPLATE, encoding="utf-8")
            print(f"Created {path.name}: list games you play windowed there (fullscreen ones are found anyway).")
        except OSError:
            return parse_games(GAMES_TEMPLATE)
    return parse_games(path.read_text(encoding="utf-8", errors="replace"))


def wait_for_game(listed, excluded, auto=True, confirm=3, poll=2.0,
                  names=process_names, front=foreground_app, sleep=time.sleep):
    """(exe, how): a listed game as soon as it runs, or ("auto") the app that has stayed fullscreen in the
    foreground for `confirm` polls in a row and isn't on the not-a-game list."""
    shown = ", ".join(listed) or "no listed games"
    print(f"Waiting for a game ({shown}{', or any app fullscreen in the foreground' if auto else ''}) ...",
          flush=True)
    streak, last = 0, None                       # how many polls in a row the same app has been fullscreen in front
    while True:
        running = names()
        for n in listed:                         # a listed game counts as soon as its process exists
            if n in running:
                return running[n], "listed"
        if auto:
            f = front() or {}
            exe = f.get("exe")
            key = exe.lower() if exe else None
            if key and f.get("fullscreen") and key not in excluded and key not in NOT_GAMES:
                streak = streak + 1 if key == last else 1       # restart the count if a different app took over
                last = key
                if streak >= confirm:            # fullscreen for several polls in a row, not just a brief flash
                    return exe, "auto"
            else:
                streak, last = 0, None
        sleep(poll)


def fmt(v, nd=0):
    """A number with nd decimals, or "-" when there is no value."""
    return "-" if v is None else f"{v:.{nd}f}"


def cpu_src(pol, st):
    """Where the CPU decision came from this tick, for the log: "trial" (trimmed by trial and error), "busy"
    (from PresentMon's CPU busy time) or "none" (no CPU timing available)."""
    if pol.cpu_probe:
        return "trial"
    return "busy" if st and st.get("cpu") is not None else "none"


def session(exe, game, gp, cp, cfg, sensor, ab, writer, logf, dry, cap=None, how="", rows=None):
    """Govern one game from start to finish: one decision per tick until the game closes.
    exe: PresentMon's path; game: the game's exe name (None = whichever app presents the most frames);
    gp / cp: the GPU and CPU profile points; sensor: GpuSensor; ab: CPU sensor source; writer / logf: the CSV log;
    dry: log only, change nothing; cap: this game's frame limit if the game list set one; how: how the game was
    found ("listed" or "auto"); rows: optional list to also collect each log row (used by the A/B test)."""
    scfg = dict(cfg)                       # per game: the cap tracker writes this session's limits into it
    capt = CapTracker(cap if cap is not None else cfg.get("fps_cap"), cfg.get("refresh"))
    scfg["caps"] = capt.caps()
    pm = PresentMonReader(exe, game, session=PM_SESSION)       # frame timings, read in a background thread
    pm.start()
    pol = Policy(gp, cp, scfg)
    act = Actuator(gp, cp, dry)
    act.apply(pol.g, pol.c)                # start from "no limit" for both chips
    found = {"listed": " (listed)", "auto": " (fullscreen in the foreground)"}.get(how, "")
    print(f"Governing {game or 'busiest app'}{found}{' (observe only)' if dry else ''}. "
          f"Frame cap: {capt.describe()}. Ctrl+C to stop.", flush=True)
    time.sleep(2)                          # give PresentMon a moment to attach before the first reading
    probe_was, learned_was, ticks = pol.cpu_probe, None, 0     # previous values, to print a message only on change
    try:
        while pm.alive():
            time.sleep(cfg["tick"])
            ticks += 1
            # PresentMon ends with the game, but not if the game exited before it attached: check ourselves.
            if game and ticks % 5 == 0 and game.lower() not in process_names():
                break
            fr = pm.window(cfg["window"], None)       # frames from the last `window` seconds
            st = frame_stats(fr)                      # None when there are too few frames
            capt.update(st and st["fps"])
            scfg["caps"] = capt.caps()
            if capt.learned != learned_was:
                print(f"  Frame cap found: {capt.learned:.0f} fps (the frame rate keeps sitting there)."
                      if capt.learned else
                      f"  Frame rate went above {learned_was:.0f} fps; no longer treating it as a cap.", flush=True)
                learned_was = capt.learned
            mode, action = pol.step(time.time(), st)  # decide ...
            act.apply(pol.g, pol.c)                   # ... and apply
            if pol.cpu_probe != probe_was:
                probe_was = pol.cpu_probe
                print("  CPU busy time reads as the whole frame even while the GPU or the frame cap sets the pace\n"
                      "  (the game waits inside its frame, e.g. Reflex). Trimming the CPU by trial instead: one step,\n"
                      "  check FPS, keep or revert." if probe_was else
                      "  CPU busy time looks real again; trimming the CPU by busy time.", flush=True)
            gs = sensor.summary(cfg["window"])        # GPU clock/power/temperature over the same window
            ab_d = ab.read()                          # CPU readings from Afterburner or Windows

            def abv(names):
                """The first of these sensor names present in the CPU readings, or None."""
                return next((ab_d[n.lower()][0] for n in names if n.lower() in ab_d), None)

            gcap, ccap = act.gcap(pol.g), act.ccap(pol.c)
            # One log row per tick. `st and x` yields None when there are no frame stats this tick.
            row = {"time": time.strftime("%H:%M:%S"), "app": fr[0]["app"] if fr else "", "mode": mode,
                   "action": action, "fps": st and round(st["fps"], 1), "low1": st and round(st["low1"], 1),
                   "low5": st and round(st["low5"], 1), "ft": st and round(st["ft"], 2),
                   "fps_cap": "/".join(f"{c:.0f}" for c in scfg["caps"]) or "none",
                   "cpu_busy": st and st["cpu"] and round(st["cpu"], 2),
                   "gpu_busy": st and st["gpu"] and round(st["gpu"], 2), "cpu_src": cpu_src(pol, st),
                   "gpu_cap": gcap or "max", "cpu_cap": ccap or "max", "gpu_clock": fmt(gs["clock"]),
                   "gpu_power": fmt(gs["power"], 1), "gpu_temp": fmt(gs["temp"]),
                   "gpu_thermal": int(gs["thermal_limited"]), "cpu_clock": fmt(abv(AB_CPU_CLOCK)),
                   "cpu_power": fmt(abv(AB_CPU_POWER), 1), "cpu_temp": fmt(abv(AB_CPU_TEMP))}
            writer.writerow(row)
            logf.flush()                              # flush every tick so the log survives a crash or closed window
            if rows is not None:
                rows.append(row)
            if st:
                print(f"{mode:>6} | {st['fps']:6.1f} fps (5% {st['low5']:5.1f}) | cpu {fmt(st['cpu'], 1)}"
                      f"{'?' if pol.cpu_probe else ''} / gpu {fmt(st['gpu'], 1)} ms | GPU {row['gpu_cap']} "
                      f"({row['gpu_clock']} MHz {row['gpu_power']} W {row['gpu_temp']} C) | CPU {row['cpu_cap']} "
                      f"({row['cpu_clock']} MHz {row['cpu_temp']} C) | {action}", flush=True)
    finally:
        pm.stop()
        if not dry:
            act.apply(len(gp) - 1, len(cp) - 1)       # game over (or error): back to "no limit" on both chips
        if pm.msgs and not pm.cols:                   # PresentMon never produced data: show what it said
            print("PresentMon output:", " | ".join(list(pm.msgs)[-5:]))


def open_log():
    """Open the CSV log for appending and return (file, writer). If the saved header doesn't match LOG_COLS (the
    columns changed between versions) the old log is renamed with a timestamp instead of mixing formats."""
    if LOG.exists() and LOG.stat().st_size > 0:
        with open(LOG, encoding="utf-8") as f:
            header = f.readline().strip()
        if header != ",".join(LOG_COLS):
            old = LOG.with_name(f"{LOG.stem}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
            LOG.rename(old)
            print(f"Log columns changed; previous log kept as {old.name}")
    new = not LOG.exists() or LOG.stat().st_size == 0
    logf = open(LOG, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(logf, fieldnames=LOG_COLS)
    if new:
        writer.writeheader()
        logf.flush()                    # now, not at the first row: closing the window before a game kept it empty
    return logf, writer


def run(a, ab=None):
    """ab: where the CPU clock/power/temp columns come from (default MSI Afterburner's shared memory); anything
    with Afterburner's read()/get(), e.g. winsensors.CpuSensors."""
    require_windows_admin()
    m = load_map()                                    # the saved profile (ThermalShare passes its own via MAP_FILE)
    if "gpu" not in m or "cpu" not in m:
        raise SystemExit("No map yet. Run mapper first: GPU map (option 4, synth, no game needed), then CPU map "
                         "with games closed. One map serves every game.")
    gp, cp = m["gpu"]["points"], m["cpu"]["points"]
    cfg = dict(DEFAULTS)
    for k in DEFAULTS:                                # command-line values override the defaults; unset ones don't
        v = getattr(a, k, None)
        if v is not None:
            cfg[k] = v
    cfg["cpu_busy"] = a.cpu_busy
    cfg["fps_cap"] = a.fps_cap            # None: found per game (display refresh rate until then)
    cfg["refresh"] = display_refresh_hz()
    listed, excluded = load_games()
    for n in a.game or []:                            # --game names are added to the file's list for this run
        listed.setdefault(n.lower(), None)
    exe = ensure_presentmon(a.presentmon)
    changes = not a.observe or a.synth_ab           # the A/B's governed runs change the caps
    if changes:
        save_restore()                              # remember the original settings before touching anything ...
        install_close_handler(restore_all)          # ... and put them back if the window is closed
    sensor = GpuSensor()
    sensor.start()
    ab = ab if ab is not None else Afterburner()
    logf, writer = open_log()
    try:
        if a.synth_ab:
            import synthab

            def govern(dry, rows):
                session(exe, synthab.GAME, gp, cp, cfg, sensor, ab, writer, logf, dry, cap=0, how="listed",
                        rows=rows)
                if not dry:
                    restore_all()                   # full speed again for the cool-down and the next run
                    save_restore()
            synthab.run_ab(a, govern, sensor, ab)
            return
        while True:                                   # wait for a game, govern it, then wait for the next
            game, how = (None, "any") if a.any else wait_for_game(listed, excluded, auto=not a.no_auto)
            cap = listed.get(game.lower()) if game else None      # a frame limit given in the game list, if any
            session(exe, game, gp, cp, cfg, sensor, ab, writer, logf, a.observe, cap=cap, how=how)
            if a.once or a.any:
                break
            print(f"Stopped governing {game}; GPU and CPU are back to full speed.", flush=True)
    except KeyboardInterrupt:                         # Ctrl+C is the normal way to stop
        pass
    finally:
        if changes:
            restore_all()                             # always leave the machine as it was found
        sensor.stop()
        logf.close()
        print("Stopped; GPU clocks and CPU frequency limit restored.")


def main():
    """Command-line entry for governor.exe (ThermalShare builds its own options and calls run() directly)."""
    ap = argparse.ArgumentParser(description="Shift the shared heat budget to whichever chip is the bottleneck")
    ap.add_argument("--game", action="append",
                    help="extra exe to govern whenever it runs (repeatable; or list it in gamegov_games.txt)")
    ap.add_argument("--no-auto", action="store_true",
                    help="only govern listed games, not any app that goes fullscreen")
    ap.add_argument("--any", action="store_true", help="govern whatever app presents the most frames, once")
    ap.add_argument("--once", action="store_true", help="exit after the game closes")
    ap.add_argument("--observe", action="store_true", help="classify and log only, change nothing")
    ap.add_argument("--presentmon")
    ap.add_argument("--restore", action="store_true", help="undo any leftover caps and exit")
    ap.add_argument("--cpu-busy", choices=["auto", "trust", "ignore"], default="auto",
                    help="plan CPU trims from PresentMon CPU busy time (trust), by trial (ignore), or detect (auto)")
    for k in DEFAULTS:                                # one --option per tunable setting, e.g. --gpu-thr
        ap.add_argument(f"--{k.replace('_', '-')}", dest=k, type=float, help=HELP.get(k))
    ab = ap.add_argument_group("synthetic A/B (governor on vs off on synthgame.exe, no game needed)")
    ab.add_argument("--synth-ab", action="store_true", help="run the A/B test and write synth_ab_report.txt")
    ab.add_argument("--synth-ab-report", action="store_true", help="print the last A/B test's report and exit")
    ab.add_argument("--rounds", type=int, default=2, help="off/on pairs, order alternating (default 2 = 4 runs)")
    ab.add_argument("--minutes", type=float, default=10.0, help="length of each run (default 10)")
    ab.add_argument("--cool-gpu", type=float, default=55.0, help="cool the GPU to this many C before each run")
    ab.add_argument("--cool-cpu", type=float, default=60.0, help="cool the CPU to this many C before each run")
    ab.add_argument("--cool-max", type=float, default=900.0, help="longest cool-down wait in seconds (default 900)")
    ab.add_argument("--latency", choices=["reflex", "normal"], default="reflex",
                    help="synthgame waits for the GPU inside its frame like War Thunder with Reflex (default)")
    ab.add_argument("--recalibrate", action="store_true", help="re-size synthgame's work before the test")
    ab.add_argument("--cpu-watts", type=float, default=34.0,
                    help="CPU power synthgame aims for at full speed when calibrating (default 34, War Thunder's)")
    ab.add_argument("--warmup-min", type=float, default=3.0,
                    help="ungoverned warm-up before the first run, so it doesn't start colder (default 3)")
    a = ap.parse_args()
    if a.synth_ab_report:
        import synthab
        print(synthab.report(synthab.read_results()))
        return
    if a.restore:
        require_windows_admin()
        restore_all()
        print("Restored.")
        return
    run(a)


if __name__ == "__main__":
    run_app(main)
