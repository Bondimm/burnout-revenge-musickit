@echo off
rem One-time setup: creates a private Python environment (.venv) and downloads ffmpeg into .\ffmpeg.
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% --version >nul 2>nul || (
    echo Python 3.11 or newer is required: https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
    pause & exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    %PY% -m venv .venv || (pause & exit /b 1)
)
echo Installing Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
where ffmpeg >nul 2>nul && goto ffmpeg_ok
if exist "ffmpeg\bin\ffmpeg.exe" goto ffmpeg_ok
echo Downloading ffmpeg (needed to read MP3/FLAC/OGG/... files)...
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $z=Join-Path $env:TEMP 'mk-ffmpeg.zip'; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $z; $t=Join-Path $env:TEMP 'mk-ffmpeg'; if (Test-Path $t) { Remove-Item $t -Recurse -Force }; Expand-Archive $z $t; $d=Get-ChildItem $t -Directory | Select-Object -First 1; New-Item -ItemType Directory -Force 'ffmpeg' | Out-Null; Copy-Item (Join-Path $d.FullName 'bin') 'ffmpeg' -Recurse -Force; Remove-Item $z,$t -Recurse -Force" || (
    echo Could not download ffmpeg automatically. Install it yourself ^(e.g. "winget install Gyan.FFmpeg"^) or put ffmpeg.exe and ffprobe.exe into "%~dp0ffmpeg\bin".
)
:ffmpeg_ok
echo.
echo Setup complete. Start MusicKit with MusicKit.bat
pause
