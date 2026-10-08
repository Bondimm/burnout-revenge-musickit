#!/bin/bash
# Starts the MusicKit window on Linux. Run ./setup.sh once first.
# Keep the terminal open while MusicKit runs; it shows the program's messages.
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
    echo "MusicKit is not set up yet: run ./setup.sh first, then ./MusicKit.sh again."
    exit 1
fi
exec ".venv/bin/python" -m musickit gui
