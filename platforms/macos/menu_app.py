"""macOS menu bar UI and TTS adapter for the vibe engine."""

from __future__ import annotations

import logging
import subprocess
import threading

import rumps

from core.engine import State, VibeDaemon

logger = logging.getLogger("vibe-daemon")

_STATE_UI = {
    State.IDLE: ("🟢 Idle", "Status: 🟢 Idle", "Idle"),
    State.RECORDING: ("🔴 Recording", "Status: 🔴 Recording", "Recording"),
    State.PROCESSING: ("🟡 Processing", "Status: 🟡 Processing", "Processing"),
}

_MENU_TEXT_LIMIT = 48


_VU_BLOCKS = (" ", "▂", "▃", "▅", "▆", "▇")
_VU_VOICE_PEAK = 0.5
_VU_WIDTH = 5


def _clip(text: str, limit: int = _MENU_TEXT_LIMIT) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _get_vu_meter(volume: float) -> str:
    """Map peak amplitude (voice typically 0.0–0.5) to a 5-character block meter."""
    normalized = max(0.0, min(1.0, float(volume) / _VU_VOICE_PEAK))
    bars: list[str] = []
    for i in range(_VU_WIDTH):
        slot = min(1.0, max(0.0, normalized * _VU_WIDTH - i))
        idx = min(len(_VU_BLOCKS) - 1, int(slot * (len(_VU_BLOCKS) - 1) + 1e-9))
        bars.append(_VU_BLOCKS[idx])
    return "".join(bars)


def _speak(text: str) -> None:
    if text:
        subprocess.Popen(["say", text])


class VibeMenuBarApp(rumps.App):
    """Menu bar wrapper: rumps owns the main thread; the engine runs elsewhere."""

    def __init__(self) -> None:
        super().__init__(
            _STATE_UI[State.IDLE][0],
            quit_button=None,
        )
        self._lock = threading.Lock()
        self._ui_state = State.IDLE
        self._last_transcript = "—"
        self._last_action = "—"

        self.status_item = rumps.MenuItem(_STATE_UI[State.IDLE][1])
        self.transcript_item = rumps.MenuItem("Transcript: —")
        self.action_item = rumps.MenuItem("Last action: —")
        self.preferences_item = rumps.MenuItem(
            "Preferences...",
            callback=self._on_preferences,
        )
        self.quit_item = rumps.MenuItem("Quit", callback=self._on_quit)

        self.menu = [
            self.status_item,
            self.transcript_item,
            self.action_item,
            None,
            self.preferences_item,
            self.quit_item,
        ]

        self.engine = VibeDaemon(
            on_state_change=self._on_state_change,
            on_tts_ready=self._on_tts_ready,
            on_transcript_ready=self._on_transcript_ready,
        )

    def _on_state_change(self, new_state: State) -> None:
        spoken = _STATE_UI.get(new_state, (None, None, new_state.value))[2]
        with self._lock:
            self._ui_state = new_state
        _speak(spoken)

    def _on_tts_ready(self, text: str) -> None:
        with self._lock:
            self._last_action = text
        _speak(text)

    def _on_transcript_ready(self, text: str) -> None:
        with self._lock:
            self._last_transcript = text

    @rumps.timer(0.15)
    def _refresh_ui(self, _sender) -> None:  # noqa: ARG002
        with self._lock:
            state = self._ui_state
            transcript = self._last_transcript
            action = self._last_action
        _title, status, _spoken = _STATE_UI.get(
            state, ("Vibe", f"Status: {state.value}", "")
        )
        if state == State.RECORDING:
            meter = _get_vu_meter(self.engine._recorder.current_volume)
            self.title = f"🔴 {meter}"
        elif state == State.PROCESSING:
            self.title = "🟡"
        else:
            self.title = "🟢"
        self.status_item.title = status
        self.transcript_item.title = f"Transcript: {_clip(transcript)}"
        self.action_item.title = f"Last action: {_clip(action)}"

    def _on_preferences(self, _sender) -> None:  # noqa: ARG002
        rumps.alert(
            title="Preferences",
            message="Preferences are not implemented yet.",
        )

    def _on_quit(self, _sender) -> None:  # noqa: ARG002
        logger.info("Quit selected from menu bar")
        self.engine.shutdown()
        rumps.quit_application()
