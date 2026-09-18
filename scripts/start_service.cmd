@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "GWT_CONFIG_DIR=%LOCALAPPDATA%\GPT-Web-Translator"
set "GWT_USER_ENV=%GWT_CONFIG_DIR%\.env"

if not exist "%GWT_CONFIG_DIR%" mkdir "%GWT_CONFIG_DIR%" >nul 2>nul

if exist "%GWT_USER_ENV%" (
  copy /y "%GWT_USER_ENV%" ".env" >nul
) else if exist ".env" (
  copy /y ".env" "%GWT_USER_ENV%" >nul
) else (
  echo DeepSeek API Key is not configured.
  echo Run the API key configuration file first.
  exit /b 2
)

if exist "%CD%\GPT-Web-Translator-Backend.exe" (
  "%CD%\GPT-Web-Translator-Backend.exe"
  exit /b %ERRORLEVEL%
)

where py.exe >nul 2>nul
if not errorlevel 1 (
  py.exe -3 "%CD%\backend\server.py"
  exit /b %ERRORLEVEL%
)

where python.exe >nul 2>nul
if not errorlevel 1 (
  python.exe "%CD%\backend\server.py"
  exit /b %ERRORLEVEL%
)

echo Python 3.11 or newer was not found.
exit /b 3
