@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_windows.ps1" %*
if errorlevel 1 (
  echo.
  echo [ERROR] Windows 安装包构建失败。
  pause
  exit /b 1
)
echo.
echo [OK] Windows 安装包构建完成。
pause
