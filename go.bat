@echo off
cd /d "%~dp0"

echo.
echo ==========================================
echo   Body Posture Data Collection System
echo ==========================================
echo.

echo [1/2] Starting backend...
start "Backend" cmd /k "python run_backend.py"

echo [2/2] Waiting 5 seconds...
timeout /t 5 /nobreak >nul

echo [3/2] Starting frontend...
start "Frontend" cmd /k "cd frontend && npm start"

echo.
echo Done! Both services are starting...
echo.
echo Backend:  ws://localhost:8765
echo Frontend: http://localhost:3000
echo.
echo Press any key to close this window...
pause >nul
