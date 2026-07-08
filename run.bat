@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run_backend.py
) else (
  echo Python virtual environment was not found.
  echo Please run install_deps.bat first.
)
pause
