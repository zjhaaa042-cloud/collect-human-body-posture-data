@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PAUSE_ON_EXIT=1"
if /i "%~1"=="--no-pause" set "PAUSE_ON_EXIT=0"

echo.
echo ==========================================
echo   Install project dependencies
echo ==========================================
echo.

set "BOOTSTRAP_PYTHON=python"
if defined BODY_POSTURE_PYTHON set "BOOTSTRAP_PYTHON=%BODY_POSTURE_PYTHON%"

"%BOOTSTRAP_PYTHON%" --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found.
  echo Please install Python 3.10 or 3.11 and check "Add python.exe to PATH".
  echo Alternatively set BODY_POSTURE_PYTHON to a compatible python.exe.
  echo Download: https://www.python.org/downloads/
  echo.
  if "%PAUSE_ON_EXIT%"=="1" pause
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm was not found.
  echo Please install Node.js 18+ LTS first.
  echo Download: https://nodejs.org/
  echo.
  if "%PAUSE_ON_EXIT%"=="1" pause
  exit /b 1
)

echo [1/5] Checking Python version...
"%BOOTSTRAP_PYTHON%" --version
if errorlevel 1 goto fail
"%BOOTSTRAP_PYTHON%" -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 12) else 1)"
if errorlevel 1 goto python_version_fail

set "PYTHON_CMD=.venv\Scripts\python.exe"
set "PIP_SCOPE="
if exist ".use_system_python" del /q ".use_system_python" >nul 2>nul

echo.
echo [2/5] Creating Python virtual environment...
if not exist ".venv\Scripts\python.exe" (
  "%BOOTSTRAP_PYTHON%" -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Python virtual environment creation failed.
    goto fail
  )
) else (
  echo Existing .venv found, reusing it.
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 12) else 1)"
  if errorlevel 1 goto venv_version_fail
)

echo.
echo [3/5] Installing Python packages...
call "!PYTHON_CMD!" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo pip was not found, trying to enable it...
  call "!PYTHON_CMD!" -m ensurepip --upgrade
  if errorlevel 1 goto pip_fail
)
call "!PYTHON_CMD!" -m pip install !PIP_SCOPE! --upgrade pip setuptools wheel
if errorlevel 1 goto fail
call "!PYTHON_CMD!" -m pip uninstall -y opencv-python >nul 2>nul
call "!PYTHON_CMD!" -m pip install !PIP_SCOPE! -r requirements.txt
if errorlevel 1 goto fail

if not exist "models\pose_landmarker_full.task" (
  echo Downloading MediaPipe full pose model...
  powershell.exe -NoProfile -NonInteractive -Command "Invoke-WebRequest -Uri 'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task' -OutFile 'models\pose_landmarker_full.task'"
  if errorlevel 1 goto fail
)

echo.
echo [4/5] Checking Node.js and npm versions...
node --version
if errorlevel 1 goto fail
npm --version
if errorlevel 1 goto fail

echo.
echo [5/5] Installing frontend packages...
cd /d "%~dp0"
call "%~dp0install_frontend_deps.bat" --no-pause
if errorlevel 1 goto fail

cd /d "%~dp0"
if not exist "frontend\node_modules\.bin\vite.cmd" (
  echo.
  echo [ERROR] Frontend packages were not installed correctly.
  echo The file frontend\node_modules\.bin\vite.cmd was not created.
  echo Please check the npm output above, then run install_deps.bat again.
  goto fail
)

echo.
echo ==========================================
echo   Dependencies installed successfully
echo ==========================================
echo.
echo You can now run go.bat to start the project.
echo.
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b 0

:pip_fail
cd /d "%~dp0"
echo.
echo ==========================================
echo   Failed to enable pip in .venv
echo ==========================================
echo.
echo pip could not be enabled for this Python installation.
echo Please reinstall Python 3.10+ from https://www.python.org/downloads/
echo and make sure "pip" and "Add python.exe to PATH" are selected.
echo.
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b 1

:python_version_fail
cd /d "%~dp0"
echo.
echo ==========================================
echo   Unsupported Python version
echo ==========================================
echo.
echo D435i firmware 5.15.1.55 requires pyrealsense2 2.54.2.
echo Its Windows wheel supports Python 3.10 and 3.11 only.
echo Install Python 3.10/3.11 or set BODY_POSTURE_PYTHON to its python.exe.
echo.
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b 1

:venv_version_fail
cd /d "%~dp0"
echo.
echo ==========================================
echo   Existing .venv is incompatible
echo ==========================================
echo.
echo The existing .venv is not Python 3.10/3.11.
echo Back it up or remove it, then rerun install_deps.bat with a compatible Python.
echo.
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b 1

:fail
cd /d "%~dp0"
echo.
echo ==========================================
echo   Dependency installation failed
echo ==========================================
echo.
echo Please check the error above.
echo Common fixes:
echo - Install Python 3.10 or 3.11 and Node.js 18+ LTS.
echo - Python 3.12/3.13 cannot install the firmware-matched pyrealsense2 2.54.2 wheel.
echo - If frontend install fails, use Node.js 20 LTS or 22 LTS instead of very new Node versions.
echo - If npm reports ECONNRESET, rerun install_frontend_deps.bat or switch to a stable network.
echo - If PyAudio fails, install Microsoft C++ Build Tools, then run this file again.
echo - If pyorbbecsdk2 fails, install the Orbbec SDK/runtime for your camera.
echo.
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b 1
