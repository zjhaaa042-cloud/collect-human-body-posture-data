@echo off
chcp 65001 >nul
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

if not defined NEED_INSTALL (
  "%PYTHON_CMD%" -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 12) else 1)" >nul 2>nul
  if errorlevel 1 (
    echo [WARN] Python 3.10 or 3.11 is required by the D435i runtime.
    set "NEED_INSTALL=1"
  )
)

if not defined NEED_INSTALL (
  "%PYTHON_CMD%" -c "import pydantic, websockets, cv2, numpy, loguru, mediapipe, pyorbbecsdk, pyrealsense2" >nul 2>nul
  if errorlevel 1 (
    echo [WARN] Backend or RGB-D camera SDK dependencies are incomplete.
    set "NEED_INSTALL=1"
  )
)

if not exist "models\pose_landmarker_full.task" (
  echo [WARN] MediaPipe pose model was not found.
  set "NEED_INSTALL=1"
)

if not exist "frontend\node_modules\.bin\vite.cmd" (
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
  "%PYTHON_CMD%" -c "import pyorbbecsdk, pyrealsense2" >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Gemini 336L or D435i Python SDK is still unavailable.
    echo Required imports: pyorbbecsdk and pyrealsense2
    pause
    exit /b 1
  )
  if not exist "frontend\node_modules\.bin\vite.cmd" (
    echo.
    echo [WARN] Frontend startup dependency is still missing.
    echo Trying frontend dependency installer again...
    echo.
    cd /d "%~dp0"
    call "%~dp0install_frontend_deps.bat" --no-pause
    cd /d "%~dp0"
    if errorlevel 1 (
      echo [ERROR] Frontend dependency installation failed.
      echo Please install Node.js 20 LTS or 22 LTS, then run go.bat again.
      pause
      exit /b 1
    )
    if not exist "frontend\node_modules\.bin\vite.cmd" (
      echo [ERROR] Frontend dependencies are still missing.
      echo Missing: frontend\node_modules\.bin\vite.cmd
      echo Please install Node.js 20 LTS or 22 LTS, then run install_deps.bat again.
      pause
      exit /b 1
    )
  )
)

echo Starting managed backend and frontend...
start "Body Posture Collector" /min "%PYTHON_CMD%" run_all.py

echo.
echo Done! Both services are starting under one process manager...
echo Closing the frontend will also stop backend and Vite after 5 seconds.
echo.
echo Backend:  ws://localhost:8765
echo Frontend: http://localhost:3000
echo.
echo Window will close in 3 seconds...
timeout /t 3 /nobreak >nul
