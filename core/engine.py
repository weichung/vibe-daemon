"""Platform-agnostic vibe engine: hotkey toggle, audio capture, and agent handoff."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import queue
import subprocess
import threading
import time
import wave
from collections.abc import Callable
from enum import Enum
from typing import Optional, Set

import numpy as np
import sounddevice as sd
from google import antigravity as ag
from pynput import keyboard
from pynput.keyboard import Key

from core.parser import _extract_transcript, _extract_tts_summary
from core.router import VibeRouter

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "float32"
BLOCKSIZE = 1024
MAX_RECORD_SECONDS = 120
DEBOUNCE_SECONDS = 0.5

HOTKEY: Set[Key] = {Key.cmd, Key.shift, Key.space}

_MODIFIER_ALIASES = {
    Key.cmd: Key.cmd,
    Key.cmd_l: Key.cmd,
    Key.cmd_r: Key.cmd,
    Key.shift: Key.shift,
    Key.shift_l: Key.shift,
    Key.shift_r: Key.shift,
}

SYSTEM_PROMPT = (
    "You are an expert system software engineer and local coding assistant. "
    "Follow these strict rules:\n"
    "1. Code Style: Always use Python 3.10+ type hinting and provide concise "
    "docstrings for any generated code.\n"
    "2. Safety: Before modifying critical system configurations or existing "
    "core logic, clearly comment the reasons and boundaries of your changes "
    "in the code.\n"
    "3. Response Format: You MUST output a concise, 1-to-2 sentence summary "
    "intended for text-to-speech at the very beginning of your response, "
    "wrapped exactly in <tts> and </tts> tags. The rest of your response "
    "can contain detailed explanations, code blocks, and reasoning.\n"
    "4. Transcript: You MUST include the user's recognized speech wrapped "
    "exactly in <transcript> and </transcript> tags."
)

logger = logging.getLogger("vibe-daemon")

OnStateChange = Callable[["State"], None]
OnText = Callable[[str], None]


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
        self.current_volume: float = 0.0

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            logger.warning("Audio stream status: %s", status)
        volume = float(np.max(np.abs(indata)))
        self.current_volume = volume
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
                    self.current_volume = 0.0
                    logger.info("Audio stream stopped")
            else:
                self.current_volume = 0.0
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


def _float32_mono_to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """Encode 16 kHz float32 mono PCM as a WAV container (audio/wav)."""
    mono = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm16 = np.clip(mono, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16.tobytes())
    return buf.getvalue()


class VibeDaemon:
    """Toggle daemon: press Cmd+Shift+Space to start or stop recording."""

    def __init__(
        self,
        on_state_change: Optional[OnStateChange] = None,
        on_tts_ready: Optional[OnText] = None,
        on_transcript_ready: Optional[OnText] = None,
        on_setup_required: Optional[Callable[[], None]] = None,
    ) -> None:
        self._on_state_change = on_state_change
        self._on_tts_ready = on_tts_ready
        self._on_transcript_ready = on_transcript_ready
        self._on_setup_required = on_setup_required
        self._state = StateManager()
        self._recorder = AudioRecorder()
        self._pressed: Set[object] = set()
        self._pressed_lock = threading.Lock()
        self._listener: Optional[keyboard.Listener] = None
        self._stop_event = threading.Event()
        self._process_thread: Optional[threading.Thread] = None
        self._record_timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()
        self.router = VibeRouter()
        self.router.on_route_to_ide = self._on_route_to_ide
        self.router.on_shell_executed = self._on_router_text
        self.router.on_conversation = self._on_router_text

    def _on_route_to_ide(self, audio_bytes: bytes) -> None:
        """Forward routed coding audio to the existing Antigravity pipeline."""
        logger.info("Router -> Antigravity IDE (%d bytes)", len(audio_bytes))
        try:
            asyncio.run(self._send_to_antigravity_async(audio_bytes))
        except ag.types.AntigravityConnectionError:
            logger.exception(
                "Failed to connect to the Antigravity IDE. Is the IDE open?"
            )
        except ag.types.AntigravityValidationError:
            logger.exception("Antigravity rejected the audio payload or agent config")
        except Exception:
            logger.exception("Unexpected error while sending audio to Antigravity")

    def _on_router_text(self, text: str) -> None:
        """Speak and display shell or conversation results in the menu bar."""
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return
        self._emit_transcript(cleaned)
        self._emit_tts(cleaned)

    def _emit_state(self, new_state: State) -> None:
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(new_state)
        except Exception:
            logger.exception("on_state_change callback failed")

    def _emit_tts(self, text: str) -> None:
        if self._on_tts_ready is None or not text:
            return
        try:
            self._on_tts_ready(text)
        except Exception:
            logger.exception("on_tts_ready callback failed")

    def _emit_transcript(self, text: str) -> None:
        if self._on_transcript_ready is None or not text:
            return
        try:
            self._on_transcript_ready(text)
        except Exception:
            logger.exception("on_transcript_ready callback failed")

    def _transition(self, from_state: State, to_state: State) -> bool:
        if not self._state.transition(from_state, to_state):
            return False
        self._emit_state(to_state)
        return True

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
        if now_complete and not already_complete:
            self._on_hotkey()

    def _on_release(self, key: object) -> None:
        normalized = self._normalize(key)
        with self._pressed_lock:
            self._pressed.discard(normalized)

    def _on_hotkey(self) -> None:
        if not os.environ.get("GEMINI_API_KEY"):
            logger.warning("Hotkey pressed but no API key set.")
            self._emit_tts("Please set your API key.")
            if self._on_setup_required:
                self._on_setup_required()
            return

        elapsed = time.monotonic() - self._state.last_change
        if elapsed < DEBOUNCE_SECONDS:
            logger.info("Hotkey debounced (%.2fs < %.2fs)", elapsed, DEBOUNCE_SECONDS)
            return

        state = self._state.current
        if state == State.IDLE:
            threading.Thread(target=self._begin_recording, daemon=True).start()
        elif state == State.RECORDING:
            logger.info("Hotkey toggle: stopping recording")
            threading.Thread(target=self._end_recording, daemon=True).start()
        else:
            logger.info("Hotkey ignored while state is %s", state.value)

    def _begin_recording(self) -> None:
        if not self._transition(State.IDLE, State.RECORDING):
            return
        try:
            self._recorder.start()
        except Exception:
            logger.exception("Failed to start audio capture")
            self._transition(State.RECORDING, State.IDLE)
            return
        self._arm_record_timer()

    def _end_recording(self) -> None:
        self._cancel_record_timer()
        if not self._transition(State.RECORDING, State.PROCESSING):
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
                subprocess.run(["git", "add", "."], capture_output=True)
                subprocess.run(
                    ["git", "commit", "-m", "vibe-checkpoint: pre-agent action"],
                    capture_output=True,
                )
                logger.info(
                    "Dispatching audio to router (%d samples)", int(audio.shape[0])
                )
                wav_bytes = _float32_mono_to_wav_bytes(audio, SAMPLE_RATE)
                summary = self.router.dispatch_audio(wav_bytes)
                logger.info("Router summary: %s", summary)
        except Exception:
            logger.exception("Error in router dispatch")
        finally:
            self._transition(State.PROCESSING, State.IDLE)

    async def _send_to_antigravity_async(self, wav_bytes: bytes) -> None:
        audio = ag.Audio(
            data=wav_bytes,
            mime_type="audio/wav",
            description="16 kHz float32 mono voice capture",
        )
        prompt = [
            SYSTEM_PROMPT,
            "This is a spoken coding instruction from a local microphone. "
            "Transcribe it and carry it out in the current workspace.",
            audio,
        ]
        async with ag.Agent(ag.LocalAgentConfig()) as agent:
            response = await agent.chat(prompt)
            text = await response.text()
            logger.info("Antigravity response: %s", text or "<empty>")
            transcript = _extract_transcript(text or "")
            if transcript:
                logger.info("Transcript: %s", transcript)
                self._emit_transcript(transcript)
            tts_text = _extract_tts_summary(text or "")
            if tts_text:
                self._emit_tts(tts_text)

    def send_to_antigravity(self, audio_numpy_array: np.ndarray) -> None:
        """Send captured audio to the Antigravity IDE via the google.antigravity SDK."""
        samples = int(audio_numpy_array.shape[0]) if audio_numpy_array.ndim >= 1 else 0
        duration = samples / SAMPLE_RATE if SAMPLE_RATE else 0.0
        logger.info(
            "Audio payload: shape=%s length=%d duration=%.2fs dtype=%s",
            audio_numpy_array.shape,
            samples,
            duration,
            audio_numpy_array.dtype,
        )
        if audio_numpy_array.size:
            max_volume = float(np.max(np.abs(audio_numpy_array)))
            logger.info("Max volume level: %.4f", max_volume)

        logger.info("Sending audio to Antigravity IDE...")
        try:
            wav_bytes = _float32_mono_to_wav_bytes(audio_numpy_array, SAMPLE_RATE)
            asyncio.run(self._send_to_antigravity_async(wav_bytes))
        except ag.types.AntigravityConnectionError:
            logger.exception(
                "Failed to connect to the Antigravity IDE. Is the IDE open?"
            )
        except ag.types.AntigravityValidationError:
            logger.exception("Antigravity rejected the audio payload or agent config")
        except Exception:
            logger.exception("Unexpected error while sending audio to Antigravity")

    def run(self) -> None:
        logger.info(
            "Vibe daemon starting. Press Cmd+Shift+Space to start/stop recording."
        )
        logger.info(
            "Grant Accessibility (for the hotkey) and Microphone permission."
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

        if self._state.current == State.RECORDING:
            if self._transition(State.RECORDING, State.IDLE):
                try:
                    self._recorder.stop()
                except Exception:
                    logger.exception("Error stopping recorder during shutdown")

        if self._process_thread is not None and self._process_thread.is_alive():
            self._process_thread.join(timeout=2.0)

        logger.info("Daemon stopped")
