"""ThermalShare: TuneKit's game governor as one program.

First start: a load test (about 6 minutes) measures how this machine's GPU and CPU speed change as their clocks are
lowered, and saves ThermalShare_profile.json next to the exe. Every start after that loads the profile and goes
straight to governing games.

ThermalShare.exe --ab measures what it gains: an A/B test of the governor off vs on, on a synthetic game (see the
"A/B test" section below).

It only ever lowers clocks, only while a game runs, and puts them back when the game closes:
  - GPU: a clock limit through the NVIDIA driver (nvidia-smi), cleared when the game exits.
  - CPU: the power plan's maximum processor frequency, restored when the game exits.
It never overclocks, undervolts, or changes power limits, fan curves or voltages. No ThrottleStop or MSI Afterburner
is needed: GPU readings come from the NVIDIA driver, CPU clock and power from Windows' own counters. If Afterburner
is running, its CPU readings are used (it also has the CPU temperature, which Windows doesn't expose on every laptop).
"""
import argparse
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
from argparse import Namespace
from pathlib import Path

import hwctl

APP = "ThermalShare"          # used for the exe name and every data file name (ThermalShare_profile.json, ...)
PROFILE_VERSION = 1           # bump when the profile layout changes so old profiles are ignored and re-measured


def app_dir():
    """The folder the app's data files live in: next to the exe when built, otherwise the folder above source/."""
    if hwctl.FROZEN:                                    # running as the PyInstaller exe
        return Path(sys.executable).resolve().parent
    here = Path(__file__).resolve().parent
    return here.parent if here.name.lower() == "source" else here


DIR = app_dir()
PROFILE = DIR / f"{APP}_profile.json"
# The shared TuneKit modules keep their files next to governor.exe under TuneKit names; point them here before
# importing the modules that copy those paths at import time.
hwctl.HERE, hwctl.TOOLS, hwctl.MAP_FILE = DIR, DIR / "tools", PROFILE
hwctl.RESTORE_FILE = DIR / f"{APP}_restore.json"

import governor  # noqa: E402
import mapper  # noqa: E402
from hwctl import (AB_CPU_CLOCK, AB_CPU_POWER, AB_CPU_TEMP, GpuSensor, atomic_json, cpu_cap,  # noqa: E402
                   finalize_speed, gpu_lock, gpu_reset, gpu_supported_clocks, install_close_handler, mean,
                   require_windows_admin, restore_all, run_app, save_restore, sh)
from winsensors import CpuSensors, cpu_base_mhz, cpu_name  # noqa: E402

# Same idea for the governor module: send its log and game list to this app's own files, update the text it writes
# into a new game list, and make sure it never mistakes this program's own window for a game.
governor.LOG = DIR / f"{APP}_log.csv"
governor.GAMES_FILE = DIR / f"{APP}_games.txt"
governor.GAMES_TEMPLATE = governor.GAMES_TEMPLATE.replace("governor.exe", f"{APP}.exe")
governor.NOT_GAMES.add(f"{APP.lower()}.exe")

STOPPED = "Load test stopped (Esc or the window was closed). Nothing was saved; start ThermalShare again to redo it."
# The fixed GPU load test renders at 1080p and the test pattern is tuned (fit_gpu) to run at about 60 FPS uncapped
# on the test machine, so there is headroom to see the FPS drop as the clock limit is lowered.
GPU_RES = (1920, 1080)
GPU_FPS = 60.0


# ---------------------------------------------------------------- profile
# The profile is the result of the one-time load test: for the GPU and the CPU, a list of clock limits ("points") with
# the clock, power and temperature measured at each and "speed" (how much work still gets done compared with no
# limit, 1.0 = full speed). The governor uses it to know how far it can lower a chip before the game slows down.

def gpu_name():
    """The GPU's name as nvidia-smi reports it, or None if nvidia-smi is missing or doesn't answer."""
    try:
        r = sh(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    except OSError:
        return None
    lines = r.stdout.strip().splitlines() if r.returncode == 0 else []
    return lines[0].strip() if lines else None


def hardware():
    """The CPU and GPU names, stored in the profile so a different machine (or a swapped GPU) is noticed."""
    return {"cpu": cpu_name(), "gpu": gpu_name()}


def load_profile(path=None):
    """The saved profile, or None if it's missing, unreadable, from another version, or has no measured points."""
    path = path or PROFILE
    try:
        p = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):                       # no file yet, or the JSON is damaged
        return None
    if p.get("version") != PROFILE_VERSION:
        return None
    if not (p.get("gpu") or {}).get("points") or not (p.get("cpu") or {}).get("points"):
        return None
    return p


def profile_problem(p, hw):
    """Why the saved profile can't be used on this machine, or None if it can."""
    if p is None:
        return "no profile yet (first start)"
    saved = p.get("hardware") or {}
    for k in ("cpu", "gpu"):
        # Only compare when both names are known, so a missing reading never forces a needless re-test.
        if hw.get(k) and saved.get(k) and hw[k] != saved[k]:
            return f"the {k.upper()} is different ({saved[k]} -> {hw[k]})"
    return None


def fmt(v, nd=0):
    """A number with nd decimals, or "-" when the value is missing (a sensor gave no reading)."""
    return "-" if v is None else f"{v:.{nd}f}"


def describe_profile(p):
    """The profile as readable text for --show: one row per measured limit, highest limit first."""
    lines = [f"Profile from {p['created']} ({p['hardware'].get('cpu')}, {p['hardware'].get('gpu')})",
             f"CPU never limited below {p['cpu_floor']:.0f} MHz."]
    for name in ("gpu", "cpu"):
        lines.append(f"{name.upper()}:")
        for q in reversed(p[name]["points"]):
            lines.append(f"  limit {('none' if q.get('unlimited') else q['cap']):>5}  clock {fmt(q.get('clock')):>5} MHz  "
                         f"{fmt(q.get('power'), 1):>5} W  speed {100 * q['speed']:5.1f}%")
    return "\n".join(lines)


# ---------------------------------------------------------------- CPU limits

def cpu_floor(base_mhz, fallback=2400):
    """The lowest CPU limit the governor will use: just under the CPU's base clock (2400 MHz on the 2592 MHz
    i7-9750H). Below base clock throughput falls off a cliff (40%+ on the i7-9750H)."""
    if not base_mhz or base_mhz < 1000:                 # base clock unknown or implausible: use the default
        return fallback
    # Round down to a multiple of 100 MHz, then go one step lower (2592 -> 2500 -> 2400), but never below 1200.
    return max(1200, (int(base_mhz) // 100) * 100 - 100)


def cpu_caps(floor, peak, step=300, top=4800):
    """CPU limits to measure, highest first: the floor and each step above it that's still below the CPU's top
    clock (games run a few cores at single-core boost, so limits between all-core and top clock still matter)."""
    hi = (peak or top) - 50                             # stop just short of the top clock; "no limit" covers that point
    caps, c = [], int(floor)
    while c < hi:                                       # floor, floor+step, floor+2*step, ...
        caps.append(c)
        c += step
    return sorted(caps, reverse=True) or [int(floor)]   # always at least the floor itself


# ---------------------------------------------------------------- load tests

def gpu_sweep(game, sensor, say=print, warmup=60.0, ref_s=15.0, settle=3.0, sample=10.0, step=90, lo=900,
              lock=gpu_lock, reset=gpu_reset, supported=gpu_supported_clocks):
    """Render the fixed load uncapped, then at GPU clock limits every ~step MHz down to lo. The work per frame never
    changes, so FPS at each limit / uncapped FPS is the GPU's speed there."""

    def run_for(seconds):
        """Render for this long and return the frame timestamps; Esc in the test window ends the whole test."""
        stamps, _, _, aborted = game.drive(seconds, "normal")
        if aborted:
            raise SystemExit(STOPPED)
        return stamps

    def point(seconds):
        """Render for this long and return one measurement: average FPS plus the GPU sensor's summary."""
        st = run_for(seconds)
        # FPS = frames completed / time between the first and last frame (needs a few frames to be meaningful).
        fps = (len(st) - 1) / (st[-1] - st[0]) if len(st) > 2 and st[-1] > st[0] else None
        s = sensor.summary(seconds)
        return {"clock": s["clock"], "power": s["power"], "temp": s["temp"], "util": s["util"],
                "thermal_limited": s["thermal_limited"], "power_limited": s["power_limited"], "fps": fps}

    def show(r):
        """Print one finished measurement as a line of progress."""
        say(f"  GPU limit {('none' if r['unlimited'] else r['cap']):>5}: {fmt(r['clock']):>5} MHz  "
            f"{fmt(r['fps'], 1):>5} fps  {fmt(r['temp']):>3} C", flush=True)

    reset()                                             # start from no clock limit
    say(f"  Warming up ({warmup:.0f} s)...", flush=True)
    run_for(warmup)                                     # let the GPU reach its normal hot, steady clock first
    ref = point(ref_s)                                  # the reference: uncapped speed everything is compared to
    if not ref["clock"] or not ref["fps"]:
        raise SystemExit("No GPU clock readings from nvidia-smi during the load test; can't build a profile.")
    top = int(round(ref["clock"]))                      # the clock the GPU actually ran at with no limit
    ref.update(cap=top, unlimited=True)
    show(ref)
    pts = [ref]
    try:
        # Step the clock limit down from the top clock; "supported" are the clocks the driver accepts.
        for cap in mapper.gpu_caps(supported(), top, lo, step):
            lock(cap)
            run_for(settle)                             # let the new limit take effect before measuring
            r = point(sample)
            r.update(cap=cap, unlimited=False)
            show(r)
            pts.append(r)
    finally:
        reset()                                         # always remove the limit, even on Esc or an error
    # "order" numbers the points from the lowest limit (0) up to the unlimited reference, which finalize_speed uses
    # to turn the raw FPS values into a speed between 0 and 1 relative to the unlimited run.
    for i, p in enumerate(reversed(pts)):
        p["order"] = i
    return finalize_speed(pts, "fps", False)


def cpu_sweep(sensors, floor, say=print, step=300, settle=4.0, sample=6.0, probe_s=3.0, threads=None,
              cap=cpu_cap):
    """Busy every CPU thread with a fixed hash workload, then measure throughput, clock and power uncapped and at
    each limit from the CPU's top clock down to the floor. One thread first, to see the single-core boost clock."""
    n = threads or os.cpu_count() or 4                  # one worker process per logical CPU
    # Each worker adds to its own slot of a shared counter every time it finishes a unit of work, so the change in
    # the total over time is the CPU's throughput. "stop" tells the workers to quit.
    counts = mp.Array("Q", n, lock=False)
    stop = mp.Event()
    procs = [mp.Process(target=mapper.stress_worker, args=(i, counts, stop), daemon=True) for i in range(n)]

    def sample_for(seconds):
        """Read the CPU sensors twice a second for this long; returns lists of clock, power and temperature."""
        clk, pw, tp, t_end = [], [], [], time.time() + seconds
        while time.time() < t_end:
            time.sleep(0.5)
            d = sensors.read()
            # Each reading is (value, ...) or missing; keep just the value, or None if the sensor has no answer.
            clk.append((d.get("cpu clock") or (None,))[0])
            pw.append((d.get("cpu power") or (None,))[0])
            tp.append((d.get("cpu temperature") or (None,))[0])
        return clk, pw, tp

    def measure(c):
        """Apply CPU limit c (0 = no limit), let it settle, then measure throughput, clock, power and temperature."""
        cap(c)
        time.sleep(settle)
        c0, t0 = sum(counts), time.time()               # work done so far, and when
        clk, pw, tp = sample_for(sample)
        thr = (sum(counts) - c0) / max(1e-6, time.time() - t0)   # work units per second over the sample window
        r = {"cap": c, "unlimited": c == 0, "throughput": thr, "clock": mean(clk), "power": mean(pw),
             "temp": mean(tp)}
        say(f"  CPU limit {('none' if c == 0 else c):>5}: {fmt(r['clock']):>5} MHz  {fmt(r['power'], 1):>5} W  "
            f"work {thr:7.1f}/s", flush=True)
        return r

    pts = []
    try:
        cap(0)                                          # start with no limit
        procs[0].start()                                # one busy thread first: the CPU boosts highest on one core
        clk, _, _ = sample_for(probe_s)
        peak = max([c for c in clk if c] or [0])        # the single-core boost clock, which sets the top limit tested
        for p in procs[1:]:
            p.start()                                   # now load every thread
        pts.append(measure(0))                          # the unlimited reference measurement
        peak = max(peak, pts[0]["clock"] or 0) or None
        for c in cpu_caps(floor, peak, step):           # then each limit from high to low, down to the floor
            pts.append(measure(c))
    finally:
        stop.set()                                      # stop the workers and wait for them
        for p in procs:
            if p.pid is not None:                       # pid is None if the process never started
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()
        cap(0)                                          # always take the CPU limit off again
    # Here the unlimited point was measured first, so it gets the highest "order" (finalize_speed treats the
    # highest order as the 100% reference); the lowest limit measured last gets 0.
    for i, p in enumerate(pts):
        p["order"] = len(pts) - 1 - i
    points = finalize_speed(pts, "throughput", False)
    # points[0] is the lowest limit. If even that does ~full work, the limit isn't taking effect on this PC.
    if points and points[0]["speed"] > 0.9:
        say("  WARNING: CPU throughput barely changed with the limit; Windows isn't applying the power plan's maximum "
            "processor frequency on this machine, so ThermalShare can only manage the GPU.")
    return points


def wait_for_ac(say, poll=3.0):
    """Block until the charger is plugged in; on battery the laptop runs slower and the profile would be wrong."""
    from synthab import on_ac       # imported here so synthab (and its ctypes setup) is only loaded when needed
    if on_ac():
        return
    say("Plug in the charger: on battery the laptop runs slower and the profile wouldn't match how you play. "
        "Waiting for it (Ctrl+C to quit)...", flush=True)
    while not on_ac():
        time.sleep(poll)


def calibrate(hw, say=print, interactive=True):
    """The one-time load test: measure the GPU (part 1), then the CPU (part 2), and save the profile.
    Returns the profile. Anything it changes (clock limits) is undone when it finishes, fails or is closed."""
    from synthgame import Game, fit_gpu     # imported here: synthgame needs numpy, so only load it for the load test
    say("\nOne-time load test (about 6 minutes): ThermalShare measures how much speed your GPU and CPU lose at\n"
        "each lower clock limit, and saves the result as a profile. Next time it starts straight away.\n"
        "  - Close games and anything heavy. Leave the laptop alone until it finishes.\n"
        "  - Part 1 fills the screen with a moving test pattern (Esc stops it). Part 2 loads the CPU.\n"
        "  - It only ever lowers clocks; everything is back to normal when it's done.", flush=True)
    wait_for_ac(say)
    if interactive:
        try:
            input("Press Enter to start (or close this window to quit)...")
        except EOFError:
            pass
    save_restore()                  # write down the current CPU/GPU settings so they can be put back, even after a crash
    game = None

    def stop():
        restore_all()

    install_close_handler(stop)     # closing the console window also puts the settings back
    sensor = GpuSensor()            # GPU clock/power/temperature from nvidia-smi, polled in the background
    sensor.start()
    sensors = CpuSensors()          # CPU clock/power/temperature from Windows counters (or Afterburner if running)
    floor = cpu_floor(cpu_base_mhz())
    try:
        say("\nPart 1 of 2: GPU (about 4 minutes)", flush=True)
        # A window with a fixed GPU-only workload: no CPU workers, no CPU units. The "gpu_iters" value is only a
        # starting point; fit_gpu adjusts it until the load runs at GPU_FPS, so every machine gets a comparable load.
        game = Game({"res": list(GPU_RES), "gpu_iters": 500, "workers": 0, "cpu_units": 0, "adapter_hint": None},
                    title=f"{APP} load test - Esc stops")
        fit_gpu(game, GPU_FPS, "normal", lambda *a, **k: None)      # the no-op swallows fit_gpu's progress messages
        gpts = gpu_sweep(game, sensor, say)
        iters = game.rnd.iters                          # the final load size, recorded in the profile
        game.close()
        game = None
        say("\nPart 2 of 2: CPU (about 2 minutes)", flush=True)
        cpts = cpu_sweep(sensors, floor, say)
    finally:
        if game is not None:                            # part 1 was interrupted with the window still open
            game.close()
        restore_all()                                   # undo every limit the test set
        sensor.stop()
    now = time.strftime("%Y-%m-%d %H:%M")
    profile = {"version": PROFILE_VERSION, "created": now, "hardware": hw, "cpu_floor": floor,
               "gpu": {"created": now, "speed_from": "fps", "load": f"built-in {GPU_RES[0]}x{GPU_RES[1]}, {iters} "
                       "iterations", "points": gpts},
               "cpu": {"created": now, "speed_from": "throughput", "points": cpts}}
    atomic_json(PROFILE, profile)   # written to a temp file then swapped in, so a crash can't leave a half-written profile
    say(f"\nSaved {PROFILE.name}.", flush=True)
    return profile


# ---------------------------------------------------------------- governing

def governor_args(profile, a, ab_opts=None):
    """The options governor.run() expects, from ThermalShare's own. ab_opts (a dict) switches on the A/B test:
    governor.run() then runs synthab's test instead of waiting for a game, and these are its settings."""
    d = {k: None for k in governor.DEFAULTS}            # every option governor.py knows, unset
    # Then fill in the ones this program decides: no --any mode, no PresentMon override, automatic CPU-busy
    # detection, no A/B test (unless ab_opts), and a CPU floor taken from the profile unless --cpu-min was given.
    d.update(game=a.game, no_auto=a.no_auto, any=False, once=a.once, observe=a.observe, presentmon=None,
             cpu_busy="auto", synth_ab=False, fps_cap=a.fps_cap,
             cpu_min=a.cpu_min if a.cpu_min is not None else profile.get("cpu_floor"))
    if ab_opts:
        d.update(ab_opts, synth_ab=True)
    return Namespace(**d)


def others_running():
    """How many other governors (TuneKit's governor.exe or another ThermalShare) are running; two would fight."""
    from synthab import count_running
    # count_running counts processes by name. A one-file PyInstaller exe shows up as two processes (a launcher and
    # the real program), so when built, our own two are subtracted to count only the *other* copies.
    mine = count_running(f"{APP}.exe") - (2 if hwctl.FROZEN else 0)
    return count_running("governor.exe") + max(0, mine)


# ---------------------------------------------------------------- A/B test
# `ThermalShare.exe --ab` measures what ThermalShare itself gains. It plays the same synthetic game (synthgame.py:
# a fixed GPU and CPU load drawn in a real window, so PresentMon sees its frames like a game's) several times, with
# the governor OFF (logging only, nothing limited) and ON, alternating A B B A so slow drift such as the room warming
# up cancels out, each run starting from a cooled-down laptop. The result is one page, ThermalShare_ab_graphs.html
# (abgraphs.py): six graphs (clock speeds, temperatures and FPS; governor off on the left, on on the right) and, at the
# bottom, synthab's text report with a 95% confidence interval on each difference. The page is the only file kept;
# the test's own logs live in a scratch folder that is deleted at the end (run_ab_test).
#
# It all lives in this one exe. The synthetic game is a copy of ThermalShare.exe saved as ThermalShare_synthgame.exe:
# started under that name, the exe acts as synthgame instead of as ThermalShare (see is_ab_game_exe). A separate
# image name matters: PresentMon is pointed at the game by name, and the governor has to see the game start and stop.
# The test itself (cool-downs, run order, statistics, report) is synthab.py, reused as it is.

AB_GAME_STEM = f"{APP}_synthgame"           # name the exe copy runs under when it plays the synthetic game
AB_GAME = f"{AB_GAME_STEM}.exe"
AB_CALIB = DIR / f"{APP}_synthgame.json"    # synthgame's calibration: how much GPU/CPU work per frame on this PC
AB_GRAPHS = DIR / f"{APP}_ab_graphs.html"   # the six graphs (off vs on: clocks, temperatures, FPS); see abgraphs.py
AB_NEEDS_EXE = (f"The A/B test runs from the built {APP}.exe (it starts a copy of it as the synthetic game). "
                f"Build it with source\\build_exe.bat first.")
_ab_orig_preflight = None                   # synthab.preflight before configure_ab() wrapped it


def is_ab_game_exe(path=None):
    """True when this process is the copy of the exe that was started as ThermalShare_synthgame.exe."""
    return Path(path or sys.executable).stem.lower() == AB_GAME_STEM.lower()


def ab_game_main():
    """What the copy does: be the synthetic game. Its command line is synthgame's (calibrate / run ...)."""
    import synthgame
    synthgame.CALIB = AB_CALIB      # read and write the calibration under ThermalShare's own name
    synthgame.main()


def prepare_ab_game(dst_dir=None, exe=None):
    """Copy the running exe to ThermalShare_synthgame.exe next to it (a fresh copy every test, so it can never be
    an older version) and return its path. dst_dir / exe are for the tests."""
    if exe is None and not hwctl.FROZEN:
        raise SystemExit(AB_NEEDS_EXE)
    dst = Path(dst_dir or DIR) / AB_GAME
    try:
        shutil.copy2(exe or sys.executable, dst)
    except PermissionError:         # the copy from an earlier test is still running, so Windows won't overwrite it
        raise SystemExit(f"{AB_GAME} is still running (from an earlier test?). Close it, then try again.")
    return dst


def remove_ab_game(dst_dir=None):
    """Delete the copy when the test is over. Best effort: if Windows still has it open, the next test overwrites it."""
    try:
        (Path(dst_dir or DIR) / AB_GAME).unlink()
    except OSError:
        pass


def ab_preflight(a, say=print):
    """synthab's own pre-test checks, preceded by making the synthetic game exe (which those checks look for)."""
    prepare_ab_game()
    return _ab_orig_preflight(a, say)


def configure_ab(work):
    """Point synthab at the exe copy instead of synthgame.exe, at ThermalShare's own calibration file, and at the
    scratch folder `work` for everything the test writes while it runs (see run_ab_test). Safe to call more than
    once. Returns the synthab module."""
    global _ab_orig_preflight
    import synthab
    work = Path(work)
    synthab.GAME = AB_GAME
    synthab.CALIB = AB_CALIB
    synthab.RESULTS = work / "results.csv"      # one row per finished run
    synthab.REPORT = work / "report.txt"        # the text report (its content goes into the page, not a file)
    synthab.RUNS = work / "runs"                # each run's per-second log and raw result
    synthab.SAVED_NOTE = ""                     # synthab's "saved to ..." line would be wrong: nothing is kept
    if _ab_orig_preflight is None:
        _ab_orig_preflight = synthab.preflight
        synthab.preflight = ab_preflight
    return synthab


def finish_ab(synthab, out):
    """Turn what the test left in its scratch folder into the one page: the six graphs (abgraphs.py) plus the text
    report (statistics) at the bottom. Returns the page's path, or None if no run finished."""
    import abgraphs
    rows = synthab.read_results()
    return abgraphs.write_graphs(rows[-1]["test"] if rows else None, synthab.RESULTS, synthab.RUNS, out,
                                 report_text=synthab.report(rows) if rows else None)


def run_ab_test(profile, a, out=None):
    """Run the A/B test and leave ONE file behind: the page of graphs (ThermalShare_ab_graphs.html, or `out`).
    Everything else the test writes (per-run logs, results, the text report, the governor log of the synthetic
    game's sessions) goes to a scratch folder that is deleted at the end, so nothing piles up and the normal
    governor log doesn't fill with test rows. governor.run() does its usual setup (saves the settings to put back,
    starts the GPU sensor, opens the log) and, because synth_ab is on, hands over to synthab.run_ab."""
    work = Path(tempfile.mkdtemp(prefix=f"{APP}_ab_"))
    synthab = configure_ab(work)
    opts = dict(rounds=a.ab_rounds, minutes=a.ab_minutes, cool_gpu=a.ab_cool_gpu, cool_cpu=a.ab_cool_cpu,
                cool_max=a.ab_cool_max, latency=a.ab_latency, recalibrate=a.ab_recalibrate,
                warmup_min=a.ab_warmup,
                cpu_watts=34.0)     # only used to size synthgame's CPU work when MSI Afterburner is running
    real_log, governor.LOG = governor.LOG, work / "governor_log.csv"
    try:
        governor.run(governor_args(profile, a, opts), ab=CpuSensors())
        # Here also after Ctrl+C (governor.run absorbs it), so a stopped test still gets graphs of its finished runs.
        try:
            path = finish_ab(synthab, out or AB_GRAPHS)
        except Exception as e:      # don't let a drawing problem hide the test's own printed report
            print(f"Couldn't make the graphs: {e}")
        else:
            print(f"Graphs: {path}" if path else "No run finished, so there is nothing to graph.")
    finally:
        governor.LOG = real_log
        remove_ab_game()
        shutil.rmtree(work, ignore_errors=True)


def main():
    """Entry point: read the command line, make sure it's safe to start, run the load test if needed, then govern."""
    ap = argparse.ArgumentParser(prog=APP, description="Moves the shared CPU/GPU heat budget to whichever chip is "
                                 "holding the frame rate back, by lowering the other one's clock while you play. "
                                 "The first start runs a one-time load test and saves a profile.")
    ap.add_argument("--recalibrate", action="store_true",
                    help="run the load test again (after a new GPU overclock, a driver/BIOS update or a repaste)")
    ap.add_argument("--calibrate-only", action="store_true", help="run the load test if needed, then exit")
    ap.add_argument("--observe", action="store_true", help="log what it would do, change nothing")
    ap.add_argument("--game", action="append", help="also govern this exe whenever it runs (repeatable)")
    ap.add_argument("--no-auto", action="store_true",
                    help=f"only govern games listed in {APP}_games.txt, not any app that goes fullscreen")
    ap.add_argument("--once", action="store_true", help="exit after the first game closes")
    ap.add_argument("--fps-cap", type=float, help="frame limit for every game (default: found per game)")
    ap.add_argument("--cpu-min", type=float, help="never limit the CPU below this many MHz (default: from the profile)")
    ap.add_argument("--show", action="store_true", help="print the saved profile and exit")
    ap.add_argument("--restore", action="store_true", help="undo leftover limits after a crash, then exit")
    g = ap.add_argument_group("A/B test: ThermalShare off vs on, on a synthetic game (about an hour by default)")
    g.add_argument("--ab", action="store_true",
                   help="run the A/B test; its only output file is ThermalShare_ab_graphs.html (needs the built exe)")
    g.add_argument("--ab-rounds", type=int, default=2, help="off/on pairs, order alternating (default 2 = 4 runs)")
    g.add_argument("--ab-minutes", type=float, default=10.0, help="length of each run (default 10)")
    g.add_argument("--ab-cool-gpu", type=float, default=55.0, help="cool the GPU to this many C before each run")
    g.add_argument("--ab-cool-cpu", type=float, default=60.0,
                   help="cool the CPU to this many C before each run (skipped if no CPU temperature is available)")
    g.add_argument("--ab-cool-max", type=float, default=900.0, help="longest cool-down wait in seconds (default 900)")
    g.add_argument("--ab-latency", choices=["reflex", "normal"], default="reflex",
                   help="reflex: the game waits for the GPU inside its frame, like War Thunder with NVIDIA Reflex "
                        "(default); normal: the CPU runs ahead of the GPU")
    g.add_argument("--ab-warmup", type=float, default=3.0,
                   help="minutes of ungoverned warm-up before the first run, so it doesn't start colder (default 3)")
    g.add_argument("--ab-recalibrate", action="store_true",
                   help="size the synthetic game's work again before the test (done automatically the first time)")
    a = ap.parse_args()
    if a.show:                  # read-only, so it works without admin rights
        p = load_profile()
        print(describe_profile(p) if p else "No profile yet: start ThermalShare to run the load test.")
        return
    if a.ab and not hwctl.FROZEN:       # say so now, not after the 6-minute load test
        raise SystemExit(AB_NEEDS_EXE)
    # Changing GPU clock limits and the power plan needs admin. The built exe asks Windows for admin when it starts
    # (--uac-admin in build_exe.bat); this check is the safety net when running the .py from a normal terminal.
    require_windows_admin()
    if a.restore:
        restore_all()
        print("Restored: GPU clock limit cleared, CPU maximum frequency back to what it was.")
        return
    if others_running():
        raise SystemExit("Another governor (ThermalShare.exe or TuneKit's governor.exe) is running. Close it first: "
                         "two would fight over the limits.")
    if hwctl.RESTORE_FILE.exists():           # last run didn't close cleanly (crash, power loss): undo its limits now
        restore_all()
        print("Put back CPU/GPU limits left over from last time (ThermalShare didn't get to close cleanly).")
    hw = hardware()
    if not hw["gpu"]:
        raise SystemExit("No NVIDIA GPU found (nvidia-smi didn't answer). ThermalShare needs an NVIDIA GPU and its "
                         "driver.")
    p = load_profile()
    # Run the load test if asked to, or if the profile is missing or belongs to different hardware.
    why = "--recalibrate" if a.recalibrate else profile_problem(p, hw)
    if why:
        print(f"Load test needed: {why}.")
        # Only wait for Enter when there's a real console to type in (not when launched without one).
        p = calibrate(hw, interactive=bool(sys.stdin and sys.stdin.isatty()))
    else:
        print(f"Using the profile from {p['created']} ({PROFILE.name}). --recalibrate redoes the load test.")
    if a.calibrate_only:
        return
    print()
    if a.ab:
        run_ab_test(p, a)
        return
    governor.run(governor_args(p, a), ab=CpuSensors())     # the main loop: watch for games and limit the idle chip


if __name__ == "__main__":
    mp.freeze_support()     # required on Windows so the load test's worker processes work inside the built exe
    # The A/B test starts a copy of this exe named ThermalShare_synthgame.exe; that copy is the synthetic game.
    # run_app runs the function, prints any error, and (as an exe) waits for Enter so the window stays open.
    run_app(ab_game_main if is_ab_game_exe() else main)
