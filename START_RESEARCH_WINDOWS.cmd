@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto venv
py -3.12 --version >nul 2>nul
if errorlevel 1 (
    echo Install Python 3.12 with the Windows Python launcher or create .venv first.
    pause
    exit /b 2
)
py -3.12 -m quarter_hour run %*
exit /b %errorlevel%

:venv
".venv\Scripts\python.exe" -m quarter_hour run %*
exit /b %errorlevel%
