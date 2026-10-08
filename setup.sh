#!/bin/bash
# One-time setup on Linux (run ./setup.sh in a terminal): creates a private Python environment (.venv) and gets
# ffmpeg (the system one if installed, otherwise a static build downloaded into ./ffmpeg/bin).
# Safe to run again (repairs or updates the environment).
# MUSICKIT_NO_PAUSE=1 (or CI=true) skips the final "Press Enter" prompt; MUSICKIT_FFMPEG_DOWNLOAD=1 downloads
# ffmpeg even when one is installed already.

cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"

finish() {
    echo
    if [ -z "${MUSICKIT_NO_PAUSE:-}" ] && [ -z "${CI:-}" ] && [ -t 0 ] && [ -t 1 ]; then
        read -r -p "Press Enter to close..." _
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
    for cand in python3 python3.14 python3.13 python3.12 python3.11 python; do
        path="$(command -v "$cand" 2>/dev/null)" || continue
        if py_ok "$path"; then
            echo "$path"
            return 0
        fi
    done
    return 1
}

install_hint() {
    echo "  Debian / Ubuntu / Mint:  sudo apt install python3 python3-venv python3-pip"
    echo "  Fedora / RHEL:           sudo dnf install python3 python3-pip"
    echo "  Arch / Manjaro:          sudo pacman -S python python-pip"
    echo "  openSUSE:                sudo zypper install python3 python3-pip"
}

# ---------------------------------------------------------------- Python environment
if [ -e ".venv/bin/python" ] && ! py_ok ".venv/bin/python"; then
    echo "The existing .venv does not work with Python 3.11+ - creating it again..."
    rm -rf ".venv"
fi
if [ ! -e ".venv/bin/python" ]; then
    PY="$(find_python)" || {
        echo "Python 3.11 or newer is required."
        command -v python3 >/dev/null 2>&1 && python3 --version
        echo "Install it with your package manager, then run ./setup.sh again:"
        install_hint
        echo "(Older distributions: use the deadsnakes PPA on Ubuntu, or https://www.python.org/downloads/)"
        finish 1
    }
    echo "Creating Python environment with $PY ($("$PY" --version 2>&1))..."
    rm -rf ".venv"
    if ! "$PY" -m venv ".venv" || [ ! -e ".venv/bin/python" ]; then
        rm -rf ".venv"
        echo
        echo "Could not create the Python environment in \"$HERE/.venv\"."
        echo "On Debian / Ubuntu / Mint the venv module is a separate package:"
        echo "  sudo apt install python3-venv     (for another version: python3.12-venv, ...)"
        echo "Then run ./setup.sh again."
        finish 1
    fi
fi
echo "Installing Python packages..."
.venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1
.venv/bin/python -m pip install --prefer-binary -r requirements.txt ||
    fail "Installing the Python packages failed - see the messages above.
Check the internet connection, then run ./setup.sh again.
(Only 64-bit Linux on x86_64 or aarch64 with glibc 2.28+ is supported; Alpine / musl is not.)"

# ---------------------------------------------------------------- ffmpeg
runs() {  # the tool starts and prints its version
    "$1" -version >/dev/null 2>&1
}

have_ffmpeg() {
    if runs "ffmpeg/bin/ffmpeg" && runs "ffmpeg/bin/ffprobe"; then
        return 0
    fi
    if [ -n "${MUSICKIT_FFMPEG_DOWNLOAD:-}" ]; then  # CI: always test the download
        return 1
    fi
    command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 && runs ffmpeg && runs ffprobe
}

# fetch <url> <tmpdir>: download a .tar.xz holding static ffmpeg + ffprobe into ffmpeg/bin
fetch() {
    local url="$1" tmp="$2" tool bin
    rm -rf "${tmp:?}/x" && mkdir -p "$tmp/x" || return 1
    echo "  $url"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --connect-timeout 20 -sS -o "$tmp/ff.tar.xz" "$url" || return 1
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$tmp/ff.tar.xz" "$url" || return 1
    else
        echo "  neither curl nor wget is installed"
        return 1
    fi
    tar -xJf "$tmp/ff.tar.xz" -C "$tmp/x" || { echo "  could not unpack it (is xz / xz-utils installed?)"; return 1; }
    for tool in ffmpeg ffprobe; do
        bin="$(find "$tmp/x" -type f -name "$tool" | head -n 1)"
        [ -n "$bin" ] || { echo "  no $tool in the download"; return 1; }
        head -c 4 "$bin" | grep -q "ELF" || { echo "  the download is not a Linux program"; return 1; }
        mkdir -p "ffmpeg/bin" && cp -f "$bin" "ffmpeg/bin/$tool" || return 1
        chmod +x "ffmpeg/bin/$tool"
        runs "ffmpeg/bin/$tool" || { echo "  ffmpeg/bin/$tool does not start"; rm -f "ffmpeg/bin/$tool"; return 1; }
    done
}

download_ffmpeg() {
    local tmp ok=1 a b
    case "$(uname -m)" in
        x86_64 | amd64) a="amd64"; b="linux64" ;;
        aarch64 | arm64) a="arm64"; b="linuxarm64" ;;
        *) echo "  no ffmpeg download for $(uname -m)"; return 1 ;;
    esac
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/musickit-ffmpeg.XXXXXX")" || return 1
    # static builds listed on ffmpeg.org: John Van Sickle, then BtbN's FFmpeg-Builds
    fetch "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-$a-static.tar.xz" "$tmp" ||
        fetch "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-$b-gpl.tar.xz" "$tmp" || ok=0
    rm -rf "$tmp"
    [ "$ok" = 1 ]
}

if have_ffmpeg; then
    echo "ffmpeg found."
else
    echo "Downloading ffmpeg (needed to read MP3/FLAC/OGG/... files)..."
    if ! download_ffmpeg; then
        echo
        echo "Could not get ffmpeg automatically. Install it with your package manager, then run ./setup.sh again:"
        echo "  Debian / Ubuntu: sudo apt install ffmpeg     Fedora: sudo dnf install ffmpeg-free (or from RPM Fusion)"
        echo "  Arch: sudo pacman -S ffmpeg                  openSUSE: sudo zypper install ffmpeg"
        echo "or put the ffmpeg and ffprobe programs into \"$HERE/ffmpeg/bin\"."
        finish 1
    fi
fi

echo
echo "Setup complete. Start MusicKit with ./MusicKit.sh"
finish 0
