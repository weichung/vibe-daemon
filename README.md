# vibe-daemon

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-macOS-black?logo=apple)
![Arch](https://img.shields.io/badge/arch-Apple%20Silicon-lightgrey)
![License](https://img.shields.io/badge/license-private-inactive)

An OS-level voice coding assistant for macOS. Hold a global hotkey, speak an instruction, and let an agent act on your workspace — with a live menu-bar status, git safety nets, and spoken summaries so you never have to look at the screen.

## Overview

**vibe-daemon** is a lightweight background agent for “vibe coding.” It sits in the macOS menu bar, listens for **Cmd+Shift+Space**, records from the system microphone, and hands the audio to the [Google Antigravity](https://antigravity.google) IDE SDK for code changes.

The loop is deliberately simple:

1. Toggle recording with a global hotkey.
2. Checkpoint the git working tree.
3. Send 16 kHz mono audio to the Antigravity agent.
4. Speak a short summary back while the full transcript stays in the menu.

Core logic is platform-agnostic. macOS-only pieces (menu bar, `say`, Accessibility) live behind a thin adapter so Windows and Linux can follow later.

## Features

- **Menu bar UI** (`rumps`) — 🟢 Idle, 🔴 Recording with a real-time VU meter, 🟡 Processing. The dropdown shows the latest transcript and agent action.
- **Hands-free capture** — Global hotkey via `pynput`; async microphone I/O via `sounddevice`.
- **Safety first** — Before every agent call, the daemon runs `git add .` and `git commit -m "vibe-checkpoint: pre-agent action"` so you can roll back.
- **Dual-track feedback** — Timestamped INFO logs in the terminal; concise TTS via macOS `say` for eyes-free coding.
- **Modular architecture** — `core/` owns state, audio, parsing, and routing; `platforms/` owns UI and OS integrations.

## Architecture

```
vibe-daemon/
├── run_mac.py                 # macOS entry point
├── daemon.py                  # thin alias for run_mac.py
├── core/
│   ├── engine.py              # state machine, hotkey, capture, agent handoff
│   ├── parser.py              # <tts> / <transcript> extraction
│   └── router.py              # intent classification (in progress)
└── platforms/
    └── macos/
        └── menu_app.py        # rumps menu bar + say() TTS
```

| Path | Role |
| --- | --- |
| `run_mac.py` | Starts `VibeDaemon` on a background thread, then blocks the main thread on the `rumps` app. |
| `core/engine.py` | Thread-safe `IDLE → RECORDING → PROCESSING` machine, push-to-toggle hotkey, `AudioRecorder`, git checkpoint, Antigravity send. |
| `core/parser.py` | Pulls a speakable summary from `<tts>` tags and recognized speech from `<transcript>` tags. |
| `platforms/macos/menu_app.py` | Menu-bar title, VU meter, dropdown dashboard, and macOS TTS. Talks to the engine only through callbacks. |

The engine never calls `say` or touches AppKit. Platform code never owns the recorder. That split is the contract for future `platforms/windows` and `platforms/linux` adapters.

## Prerequisites

### System

- macOS on Apple Silicon
- Python **3.10+**
- [Antigravity IDE](https://antigravity.google) installed and open (the SDK talks to a local harness)
- A git repository as the working directory (checkpoints are commits)

### macOS permissions

Grant these to the **Terminal** (or the Python binary) that launches the daemon:

| Permission | Why |
| --- | --- |
| **Accessibility** | Global `Cmd+Shift+Space` listener (`pynput`) |
| **Microphone** | `sounddevice` capture |

System Settings → Privacy & Security → Accessibility / Microphone.

### Python dependencies

```bash
pip install rumps sounddevice pynput numpy google-antigravity
```

| Package | Used for |
| --- | --- |
| `rumps` | Menu bar app (main thread) |
| `sounddevice` | Async microphone stream |
| `pynput` | Global hotkey |
| `numpy` | Audio buffers |
| `google-antigravity` | Agent SDK (`google.antigravity`) |

## Installation

```bash
git clone <repo-url> vibe-daemon
cd vibe-daemon
python3 -m venv .venv
source .venv/bin/activate
pip install rumps sounddevice pynput numpy google-antigravity
```

Run from the git repo you want the agent to edit — checkpoints and the Antigravity workspace both use the current working directory.

## Usage

```bash
python run_mac.py
```

(`python daemon.py` is equivalent.)

A 🟢 indicator appears in the menu bar.

| Action | What happens |
| --- | --- |
| Press **Cmd+Shift+Space** | Idle → Recording. Title becomes a live 🔴 VU meter. `say Recording`. |
| Press **Cmd+Shift+Space** again | Recording → Processing. Stream stops, git checkpoint runs, audio is sent to Antigravity. `say Processing`. |
| Agent finishes | Menu shows transcript + last action. TTS reads the `<tts>` summary. State returns to 🟢 Idle. |
| 120 s without a stop | Fail-safe stops recording and processes whatever was captured. |
| **Quit** in the menu | Engine shutdown, then the app exits. |

Hotkey presses within 0.5 s of a state change are ignored so key-repeat cannot double-toggle.

Keep the Antigravity IDE open. If the local harness is down, the daemon logs a connection error and returns to Idle without crashing.

## Configuration (defaults)

| Constant | Value | Meaning |
| --- | --- | --- |
| Hotkey | `Cmd+Shift+Space` | Exact-match rising edge |
| Sample rate | 16 kHz mono float32 | Speech capture |
| Max record | 120 s | Auto-stop fail-safe |
| Debounce | 0.5 s | Ignore rapid retriggers |
| Checkpoint message | `vibe-checkpoint: pre-agent action` | Git commit before the agent |

## Roadmap

**Intent routing (coming soon).** A Gemini Flash function-calling layer will sit in front of the IDE: spoken audio is classified, then dispatched to Antigravity (code), a local shell (OS tasks), or a short conversational reply. Scaffolding lives in `core/router.py`.

**Windows and Linux.** The engine is already callback-driven. Next up: tray/status adapters under `platforms/windows` and `platforms/linux`, plus a TTS backend that is not `say`.

**Preferences.** The menu item is a placeholder for hotkey, mic device, and max-record settings.

## License

Private / unpublished. All rights reserved unless a license file is added to this repository.
