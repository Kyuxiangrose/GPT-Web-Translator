@echo off
chcp 65001 >nul
call "%~dp0scripts\start_service.cmd"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo 翻译服务未能正常运行，错误代码：%EXIT_CODE%
  echo 日志位置：backend\logs\service.log
  pause
)
exit /b %EXIT_CODE%
