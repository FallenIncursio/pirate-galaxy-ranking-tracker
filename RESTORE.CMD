@echo off
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" "windows\restore-game-window.py" > "logs\restore-window.log" 2>&1
start "" notepad.exe "%~dp0logs\restore-window.log"
