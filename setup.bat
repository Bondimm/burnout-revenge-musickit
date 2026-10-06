@echo off
rem One-time setup: creates a private Python environment (.venv) and downloads ffmpeg into .\ffmpeg.
rem Safe to run again (repairs or updates the environment).
setlocal
cd /d "%~dp0"
set "PYCHECK=import sys; sys.exit(sys.version_info[:2] < (3, 11))"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "%PYCHECK%" >nul 2>nul || (
        echo The existing .venv does not work with Python 3.11+ - creating it again...
        rmdir /s /q ".venv"
    )
)
if exist ".venv\Scripts\python.exe" goto venv_ok
set "PY="
where py >nul 2>nul && py -3 -c "%PYCHECK%" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "%PYCHECK%" >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python 3.11 or newer is required.
    where py >nul 2>nul && py -3 --version 2>nul
    python --version 2>nul
    echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then run setup.bat again.
    pause & exit /b 1
)
echo Creating Python environment...
%PY% -m venv .venv || (
    echo Could not create the Python environment in "%~dp0.venv".
    pause & exit /b 1
)
:venv_ok
echo Installing Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (
    echo.
    echo Installing the Python packages failed - see the messages above.
    echo Check the internet connection, then run setup.bat again.
    pause & exit /b 1
)
if exist "ffmpeg\bin\ffmpeg.exe" if exist "ffmpeg\bin\ffprobe.exe" goto ffmpeg_ok
where ffmpeg >nul 2>nul && where ffprobe >nul 2>nul && goto ffmpeg_ok
echo Downloading ffmpeg (needed to read MP3/FLAC/OGG/... files)...
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; $z=Join-Path $env:TEMP 'mk-ffmpeg.zip'; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $z -UseBasicParsing; $t=Join-Path $env:TEMP 'mk-ffmpeg'; if (Test-Path $t) { Remove-Item $t -Recurse -Force }; Expand-Archive $z $t; $d=Get-ChildItem $t -Directory | Select-Object -First 1; New-Item -ItemType Directory -Force 'ffmpeg\bin' | Out-Null; Copy-Item (Join-Path $d.FullName 'bin\ffmpeg.exe'),(Join-Path $d.FullName 'bin\ffprobe.exe') 'ffmpeg\bin' -Force; Remove-Item $z,$t -Recurse -Force" || (
    echo Could not download ffmpeg automatically. Install it yourself ^(e.g. "winget install Gyan.FFmpeg"^) or put ffmpeg.exe and ffprobe.exe into "%~dp0ffmpeg\bin".
)
:ffmpeg_ok
echo.
echo Setup complete. Start MusicKit with MusicKit.bat
pause
