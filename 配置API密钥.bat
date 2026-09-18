@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\configure_key.ps1"
if errorlevel 1 (
  echo.
  echo 配置未完成。
)
pause
