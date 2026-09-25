#!/usr/bin/env python3
"""macOS entry point: rumps on the main thread, vibe engine in the background."""

from __future__ import annotations

import logging
import sys
import threading

from platforms.macos.menu_app import VibeMenuBarApp


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    app = VibeMenuBarApp()
    engine_thread = threading.Thread(
        target=app.engine.run,
        name="vibe-engine",
        daemon=True,
    )
    engine_thread.start()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
