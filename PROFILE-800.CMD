@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "windows\set-800-profile.py"
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pg_rankings --config config.toml validate-config
if errorlevel 1 exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "windows\update-scheduled-task.ps1"
exit /b %errorlevel%
