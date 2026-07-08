@echo off
cd /d "%~dp0"

echo.
echo ==========================================
echo   Body Posture Data Collection System
echo ==========================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [WARN] Python virtual environment was not found.
  echo Please run install_deps.bat first to install backend and frontend dependencies.
  echo.
  pause
  exit /b 1
)

if not exist "frontend\node_modules" (
  echo [WARN] Frontend dependencies were not found.
  echo Please run install_deps.bat first to install backend and frontend dependencies.
  echo.
  pause
  exit /b 1
)

echo [1/2] Starting backend...
start "Backend" cmd /c ".venv\Scripts\python.exe run_backend.py"

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
