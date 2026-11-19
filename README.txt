ThermalShare - more FPS on laptops where the CPU and GPU share one cooler

What it does
  On most gaming laptops the CPU and GPU share heatpipes, so heat from one slows the other. While a game runs,
  ThermalShare works out which chip is holding the frame rate back and lowers the other one's clock. The chip it
  slows makes less heat, which leaves more cooling for the one that matters. When the game closes, everything goes
  back to normal.

  It only ever LOWERS clocks, and only while a game runs. It never overclocks, undervolts, or changes power limits,
  voltages or fans. If lowering a clock costs frames, it puts it back.

What you need
  - Windows 10 or 11, an NVIDIA GPU with its normal driver (nvidia-smi comes with it), Intel or AMD CPU.
  - Nothing else: no ThrottleStop, no MSI Afterburner. GPU readings and limits go through the NVIDIA driver, the
    CPU limit through the Windows power plan, CPU clock and power readings come from Windows.
    (If MSI Afterburner happens to be running, its CPU temperature is logged too. Windows doesn't report the CPU
    temperature on many laptops, so without Afterburner that log column stays empty. Nothing depends on it.)
  - PresentMon (Intel's frame-time tool, MIT licence) is in tools\. If it's missing, it downloads once.

How to use
  1. Put this folder somewhere writable (Desktop, C:\Tools), not Program Files.
  2. Plug in the charger and close your games.
  3. Double-click ThermalShare.exe and allow the admin prompt.
  4. First start only: a one-time load test, about 6 minutes. Press Enter to begin, then leave the laptop alone.
       Part 1 fills the screen with a moving pattern to load the GPU (Esc stops it).
       Part 2 loads the CPU.
     It measures how much speed each chip loses at each lower clock limit and saves that as
     ThermalShare_profile.json.
  5. Then it waits for a game. Leave its window open while you play (minimise it). Closing the window puts the
     clocks back.
  Every start after that: double-click, and it goes straight to waiting for a game using the saved profile.

Which games
  Any app that stays fullscreen in the foreground for about 6 seconds, plus anything listed in
  ThermalShare_games.txt (created on first start; add games you play in a window). Browsers, video players,
  launchers and chat apps are never governed. A line  -name.exe  in the file keeps an app from being governed.
  If a game has its own frame limit that ThermalShare doesn't find, write it after the name:  game.exe 60

Run the load test again (ThermalShare.exe --recalibrate) after
  - changing a GPU overclock (in Afterburner or anywhere else),
  - a GPU driver or BIOS update,
  - cleaning the fans or repasting.
  A different CPU or GPU is noticed automatically.

If the PC crashes or loses power mid-game
  The CPU limit is a power-plan setting and survives a restart. Just start ThermalShare again: it puts back
  anything left over before doing anything else. Or run  ThermalShare.exe --restore  to only do that.

Options (from a terminal)
  --recalibrate      run the load test again, then govern
  --calibrate-only   run the load test if it's needed, then exit
  --observe          log what it would do, change nothing
  --game x.exe       also govern x.exe whenever it runs
  --no-auto          only govern games listed in ThermalShare_games.txt
  --once             exit after the first game closes
  --fps-cap 60       frame limit for every game (normally found per game)
  --cpu-min 2400     never limit the CPU below this (default: just under the CPU's base clock, from the profile)
  --show             print the saved profile
  --restore          undo leftover limits, then exit

Does it actually help? The A/B test
  Double-click ThermalShare_ab.bat, or from a terminal:  ThermalShare.exe --ab
  (plug in the charger, close games and other heavy apps; the .bat shows these reminders and passes any options on)
  It plays the same built-in test game several times with ThermalShare OFF (it only logs) and ON, in the order
  off, on, on, off ... so slow drift such as the room warming up cancels out. Every run starts after the laptop has
  cooled down. The first time it also sizes the test game's work for your PC (about 3 minutes, a window opens).
  With the defaults it takes about an hour. Leave the laptop alone; Esc in the test game's window stops it.
  The result is one file, ThermalShare_ab_graphs.html (double-click to open it in your browser). It has six graphs,
  governor OFF (the control) on the left and governor ON on the right. Top: GPU and CPU clock speed. Middle: GPU and
  CPU temperature. Bottom: FPS, with its average in the title. Each line is the average of the runs, the pale band is
  the lowest to highest run, and both graphs in a row use the same scale. Hover a graph to read exact values. At the
  bottom, "Table view" lists the averages and "Statistics" gives both sides' FPS, 5% and 1% lows, clocks and
  temperatures, and for each difference a 95% interval with a verdict (REAL GAIN, REAL LOSS or within noise). Two runs
  a side give wide intervals; --ab-rounds 3 or more narrows them. (If Windows gives no CPU temperature on your PC,
  that line is left out.)
  Nothing else is kept: the test's own logs go to a temporary folder that is deleted at the end, and the synthetic
  game's rows are not added to ThermalShare_log.csv. A new test replaces the page; copy it if you want to keep it.
  The test game is a copy of ThermalShare.exe saved as ThermalShare_synthgame.exe while the test runs, so it only
  works from the built exe, not from the .py. The copy is deleted afterwards.

  --ab-rounds 2      off/on pairs (default 2 = 4 runs)
  --ab-minutes 10    length of each run
  --ab-cool-gpu 55   cool the GPU to this many C before each run
  --ab-cool-cpu 60   cool the CPU to this many C before each run (skipped when Windows gives no CPU temperature)
  --ab-cool-max 900  longest cool-down wait, in seconds
  --ab-latency reflex|normal   reflex: the game waits for the GPU inside its frame, like War Thunder with NVIDIA
                     Reflex (default); normal: the CPU runs ahead of the GPU
  --ab-warmup 3      minutes of ungoverned warm-up before the first run
  --ab-recalibrate   size the test game's work again

Files it creates here
  ThermalShare_profile.json   the load test result
  ThermalShare_log.csv        one line per second while governing: FPS, which chip is the bottleneck, limits,
                              clocks, temperatures, power
  ThermalShare_games.txt      your game list
  ThermalShare_restore.json   while ThermalShare is running: the original settings to put back
  The A/B test adds:
  ThermalShare_ab_graphs.html the latest test's six graphs and statistics (the only result file)
  ThermalShare_synthgame.json the test game's calibration for this PC (so it's only set up once)
  ThermalShare_synthgame.exe  only while a test is running

Windows Defender / SmartScreen may warn about it: unsigned programs that ask for admin often get flagged.
"More info -> Run anyway", or build it yourself from source\ (see source\build_exe.bat; needs
source\requirements.txt).
