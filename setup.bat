@echo off
rem One-time setup: creates a private Python environment (.venv) and downloads ffmpeg into .\ffmpeg.
rem Works with any installed 64-bit Python 3.12 or newer, or with none at all: setup picks an installed Python for
rem which every package is available ready-made (nothing is compiled). When there is none, it downloads a private
rem Python into .\.python (no admin rights needed; nothing outside this folder changes).
rem Safe to run again (repairs or updates the environment).
rem MUSICKIT_NO_PAUSE=1 (or CI=true) skips the final pause; MUSICKIT_PRIVATE_PYTHON=1 skips the installed Pythons.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
rem the private Python: a version with ready-made packages everywhere, downloaded with uv (https://astral.sh/uv)
set "PRIVATE_PY=3.13"
set "UV_VERSION=0.12.24"
set "VERCHECK=import sys; ok = sys.version_info >= (3, 11) and sys.maxsize > 2**32; print(*sys.version_info[:2], sep='.') if ok else sys.exit(1)"
set "TRIED=;"
set "FORCE="

rem ---------------------------------------------------------------- Python environment
if not exist ".venv\Scripts\python.exe" goto new_venv
set "VER="
for /f "delims=" %%v in ('call ".venv\Scripts\python.exe" -c "%VERCHECK%" 2^>nul') do set "VER=%%v"
if defined VER (
    echo Updating the Python packages ^(Python %VER%^)...
    call :install_packages && goto packages_ok
    set "TRIED=;%VER%;"
)
echo The existing Python environment (.venv) cannot be used - setting up a new one...
if exist ".venv-old" rmdir /s /q ".venv-old"
move ".venv" ".venv-old" >nul 2>nul || rmdir /s /q ".venv"

:new_venv
if exist ".venv" rmdir /s /q ".venv"
call :try_existing_private
if "%MUSICKIT_PRIVATE_PYTHON%"=="1" goto private
call :try_python py -3
call :try_python python
for %%v in (3.13 3.12 3.14 3.15 3.16 3.17 3.11) do call :try_python py -%%v
:private
if not exist ".venv\Scripts\python.exe" call :private_python
if exist ".venv\Scripts\python.exe" goto packages_ok
if exist ".venv-old\Scripts\python.exe" if not exist ".venv" move ".venv-old" ".venv" >nul 2>nul
echo.
echo Setting up Python failed - see the messages above.
echo Check the internet connection, then run setup.bat again.
call :pause
exit /b 1

:packages_ok
if exist ".venv-old" rmdir /s /q ".venv-old"

rem ---------------------------------------------------------------- ffmpeg
if exist "ffmpeg\bin\ffmpeg.exe" if exist "ffmpeg\bin\ffprobe.exe" goto ffmpeg_ok
where ffmpeg >nul 2>nul && where ffprobe >nul 2>nul && goto ffmpeg_ok
echo Downloading ffmpeg (needed to read MP3/FLAC/OGG/... files)...
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; $z=Join-Path $env:TEMP 'mk-ffmpeg.zip'; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $z -UseBasicParsing; $t=Join-Path $env:TEMP 'mk-ffmpeg'; if (Test-Path $t) { Remove-Item $t -Recurse -Force }; Expand-Archive $z $t; $d=Get-ChildItem $t -Directory | Select-Object -First 1; New-Item -ItemType Directory -Force 'ffmpeg\bin' | Out-Null; Copy-Item (Join-Path $d.FullName 'bin\ffmpeg.exe'),(Join-Path $d.FullName 'bin\ffprobe.exe') 'ffmpeg\bin' -Force; Remove-Item $z,$t -Recurse -Force" || (
    echo Could not download ffmpeg automatically. Install it yourself ^(e.g. "winget install Gyan.FFmpeg"^) or put ffmpeg.exe and ffprobe.exe into "%~dp0ffmpeg\bin".
)
:ffmpeg_ok
echo.
echo Setup complete ^(Python %VER%^). Start MusicKit with MusicKit.bat
call :pause
exit /b 0

rem ---------------------------------------------------------------- subroutines
:pause
if not "%MUSICKIT_NO_PAUSE%"=="1" if not defined CI pause
exit /b 0

:install_packages
rem Installs ready-made packages only (never compiles). A quiet check first whether every package has a download
rem for this Python, so an unsupported Python is skipped fast and without a wall of errors.
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>nul
".venv\Scripts\python.exe" -m pip install --dry-run --quiet --only-binary=:all: -r requirements.txt >nul 2>nul || (
    if not defined FORCE exit /b 2
)
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements.txt || exit /b 1
exit /b 0

:try_python
rem try_python <command that starts Python>: builds .venv with it; fails when that Python cannot be used
if exist ".venv\Scripts\python.exe" exit /b 0
set "VER="
for /f "delims=" %%v in ('call %* -c "%VERCHECK%" 2^>nul') do set "VER=%%v"
if not defined VER exit /b 1
if defined FORCE goto try_python_go
call set "SEEN=%%TRIED:;%VER%;=%%"
if not "%SEEN%"=="%TRIED%" exit /b 1
:try_python_go
echo.
echo Using Python %VER% (%*)...
%* -m venv .venv >nul 2>nul
if not exist ".venv\Scripts\python.exe" (
    echo   Could not create a Python environment with it - trying another Python...
    if exist ".venv" rmdir /s /q ".venv"
    exit /b 1
)
echo Installing Python packages...
call :install_packages && exit /b 0
set "TRIED=%TRIED%%VER%;"
if exist ".venv" rmdir /s /q ".venv"
echo   Not all packages are available ready-made for Python %VER% ^(or there is no internet connection^).
echo   Trying another Python...
exit /b 1

:try_existing_private
rem a private Python downloaded by an earlier run
if exist ".venv\Scripts\python.exe" exit /b 0
set "PPY="
for /d %%d in (".python\cpython-%PRIVATE_PY%.*") do if exist "%%d\python.exe" set "PPY=%%d\python.exe"
if not defined PPY exit /b 1
set "FORCE=1"
call :try_python "%PPY%"
set "FORCE="
exit /b 0

:private_python
echo.
echo No installed Python can be used, so setup downloads a private Python %PRIVATE_PY% into the .python folder
echo here (about 100 MB; no admin rights needed, nothing outside this folder changes).
set "UVARCH=x86_64"
set "UVSHA=7c38608c8a18ee137d748a1773053b07ec8f3a30fab49aebaa6f4e4efeceb019"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "UVARCH=aarch64"
if /i "%PROCESSOR_ARCHITEW6432%"=="ARM64" set "UVARCH=aarch64"
if "%UVARCH%"=="aarch64" set "UVSHA=4b783bda5cc44bbae0651a837223873a7acee31152381aef5fc86ef6bef25997"
if exist ".python\uv\uv.exe" goto have_uv
echo Downloading uv %UV_VERSION% (the Python installer from astral.sh)...
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; $z=Join-Path $env:TEMP 'kit-uv.zip'; Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/download/%UV_VERSION%/uv-%UVARCH%-pc-windows-msvc.zip' -OutFile $z -UseBasicParsing; if ((Get-FileHash $z -Algorithm SHA256).Hash -ne '%UVSHA%') { Remove-Item $z; throw 'the download is damaged (wrong checksum)' }; if (Test-Path '.python\uv') { Remove-Item '.python\uv' -Recurse -Force }; Expand-Archive $z '.python\uv'; Remove-Item $z" || (
    echo   Could not download uv.
    exit /b 1
)
:have_uv
echo Downloading Python %PRIVATE_PY%...
set "UV_PYTHON_INSTALL_DIR=%CD%\.python"
set "UV_CACHE_DIR=%CD%\.python\cache"
set "UV_NO_CONFIG=1"
".python\uv\uv.exe" python install %PRIVATE_PY% --no-bin --no-registry || (
    echo   Could not download Python %PRIVATE_PY%.
    exit /b 1
)
if exist ".python\cache" rmdir /s /q ".python\cache"
call :try_existing_private
exit /b 0
