@echo off
rem A/B test of ThermalShare: governor off vs on, on a built-in synthetic game.
rem Extra options pass straight through, e.g.  ThermalShare_ab_test.bat --ab-rounds 3 --ab-minutes 5
rem (full list: README.txt, or  ThermalShare.exe --help)
cd /d "%~dp0"
if not exist ThermalShare.exe (
    echo ThermalShare.exe is not in this folder: %~dp0
    echo Build it first with source\build_exe.bat
    pause
    exit /b 1
)
echo ================================================================
echo  ThermalShare A/B test: governor OFF vs ON on a synthetic game
echo  Default: 4 runs of 10 minutes with cool-downs, about 1 hour.
echo ================================================================
echo  - Plug the charger in.
echo  - Close games, heavy apps and any other governor window.
echo  - Leave the laptop alone. Esc in the test game's window stops the test.
echo  - The first time it also sets up the test game for this PC (about 3 minutes).
echo.
echo  Windows will ask for admin; the test then runs in its own window
echo  and, when it is done, writes ThermalShare_ab_graphs.html: six graphs
echo  (governor off on the left, on on the right) and the statistics. That is
echo  the only file it leaves behind.
echo  (Needs the load test done once: the first start of ThermalShare.exe
echo   does it, and this does it for you if it hasn't been done.)
echo.
pause
ThermalShare.exe --ab %*
