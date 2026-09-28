#!/usr/bin/env python3
"""Standalone tkinter preferences dashboard for vibe-daemon."""

from __future__ import annotations

import os
import pathlib
import tkinter as tk
from tkinter import ttk

from dotenv import load_dotenv, set_key

ENV_FILE = pathlib.Path.home() / ".vibe_daemon_env"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


class PreferencesApp:
    """Simple preferences window backed by ~/.vibe_daemon_env."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Vibe Daemon Preferences")
        self.root.resizable(False, False)
        self.root.bind(
            "<Command-v>",
            lambda e: self.root.focus_get().event_generate("<<Paste>>"),
        )
        self.root.bind(
            "<Command-c>",
            lambda e: self.root.focus_get().event_generate("<<Copy>>"),
        )
        self.root.bind(
            "<Command-a>",
            lambda e: self.root.focus_get().event_generate("<<SelectAll>>"),
        )

        # Force macOS to bring this specific Python process to the front
        os.system(
            "osascript -e "
            f"'tell application \"System Events\" to set frontmost of "
            f"the first process whose unix id is {os.getpid()} to true'"
        )
        self.root.lift()
        self.root.attributes("-topmost", True)

        ENV_FILE.touch(exist_ok=True)
        load_dotenv(ENV_FILE)

        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")

        ttk.Label(frame, text="Gemini API Key").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.api_key_var = tk.StringVar(value=os.environ.get("GEMINI_API_KEY", ""))
        self.api_key_entry = ttk.Entry(
            frame, textvariable=self.api_key_var, show="*", width=44
        )
        self.api_key_entry.grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12)
        )

        ttk.Label(frame, text="Max Recording (s)").grid(
            row=2, column=0, sticky="w", pady=(0, 4)
        )
        self.max_record_var = tk.StringVar(
            value=os.environ.get("MAX_RECORD_SECONDS", "120")
        )
        ttk.Entry(frame, textvariable=self.max_record_var, width=12).grid(
            row=3, column=0, sticky="w", pady=(0, 12)
        )

        self.enable_tts_var = tk.BooleanVar(value=_env_bool("ENABLE_TTS", True))
        ttk.Checkbutton(
            frame,
            text="Enable TTS",
            variable=self.enable_tts_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 16))

        ttk.Button(frame, text="Save", command=self._save).grid(
            row=5, column=0, sticky="w"
        )

        self.root.bind("<Return>", lambda _event: self._save())
        self.api_key_entry.focus_force()

    def _save(self) -> None:
        ENV_FILE.touch(exist_ok=True)
        try:
            ENV_FILE.chmod(0o600)
        except OSError:
            pass
        env_path = str(ENV_FILE)
        set_key(env_path, "GEMINI_API_KEY", self.api_key_var.get().strip())
        max_record = self.max_record_var.get().strip() or "120"
        set_key(env_path, "MAX_RECORD_SECONDS", max_record)
        set_key(
            env_path,
            "ENABLE_TTS",
            "true" if self.enable_tts_var.get() else "false",
        )
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    PreferencesApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
