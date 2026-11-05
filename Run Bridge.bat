@echo off
rem Double-click to start the bridge on Windows. The control page opens in your browser:
rem http://127.0.0.1:8766  -  close this window (or press Ctrl+C) to stop the bridge.
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONUTF8=1
if not exist ".venv\Scripts\ek-bridge.exe" (
  echo The bridge is not installed yet. See docs\setup-guide.md, step 2.
  pause
  exit /b 1
)
".venv\Scripts\ek-bridge.exe" --open %*
pause
