# ThermalShare

More FPS on gaming laptops where the CPU and GPU share one cooler.

On laptops where GPU and CPU share heatpipes, the heat from one component slows the other. While a game runs,
ThermalShare works out which chip is holding the frame rate back and lowers the clock of the *other* one. That chip
makes less heat, which leaves more cooling for the chip that matters. When the game closes, everything goes back
to normal.

It only ever **lowers** clocks, and only while a game runs. It never overclocks, undervolts, or touches power
limits, voltages or fans. If lowering a clock costs frames, it puts the clock back.

<img width="2560" height="2520" alt="ThermalShare_ab_graphs" src="https://github.com/user-attachments/assets/3e3761c9-bca7-4816-a28f-c8e5ed4f6c27" />

> This file is the overview for people working on the project. The user guide (how to use the exe, every option,
> troubleshooting) is [README.txt](README.txt).

## Requirements

- Windows 10 or 11, run as administrator (the exe asks).
- An NVIDIA GPU with its normal driver (`nvidia-smi` is used for readings and clock limits).
- Nothing else for normal use. CPU clock and power come from Windows' performance counters; MSI Afterburner, if it
  happens to be running, adds the CPU temperature.
- PresentMon (frame timing, in `tools\`) downloads once if it is missing.

## Quick start

1. Double-click `ThermalShare.exe` and allow the admin prompt.
2. First start only: a ~6 minute load test measures how much speed each chip loses at each lower clock limit and
   saves it as `ThermalShare_profile.json`. Plug in the charger and leave the laptop alone.
3. It then waits for a game (any app fullscreen in the foreground, or anything in `ThermalShare_games.txt`).
   Leave the window open while you play.

To check that it really helps, double-click `ThermalShare_ab.bat` (see [A/B test](#ab-test)).

## How it works

```
 first start                         every game
 ┌────────────────┐   profile    ┌──────────────────────────────────────────────┐
 │  load test     │ ──────────►  │ PresentMon frame timing  ─►  classify        │
 │ (GPU sweep,    │   .json      │   gpu-bound / cpu-bound / capped              │
 │  CPU sweep)    │              │ policy: lower the idle chip one step,         │
 └────────────────┘              │   check FPS held, else revert and back off    │
                                 │ apply: nvidia-smi GPU limit, power-plan CPU   │
                                 │   limit; log one row a second                 │
                                 └──────────────────────────────────────────────┘
```

- **Load test** (`thermalshare.py`): renders a fixed GPU load and steps the GPU clock limit down, then loads every
  CPU thread and steps the CPU frequency limit down. At each step it records clock, power, temperature and
  *speed* (work done compared with no limit). That list of points is the profile.
- **Governor** (`governor.py`): once a second it looks at the last two seconds of frames. If the GPU is busy most
  of the frame the game is GPU-bound, so the CPU can slow as far as its work still fits in the GPU's time; if the
  CPU is the limit, the GPU can slow instead; if the game waits on a frame limiter, both have slack. It lowers one
  step at a time, waits, and reverts (with a growing back-off) if FPS or the 5% low fell.
- **Limits**: the GPU limit is `nvidia-smi -lgc`; the CPU limit is the Windows power plan's *maximum processor
  frequency*. The original values are written to `ThermalShare_restore.json` first and put back on game exit,
  Ctrl+C, closing the window, or the next start after a crash or power loss (`--restore` does only that).

## Folder layout

| Path | What it is |
|---|---|
| `ThermalShare.exe` | The program (PyInstaller one-file build, asks for admin) |
| `ThermalShare_ab.bat` | Starts the A/B test with a few reminders first |
| `README.txt` | User guide |
| `SHA256SUMS.txt` | Checksums of the exe and PresentMon |
| `tools\` | `PresentMon-*.exe` (Intel's frame-time tool, MIT licence) |
| `source\` | All the source needed to build the exe (below) |
| `ThermalShare_profile.json` | Load test result (created on first start) |
| `ThermalShare_games.txt` | Your game list (created on first start) |
| `ThermalShare_log.csv` | One row per second while governing |
| `ThermalShare_restore.json` | Only while running: the settings to put back |

The A/B test adds `ThermalShare_ab_graphs.html` (its only result file), `ThermalShare_synthgame.json` (the test
game's calibration) and, only while a test runs, `ThermalShare_synthgame.exe`.

## Source (`source\`)

| File | Role |
|---|---|
| `thermalshare.py` | Entry point: command line, profile, the load test, the A/B test hook-up |
| `governor.py` | The game watcher, classifier, step-down/revert policy, actuator and CSV log |
| `hwctl.py` | Hardware and Windows plumbing: `nvidia-smi`, power plan, Afterburner shared memory, PresentMon, restore file |
| `winsensors.py` | CPU clock, power and temperature from Windows performance counters |
| `mapper.py` | Helpers for choosing GPU limits and the CPU stress worker (also TuneKit's mapper tool) |
| `synthgame.py` | A repeatable synthetic game: Direct3D 11 GPU load plus CPU math per frame (needs numpy) |
| `synthab.py` | The A/B test: run order, cool-downs, statistics, report |
| `abgraphs.py` | Draws the A/B test's six graphs as one self-contained HTML page (plain SVG, standard library only) |
| `synthrun.py` | Runs TuneKit's `synth.exe` (pulled in by `mapper.py`; not used by ThermalShare itself) |
| `thermalshare_auto_tests.py` | Tests (fakes for the hardware, plus two live Windows checks) |
| `build_exe.bat` | Runs the tests, then builds `..\ThermalShare.exe` with PyInstaller |
| `requirements.txt` | `numpy` (runtime), `pyinstaller` (build only) |

`governor.py`, `hwctl.py`, `mapper.py`, `synthab.py`, `synthgame.py`, `synthrun.py` and `winsensors.py` are
copies of the modules in `TuneKit\source`, with the same code but extra comments (plus the one addition to `synthab.py`
noted below). `thermalshare.py` repoints the
file names they use (profile, log, game list, restore file) at ThermalShare's own before importing them.

## Build, run and test

```bash
py -m pip install -r source\requirements.txt
py source\thermalshare_auto_tests.py     # tests only
source\build_exe.bat                      # tests, then ..\ThermalShare.exe
```

`py source\thermalshare.py` runs it from source (from an elevated terminal). The A/B test needs the built exe.

## A/B test

`ThermalShare.exe --ab` (or `ThermalShare_ab_test.bat`) plays the same synthetic game several times with the governor
**off** (it only logs) and **on**, in the order off, on, on, off so slow drift cancels out, each run starting from a
cooled-down laptop. The defaults (2 pairs of 10 minutes) take about an hour; options are in `README.txt`.

The only thing it leaves behind is `ThermalShare_ab_graphs.html`: six graphs in two columns, control (governor off)
on the left and governor on on the right. Rows: GPU and CPU clock speed, GPU and CPU temperature, FPS with its
average. Lines are the average of the runs, bands the lowest to highest run, and both sides of a row share a scale.
A table view lists the averages, and a Statistics section gives both sides' FPS, lows, clocks and temperatures with a
95% interval on each difference and a verdict (REAL GAIN, REAL LOSS or within noise).

While it runs, the test's per-run logs, results and report live in a scratch folder in the system temp folder
(`run_ab_test` in `thermalshare.py`) that is deleted at the end, and the governor log is pointed at that folder too,
so the synthetic game's rows never reach `ThermalShare_log.csv`. The page is built from those logs just before they
are deleted. (`synthab.py` has one small addition for this: a `SAVED_NOTE` setting for the "saved to ..." line.)

The synthetic game is a copy of `ThermalShare.exe` saved as `ThermalShare_synthgame.exe` for the duration of the
test: started under that name the exe acts as the game instead of as ThermalShare. It gets its own image name so
PresentMon can be pointed at it alone and the governor can see it start and stop.

## Limits

- NVIDIA GPUs only; Windows only.
- Written and tuned on one laptop (an i7-9750H with an NVIDIA GPU). Defaults such as the CPU floor (just under base clock) come
  from the profile and should adapt, but other machines are less tested.
