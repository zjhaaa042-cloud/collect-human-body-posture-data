@echo off
cd /d "%~dp0"

echo.
echo ==========================================
echo   Body Posture Data Collection System
echo ==========================================
echo.

set "NEED_INSTALL="

set "PYTHON_CMD=.venv\Scripts\python.exe"
if exist ".use_system_python" (
  set "PYTHON_CMD=python"
)

if /i not "%PYTHON_CMD%"=="python" (
  if not exist ".venv\Scripts\python.exe" (
    echo [WARN] Python virtual environment was not found.
    set "NEED_INSTALL=1"
  )
)

if not exist "frontend\node_modules" (
  echo [WARN] Frontend dependencies were not found.
  set "NEED_INSTALL=1"
)

if defined NEED_INSTALL (
  echo.
  echo Running install_deps.bat now...
  echo.
  call "%~dp0install_deps.bat" --no-pause
  if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed. Startup stopped.
    echo.
    pause
    exit /b 1
  )
  cd /d "%~dp0"
  set "PYTHON_CMD=.venv\Scripts\python.exe"
  if exist ".use_system_python" (
    set "PYTHON_CMD=python"
  )
  if /i not "%PYTHON_CMD%"=="python" (
    if not exist ".venv\Scripts\python.exe" (
      echo [ERROR] Backend Python environment is still missing.
      pause
      exit /b 1
    )
  )
  if not exist "frontend\node_modules" (
    echo [ERROR] Frontend dependencies are still missing.
    pause
    exit /b 1
  )
)

echo [1/2] Starting backend...
start "Backend" cmd /c "%PYTHON_CMD% run_backend.py"

echo [2/2] Waiting 5 seconds...
timeout /t 5 /nobreak >nul

echo [3/2] Starting frontend...
start "Frontend" cmd /c "cd frontend && npm start"

echo.
echo Done! Both services are starting...
echo.
echo Backend:  ws://localhost:8765
echo Frontend: http://localhost:3000
echo.
echo Window will close in 3 seconds...
timeout /t 3 /nobreak >nul
