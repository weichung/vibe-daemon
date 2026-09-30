"""Watch ~/Documents/VibeTasks.md and execute unchecked Markdown tasks."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from core.router import VibeRouter

logger = logging.getLogger("vibe-daemon")

TODO_FILE = Path(os.path.expanduser("~/Documents/VibeTasks.md"))
POLL_INTERVAL_SECONDS = 3.0
UNFINISHED_PREFIX = "- [ ] "
DONE_PREFIX = "- [x] "


class TodoWatcher:
    """Poll a Markdown checklist and dispatch new unfinished items to the router."""

    def __init__(self, router: VibeRouter) -> None:
        self._router = router
        self._path = TODO_FILE
        if self._path.is_file():
            self._last_mtime = self._path.stat().st_mtime
        else:
            self._last_mtime = 0.0

    def poll(self) -> None:
        logger.info(
            "TODO watcher started (%s, every %.0fs)",
            self._path,
            POLL_INTERVAL_SECONDS,
        )
        while True:
            try:
                self._tick()
            except Exception:
                logger.exception("TODO watcher tick failed")
            time.sleep(POLL_INTERVAL_SECONDS)

    def _tick(self) -> None:
        if not self._path.is_file():
            return
        mtime = self._path.stat().st_mtime
        if mtime <= self._last_mtime:
            return
        self._process_file()

    def _process_file(self) -> None:
        try:
            snapshot = self._path.read_text(encoding="utf-8")
        except OSError:
            logger.exception("TODO watcher could not read %s", self._path)
            return

        tasks = [
            line[len(UNFINISHED_PREFIX) :].strip()
            for line in snapshot.splitlines()
            if line.startswith(UNFINISHED_PREFIX)
            and line[len(UNFINISHED_PREFIX) :].strip()
        ]
        if not tasks:
            self._refresh_mtime()
            return

        for task in tasks:
            logger.info("TODO executing: %s", task)
            try:
                result = self._router.dispatch_text(task)
                logger.info("TODO result: %s", result)
            except Exception:
                logger.exception("TODO dispatch failed: %s", task)
                continue
            self._mark_task_done(task)
        self._refresh_mtime()

    def _mark_task_done(self, task: str) -> None:
        """Re-read from disk and flip only the matching unfinished line."""
        try:
            current = self._path.read_text(encoding="utf-8")
        except OSError:
            logger.exception("TODO watcher could not re-read %s", self._path)
            return

        target = f"{UNFINISHED_PREFIX}{task}"
        lines = current.splitlines(keepends=True)
        replaced = False
        rewritten: list[str] = []
        for line in lines:
            body = line.rstrip("\r\n")
            if not replaced and body == target:
                newline = line[len(body) :]
                rewritten.append(f"{DONE_PREFIX}{task}{newline}")
                replaced = True
            else:
                rewritten.append(line)

        if not replaced:
            logger.warning("TODO line no longer present, skip mark: %s", task)
            self._refresh_mtime()
            return

        self._path.write_text("".join(rewritten), encoding="utf-8")
        self._refresh_mtime()
        logger.info("TODO marked done: %s", task)

    def _refresh_mtime(self) -> None:
        try:
            self._last_mtime = self._path.stat().st_mtime
        except OSError:
            self._last_mtime = time.time()
