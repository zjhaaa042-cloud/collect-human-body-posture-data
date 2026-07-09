@echo off
setlocal
cd /d "%~dp0"

set "PAUSE_ON_EXIT=1"
if /i "%~1"=="--no-pause" set "PAUSE_ON_EXIT=0"

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm was not found.
  echo Please install Node.js 20 LTS or 22 LTS first.
  echo Download: https://nodejs.org/
  echo.
  if "%PAUSE_ON_EXIT%"=="1" pause
  exit /b 1
)

echo.
echo [Frontend] Installing runtime packages from npmmirror registry...
cd /d "%~dp0frontend"
set "INSTALL_FAILED=0"
call npm ci --omit=dev --registry=https://registry.npmmirror.com --legacy-peer-deps --no-audit --no-fund --fetch-retries=5 --fetch-retry-mintimeout=20000 --fetch-retry-maxtimeout=120000

if errorlevel 1 (
  echo.
  echo [WARN] npmmirror registry failed. Retrying with npm registry...
  echo.
  call npm ci --omit=dev --legacy-peer-deps --no-audit --no-fund --fetch-retries=5 --fetch-retry-mintimeout=20000 --fetch-retry-maxtimeout=120000
  if errorlevel 1 set "INSTALL_FAILED=1"
)

cd /d "%~dp0"
if "%INSTALL_FAILED%"=="1" (
  echo.
  echo [ERROR] Frontend dependency installation failed.
  echo This is usually caused by network/proxy issues or an unsupported Node.js version.
  echo Try Node.js 20 LTS or 22 LTS, then run this file again.
  echo.
  if "%PAUSE_ON_EXIT%"=="1" pause
  exit /b 1
)

if not exist "frontend\node_modules\.bin\vite.cmd" (
  echo.
  echo [ERROR] Frontend packages were not installed correctly.
  echo Missing: frontend\node_modules\.bin\vite.cmd
  echo Try running install_frontend_deps.bat again on a stable network.
  echo.
  if "%PAUSE_ON_EXIT%"=="1" pause
  exit /b 1
)

echo.
echo [Frontend] Dependencies installed successfully.
echo.
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b 0
