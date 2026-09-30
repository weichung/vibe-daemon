# vibe-daemon

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-macOS-black?logo=apple)
![Arch](https://img.shields.io/badge/arch-Apple%20Silicon-lightgrey)
![Model](https://img.shields.io/badge/Gemini-3.8%20Flash-4285F4?logo=google&logoColor=white)
![Status](https://img.shields.io/badge/status-feature--complete-success)

A secure, OS-level **voice coding assistant** and **intent-routing agent** for macOS. Speak a command, drop a Markdown task, or POST to a local webhook; the daemon classifies it with **Gemini 3.8 Flash** and routes it to the IDE, the shell, or a short conversation — without hardcoded secrets, and without taking your eyes off the work.

## Overview

**vibe-daemon** lives in the macOS menu bar. Press **Cmd+Shift+Space**, talk, and press the hotkey again. Audio is captured at 16 kHz mono, checkpointed in git, then classified by Gemini function calling:

| Intent | Destination |
| --- | --- |
| Write or edit code | [Google Antigravity](https://antigravity.google) IDE SDK |
| OS / filesystem / apps | Local shell (`bash` / `zsh`) |
| General questions | Spoken conversational reply (same language as the user) |

The engine never stores API keys in source. Keys live in `~/.vibe_daemon_env` and are edited through an isolated tkinter Preferences process that can steal focus and accept native Cmd+V.

## Core features

### Intelligent routing

`core/router.py` sends the WAV clip to **Gemini 3.8 Flash** with three tools. Automatic function calling is disabled; the daemon parses `function_calls` and runs the matching Python method:

- **IDE** — coding / file edits → Antigravity agent (`<tts>` summary + `<transcript>`)
- **Shell** — OS tasks → `subprocess` with stdout/stderr spoken and shown in the menu
- **Conversation** — Q&A → bilingual TTS, matching the spoken language

### Local Webhook API

At startup the engine spawns a background **Flask** server bound to **`127.0.0.1:50051`** (`core/api_server.py`). External Python scripts, web scrapers, and agentic loops can **bypass the microphone** and inject text commands straight into the same Gemini intent router used for speech (`dispatch_text`).

The payload is classified as IDE / shell / conversation exactly as if you had spoken it — no hotkey, no WAV, no TTS round-trip required.

- **URL:** `POST http://127.0.0.1:50051/execute` (localhost only)
- **Body:** `{"text": "<command>"}`
- **Success:** `{"status": "success", "result": "<router summary>"}`

The daemon must already be running (`python run_mac.py`). A missing API key returns the usual Preferences reminder in `result` instead of crashing. Sample webhook clients live in `tests/` (`test_01_single_shot.py`, `test_02_pipeline.py`, `test_03_batch_agent.py`).

**Python (`requests`):**

```python
import requests

payload = {"text": "Summarize the latest logs in my terminal."}
response = requests.post("http://127.0.0.1:50051/execute", json=payload)
print(response.json())
```

**cURL:**

```bash
curl -X POST http://127.0.0.1:50051/execute \
     -H "Content-Type: application/json" \
     -d '{"text": "Summarize the latest logs in my terminal."}'
```

### Markdown TODO watcher

An asynchronous daemon thread (`core/todo_watcher.py`) polls **`~/Documents/VibeTasks.md`** every 3 seconds. Unfinished Markdown tasks (`- [ ] …`) are picked up, sent through Gemini via `dispatch_text`, and marked **`- [x]`** when they finish.

Race-condition protection is strict: after a long-running task completes, the watcher **re-reads the file from disk**, finds the **exact** unfinished line, replaces **only that line**, then writes back and refreshes `mtime`. User edits (added, deleted, or rewritten lines) made while the AI was working are never overwritten by a stale in-memory buffer. The watcher also updates `mtime` immediately after its own save so it cannot loop on itself.

Open the list from the menu bar: **📝 Open TODO List**. If the file does not exist, a starter template is created first.

### Robust VAD (voice activity detection)

Before any Gemini or Antigravity call, `core/engine.py` runs **local RMS energy thresholding** on the captured buffer (`SILENCE_THRESHOLD = 0.005`). Near-silent clips are dropped with a spoken *“I didn't hear anything.”*

That filter cuts background-noise hallucinations (empty-room “transcripts”) and **avoids wasting API calls** on non-speech. The threshold is tuned so normal conversational speech is not clipped.

### Secure GUI dashboard

Zero hardcoded keys. On first launch (or **Preferences…**), an isolated `tkinter` process opens **Vibe Daemon Preferences**:

- Masked Gemini API Key field
- Max recording duration
- Enable TTS checkbox
- Writes `~/.vibe_daemon_env` via `python-dotenv` (`chmod 600`)
- Forced to the foreground (System Events + `-topmost`)
- Explicit Cmd+V / Cmd+C / Cmd+A bindings so paste works outside an `.app` bundle

### Graceful fallback

If `GEMINI_API_KEY` is missing, the daemon does **not** crash:

- Gemini client stays `None`; `dispatch_audio` returns a Preferences reminder
- Hotkey intercepts: TTS *“Please set your API key.”* and auto-launches the dashboard
- Canceling setup only logs a warning; recording stays idle until a key is saved

### Bilingual TTS

Menu-bar TTS runs on a background thread (`subprocess.run`, not the rumps main loop). CJK (`\u4e00–\u9fff`) uses macOS **Meijia**; otherwise the system English voice. Failures log `stderr` instead of failing silently.

### Live menu bar

`rumps` owns the main thread:

| Title | Meaning |
| --- | --- |
| 🟢 | Idle |
| 🔴 ▂▃▅▆▇ | Recording + live VU meter |
| 🟡 | Processing |

The dropdown shows status, last transcript, and last action. **Preferences…**, **📝 Open TODO List**, and **Quit** are first-class items.

### Safety net

Before every routed action, the engine runs:

```bash
git add .
git commit -m "vibe-checkpoint: pre-agent action"
```

Empty commits fail quietly (`capture_output=True`). Roll back with normal git history.

## Recent fixes

- **Preferences Cmd+V double-paste** — macOS Tkinter was bubbling the native paste *and* the bound `<<Paste>>` handler, inserting the clipboard twice. Paste is now a single insert (`clipboard_get`) that returns `"break"` so the event does not propagate.
- **Recording-timeout deadlock** — the state machine used to end recording on the `threading.Timer` thread, which could lock against rumps / hotkey callbacks. Timeouts now hop to a dedicated `daemon=True` worker that calls `_end_recording`.
- **Silence threshold** — RMS VAD was too aggressive (`0.02`) and clipped quiet but real speech. The floor is now `0.005`, which still rejects room noise without cutting off a normal speaking voice.

## Architecture

```
vibe-daemon/
├── run_mac.py                      # Entry: engine thread + rumps main loop
├── daemon.py                       # Alias → run_mac.py
├── core/
│   ├── engine.py                   # State machine, hotkey, VAD, git, callbacks
│   ├── router.py                   # Gemini 3.8 Flash function calling
│   ├── api_server.py               # Flask webhook on 127.0.0.1:50051
│   ├── todo_watcher.py             # ~/Documents/VibeTasks.md poller
│   └── parser.py                   # <tts> / <transcript> extraction
├── tests/                          # Webhook / pipeline sample clients
└── platforms/
    └── macos/
        ├── menu_app.py             # Menu bar, VU meter, bilingual say()
        └── prefs_ui.py             # Isolated tkinter Preferences process
```

| Module | Responsibility |
| --- | --- |
| `run_mac.py` | `threading.Thread(target=engine.run, daemon=True)` then `rumps.App.run()` on the main thread. |
| `core/engine.py` | `IDLE → RECORDING → PROCESSING`, Cmd+Shift+Space toggle, RMS VAD, git checkpoint, webhook + TODO watcher threads. |
| `core/router.py` | `genai.Client` (safe init), `dispatch_audio` / `dispatch_text`, tools for IDE / shell / chat. |
| `core/api_server.py` | Local Flask `POST /execute` for microphone-free injection. |
| `core/todo_watcher.py` | Poll `VibeTasks.md`, dispatch unfinished tasks, mark done with a disk re-read. |
| `platforms/macos/menu_app.py` | Callbacks only: TTS, menu titles, Open TODO List, launches `prefs_ui.py` as a child process. |
| `platforms/macos/prefs_ui.py` | Standalone tkinter UI in its **own process** so rumps / LSUIElement cannot steal or bury the window. |

The engine never calls `say` or Tk. The Preferences GUI never imports the recorder. That split is the contract for a future Windows/Linux tray.

## Installation

### System

- macOS (Apple Silicon recommended)
- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or Anaconda
- [Antigravity IDE](https://antigravity.google) installed and **open** for coding routes
- Working directory should be a git repo (checkpoints are commits)

### macOS permissions

Grant these to the Terminal / conda Python that launches the daemon:

| Permission | Why |
| --- | --- |
| **Accessibility** | Global hotkey (`pynput`) |
| **Microphone** | Capture (`sounddevice`) |

System Settings → Privacy & Security → Accessibility / Microphone.

### Conda environment

```bash
git clone https://github.com/<you>/vibe-daemon.git
cd vibe-daemon

conda create -n vibe-daemon python=3.11 -y
conda activate vibe-daemon

pip install \
  rumps \
  google-genai \
  python-dotenv \
  pynput \
  sounddevice \
  numpy \
  google-antigravity \
  flask
```

`tkinter` ships with the conda Python.org / official macOS builds. If `import tkinter` fails: `conda install tk`.

| Package | Role |
| --- | --- |
| `rumps` | Menu bar (main thread) |
| `google-genai` | Gemini 3.8 Flash router |
| `python-dotenv` | `~/.vibe_daemon_env` |
| `pynput` | Global hotkey |
| `sounddevice` | Microphone stream |
| `numpy` | Audio buffers |
| `google-antigravity` | IDE agent SDK |
| `flask` | Local webhook API (`POST /execute`) |

## Usage

```bash
conda activate vibe-daemon
cd /path/to/your/project    # git repo the agent should edit
python /path/to/vibe-daemon/run_mac.py
```

On first run, Preferences opens if no key is stored. Paste a [Gemini API key](https://aistudio.google.com/apikey), save, then:

| Action | Result |
| --- | --- |
| **Cmd+Shift+Space** | Start recording (🔴 VU meter) |
| **Cmd+Shift+Space** again | Stop, checkpoint, route audio |
| No key | TTS warning + Preferences window; no crash |
| **Preferences…** | Edit key, max record, TTS enable; client reloads in place |
| **📝 Open TODO List** | Open `~/Documents/VibeTasks.md` (create template if missing) |
| Near-silent clip | Local VAD drops the buffer; no Gemini call |
| 120 s timeout | Auto-stop fail-safe on a worker thread (override in Preferences) |
| **Quit** | Engine shutdown |

Hotkey debounce is 0.5 s. Conversational replies follow the user’s language (English or Traditional Chinese).

Text-only automation (no microphone) is documented under [Local Webhook API](#local-webhook-api).

Secrets stay in `~/.vibe_daemon_env` — never commit that file.

## Roadmap

### Phase 2: Autonomous multi-turn agent (ReAct loop)

Upgrade `core/router.py` from **single-shot** tool calls to a **stateful, multi-turn reasoning loop**. The agent should observe results, plan the next step, and continue until the goal is met — including tasks with sequential dependencies (search → read → edit → verify) that one Gemini turn cannot finish safely.

### Expanded agentic pipelines

Build dedicated external Python scripts that talk to the **Local Webhook API** (`127.0.0.1:50051`) to automate daily workflows without the microphone: download harvesters, log summarizers, and code reviewers that POST text commands into the same Gemini router the hotkey uses.

### Later

- **PyInstaller `.app` + GitHub Actions** — signed menu-bar bundle, CI install/test/release, no Terminal required.
- **Vision & screen capture** — multimodal context (screenshot + voice) for UI bugs and design diffs.

## License

MIT — see [`LICENSE`](LICENSE) once published. Until then, all rights reserved.
