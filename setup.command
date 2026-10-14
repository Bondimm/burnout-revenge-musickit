#!/bin/bash
# One-time setup on macOS (double-click it in Finder): creates a private Python environment (.venv) and gets
# ffmpeg (Homebrew's if installed, otherwise a static build downloaded into ./ffmpeg/bin).
# Safe to run again (repairs or updates the environment).
# MUSICKIT_NO_PAUSE=1 (or CI=true) skips the final "Press Enter" prompt; MUSICKIT_FFMPEG_DOWNLOAD=1 downloads
# ffmpeg even when one is installed already (MUSICKIT_FFMPEG_DOWNLOAD=backup uses only the backup download).
# MUSICKIT_PRIVATE_PYTHON=1 skips the installed Pythons and uses a private one (downloaded into ./.python).

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

# ---------------------------------------------------------------- Python environment
# Any installed 64-bit Python 3.12 or newer works: setup uses one for which every package is available ready-made
# (nothing is compiled). When there is none, or no Python at all, it downloads a private Python into ./.python with
# uv (https://astral.sh/uv): no admin rights needed, nothing outside this folder changes.
PRIVATE_PY="3.13"     # a version with ready-made packages on every supported system
UV_VERSION="0.12.24"
TRIED=" "
FORCE=""

py_version() {  # prints e.g. "3.13" when "$1" is a usable 64-bit Python 3.11+
    "$1" -c "import sys; ok = sys.version_info >= (3, 11) and sys.maxsize > 2**32; print(*sys.version_info[:2], sep='.') if ok else sys.exit(1)" 2>/dev/null
}

# install_packages: ready-made packages only (never compiles). A quiet check first whether every package has a
# download for this Python, so an unsupported Python is skipped fast and without a wall of errors.
install_packages() {
    .venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1
    if ! .venv/bin/python -m pip install --dry-run --quiet --only-binary=:all: -r requirements.txt >/dev/null 2>&1; then
        [ -n "$FORCE" ] || return 2
    fi
    .venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
}

try_python() {  # try_python <python>: builds .venv with it; fails when that Python cannot be used
    local ver
    [ -e ".venv/bin/python" ] && return 0
    ver="$(py_version "$1")" && [ -n "$ver" ] || return 1
    if [ -z "$FORCE" ]; then
        case "$TRIED" in *" $ver "*) return 1 ;; esac
    fi
    echo
    echo "Using Python $ver ($1)..."
    rm -rf ".venv"
    if ! "$1" -m venv ".venv" >/dev/null 2>&1 || [ ! -e ".venv/bin/python" ]; then
        rm -rf ".venv"
        echo "  Could not create a Python environment with it - trying another Python..."
        return 1
    fi
    echo "Installing Python packages..."
    install_packages && return 0
    TRIED="$TRIED$ver "
    rm -rf ".venv"
    echo "  Not all packages are available ready-made for Python $ver (or there is no internet connection)."
    echo "  Trying another Python..."
    return 1
}

try_existing_private() {  # a private Python downloaded by an earlier run
    local py rc
    [ -e ".venv/bin/python" ] && return 0
    for py in .python/cpython-"$PRIVATE_PY".*/bin/python3; do
        [ -x "$py" ] || continue
        FORCE=1
        try_python "$py"
        rc=$?
        FORCE=""
        return $rc
    done
    return 1
}

download() {  # download <url> <file>
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --connect-timeout 20 -sS -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        echo "  neither curl nor wget is installed"
        return 1
    fi
}

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d ' ' -f 1
}

private_python() {
    local os arch target sum tmp uv
    echo
    echo "No installed Python can be used, so setup downloads a private Python $PRIVATE_PY into the .python folder"
    echo "here (about 100 MB; no admin rights needed, nothing outside this folder changes)."
    case "$(uname -s)" in
        Darwin) os="apple-darwin" ;;
        Linux) os="unknown-linux-gnu" ;;
        *) echo "  There is no private Python for $(uname -s)."; return 1 ;;
    esac
    case "$(uname -m)" in
        x86_64 | amd64) arch="x86_64" ;;
        aarch64 | arm64) arch="aarch64" ;;
        *) echo "  There is no private Python for $(uname -m)."; return 1 ;;
    esac
    if [ "$os" = "apple-darwin" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
        arch="aarch64"  # Apple Silicon, also when this Terminal runs under Rosetta
    fi
    target="$arch-$os"
    case "$target" in
        x86_64-unknown-linux-gnu) sum="b4dfaef47d491a7296981f8374a4595f55dbf84e8937c8ecd2983574d8bb3da6" ;;
        aarch64-unknown-linux-gnu) sum="5231be65f496304623895dacdbf1de8504fec90303684bdf05805aa34414dd21" ;;
        x86_64-apple-darwin) sum="4fa82e37cb94767661f532b001e470b67a186c7260e305bd84ddb78fd545c0b6" ;;
        aarch64-apple-darwin) sum="0c4346de7abdb49495b393b9ec809fe387aa43e586be20fecb972216c1e71732" ;;
        *) echo "  There is no private Python for $target."; return 1 ;;
    esac
    if [ ! -x ".python/uv/uv" ]; then
        echo "Downloading uv $UV_VERSION (the Python installer from astral.sh)..."
        tmp="$(mktemp -d "${TMPDIR:-/tmp}/kit-uv.XXXXXX")" || return 1
        if ! download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$target.tar.gz" "$tmp/uv.tar.gz"; then
            echo "  Could not download uv."
            rm -rf "$tmp"
            return 1
        fi
        if [ "$(sha256 "$tmp/uv.tar.gz")" != "$sum" ]; then
            echo "  The uv download is damaged (wrong checksum)."
            rm -rf "$tmp"
            return 1
        fi
        tar -xzf "$tmp/uv.tar.gz" -C "$tmp" && uv="$(find "$tmp" -type f -name uv | head -n 1)" && [ -n "$uv" ] &&
            mkdir -p ".python/uv" && cp -f "$uv" ".python/uv/uv" && chmod +x ".python/uv/uv" || {
            echo "  Could not unpack uv."
            rm -rf "$tmp"
            return 1
        }
        rm -rf "$tmp"
    fi
    echo "Downloading Python $PRIVATE_PY..."
    if ! UV_PYTHON_INSTALL_DIR="$HERE/.python" UV_CACHE_DIR="$HERE/.python/cache" UV_NO_CONFIG=1 \
        .python/uv/uv python install "$PRIVATE_PY" --no-bin; then
        echo "  Could not download Python $PRIVATE_PY."
        return 1
    fi
    rm -rf ".python/cache"
    try_existing_private
}

VENV_OK=""
if [ -e ".venv/bin/python" ]; then
    ver="$(py_version ".venv/bin/python")"
    if [ -n "$ver" ]; then
        echo "Updating the Python packages (Python $ver)..."
        if install_packages; then
            VENV_OK=1
        else
            TRIED=" $ver "
        fi
    fi
    if [ -z "$VENV_OK" ]; then
        echo "The existing Python environment (.venv) cannot be used - setting up a new one..."
        rm -rf ".venv-old"
        mv ".venv" ".venv-old" 2>/dev/null || rm -rf ".venv"
    fi
fi
if [ -z "$VENV_OK" ]; then
    rm -rf ".venv"
    try_existing_private
    if [ "${MUSICKIT_PRIVATE_PYTHON:-}" != "1" ]; then
        for cand in python3 python3.13 python3.12 python3.14 python3.15 python3.16 python3.17 python3.11 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.{13,12,14,15,16,17,11}/bin/python3; do
            [ -e ".venv/bin/python" ] && break
            path="$(command -v "$cand" 2>/dev/null)" || continue
            # Apple's /usr/bin/python3 is only a stub that pops up an installer when the Command Line Tools are missing
            if [ "$path" = "/usr/bin/python3" ] && [ "$(uname -s)" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then
                continue
            fi
            try_python "$path"
        done
    fi
    [ -e ".venv/bin/python" ] || private_python
    if [ ! -e ".venv/bin/python" ]; then
        rm -rf ".venv"
        [ -e ".venv-old/bin/python" ] && mv ".venv-old" ".venv"
        fail "Setting up Python failed - see the messages above.
Check the internet connection, then run setup.command again.
If it keeps failing, install Python 3.13 from $PYURL (or: brew install python@3.13)."
    fi
fi
rm -rf ".venv-old"

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

# fetch <tool> <url> <tmpdir>: download one macOS binary (in a .zip or a .gz) into ffmpeg/bin
fetch() {
    local tool="$1" url="$2" tmp="$3" bin
    rm -rf "${tmp:?}/$tool" && mkdir -p "$tmp/$tool" || return 1
    echo "  $tool: $url"
    case "$url" in
        *.gz)
            curl -fL --retry 3 --connect-timeout 20 -sS -o "$tmp/$tool.gz" "$url" || return 1
            gunzip -c "$tmp/$tool.gz" > "$tmp/$tool/$tool" || return 1
            ;;
        *)
            curl -fL --retry 3 --connect-timeout 20 -sS -o "$tmp/$tool.zip" "$url" || return 1
            unzip -o -q "$tmp/$tool.zip" -d "$tmp/$tool" || return 1
            ;;
    esac
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
    local arch arch2 tmp ok=1 tool
    [ "$(uname -s)" = "Darwin" ] || return 1
    arch="amd64" arch2="x64"
    if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
        arch="arm64" arch2="arm64"
    fi
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/musickit-ffmpeg.XXXXXX")" || return 1
    for tool in ffmpeg ffprobe; do
        # static builds (native arm64 + x86_64): Martin Riedl's latest release, backup: the ffmpeg-static
        # project's GitHub release (ffmpeg 6.1.1)
        if [ "${MUSICKIT_FFMPEG_DOWNLOAD:-}" != "backup" ]; then
            fetch "$tool" "https://ffmpeg.martin-riedl.de/redirect/latest/macos/$arch/release/$tool.zip" "$tmp" && continue
        fi
        fetch "$tool" "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/$tool-darwin-$arch2.gz" \
            "$tmp" || ok=0
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
