@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ==========================================
echo   Body Posture Data Collection System
echo ==========================================
echo.

rem Avoid starting a second manager when the workspace is already ready.
powershell -NoProfile -Command "$ErrorActionPreference = 'Stop'; $backend = (Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8765/health' -TimeoutSec 2).StatusCode -eq 200; $frontend = (Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:3000' -TimeoutSec 2).StatusCode -eq 200; if ($backend -and $frontend) { exit 0 }; exit 1" >nul 2>nul
if not errorlevel 1 (
  echo [OK] The collector is already running.
  echo Opening the workspace instead of starting duplicate services...
  start "" "http://127.0.0.1:3000"
  exit /b 0
)

where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Node.js was not found. Install Node.js 20.19+ or 22.12+.
  pause
  exit /b 1
)
node -e "const [major, minor] = process.versions.node.split('.').map(Number); process.exit((major === 20 && minor >= 19) || (major === 22 && minor >= 12) || major > 22 ? 0 : 1)"
if errorlevel 1 (
  echo [ERROR] Unsupported Node.js version. Vite 8 requires Node.js 20.19+ or 22.12+.
  pause
  exit /b 1
)

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
  "%PYTHON_CMD%" -c "import pydantic, websockets, cv2, numpy, loguru" >nul 2>nul
  if errorlevel 1 (
    echo [WARN] Core backend dependencies are incomplete.
    set "NEED_INSTALL=1"
  )
)

if not exist "models\pose_landmarker_full.task" (
  echo [INFO] Pose model was not found. The workspace can still start; pose quality tips will be unavailable.
)

if not defined NEED_INSTALL (
  "%PYTHON_CMD%" -c "import mediapipe" >nul 2>nul
  if errorlevel 1 echo [INFO] MediaPipe is not installed. The workspace can still start; pose quality tips will be unavailable.
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
echo Run go.bat again later to reopen an already-running workspace safely.
echo.
echo Backend:  ws://localhost:8765
echo Frontend: http://localhost:3000
echo.
exit /b 0
