@echo off
rem Run msgwatch in foreground (Ctrl+C to stop). Background autostart: see scripts\install_startup.ps1
cd /d "%~dp0"
".venv\Scripts\python.exe" -m msgwatch run
pause
