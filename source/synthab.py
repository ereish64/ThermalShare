"""governor.exe --synth-ab: the governor against a hands-off baseline on synthgame.exe, a repeatable synthetic game,
so the comparison doesn't depend on which War Thunder map came up.

Runs alternate governor off (observe only: logged, nothing changed) and governor on, in ABBA order so slow drift
(the room warming up) cancels out, each from the same cooled-down start. The report gives both arms' FPS, lows,
clocks and temperatures, and a 95% confidence interval on each difference.

ThermalShare only borrows two small helpers from here, on_ac() and count_running(); the A/B test itself is part
of TuneKit's governor.exe and needs synthgame.exe, which ThermalShare doesn't ship."""
import csv
import ctypes
import json
import math
import subprocess
import time
from statistics import mean, variance

from hwctl import AB_CPU_TEMP, FROZEN, HERE, NO_WINDOW, fnum, process_names, sh

GAME = "synthgame.exe"                  # the repeatable synthetic game the A/B test runs
CALIB = HERE / "synthgame.json"         # its calibration (sizes the work to this machine)
CALIB_VERSION = 2               # synthgame's: older calibrations are for the old (memory-bound) CPU work
RESULTS = HERE / "synth_ab_results.csv"     # one row per finished run, accumulating across tests
REPORT = HERE / "synth_ab_report.txt"       # the latest test's report
RUNS = HERE / "synth_ab_runs"               # per-run logs and raw results
# What the test prints after the report. ThermalShare sets it to "" because it keeps none of these files.
SAVED_NOTE = "\n\nSaved to {report}; per-run logs in {runs}\\."
ARMS = ("observe", "governed")              # the two arms: governor logging only (off) vs really limiting (on)
LABEL = {"observe": "governor off", "governed": "governor on"}
RESULT_COLS = ["test", "round", "arm", "started", "minutes", "latency", "aborted", "start_gpu_temp", "start_cpu_temp",
               "cooled_s", "cool_reached", "frames", "fps", "low1", "low5", "fps_h1", "fps_h2", "low1_h2", "low5_h2",
               "sim_ms", "sim_ms_h2", "gpu_clock", "gpu_clock_h2", "gpu_temp", "gpu_temp_h2", "gpu_power",
               "heat_s", "cpu_power", "cpu_power_h2", "cpu_temp", "cpu_temp_h2", "cpu_clock", "cpu_capped_pct",
               "gpu_capped_pct"]
# key, label, unit, kind ("fps": a frame rate, verdict in bench.exe's words; "num": anything else)
METRICS = [("fps", "Average FPS", "", "fps"), ("low5", "5% low FPS", "", "fps"), ("low1", "1% low FPS", "", "fps"),
           ("fps_h2", "Average FPS, 2nd half", "", "fps"), ("low5_h2", "5% low FPS, 2nd half", "", "fps"),
           ("gpu_clock_h2", "GPU clock, 2nd half", "MHz", "num"), ("gpu_temp_h2", "GPU temp, 2nd half", "C", "num"),
           ("heat_s", "GPU at its heat limit", "s", "num"), ("gpu_power", "GPU power", "W", "num"),
           ("cpu_power", "CPU power", "W", "num"), ("cpu_temp", "CPU temp", "C", "num"),
           ("sim_ms", "CPU work per frame", "ms", "num")]
# Two-sided 95% critical values of Student's t distribution, as (degrees of freedom, value). The confidence
# interval for a difference between two small samples needs these; values between rows are interpolated (t95).
T95 = [(1, 12.706), (2, 4.303), (3, 3.182), (4, 2.776), (5, 2.571), (6, 2.447), (7, 2.365), (8, 2.306),
       (9, 2.262), (10, 2.228), (12, 2.179), (15, 2.131), (20, 2.086), (30, 2.042), (60, 2.000), (120, 1.980)]


def arm_order(rounds):
    """ABBA...: round 1 off then on, round 2 on then off, and so on."""
    out = []
    for r in range(1, rounds + 1):
        out += [(r, arm) for arm in (ARMS if r % 2 else ARMS[::-1])]
    return out


def t95(df):
    """The 95% t critical value for df degrees of freedom: linear interpolation in the T95 table, and 1.96 (the
    normal distribution's value) beyond its end."""
    if df <= T95[0][0]:
        return T95[0][1]
    for (d0, v0), (d1, v1) in zip(T95, T95[1:]):
        if df <= d1:
            return v0 + (v1 - v0) * (df - d0) / (d1 - d0)
    return 1.96


def welch(a, b):
    """mean(b) - mean(a) and its 95% CI (Welch), or None with fewer than 2 runs on a side.
    Welch's t-test doesn't assume the two groups have equal variance, which suits a handful of noisy runs.
    Returns (difference, low, high); if the interval excludes 0 the difference is unlikely to be chance."""
    a, b = [x for x in a if x is not None], [x for x in b if x is not None]
    if len(a) < 2 or len(b) < 2:
        return None
    d = mean(b) - mean(a)
    qa, qb = variance(a) / len(a), variance(b) / len(b)     # each group's variance of its mean
    se2 = qa + qb                                           # squared standard error of the difference
    if se2 == 0:                                            # no spread at all: the interval is just the difference
        return d, d, d
    # Welch-Satterthwaite approximation of the degrees of freedom.
    df = se2 ** 2 / ((qa ** 2 / (len(a) - 1) if qa else 0) + (qb ** 2 / (len(b) - 1) if qb else 0))
    h = t95(df) * math.sqrt(se2)
    return d, d - h, d + h


def verdict(ci, kind):
    """Plain-words reading of a confidence interval: whether the whole interval is above zero (real gain), below
    (real loss) or straddles it (within noise)."""
    if ci is None:
        return "need 2+ runs each"
    _, lo, hi = ci
    if kind == "fps":
        return "REAL GAIN" if lo > 0 else "REAL LOSS" if hi < 0 else "within noise"
    return "higher with governor" if lo > 0 else "lower with governor" if hi < 0 else "no clear difference"


def cooldown(read_gpu, read_cpu, gpu_to, cpu_to, max_s, min_s=20.0, poll=5.0, sleep=time.sleep, now=time.time,
             say=print):
    """Wait until the GPU is at or below gpu_to and the CPU at or below cpu_to (a sensor that can't be read
    doesn't hold things up), but at least min_s and at most max_s seconds."""
    t0, said = now(), -1e9              # `said`: when the last progress message was printed (-1e9 = print at once)
    while True:
        g, c = read_gpu(), read_cpu()
        el = now() - t0
        ready = (g is None or g <= gpu_to) and (c is None or c <= cpu_to)
        if (ready and el >= min_s) or el >= max_s:
            return {"gpu": g, "cpu": c, "waited": round(el), "reached": ready}
        if el - said >= 30:             # a progress line every 30 seconds
            said = el
            say(f"  Cooling down: GPU {'-' if g is None else f'{g:.0f}'} C (to {gpu_to:.0f}), CPU "
                f"{'-' if c is None else f'{c:.0f}'} C (to {cpu_to:.0f})  {el / 60:.1f} min", flush=True)
        sleep(poll)


def _vals(rows, key):
    """The numeric values of one column across rows, skipping blanks."""
    return [v for v in (fnum(r.get(key)) for r in rows) if v is not None]


def _avg(rows, key, nd=1):
    """Average of one column rounded to nd decimals, or None if the column is empty."""
    v = _vals(rows, key)
    return round(mean(v), nd) if v else None


def summarize_run(rows, res, start, dry=False):
    """One results row from the governor's per-second log rows of a run plus synthgame's own frame statistics.
    dry (observe only): the log's cap columns are what the governor would have done, so no capped share."""
    play = [r for r in rows if r.get("mode") != "idle"]      # seconds when the game was actually running
    h2 = play[len(play) // 2:]                               # second half: temperatures have settled by then
    out = {k: res.get(k) for k in ("frames", "fps", "low1", "low5", "fps_h1", "fps_h2", "low1_h2", "low5_h2",
                                   "sim_ms", "sim_ms_h2")}
    out["aborted"] = int(bool(res.get("aborted")))
    out.update({"gpu_clock": _avg(play, "gpu_clock", 0), "gpu_clock_h2": _avg(h2, "gpu_clock", 0),
                "gpu_temp": _avg(play, "gpu_temp"), "gpu_temp_h2": _avg(h2, "gpu_temp"),
                "gpu_power": _avg(play, "gpu_power"), "heat_s": sum(int(fnum(r.get("gpu_thermal")) or 0) for r in play),
                "cpu_power": _avg(play, "cpu_power"), "cpu_power_h2": _avg(h2, "cpu_power"),
                "cpu_temp": _avg(play, "cpu_temp"), "cpu_temp_h2": _avg(h2, "cpu_temp"),
                "cpu_clock": _avg(play, "cpu_clock", 0),
                "cpu_capped_pct": None if dry or not play else
                round(100 * sum(str(r.get("cpu_cap")) != "max" for r in play) / len(play)),
                "gpu_capped_pct": None if dry or not play else
                round(100 * sum(str(r.get("gpu_cap")) != "max" for r in play) / len(play)),
                "start_gpu_temp": start.get("gpu"), "start_cpu_temp": start.get("cpu"),
                "cooled_s": start.get("waited"), "cool_reached": int(bool(start.get("reached")))})
    return out


def read_results(path=None):
    """All saved A/B result rows (dicts of strings), or [] if there is no results file yet."""
    path = path or RESULTS
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_result(row, path=None):
    """Add one run's row to the results file, writing the header first if the file is new."""
    path = path or RESULTS
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=RESULT_COLS, extrasaction="ignore")    # ignore extra keys in row
        if new:
            wr.writeheader()
        wr.writerow(row)


def _fmt(v, kind, unit):
    """A report cell: "-" if missing, else one decimal for FPS and temperature/power/time values, none otherwise."""
    if v is None:
        return "-"
    return f"{v:.1f}" if kind == "fps" or unit in ("C", "W", "ms") else f"{v:.0f}"


def report(rows, test=None):
    """The comparison for one test (the latest in the file if none is given)."""
    if not rows:
        return "No synthetic A/B results yet."
    test = test or rows[-1]["test"]
    # Runs stopped early with Esc are listed at the end of the report but left out of the statistics.
    runs = [r for r in rows if r["test"] == test and r.get("aborted") in ("0", 0, "", None)]
    first = next(r for r in rows if r["test"] == test)
    by = {arm: [r for r in runs if r["arm"] == arm] for arm in ARMS}
    lines = [f"Synthetic A/B on {GAME}: governor off (observe only) vs governor on",
             f"Test {test}: {len(by['observe'])} run(s) off, {len(by['governed'])} on, {first['minutes']} min each, "
             f"latency {first['latency']}.", ""]
    head = f"{'':24}{'governor off':>14}{'governor on':>14}   {'difference (95% CI)':<30}verdict"
    lines += [head, "-" * len(head)]
    for key, label, unit, kind in METRICS:
        a, b = (_vals(by[arm], key) for arm in ARMS)
        if not a and not b:
            continue
        ma, mb = (mean(v) if v else None for v in (a, b))
        ci = welch(a, b)
        if ma is not None and mb is not None:
            d = mb - ma
            if kind == "fps" and ma:
                diff = f"{100 * d / ma:+.1f}%" + (f" ({100 * ci[1] / ma:+.1f} .. {100 * ci[2] / ma:+.1f})" if ci else "")
            else:
                u = f" {unit}" if unit else ""
                diff = f"{d:+.1f}{u}" + (f" ({ci[1]:+.1f} .. {ci[2]:+.1f})" if ci else "")
        else:
            diff = "-"
        lab = f"{label}{f' ({unit})' if unit and kind != 'fps' else ''}"
        lines.append(f"{lab:24}{_fmt(ma, kind, unit):>14}{_fmt(mb, kind, unit):>14}   {diff:<30}{verdict(ci, kind)}")
    lines += ["", "Runs:"]
    for r in [r for r in rows if r["test"] == test]:
        r = {k: (v if v not in ("", None) else "-") for k, v in r.items()}
        lines.append(
            f"  round {r['round']} {LABEL[r['arm']]:<13} start GPU {r['start_gpu_temp'] or '-'} C, CPU "
            f"{r['start_cpu_temp'] or '-'} C | {r['fps']} fps, 5% {r['low5']}, 1% {r['low1']}, 2nd half {r['fps_h2']}"
            f" | GPU {r['gpu_clock_h2']} MHz {r['gpu_temp_h2']} C, heat limit {r['heat_s']} s | CPU {r['cpu_power']} W "
            f"{r['cpu_temp']} C, "
            + ("nothing changed" if r["arm"] == "observe" else f"held down {r['cpu_capped_pct']}% of the time")
            + (" | STOPPED EARLY, not counted" if r.get("aborted") not in ("0", 0, "", None) else "")
            + ("" if r.get("cool_reached") in ("1", 1) else " | didn't cool to target"))
    lines += ["", "REAL GAIN / REAL LOSS: the 95% interval of the difference doesn't include zero. Two runs a side",
              "give wide intervals; --rounds 3 or more narrows them."]
    return "\n".join(lines)


def on_ac():
    """True if the laptop is plugged in. Also True when Windows can't say (e.g. a desktop), so a missing answer
    never blocks anything."""

    class SPS(ctypes.Structure):       # the SYSTEM_POWER_STATUS struct that GetSystemPowerStatus fills in
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    s = SPS()
    try:
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            return s.ACLineStatus != 0
    except Exception:
        pass
    return True


def count_running(image):
    """How many processes with this image name (e.g. "governor.exe") are running, from tasklist."""
    out = sh(["tasklist", "/NH", "/FO", "CSV", "/FI", f"IMAGENAME eq {image}"]).stdout
    return sum(1 for line in out.splitlines() if line.lower().startswith(f'"{image.lower()}"'))


def preflight(a, say=print):
    """Checks before the test: synthgame.exe exists, no other governor or synthgame is running, and it's
    calibrated for this machine (running the calibration first if not). Returns synthgame.exe's path."""
    exe = HERE / GAME
    if not exe.exists():
        raise SystemExit(f"{GAME} is missing: put it next to governor.exe.")
    if count_running("governor.exe") > (2 if FROZEN else 0):    # a one-file exe is two processes
        raise SystemExit("Another governor.exe is running. Close its window first: two governors would fight "
                         "over the caps, and the 'off' runs wouldn't be off.")
    if GAME in process_names():
        raise SystemExit(f"{GAME} is already running; close it first.")
    if not on_ac():
        say("Warning: running on battery. Plug the charger in for numbers that match how you play.")
    if a.recalibrate or calib_version() < CALIB_VERSION:
        say(f"Calibrating {GAME} first (about 3 minutes; a window opens)...", flush=True)
        r = subprocess.run([str(exe), "calibrate", "--latency", a.latency, "--cpu-watts", f"{a.cpu_watts:g}",
                            "--floor", f"{getattr(a, 'cpu_min', None) or 2400:g}"], cwd=str(HERE),
                           stdin=subprocess.DEVNULL)
        if r.returncode or calib_version() < CALIB_VERSION:
            raise SystemExit("Calibration didn't finish.")
    return exe


def calib_version():
    """The version stored in synthgame's calibration file: 1 if it has none, 0 if the file is missing/unreadable."""
    try:
        return json.loads(CALIB.read_text(encoding="utf-8")).get("version", 1)
    except (OSError, ValueError):
        return 0


def warm_up(exe, minutes, say=print):
    """Run synthgame ungoverned before the first run, so that run starts from a just-cooled laptop like the
    others instead of a cold one."""
    say(f"\nWarming up for {minutes:g} min (not measured)...", flush=True)
    out = RUNS / "warmup.json"
    subprocess.run([str(exe), "run", "--minutes", f"{minutes:g}", "--out", str(out)], cwd=str(HERE),
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=NO_WINDOW, timeout=minutes * 60 + 180)
    if out.exists():
        out.unlink()


def keep_awake(on):
    """No sleep or display-off for the whole test, cool-downs included."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x3 if on else 0))
    except Exception:
        pass


def run_ab(a, govern, sensor, ab, say=print):
    """govern(dry, rows) runs one governor session on GAME (dry: observe only) and appends its log rows."""
    keep_awake(True)
    try:
        return _run_ab(a, govern, sensor, ab, say)
    finally:
        keep_awake(False)


def _run_ab(a, govern, sensor, ab, say):
    """The test itself: for each run in the ABBA order, cool down, start synthgame, run the governor (on or in
    observe-only mode) alongside it, collect the results and append them; at the end write the report."""
    exe = preflight(a, say)
    if ab.get(*AB_CPU_TEMP) is None:
        say("Warning: no CPU readings from MSI Afterburner (is it running?). The report will have no CPU power or\n"
            "temperature, and the cool-down will only wait for the GPU.")
    test = time.strftime("%Y%m%d_%H%M%S")
    order = arm_order(a.rounds)
    RUNS.mkdir(exist_ok=True)
    say(f"\nSynthetic A/B {test}: {len(order)} runs of {a.minutes:g} min ({a.rounds} rounds, off/on order "
        f"alternating), cooling to GPU {a.cool_gpu:.0f} C / CPU {a.cool_cpu:.0f} C before each.\n"
        f"Takes roughly {(len(order) * (a.minutes + 5) + a.warmup_min) / 60:.1f} h. Leave the laptop alone; Esc in "
        f"the synthgame window stops the test.", flush=True)
    if a.warmup_min:
        warm_up(exe, a.warmup_min, say)
    for i, (rnd, arm) in enumerate(order, 1):
        say(f"\n=== Run {i} of {len(order)}: round {rnd}, {LABEL[arm]} ===", flush=True)
        start = cooldown(lambda: (sensor.latest() or {}).get("temp"), lambda: ab.get(*AB_CPU_TEMP),
                         a.cool_gpu, a.cool_cpu, a.cool_max, say=say)
        if not start["reached"]:
            say(f"  Didn't cool to the target in {a.cool_max / 60:.0f} min; starting anyway (noted in the report).")
        base = RUNS / f"{test}_r{rnd}_{arm}"        # this run's files: .json (synthgame's result), .csv (governor log)
        out = base.with_suffix(".json")
        rows = []                                   # filled by govern() with the governor's per-second log rows
        with open(base.with_name(base.name + "_synthgame.txt"), "w", encoding="utf-8") as log:
            p = subprocess.Popen([str(exe), "run", "--minutes", f"{a.minutes:g}", "--latency", a.latency,
                                  "--out", str(out)], cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            try:
                govern(arm == "observe", rows)
                if p.poll() is None:            # the session ended first (PresentMon quit): let the run finish
                    p.wait(timeout=a.minutes * 60 + 300)
                p.wait(timeout=60)
            finally:
                if p.poll() is None:
                    sh(["taskkill", "/T", "/F", "/PID", str(p.pid)])
        if rows:
            with open(base.with_suffix(".csv"), "w", newline="", encoding="utf-8") as f:
                wr = csv.DictWriter(f, fieldnames=list(rows[0]))
                wr.writeheader()
                wr.writerows(rows)
        if not out.exists():
            raise SystemExit(f"{GAME} didn't save a result (exit code {p.returncode}); see {base.name}_synthgame.txt")
        res = json.loads(out.read_text(encoding="utf-8"))
        row = {"test": test, "round": rnd, "arm": arm, "started": res.get("started"), "minutes": f"{a.minutes:g}",
               "latency": a.latency, **summarize_run(rows, res, start, dry=arm == "observe")}
        append_result(row)
        v = {k: ("-" if x is None else x) for k, x in row.items()}
        say(f"  {LABEL[arm]}: {v['fps']} fps (5% low {v['low5']}), 2nd half {v['fps_h2']} fps, GPU "
            f"{v['gpu_clock_h2']} MHz {v['gpu_temp_h2']} C, heat limit {v['heat_s']} s, CPU "
            f"{v['cpu_power']} W {v['cpu_temp']} C", flush=True)
        if res.get("aborted"):
            say("Stopped with Esc: ending the test here.")
            break
    text = report(read_results(), test)
    REPORT.write_text(text + "\n", encoding="utf-8")
    say("\n" + text + SAVED_NOTE.format(report=REPORT.name, runs=RUNS.name))
    return test
