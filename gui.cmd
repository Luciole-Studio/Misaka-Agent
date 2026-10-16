@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3.12 or newer, then try again.
  pause
  exit /b 1
)
python "%~dp0scripts\start-gui.py" %*
if errorlevel 1 pause
