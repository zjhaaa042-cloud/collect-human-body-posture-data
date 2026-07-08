@echo off
cd /d "%~dp0"

set "PYTHON_CMD=.venv\Scripts\python.exe"
if exist ".use_system_python" (
  set "PYTHON_CMD=python"
)

if /i "%PYTHON_CMD%"=="python" (
  python run_backend.py
) else if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run_backend.py
) else (
  echo Python virtual environment was not found.
  echo Please run install_deps.bat first.
)
pause
