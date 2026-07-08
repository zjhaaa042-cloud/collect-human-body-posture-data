@echo off
setlocal
cd /d "%~dp0"

echo.
echo ==========================================
echo   Install project dependencies
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found.
  echo Please install Python 3.10+ and check "Add python.exe to PATH".
  echo Download: https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm was not found.
  echo Please install Node.js 18+ LTS first.
  echo Download: https://nodejs.org/
  echo.
  pause
  exit /b 1
)

echo [1/5] Checking Python version...
python --version
if errorlevel 1 goto fail

echo.
echo [2/5] Creating Python virtual environment...
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto fail
) else (
  echo Existing .venv found, reusing it.
)

echo.
echo [3/5] Installing Python packages...
call ".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo pip was not found in .venv, trying to repair it...
  call ".venv\Scripts\python.exe" -m ensurepip --upgrade
  if errorlevel 1 goto pip_fail
)
call ".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto fail
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
echo [4/5] Checking Node.js and npm versions...
node --version
if errorlevel 1 goto fail
npm --version
if errorlevel 1 goto fail

echo.
echo [5/5] Installing frontend packages...
cd /d "%~dp0frontend"
if exist "package-lock.json" (
  call npm ci
) else (
  call npm install
)
if errorlevel 1 goto fail

cd /d "%~dp0"
echo.
echo ==========================================
echo   Dependencies installed successfully
echo ==========================================
echo.
echo You can now run go.bat to start the project.
echo.
pause
exit /b 0

:pip_fail
cd /d "%~dp0"
echo.
echo ==========================================
echo   Failed to enable pip in .venv
echo ==========================================
echo.
echo The existing .venv is incomplete or broken.
echo Please delete the .venv folder, make sure Python was installed with pip,
echo then run install_deps.bat again.
echo.
pause
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
echo - Install Python 3.10+ and Node.js 18+ LTS.
echo - If PyAudio fails, install Microsoft C++ Build Tools, then run this file again.
echo - If pyorbbecsdk2 fails, install the Orbbec SDK/runtime for your camera.
echo.
pause
exit /b 1
