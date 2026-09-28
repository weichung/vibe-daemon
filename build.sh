#!/usr/bin/env bash
# Package vibe-daemon as a standalone macOS .app via PyInstaller.
set -euo pipefail

cd "$(dirname "$0")"

# Place a macOS .icns file at assets/icon.icns before shipping a branded build.
ICON_ARGS=()
if [[ -f assets/icon.icns ]]; then
  ICON_ARGS=(--icon assets/icon.icns)
else
  echo "Note: add assets/icon.icns to set the app icon (optional for now)."
fi

pyinstaller \
  --noconfirm \
  --windowed \
  --clean \
  --name "VibeDaemon" \
  "${ICON_ARGS[@]}" \
  --hidden-import google.genai \
  --hidden-import rumps \
  --hidden-import pynput \
  --hidden-import sounddevice \
  --hidden-import tkinter \
  run_mac.py
