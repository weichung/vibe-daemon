#!/usr/bin/env python3
"""Vibe Coding daemon: global hotkey toggle audio capture for macOS."""

from __future__ import annotations

import logging
import queue
import signal
import sys
import threading
import time
from enum import Enum
from typing import Optional, Set

import numpy as np
import sounddevice as sd
from pynput import keyboard
from pynput.keyboard import Key

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "float32"
BLOCKSIZE = 1024
MAX_RECORD_SECONDS = 60
DEBOUNCE_SECONDS = 0.5

# Command + Shift + Space
HOTKEY: Set[Key] = {Key.cmd, Key.shift, Key.space}

_MODIFIER_ALIASES = {
    Key.cmd: Key.cmd,
    Key.cmd_l: Key.cmd,
    Key.cmd_r: Key.cmd,
    Key.shift: Key.shift,
    Key.shift_l: Key.shift,
    Key.shift_r: Key.shift,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vibe-daemon")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class State(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"


class StateManager:
    """Thread-safe state transitions between the keyboard and audio threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = State.IDLE
        self._last_change = 0.0  # time.monotonic(); 0 means never changed

    @property
    def current(self) -> State:
        with self._lock:
            return self._state

    @property
    def last_change(self) -> float:
        with self._lock:
            return self._last_change

    def transition(self, from_state: State, to_state: State) -> bool:
        """Atomically move from_state -> to_state. Returns False if the current state mismatches."""
        with self._lock:
            if self._state != from_state:
                return False
            logger.info("State: %s -> %s", from_state.value, to_state.value)
            self._state = to_state
            self._last_change = time.monotonic()
            return True


# ---------------------------------------------------------------------------
# Audio capture
# ---------------------------------------------------------------------------

class AudioRecorder:
    """Asynchronous microphone capture via sounddevice.InputStream."""

    def __init__(
        self,
        samplerate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        dtype: str = DTYPE,
        blocksize: int = BLOCKSIZE,
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.blocksize = blocksize
        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            logger.warning("Audio stream status: %s", status)
        # Copy: the callback buffer is reused by PortAudio on the next block.
        self._queue.put(indata.copy())

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            self._drain()
            self._stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype=self.dtype,
                blocksize=self.blocksize,
                callback=self._callback,
            )
            self._stream.start()
            logger.info(
                "Audio stream started (%d Hz, %d ch)",
                self.samplerate,
                self.channels,
            )

    def stop(self) -> np.ndarray:
        """Stop the stream and return captured audio as a 1-D float32 numpy array."""
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    logger.exception("Error while stopping audio stream")
                finally:
                    self._stream = None
                    logger.info("Audio stream stopped")
            return self._collect()

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _collect(self) -> np.ndarray:
        chunks: list[np.ndarray] = []
        while True:
            try:
                chunks.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return np.empty((0,), dtype=np.float32)
        return np.concatenate(chunks, axis=0).reshape(-1)


# ---------------------------------------------------------------------------
# Agent placeholder
# ---------------------------------------------------------------------------

def send_to_antigravity(audio_numpy_array: np.ndarray) -> None:
    """Placeholder for the google_antigravity / Agent SDK handoff."""
    samples = int(audio_numpy_array.shape[0]) if audio_numpy_array.ndim >= 1 else 0
    duration = samples / SAMPLE_RATE if SAMPLE_RATE else 0.0
    logger.info(
        "Audio payload: shape=%s length=%d duration=%.2fs dtype=%s",
        audio_numpy_array.shape,
        samples,
        duration,
        audio_numpy_array.dtype,
    )
    logger.info("[Mock] Sending to Antigravity IDE...")
    max_volume = np.max(np.abs(audio_numpy_array))
    logger.info("Max volume level: %.4f", max_volume)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class VibeDaemon:
    """Toggle daemon: press Cmd+Shift+Space to start or stop recording."""

    def __init__(self) -> None:
        self._state = StateManager()
        self._recorder = AudioRecorder()
        self._pressed: Set[object] = set()
        self._pressed_lock = threading.Lock()
        self._listener: Optional[keyboard.Listener] = None
        self._stop_event = threading.Event()
        self._process_thread: Optional[threading.Thread] = None
        self._record_timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()

    @staticmethod
    def _normalize(key: object) -> object:
        if isinstance(key, Key) and key in _MODIFIER_ALIASES:
            return _MODIFIER_ALIASES[key]
        return key

    def _on_press(self, key: object) -> None:
        if self._stop_event.is_set():
            return
        normalized = self._normalize(key)
        with self._pressed_lock:
            already_complete = self._pressed == HOTKEY
            self._pressed.add(normalized)
            now_complete = self._pressed == HOTKEY
        # Rising edge only: fire once when the combo becomes an exact match.
        if now_complete and not already_complete:
            self._on_hotkey()

    def _on_release(self, key: object) -> None:
        # Track key-up so the next press can form a fresh exact match.
        # Never change state here — avoids macOS key-repeat alert sounds.
        normalized = self._normalize(key)
        with self._pressed_lock:
            self._pressed.discard(normalized)

    def _on_hotkey(self) -> None:
        elapsed = time.monotonic() - self._state.last_change
        if elapsed < DEBOUNCE_SECONDS:
            logger.info("Hotkey debounced (%.2fs < %.2fs)", elapsed, DEBOUNCE_SECONDS)
            return

        state = self._state.current
        if state == State.IDLE:
            # 建立背景執行緒來啟動硬體，避免阻塞鍵盤中斷
            threading.Thread(target=self._begin_recording, daemon=True).start()
        elif state == State.RECORDING:
            logger.info("Hotkey toggle: stopping recording")
            # 同樣將停止動作交給背景執行緒
            threading.Thread(target=self._end_recording, daemon=True).start()
        else:
            logger.info("Hotkey ignored while state is %s", state.value)

    def _begin_recording(self) -> None:
        if not self._state.transition(State.IDLE, State.RECORDING):
            return
        try:
            self._recorder.start()
        except Exception:
            logger.exception("Failed to start audio capture")
            self._state.transition(State.RECORDING, State.IDLE)
            return
        self._arm_record_timer()

    def _end_recording(self) -> None:
        self._cancel_record_timer()
        if not self._state.transition(State.RECORDING, State.PROCESSING):
            return
        try:
            audio = self._recorder.stop()
        except Exception:
            logger.exception("Failed to stop audio capture")
            audio = np.empty((0,), dtype=np.float32)

        self._process_thread = threading.Thread(
            target=self._process,
            args=(audio,),
            name="antigravity-process",
            daemon=True,
        )
        self._process_thread.start()

    def _arm_record_timer(self) -> None:
        self._cancel_record_timer()
        timer = threading.Timer(MAX_RECORD_SECONDS, self._on_record_timeout)
        timer.daemon = True
        with self._timer_lock:
            self._record_timer = timer
            timer.start()
        logger.info("Recording fail-safe armed (%ds)", MAX_RECORD_SECONDS)

    def _cancel_record_timer(self) -> None:
        with self._timer_lock:
            timer = self._record_timer
            self._record_timer = None
        if timer is not None:
            timer.cancel()

    def _on_record_timeout(self) -> None:
        if self._stop_event.is_set():
            return
        if self._state.current != State.RECORDING:
            return
        logger.warning(
            "Max recording time (%ds) reached; auto-stopping",
            MAX_RECORD_SECONDS,
        )
        self._end_recording()

    def _process(self, audio: np.ndarray) -> None:
        try:
            if audio.size == 0:
                logger.warning("No audio captured; skipping agent send")
            else:
                send_to_antigravity(audio)
        except Exception:
            logger.exception("Error in send_to_antigravity")
        finally:
            self._state.transition(State.PROCESSING, State.IDLE)

    def run(self) -> None:
        logger.info(
            "Vibe daemon starting. Press Cmd+Shift+Space to start/stop recording. "
            "Ctrl+C to quit."
        )
        logger.info(
            "macOS: grant Accessibility (for the hotkey) and Microphone permission."
        )

        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.start()

        try:
            while not self._stop_event.is_set() and self._listener.is_alive():
                self._listener.join(timeout=0.25)
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._stop_event.is_set() and self._listener is None:
            return
        self._stop_event.set()
        logger.info("Shutting down...")

        self._cancel_record_timer()

        if self._listener is not None:
            self._listener.stop()
            self._listener = None

        # If we were mid-take, drop the buffer rather than sending a partial clip.
        if self._state.current == State.RECORDING:
            if self._state.transition(State.RECORDING, State.IDLE):
                try:
                    self._recorder.stop()
                except Exception:
                    logger.exception("Error stopping recorder during shutdown")

        if self._process_thread is not None and self._process_thread.is_alive():
            self._process_thread.join(timeout=2.0)

        logger.info("Daemon stopped")


def main() -> int:
    daemon = VibeDaemon()

    def _handle_signal(signum, _frame) -> None:
        logger.info("Received signal %s", signum)
        daemon._stop_event.set()
        if daemon._listener is not None:
            daemon._listener.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
