@echo off
rem Starts the MusicKit window. Run setup.bat once first.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo MusicKit is not set up yet - running setup.bat first.
    call setup.bat
)
start "" ".venv\Scripts\pythonw.exe" -m musickit gui
