#!/bin/bash
# One-time setup on macOS (double-click it in Finder): creates a private Python environment (.venv) and gets
# ffmpeg (Homebrew's if installed, otherwise a static build downloaded into ./ffmpeg/bin).
# Safe to run again (repairs or updates the environment).
# MUSICKIT_NO_PAUSE=1 (or CI=true) skips the final "Press Enter" prompt; MUSICKIT_FFMPEG_DOWNLOAD=1 downloads
# ffmpeg even when one is installed already.

cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
PYURL="https://www.python.org/downloads/macos/"

finish() {
    echo
    if [ -z "${MUSICKIT_NO_PAUSE:-}" ] && [ -z "${CI:-}" ] && [ -t 0 ]; then
        read -r -p "Press Enter to close this window..." _
    fi
    exit "$1"
}

fail() {
    echo
    echo "ERROR: $*"
    finish 1
}

py_ok() {  # python >= 3.11?
    "$1" -c "import sys; sys.exit(sys.version_info[:2] < (3, 11))" >/dev/null 2>&1
}

find_python() {
    local cand path
    for cand in python3 python3.14 python3.13 python3.12 python3.11 \
        /opt/homebrew/bin/python3 /usr/local/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.11/bin/python3; do
        path="$(command -v "$cand" 2>/dev/null)" || continue
        # Apple's /usr/bin/python3 is only a stub that pops up an installer when the Command Line Tools are missing
        if [ "$path" = "/usr/bin/python3" ] && [ "$(uname -s)" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then
            continue
        fi
        if py_ok "$path"; then
            echo "$path"
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------- Python environment
if [ -e ".venv/bin/python" ] && ! py_ok ".venv/bin/python"; then
    echo "The existing .venv does not work with Python 3.11+ - creating it again..."
    rm -rf ".venv"
fi
if [ ! -e ".venv/bin/python" ]; then
    PY="$(find_python)" || {
        echo "Python 3.11 or newer is required."
        command -v python3 >/dev/null 2>&1 && [ "$(command -v python3)" != "/usr/bin/python3" ] && python3 --version
        echo "Install it from $PYURL (or: brew install python), then run setup.command again."
        finish 1
    }
    echo "Creating Python environment with $PY ($("$PY" --version 2>&1))..."
    "$PY" -m venv ".venv" || fail "Could not create the Python environment in \"$HERE/.venv\"."
fi
echo "Installing Python packages..."
.venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1
.venv/bin/python -m pip install --prefer-binary -r requirements.txt ||
    fail "Installing the Python packages failed - see the messages above.
Check the internet connection, then run setup.command again."

# ---------------------------------------------------------------- ffmpeg
runs() {  # the tool starts and prints its version
    "$1" -version >/dev/null 2>&1
}

have_ffmpeg() {
    local d
    if runs "ffmpeg/bin/ffmpeg" && runs "ffmpeg/bin/ffprobe"; then
        return 0
    fi
    if [ -n "${MUSICKIT_FFMPEG_DOWNLOAD:-}" ]; then  # CI: always test the download
        return 1
    fi
    if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
        return 0
    fi
    for d in /opt/homebrew/bin /usr/local/bin; do  # Homebrew, also when it is not on PATH
        if runs "$d/ffmpeg" && runs "$d/ffprobe"; then
            return 0
        fi
    done
    return 1
}

# fetch <tool> <url> <tmpdir>: download a zip holding one macOS binary into ffmpeg/bin
fetch() {
    local tool="$1" url="$2" tmp="$3" bin
    rm -rf "${tmp:?}/$tool" && mkdir -p "$tmp/$tool" || return 1
    echo "  $tool: $url"
    curl -fL --retry 3 --connect-timeout 20 -sS -o "$tmp/$tool.zip" "$url" || return 1
    unzip -o -q "$tmp/$tool.zip" -d "$tmp/$tool" || return 1
    bin="$(find "$tmp/$tool" -type f -name "$tool" ! -path "*/__MACOSX/*" | head -n 1)"
    [ -n "$bin" ] || { echo "  no $tool in the download"; return 1; }
    file -b "$bin" | grep -q "Mach-O" || { echo "  the download is not a macOS program"; return 1; }
    mkdir -p "ffmpeg/bin" && cp -f "$bin" "ffmpeg/bin/$tool" || return 1
    chmod +x "ffmpeg/bin/$tool"
    xattr -d com.apple.quarantine "ffmpeg/bin/$tool" >/dev/null 2>&1
    if ! runs "ffmpeg/bin/$tool"; then
        codesign --force --sign - "ffmpeg/bin/$tool" >/dev/null 2>&1  # Apple Silicon needs at least an ad-hoc signature
    fi
    runs "ffmpeg/bin/$tool" || { echo "  ffmpeg/bin/$tool does not start"; rm -f "ffmpeg/bin/$tool"; return 1; }
}

download_ffmpeg() {
    local arch tmp ok=1 tool
    [ "$(uname -s)" = "Darwin" ] || return 1
    arch="amd64"
    if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
        arch="arm64"
    fi
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/musickit-ffmpeg.XXXXXX")" || return 1
    for tool in ffmpeg ffprobe; do
        # static builds listed on ffmpeg.org: Martin Riedl (native arm64 + x86_64), evermeet.cx (x86_64; runs on
        # Apple Silicon through Rosetta 2)
        fetch "$tool" "https://ffmpeg.martin-riedl.de/redirect/latest/macos/$arch/release/$tool.zip" "$tmp" ||
            fetch "$tool" "https://evermeet.cx/ffmpeg/getrelease/$tool/zip" "$tmp" || ok=0
    done
    rm -rf "$tmp"
    [ "$ok" = 1 ]
}

if have_ffmpeg; then
    echo "ffmpeg found."
else
    echo "Downloading ffmpeg (needed to read MP3/FLAC/OGG/... files)..."
    if ! download_ffmpeg; then
        echo
        echo "Could not get ffmpeg automatically. Install it with Homebrew (https://brew.sh): brew install ffmpeg"
        echo "or put the ffmpeg and ffprobe programs into \"$HERE/ffmpeg/bin\", then run setup.command again."
        finish 1
    fi
fi

echo
echo "Setup complete. Start MusicKit with MusicKit.command"
finish 0
