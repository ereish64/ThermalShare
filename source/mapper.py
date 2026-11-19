"""mapper: measures how each chip's speed, power and temperature change as its clock limit is lowered, and saves
the result (the "map", i.e. profile) the governor plans with.

TuneKit's own mapper.exe runs the sweeps from here (a GPU map under a game or synth.exe, a CPU map under a built-in
stress). ThermalShare doesn't run these commands; it reuses two helpers from this module, gpu_caps() (which GPU
limits to test) and stress_worker() (the CPU load), and does its own sweeps in thermalshare.py.
"""
import argparse
import hashlib
import multiprocessing as mp
import os
import sys
import time
from collections import Counter

from hwctl import (AB_CPU_CLOCK, AB_CPU_POWER, AB_CPU_TEMP, AB_GPU_VOLT, FROZEN, REASON_POWER, Afterburner,
                   GpuSensor, PresentMonReader, ask, cpu_cap, ensure_presentmon, finalize_speed, frame_stats,
                   gpu_lock, gpu_reset, gpu_supported_clocks, install_close_handler, load_map, mean,
                   require_windows_admin, restore_all, run_app, save_map_section, save_restore)
from synthrun import SYNTH, SynthRunner

M = (1 << 1024) - 1                 # a 1024-bit modulus: the CPU stress does big-number modular exponentiation with it
GAME_MIN, SYNTH_MIN = 600, 900      # lowest GPU limit (MHz) to test when mapping under a game / under synth.exe


def gpu_caps(supported, top, lo, step):
    """The GPU clock limits (MHz) to test, highest first: from the driver's supported clocks, between `lo` and
    just under `top`, keeping each one at least `step` MHz below the last so the points are evenly spread. If the
    driver lists none in range, every 15 MHz from lo to top is used instead."""
    cands = [c for c in supported if lo <= c < top - step / 2] or list(range(int(lo), int(top), 15))
    caps, last = [], top
    for c in sorted(cands, reverse=True):
        if last - c >= step:
            caps.append(c)
            last = c
    return caps


def fmt(v, nd=0):
    """A number with nd decimals, or "-" when there is no value."""
    return "-" if v is None else f"{v:.{nd}f}"


def is_loaded(d, util):
    """Busy enough to map: util at the threshold, or sitting on the power limit while mostly busy. synth.exe run on
    its own reads 83-85% busy yet draws the full 77 W (2026-11-06); the GPU can't be loaded harder than that."""
    u = d.get("util") or 0
    return u >= util or (u >= 50 and bool((d.get("reasons") or 0) & REASON_POWER))


def wait_for_load(sensor, util, hold=5):
    """Block until the GPU has stayed loaded for `hold` seconds in a row, printing its state every 5 s, so the
    sweep doesn't start before the game or benchmark is running steadily."""
    print(f"Waiting for GPU load ({util}%+ busy, or on its power limit, for {hold}s). Start the game/benchmark now "
          "and leave it in a steady scene.")
    ok_since, last_print = None, 0.0
    while True:
        d = sensor.latest()
        if d and is_loaded(d, util):
            ok_since = ok_since or time.time()
            if time.time() - ok_since >= hold:
                return
        else:
            ok_since = None
        if d and time.time() - last_print >= 5:
            last_print = time.time()
            capped = " on power limit" if (d.get("reasons") or 0) & REASON_POWER else ""
            print(f"  GPU {d.get('util') or 0:.0f}% busy, {fmt(d.get('clock'))} MHz, {fmt(d.get('power'), 1)} W{capped}"
                  f"{'  -> loaded' if is_loaded(d, util) else ''}", flush=True)
        time.sleep(0.5)


def sample_gpu(sensor, ab, pm, seconds, app):
    """Measure the GPU for `seconds`: averages from nvidia-smi, the voltage from Afterburner, and (when PresentMon
    is running) FPS and GPU busy time for the given app. One point of the map."""
    volts, end = [], time.time() + seconds
    while time.time() < end:
        v = ab.get(*AB_GPU_VOLT)
        if v:
            volts.append(v)
        time.sleep(0.5)
    s = sensor.summary(seconds)
    st = frame_stats(pm.window(seconds, app)) if pm else None
    return {"clock": s["clock"], "power": s["power"], "temp": s["temp"], "util": s["util"],
            "voltage": mean(volts), "thermal_limited": s["thermal_limited"], "power_limited": s["power_limited"],
            "fps": st["fps"] if st else None, "gpu_busy": st["gpu"] if st else None}


def print_gpu_row(r):
    """Print one GPU map point as a line (THERM / PWR flag thermal or power throttling)."""
    print(f"  cap {fmt(r['cap']):>5}  clock {fmt(r['clock']):>5}  {fmt(r['power'], 1):>5} W  {fmt(r['temp']):>3} C  "
          f"util {fmt(r['util']):>3}%  V {fmt(r['voltage']):>5}  fps {fmt(r['fps'], 1):>6}  gpu_busy {fmt(r['gpu_busy'], 2):>6}"
          f"{'  THERM' if r['thermal_limited'] else ''}{'  PWR' if r['power_limited'] else ''}", flush=True)


def synth_point(runner, sensor, ab, cap, warmup, label, preset, res):
    """One full synth camera loop with the GPU capped at `cap` MHz (None = uncapped). FPS is averaged over the
    identical 1800-frame path at every cap, so points compare like for like."""
    lock = (lambda: gpu_lock(cap)) if cap else gpu_reset
    lock()
    started = {}

    def on_render():
        started["t"] = time.time()
        lock()                      # again: an Optimus GPU may have powered down since

    run = runner.run(label, preset, 1, warmup, False, timeout=warmup + 900, args=("--runs", "1", "--res", res),
                     on_render=on_render, probe=lambda: ab.get(*AB_GPU_VOLT))
    row = run["rows"][-1] if run["rows"] else None
    if row is None:
        raise SystemExit(f"synth did not finish at cap {cap or 'max'}: " + " | ".join(run["tail"][-3:]))
    since = started.get("t", time.time()) + warmup + 2
    s = sensor.summary(max(1.0, time.time() - since))
    return {"clock": s["clock"], "power": s["power"], "temp": s["temp"], "util": s["util"],
            "voltage": mean([v for v in run["probes"] if v]), "thermal_limited": s["thermal_limited"],
            "power_limited": s["power_limited"], "fps": float(row["avg_fps"]), "gpu_busy": None,
            "errors": int(float(row.get("gpu_errors") or 0))}


def synth_sweep(a, runner, sensor, ab):
    """GPU map using synth.exe as the load: an uncapped reference run, then one run per lower clock limit.
    Saves the "gpu" section of the map and returns its points."""
    stamp = time.strftime("%m%d%H%M")                  # makes each run's label unique
    print(f"GPU map with synth ({a.synth_preset}, {a.synth_res}): one full camera loop per clock cap, about "
          "15-20 minutes. No game needed; leave the laptop alone.")
    pts = []
    try:
        ref = synth_point(runner, sensor, ab, None, 60, f"map{stamp}_max", a.synth_preset, a.synth_res)
        top = int(round(ref["clock"]))
        ref.update(cap=top, unlimited=True)
        print_gpu_row(ref)
        pts.append(ref)
        for cap in gpu_caps(gpu_supported_clocks(), top, a.min or SYNTH_MIN, a.step):
            r = synth_point(runner, sensor, ab, cap, 5, f"map{stamp}_{cap}", a.synth_preset, a.synth_res)
            r.update(cap=cap, unlimited=False)
            print_gpu_row(r)
            pts.append(r)
    finally:
        gpu_reset()
    bad = [p for p in pts if p["errors"]]
    if bad:
        print(f"WARNING: synth counted wrong frames at {', '.join(str(p['cap']) for p in bad)} MHz. Your clock offset "
              "is not fully stable; lower it (gpu_ocscan.exe) before trusting this map.")
    for i, p in enumerate(reversed(pts)):             # order 0 = lowest limit ... highest = unlimited (the reference)
        p["order"] = i
    points = finalize_speed(pts, "fps", False)
    save_map_section("gpu", {"created": time.strftime("%Y-%m-%d %H:%M"), "speed_from": "fps", "app": "synth.exe",
                             "load": f"synth {a.synth_preset} {a.synth_res}", "points": points})
    print_slopes(points, "clock", "power", "W")
    print(f"Saved {len(points)} GPU points (speed model from synth fps).")
    return points


def map_gpu_synth(a):
    """`mapper gpu --synth`: set up and run synth_sweep, making sure the clock limit is cleared and synth.exe is
    stopped however it ends (including by closing the window)."""
    require_windows_admin()
    if not SYNTH.exists():
        raise SystemExit(f"synth.exe not found next to mapper ({SYNTH}).")
    runner = SynthRunner()

    def stop():
        runner.kill()
        gpu_reset()

    install_close_handler(stop)
    sensor = GpuSensor()
    sensor.start()
    try:
        synth_sweep(a, runner, sensor, Afterburner())
    finally:
        runner.kill()
        sensor.stop()


def map_gpu(a):
    """`mapper gpu`: map the GPU while a game or benchmark the user has started runs in a steady scene. Waits for
    the load, measures uncapped, then at each lower clock limit, and saves the "gpu" section of the map."""
    if a.synth:
        return map_gpu_synth(a)
    require_windows_admin()
    install_close_handler(gpu_reset)
    sensor = GpuSensor()
    sensor.start()
    ab = Afterburner()
    pm = None
    if not a.no_presentmon:
        pm = PresentMonReader(ensure_presentmon(a.presentmon), a.process)
        pm.start()
    pts = []
    try:
        gpu_reset()
        time.sleep(2)
        wait_for_load(sensor, a.util)
        app = None
        if pm:
            fr = pm.window(5)
            if fr:
                app = Counter(f["app"] for f in fr).most_common(1)[0][0]
                print(f"PresentMon target: {app}")
            else:
                print("PresentMon sees no frames yet; FPS/GPU-busy columns may be empty. "
                      + " | ".join(list(pm.msgs)[-3:]))
        time.sleep(a.settle)
        ref = sample_gpu(sensor, ab, pm, a.sample, app)
        top = int(round(ref["clock"]))
        ref.update(cap=top, unlimited=True)
        print_gpu_row(ref)
        pts.append(ref)
        for cap in gpu_caps(gpu_supported_clocks(), top, a.min or GAME_MIN, a.step):
            gpu_lock(cap)
            time.sleep(a.settle)
            r = sample_gpu(sensor, ab, pm, a.sample, app)
            r.update(cap=cap, unlimited=False)
            if (r["util"] or 0) < a.util - 15:
                print("  ! GPU utilization dropped; load may have changed. Point kept but flagged.")
                r["low_util"] = True
            print_gpu_row(r)
            pts.append(r)
    finally:
        gpu_reset()
        sensor.stop()
        if pm:
            pm.stop()
    for i, p in enumerate(reversed(pts)):
        p["order"] = i
    # Measure "speed" from the best signal available at every point: GPU busy time (shorter = faster, so
    # inverse), else FPS, else just the clock speed.
    if all(p.get("gpu_busy") for p in pts):
        key, inv = "gpu_busy", True
    elif all(p.get("fps") for p in pts):
        key, inv = "fps", False
    else:
        key, inv = "clock", False
    points = finalize_speed(pts, key, inv)
    save_map_section("gpu", {"created": time.strftime("%Y-%m-%d %H:%M"), "speed_from": key, "app": app,
                             "points": points})
    print_slopes(points, "clock", "power", "W")
    print(f"Saved {len(points)} GPU points (speed model from {key}).")


def stress_worker(i, counts, stop):
    """One CPU-load process. Each round does a fixed amount of work (2000 SHA-256 hashes and one big modular
    exponentiation) and then adds 1 to counts[i]; the rate of that counter is the CPU's throughput. The work per
    round never changes, so a slower CPU clock shows up directly as fewer rounds per second. Runs until `stop`
    is set. (Reused by ThermalShare's load test.)"""
    h, x = i.to_bytes(8, "little"), 3 + i              # per-worker starting values so workers don't all do identical work
    while not stop.is_set():
        for _ in range(2000):
            h = hashlib.sha256(h).digest()
        x = pow(x, 65537, M)
        counts[i] += 1


def map_cpu(a):
    """`mapper cpu`: load every CPU thread, then step the CPU's maximum frequency down from unlimited to the
    lowest limit, measuring throughput, clock, power and temperature at each, and save the "cpu" map section.
    The original power-plan settings are saved first and put back at the end."""
    require_windows_admin()
    save_restore()
    install_close_handler(restore_all)
    ab = Afterburner()
    if ab.get(*AB_CPU_TEMP) is None:
        print("Afterburner CPU sensors not found: clock/power/temp columns will be empty "
              "(throughput still measured). Enable CPU temperature/power/clock in Afterburner monitoring.")
    n = a.threads or os.cpu_count() or 4
    counts = mp.Array("Q", n, lock=False)
    stop = mp.Event()
    procs = [mp.Process(target=stress_worker, args=(i, counts, stop), daemon=True) for i in range(n)]
    caps = [0] + list(range(a.max - a.step, a.min - 1, -a.step))     # 0 = no limit, then max-step, max-2*step, ... min
    pts = []
    try:
        for p in procs:
            p.start()
        print(f"Stressing {n} threads. Sweeping caps: unlimited, {', '.join(map(str, caps[1:]))} MHz")
        for i, cap in enumerate(caps):
            cpu_cap(cap)
            time.sleep(a.settle)
            c0, t0 = sum(counts), time.time()
            clk, pw, tp = [], [], []
            while time.time() - t0 < a.sample:
                clk.append(ab.get(*AB_CPU_CLOCK))
                pw.append(ab.get(*AB_CPU_POWER))
                tp.append(ab.get(*AB_CPU_TEMP))
                time.sleep(0.5)
            thr = (sum(counts) - c0) / (time.time() - t0)
            r = {"cap": cap, "unlimited": cap == 0, "order": len(caps) - 1 - i, "throughput": thr,
                 "clock": mean(clk), "power": mean(pw), "temp": mean(tp)}
            print(f"  cap {('max' if cap == 0 else cap):>5}  clock {fmt(r['clock']):>5}  {fmt(r['power'], 1):>5} W  "
                  f"{fmt(r['temp']):>3} C  throughput {thr:8.1f}/s", flush=True)
            pts.append(r)
    finally:
        stop.set()
        for p in procs:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()
        restore_all()
    points = finalize_speed(pts, "throughput", False)
    if points and points[0]["speed"] > 0.9:
        print("WARNING: throughput barely changed across caps; the frequency cap is not taking effect. "
              "The governor will not be able to trim the CPU.")
    save_map_section("cpu", {"created": time.strftime("%Y-%m-%d %H:%M"), "speed_from": "throughput",
                             "points": points})
    print_slopes(points, "clock", "power", "W")
    print(f"Saved {len(points)} CPU points.")


def print_slopes(points, xk, yk, unit):
    """Print how much y (e.g. power) changes per 100 units of x (e.g. MHz) between neighbouring points: a quick
    way to see where lowering the clock saves the most power."""
    rows = [(p, q) for p, q in zip(points, points[1:]) if p.get(xk) and q.get(xk) and p.get(yk) and q.get(yk)
            and q[xk] != p[xk]]
    if not rows:
        return
    print(f"Slope ({unit}/100 MHz):")
    for p, q in rows:
        print(f"  {p[xk]:.0f} -> {q[xk]:.0f} MHz: {100 * (q[yk] - p[yk]) / (q[xk] - p[xk]):+.2f}")


def show(a):
    """`mapper show`: print the saved map."""
    m = load_map()
    if not m:
        print("No map yet.")
        return
    for name in ("gpu", "cpu"):
        sec = m.get(name)
        if not sec:
            print(f"{name}: not mapped")
            continue
        print(f"{name}: {sec['created']} speed from {sec['speed_from']}")
        for p in sec["points"]:
            print(f"  cap {('max' if p.get('unlimited') else p['cap']):>5}  clock {fmt(p.get('clock')):>5}  "
                  f"{fmt(p.get('power'), 1):>5} W  {fmt(p.get('temp')):>3} C  speed {p['speed']:.3f}")


def main():
    """Command-line entry: `mapper gpu|cpu|show`. Started with no arguments as an exe (double-clicked), it shows
    a small menu instead."""
    ap = argparse.ArgumentParser(description="Map clock -> power/temp/voltage/performance for GPU and CPU")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gpu")
    g.add_argument("--process", help="game/benchmark exe for PresentMon (default: busiest app)")
    g.add_argument("--presentmon")
    g.add_argument("--no-presentmon", action="store_true")
    g.add_argument("--synth", action="store_true",
                   help="drive the GPU with synth.exe instead of a game (steadier; no game needed)")
    g.add_argument("--synth-preset", default="heavy", choices=["light", "medium", "heavy"])
    g.add_argument("--synth-res", default="3840x2160",
                   help="synth resolution; 4K keeps the GPU ~94%% busy vs ~88%% at the preset's 1440p")
    g.add_argument("--step", type=int, default=90)
    g.add_argument("--min", type=int, help=f"lowest cap, MHz (default {GAME_MIN}, or {SYNTH_MIN} with --synth)")
    g.add_argument("--settle", type=float, default=6)
    g.add_argument("--sample", type=float, default=8)
    g.add_argument("--util", type=int, default=75,
                   help="GPU busy %% that counts as loaded (a GPU on its power limit counts from 50%%)")
    g.set_defaults(fn=map_gpu)
    c = sub.add_parser("cpu")
    c.add_argument("--max", type=int, default=4500)
    c.add_argument("--min", type=int, default=1200)
    c.add_argument("--step", type=int, default=300)
    c.add_argument("--settle", type=float, default=5)
    c.add_argument("--sample", type=float, default=8)
    c.add_argument("--threads", type=int)
    c.set_defaults(fn=map_cpu)
    sub.add_parser("show").set_defaults(fn=show)
    argv = sys.argv[1:]
    if not argv and FROZEN:
        print("Mapper\n  1) GPU map  (game running in a steady scene)\n  2) CPU map  (game closed)\n  3) Show map\n"
              "  4) GPU map  using synth.exe (no game needed, steadier, about 15-20 min)")
        choice = ask("Choose", "1")
        if choice == "1":
            argv = ["gpu"]
            proc = ask("Game exe for PresentMon (blank = whichever app draws the most frames)")
            if proc:
                argv += ["--process", proc]
        elif choice == "2":
            argv = ["cpu"]
        elif choice == "4":
            argv = ["gpu", "--synth"]
        else:
            argv = ["show"]
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    mp.freeze_support()
    run_app(main)
