@echo off
rem Builds ..\ThermalShare.exe from thermalshare.py plus the modules beside it in this folder
rem (hwctl, governor, mapper, synthrun, synthab, synthgame, winsensors).
rem Needs Python 3.9+ with:  py -m pip install -r requirements.txt   (numpy, and pyinstaller to build)
cd /d "%~dp0"
py thermalshare_auto_tests.py || exit /b 1
py -m PyInstaller --noconfirm --onefile --uac-admin --name ThermalShare --paths . ^
  --exclude-module tkinter --distpath .. --workpath build --specpath build thermalshare.py
