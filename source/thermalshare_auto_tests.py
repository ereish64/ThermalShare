"""Tests for ThermalShare. Run with:  py thermalshare_auto_tests.py   (build_exe.bat runs this before building).

Most tests use small fakes (FakeGame, FakeGpu, FakeSensors) in place of the real GPU, CPU and sensors, so they
run in seconds and change nothing on the machine. The last two tests read real Windows data (the power plan and
the performance counters) and need to run on Windows. There is no test framework: the block at the bottom just
runs every function whose name starts with test_ and prints "ok" for each; the first failed assert stops it."""
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Everything ThermalShare needs is in this folder, so only this folder goes on the import path. (An older version
# of this file also added the shared TuneKit\source folder first, which made the tests silently check those
# copies of hwctl, governor, ... instead of the ones in this folder.)
sys.path.insert(0, str(HERE))

import thermalshare as gg  # noqa: E402
import governor  # noqa: E402
import hwctl  # noqa: E402
import winsensors as ws  # noqa: E402


def test_files_live_in_the_app_folder():
    """Every data file (profile, log, game list, restore file, tools) is next to the app, not next to TuneKit's."""
    d = HERE.parent
    assert gg.DIR == d and gg.PROFILE == d / "ThermalShare_profile.json"
    assert hwctl.MAP_FILE == gg.PROFILE and hwctl.RESTORE_FILE == d / "ThermalShare_restore.json"
    assert hwctl.TOOLS == d / "tools"
    assert governor.LOG == d / "ThermalShare_log.csv" and governor.GAMES_FILE == d / "ThermalShare_games.txt"
    assert "ThermalShare.exe" in governor.GAMES_TEMPLATE and "governor.exe" not in governor.GAMES_TEMPLATE
    assert "thermalshare.exe" in governor.NOT_GAMES


def test_cpu_floor_and_caps():
    """The lowest CPU limit is just under the base clock, and the limits tested run from there up in 300 MHz steps."""
    assert gg.cpu_floor(2592) == 2400                       # i7-9750H: the floor TuneKit's governor uses
    assert gg.cpu_floor(2300) == 2200 and gg.cpu_floor(None) == 2400 and gg.cpu_floor(1100) == 1200
    assert gg.cpu_caps(2400, 4500) == [4200, 3900, 3600, 3300, 3000, 2700, 2400]
    assert gg.cpu_caps(2400, 4000) == [3900, 3600, 3300, 3000, 2700, 2400]
    assert gg.cpu_caps(2400, 2450) == [2400]
    assert gg.cpu_caps(2400, None)[0] == 4500               # no clock reading: up to 4.8 GHz


def _profile(hw=None):
    """A minimal valid profile (two points per chip) for the tests that need one."""
    pts = [{"cap": 900, "speed": 0.6, "order": 0}, {"cap": 1700, "speed": 1.0, "unlimited": True, "order": 1}]
    return {"version": gg.PROFILE_VERSION, "created": "2026-11-13 12:00", "cpu_floor": 2400,
            "hardware": hw or {"cpu": "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz", "gpu": "NVIDIA GeForce RTX 2060"},
            "gpu": {"points": pts}, "cpu": {"points": pts}}


def test_profile_load_and_checks():
    """A bad, old or half-finished profile file is rejected; a good one loads; and the hardware check only asks
    for a new load test when a CPU or GPU name that is known on both sides differs."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "profile.json"
        assert gg.load_profile(p) is None
        p.write_text("not json")
        assert gg.load_profile(p) is None
        p.write_text(json.dumps({**_profile(), "version": 0}))
        assert gg.load_profile(p) is None                    # older format: test again
        p.write_text(json.dumps({**_profile(), "cpu": {"points": []}}))
        assert gg.load_profile(p) is None                    # half-finished
        p.write_text(json.dumps(_profile()))
        prof = gg.load_profile(p)
        assert prof["cpu_floor"] == 2400
    hw = dict(_profile()["hardware"])
    assert gg.profile_problem(prof, hw) is None
    assert gg.profile_problem(prof, {**hw, "gpu": None}) is None          # unknown now: don't force a re-test
    assert "GPU is different" in gg.profile_problem(prof, {**hw, "gpu": "NVIDIA GeForce RTX 3060 Laptop GPU"})
    assert gg.profile_problem(None, hw).startswith("no profile")
    assert "limit  none" in gg.describe_profile(prof)


class FakeGame:
    """Fixed work per frame: fps follows the GPU clock, which follows the limit."""

    def __init__(self, gpu):
        self.gpu = gpu

    def drive(self, seconds, latency):
        """Same return shape as synthgame.Game.drive: (frame timestamps, sim times, GPU waits, aborted)."""
        fps = 60.0 * self.gpu.clock / 1800              # 60 FPS at the full 1800 MHz, proportionally less below
        n = int(fps * seconds)
        return [i / fps for i in range(n + 1)], [], [], False


class FakeGpu:
    """Stands in for both the GPU sensor and the clock-limit functions, and records how it was used."""

    def __init__(self):
        self.clock, self.locks, self.resets = 1800, [], 0       # current clock; every limit set; reset count

    def lock(self, cap):
        """Like gpu_lock: the clock follows the limit (but never exceeds the 1800 MHz maximum)."""
        self.locks.append(cap)
        self.clock = min(1800, cap)

    def reset(self):
        """Like gpu_reset: back to the full clock."""
        self.resets += 1
        self.clock = 1800

    def summary(self, seconds):
        """Like GpuSensor.summary: a fixed, busy, power-limited reading at the current clock."""
        return {"clock": self.clock, "power": None, "temp": 70, "util": 99, "thermal_limited": False,
                "power_limited": True}


def test_gpu_sweep():
    """The GPU load test steps the clock limit down from just under the top, never leaves a limit set, and rates
    each point's speed as its FPS relative to the unlimited run (here equal to clock / 1800 by construction)."""
    gpu = FakeGpu()
    supported = list(range(300, 2101, 15))
    pts = gg.gpu_sweep(FakeGame(gpu), gpu, say=lambda *a, **k: None, warmup=1, ref_s=1, settle=1, sample=1,
                       lock=gpu.lock, reset=gpu.reset, supported=lambda: supported)
    assert pts[-1]["unlimited"] and pts[-1]["speed"] == 1.0 and pts[-1]["cap"] == 1800
    assert gpu.locks[0] == 1710 and gpu.locks[-1] >= 900 and all(a - b >= 90 for a, b in zip(gpu.locks, gpu.locks[1:]))
    assert gpu.resets == 2 and gpu.clock == 1800             # cleared before and after
    assert [p["cap"] for p in pts] == sorted(p["cap"] for p in pts)
    assert abs(pts[0]["speed"] - pts[0]["cap"] / 1800) < 0.01


def test_gpu_sweep_stops_on_escape():
    """Pressing Esc (drive reports "aborted") ends the load test with the "Nothing was saved" message."""
    class Quit(FakeGame):
        def drive(self, seconds, latency):
            return [], [], [], True                     # aborted=True, as if Esc had been pressed
    gpu = FakeGpu()
    try:
        gg.gpu_sweep(Quit(gpu), gpu, say=lambda *a, **k: None, lock=gpu.lock, reset=gpu.reset, supported=lambda: [])
        raise AssertionError("should have stopped")
    except SystemExit as e:
        assert "Nothing was saved" in str(e)


class FakeSensors:
    """CPU sensors whose clock follows the limit (4000 MHz when unlimited) and whose power is clock / 100 W."""

    def __init__(self):
        self.cap = 0

    def read(self):
        clock = 4000 if self.cap == 0 else min(4000, self.cap)
        return {"cpu clock": (clock, "MHz"), "cpu power": (clock / 100.0, "W")}


def test_cpu_sweep():
    """The CPU load test starts and ends with no limit, measures each limit from the floor up to the unlimited
    run, and shuts its worker processes down. (It really runs two worker processes for a few seconds.)"""
    s = FakeSensors()
    caps = []

    def cap(c):
        """Replaces cpu_cap: remember the limit and tell the fake sensors about it."""
        caps.append(c)
        s.cap = c

    pts = gg.cpu_sweep(s, 2400, say=lambda *a, **k: None, settle=0.1, sample=0.6, probe_s=0.6, threads=2, cap=cap)
    assert caps[0] == 0 and caps[-1] == 0                    # starts and ends uncapped
    measured = [p["cap"] for p in pts]
    assert measured == [2400, 2700, 3000, 3300, 3600, 3900, 0] and pts[-1]["unlimited"]
    assert pts[-1]["power"] == 40.0 and pts[0]["clock"] == 2400
    assert all(p["speed"] > 0 for p in pts)                 # the fake limit doesn't slow the real workers


def test_governor_args():
    """ThermalShare's options become a complete set of governor options; the CPU floor comes from the profile
    unless --cpu-min is given."""
    a = gg.argparse.Namespace(game=["x.exe"], no_auto=True, once=False, observe=True, fps_cap=None, cpu_min=None)
    ns = gg.governor_args(_profile(), a)
    for k in governor.DEFAULTS:
        assert hasattr(ns, k)
    assert ns.cpu_min == 2400 and ns.fps_cap is None and ns.observe and ns.game == ["x.exe"] and not ns.synth_ab
    a.cpu_min, a.fps_cap = 2100.0, 60.0
    ns = gg.governor_args(_profile(), a)
    assert ns.cpu_min == 2100.0 and ns.fps_cap == 60.0


def test_cpu_sensors_merge():
    """Windows' readings fill in what Afterburner doesn't have; where both have a value, Afterburner's wins."""
    class Fixed:
        def __init__(self, d):
            self.d = d

        def read(self):
            return dict(self.d)

    win = Fixed({"cpu clock": (3988, "MHz"), "cpu power": (63.2, "W")})
    s = ws.CpuSensors(ab=Fixed({}), win=win)
    assert s.get(*hwctl.AB_CPU_POWER) == 63.2 and s.get(*hwctl.AB_CPU_TEMP) is None
    ab = Fixed({"cpu temperature": (83.0, "C"), "cpu power": (63.3, "W"), "gpu voltage": (0.8, "V")})
    s = ws.CpuSensors(ab=ab, win=win)
    assert s.get(*hwctl.AB_CPU_TEMP) == 83.0 and s.get(*hwctl.AB_CPU_POWER) == 63.3
    assert s.get(*hwctl.AB_CPU_CLOCK) == 3988 and s.get(*hwctl.AB_GPU_VOLT) == 0.8


def test_winsensors_helpers():
    """The package-power counter is chosen over the DRAM one, and temperatures convert from tenths of a kelvin."""
    assert ws.pick_power_instance([r"\\PC\Energy Meter(RAPL_Package0_DRAM)\Power",
                                   r"\\PC\Energy Meter(RAPL_Package0_PKG)\Power"]).endswith("PKG)\\Power")
    assert ws.pick_power_instance([r"\\PC\Energy Meter(_Total)\Power"]) is None
    assert abs(ws.kelvin_tenths_to_c(2982) - 25.05) < 0.06 and ws.kelvin_tenths_to_c(0) is None


def test_log_header_written_at_once_and_empty_log_reused():
    """An empty log gets its header straight away and is reused, but a log with different columns is renamed
    (with a timestamp) and a fresh one is started."""
    old = governor.LOG                                  # restored in `finally`, so other tests aren't affected
    with tempfile.TemporaryDirectory() as d:
        try:
            governor.LOG = Path(d) / "ThermalShare_log.csv"
            governor.LOG.write_text("")                       # left empty by a window closed before any game
            logf, _ = governor.open_log()
            assert governor.LOG.read_text().strip() == ",".join(governor.LOG_COLS)   # flushed before any row
            logf.close()
            assert [p.name for p in Path(d).iterdir()] == ["ThermalShare_log.csv"]  # nothing renamed
            governor.LOG.write_text("old,columns\n")
            logf, _ = governor.open_log()
            logf.close()
            assert sorted(p.name.split("_")[0] for p in Path(d).iterdir()) == ["ThermalShare", "ThermalShare"]
        finally:
            governor.LOG = old


def test_ab_names_and_paths():
    """The A/B test's files carry ThermalShare's name, and its test game has an image name of its own (so it isn't
    counted as another ThermalShare, and PresentMon can be pointed at it alone)."""
    with tempfile.TemporaryDirectory() as work:
        synthab = gg.configure_ab(work)
        assert synthab.GAME == "ThermalShare_synthgame.exe" and synthab.GAME == gg.AB_GAME
        assert gg.AB_GAME.lower() != f"{gg.APP}.exe".lower()
        assert synthab.CALIB == gg.AB_CALIB == gg.DIR / "ThermalShare_synthgame.json"   # the one file that is kept
        # Everything the test writes while it runs goes to the scratch folder, not into the app folder.
        for p in (synthab.RESULTS, synthab.REPORT, synthab.RUNS):
            assert Path(work) in p.parents
        assert synthab.SAVED_NOTE == ""
        first = synthab.preflight
        gg.configure_ab(work)                                    # again: must not wrap the check a second time
        assert synthab.preflight is first is gg.ab_preflight


def test_is_ab_game_exe():
    """Only the copy named ThermalShare_synthgame.exe acts as the test game; ThermalShare.exe and python don't."""
    assert gg.is_ab_game_exe(r"C:\Tools\ThermalShare\ThermalShare_synthgame.exe")
    assert gg.is_ab_game_exe(r"c:\tools\thermalshare_synthgame.EXE")
    assert not gg.is_ab_game_exe(r"C:\Tools\ThermalShare\ThermalShare.exe")
    assert not gg.is_ab_game_exe(r"C:\Python311\python.exe")


def test_ab_game_copy_made_and_removed():
    """The test game is a fresh copy of the exe next to it, deleted afterwards (and deleting twice is fine)."""
    with tempfile.TemporaryDirectory() as d:
        fake_exe = Path(d) / "ThermalShare.exe"
        fake_exe.write_bytes(b"MZ-pretend-exe")
        dst = gg.prepare_ab_game(dst_dir=d, exe=fake_exe)
        assert dst == Path(d) / "ThermalShare_synthgame.exe" and dst.read_bytes() == b"MZ-pretend-exe"
        fake_exe.write_bytes(b"MZ-newer-build")
        assert gg.prepare_ab_game(dst_dir=d, exe=fake_exe).read_bytes() == b"MZ-newer-build"   # replaced, not kept
        gg.remove_ab_game(d)
        assert not dst.exists() and fake_exe.exists()
        gg.remove_ab_game(d)


def test_ab_needs_the_built_exe():
    """From the .py the test can't start its game copy; --ab says so straight away (before any load test)."""
    assert not hwctl.FROZEN
    try:
        gg.prepare_ab_game()
        raise AssertionError("should have refused")
    except SystemExit as e:
        assert "built ThermalShare.exe" in str(e)
    old = sys.argv
    try:
        sys.argv = ["ThermalShare", "--ab"]
        try:
            gg.main()
            raise AssertionError("should have refused")
        except SystemExit as e:
            assert "built ThermalShare.exe" in str(e)
    finally:
        sys.argv = old


def test_ab_args():
    """--ab turns the governor's own A/B switch on and carries every setting synthab reads; without --ab it's off."""
    a = gg.argparse.Namespace(game=None, no_auto=False, once=False, observe=False, fps_cap=None, cpu_min=None)
    assert not gg.governor_args(_profile(), a).synth_ab
    opts = dict(rounds=3, minutes=5.0, cool_gpu=50.0, cool_cpu=58.0, cool_max=600.0, latency="normal",
                recalibrate=True, warmup_min=1.0, cpu_watts=34.0)
    ns = gg.governor_args(_profile(), a, opts)
    assert ns.synth_ab and not ns.observe
    for k, v in opts.items():
        assert getattr(ns, k) == v
    assert ns.cpu_min == 2400                                # still the profile's floor, which synthgame is sized for


LOG_HEAD = "time,app,mode,action,fps,gpu_clock,gpu_temp,cpu_clock,cpu_temp\n"


def _write_run_log(path, start, seconds, fps, gpu_clock, gpu_temp, cpu_clock, cpu_temp, idle=0):
    """A fake per-second governor log (the columns abgraphs reads): `idle` rows first, then `seconds` rows of play.
    start is an hour:minute:second text, e.g. "09:00:00". None for cpu_temp writes the "-" the governor logs for
    a missing reading."""
    h, m, s = (int(x) for x in start.split(":"))
    t0 = h * 3600 + m * 60 + s
    lines = [LOG_HEAD]
    for i in range(idle + seconds):
        t = (t0 + i) % 86400
        clock = f"{t // 3600:02d}:{t // 60 % 60:02d}:{t % 60:02d}"
        if i < idle:
            lines.append(f"{clock},,idle,,,-,-,-,-\n")
        else:
            lines.append(f"{clock},synthgame.exe,gpu,hold,{fps},{gpu_clock},{gpu_temp},{cpu_clock},"
                         f"{'-' if cpu_temp is None else cpu_temp}\n")
    Path(path).write_text("".join(lines))


def test_graph_scales_and_parsing():
    """Round axis numbers, a minutes axis, times that wrap past midnight, and idle / missing readings left out."""
    import abgraphs as ag
    t = ag.nice_ticks(61.3, 83.9)
    assert t[0] <= 61.3 and t[-1] >= 83.9 and 3 <= len(t) <= 8 and all(abs(b - a - (t[1] - t[0])) < 1e-9 for a, b in zip(t, t[1:]))
    assert ag.nice_ticks(5, 5)[0] < 5 < ag.nice_ticks(5, 5)[-1]                   # a flat line still gets an axis
    assert ag.time_ticks(600) == [0, 120, 240, 360, 480, 600]
    assert ag.clock_seconds("09:20:03") == 9 * 3600 + 20 * 60 + 3 and ag.clock_seconds("nope") is None
    rows = [{"time": "23:59:58", "mode": "idle", "fps": ""}, {"time": "23:59:59", "mode": "gpu", "fps": "90", "cpu_temp": "-"},
            {"time": "00:00:00", "mode": "gpu", "fps": "92", "cpu_temp": "70"},
            {"time": "00:00:01", "mode": "gpu", "fps": "", "cpu_temp": "71"}]
    s = ag.run_series(rows)
    assert s["fps"] == {0: 90, 1: 92}                         # seconds count from the first non-idle row, across midnight
    assert s["cpu_temp"] == {1: 70, 2: 71}                    # "-" and blanks are just missing, not zero


def test_graph_runs_are_combined():
    """Two runs on one side give an average line and a lowest-to-highest band; seconds only one run has still count."""
    import abgraphs as ag
    agg = ag.aggregate([{"fps": {0: 80.0, 1: 90.0}}, {"fps": {0: 100.0, 1: 90.0, 2: 70.0}}], "fps")
    assert agg["x"] == [0, 1, 2] and agg["mean"] == [90.0, 90.0, 70.0]
    assert agg["lo"] == [80.0, 90.0, 70.0] and agg["hi"] == [100.0, 90.0, 70.0]


def test_ab_graph_page():
    """The page has six graphs (clocks, temperatures, FPS; control on the left, governor on the right), the right
    FPS title shows the change against the control, a missing CPU temperature is said so, and no logs gives None."""
    import abgraphs as ag
    with tempfile.TemporaryDirectory() as d:
        runs = Path(d) / "runs"
        runs.mkdir()
        assert ag.write_graphs(None, Path(d) / "none.csv", runs, Path(d) / "out.html") is None
        for rnd in (1, 2):
            _write_run_log(runs / f"20261118_010203_r{rnd}_observe.csv", "09:00:00", 120, 80 + rnd, 1700, 80, 4000, None, idle=3)
            _write_run_log(runs / f"20261118_010203_r{rnd}_governed.csv", "10:00:00", 100, 88 + rnd, 1650, 70, 2400, None)
        (Path(d) / "res.csv").write_text("test,round,arm\n20261118_010203,1,observe\n")
        out = ag.write_graphs(None, Path(d) / "res.csv", runs, Path(d) / "out.html", report_text="a < b & c")
        page = out.read_text(encoding="utf-8")
    assert "<pre>a &lt; b &amp; c</pre>" in page                # the text report is shown, escaped
    assert page.count("<svg id=") == 6
    for panel in ("p-observe-clock", "p-governed-clock", "p-observe-temp", "p-governed-temp", "p-observe-fps", "p-governed-fps"):
        assert f'id="{panel}"' in page
    assert page.index("Governor OFF (control)") < page.index("Governor ON")
    assert "average <b>81.5</b>" in page and "average <b>89.5</b>" in page and "+8.0" in page and "vs control" in page
    assert "CPU: no reading on this PC" in page                # no CPU temperature was logged
    assert "2 runs" in page and "</script>" in page.split('id="graph-data"')[1]


def test_ab_test_leaves_only_the_page():
    """A whole test with a stand-in for governor.run: the page (graphs plus the statistics text) is written where
    asked, the scratch folder the test used is gone, and the normal governor log was never the one written to."""
    import synthab
    seen = {}

    def fake_run(args, ab=None):
        """What governor.run does for the A/B test, in miniature: runs logs, a results row, a governor log."""
        assert args.synth_ab and args.rounds == 1 and args.latency == "reflex"
        seen["work"] = synthab.RUNS.parent
        seen["log"] = governor.LOG
        synthab.RUNS.mkdir(parents=True)
        governor.LOG.write_text("governor log rows")
        for arm, fps in (("observe", 80), ("governed", 90)):
            _write_run_log(synthab.RUNS / f"20261118_010203_r1_{arm}.csv", "09:00:00", 60, fps, 1700, 80, 4000, 90)
            synthab.append_result({"test": "20261118_010203", "round": 1, "arm": arm, "minutes": "1",
                                   "latency": "reflex", "aborted": 0, "cool_reached": 1, "fps": fps})

    real_log, real_run, real_sensors = governor.LOG, governor.run, gg.CpuSensors
    governor.run, gg.CpuSensors = fake_run, lambda: None
    a = gg.argparse.Namespace(game=None, no_auto=False, once=False, observe=False, fps_cap=None, cpu_min=None,
                              ab_rounds=1, ab_minutes=1.0, ab_cool_gpu=55.0, ab_cool_cpu=60.0, ab_cool_max=900.0,
                              ab_latency="reflex", ab_recalibrate=False, ab_warmup=0.0)
    try:
        with tempfile.TemporaryDirectory() as d:
            gg.run_ab_test(_profile(), a, out=Path(d) / "graphs.html")
            page = (Path(d) / "graphs.html").read_text(encoding="utf-8")
            assert page.count("<svg id=") == 6
            assert "<summary>Statistics</summary>" in page and "Synthetic A/B on" in page   # the report is in the page
            assert [p.name for p in Path(d).iterdir()] == ["graphs.html"]                    # and nothing else there
    finally:
        governor.run, gg.CpuSensors = real_run, real_sensors
    assert not seen["work"].exists()                         # scratch folder deleted
    assert seen["log"] != real_log and governor.LOG == real_log    # test rows never went into the normal log
    assert gg.AB_GRAPHS == gg.DIR / "ThermalShare_ab_graphs.html"


def test_reads_hidden_max_frequency():
    """Live check: the power plan's maximum processor frequency can be read (it's a hidden setting, so this
    needs `powercfg /qh`, not `/query`). Read-only; changes nothing."""
    v = hwctl.powercfg_get(hwctl.PROCFREQMAX)
    assert v is not None and v >= 0


def test_winsensors_live():
    """Live check: Windows' performance counters give a believable CPU clock, and the registry gives the CPU's
    base clock and name."""
    w = ws.WinCpuSensors()
    assert w.ok
    time.sleep(0.5)
    d = w.read()
    assert 300 < d["cpu clock"][0] < 6500
    assert ws.cpu_base_mhz() and ws.cpu_name()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
