"""
File watcher — monitors the project for changes, triggers re-scan and
broadcasts events to connected WebSocket clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger("arch-viewer.watcher")

IGNORE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    ".next", ".nuxt", "dist", "build", ".turbo", ".cache",
    ".mypy_cache", ".pytest_cache",
}

IGNORE_EXTS = {".pyc", ".pyo", ".swp", ".swo", ".tmp"}


class ChangeEvent:
    """A file change event with metadata."""

    __slots__ = ("event_type", "path", "name", "timestamp")

    def __init__(self, event_type: str, path: str, name: str):
        self.event_type = event_type
        self.path = path
        self.name = name
        self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "type": "file_change",
            "event": self.event_type,
            "path": self.path,
            "name": self.name,
            "timestamp": self.timestamp,
        }


class ProjectWatcher:
    """
    Watches a project root for file changes.
    Coalesces rapid changes (debounce), skips ignored dirs,
    and triggers callbacks for WebSocket broadcast and re-analysis.
    """

    def __init__(
        self,
        root: str | Path,
        on_change: Callable[[ChangeEvent], None] | None = None,
        on_rescan_needed: Callable[[], None] | None = None,
        debounce_seconds: float = 1.0,
    ):
        self.root = Path(root).resolve()
        self._on_change = on_change
        self._on_rescan_needed = on_rescan_needed
        self._debounce = debounce_seconds
        self._observer: Observer | None = None
        self._recent: deque[ChangeEvent] = deque(maxlen=200)
        self._pending_rescan = False
        self._last_rescan_trigger = 0.0

    def start(self):
        handler = _Handler(
            root=self.root,
            callback=self._handle_event,
        )
        self._observer = Observer()
        self._observer.schedule(handler, str(self.root), recursive=True)
        self._observer.start()
        log.info("Watching %s for changes", self.root)

    def stop(self):
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            log.info("Watcher stopped")

    def get_recent(self) -> list[dict]:
        return [e.to_dict() for e in self._recent]

    def _handle_event(self, event: ChangeEvent):
        self._recent.appendleft(event)

        if self._on_change:
            self._on_change(event)

        # Debounced rescan trigger for structural changes
        if event.event_type in ("created", "deleted", "moved"):
            now = time.time()
            if now - self._last_rescan_trigger > self._debounce:
                self._last_rescan_trigger = now
                if self._on_rescan_needed:
                    self._on_rescan_needed()


class _Handler(FileSystemEventHandler):
    """Watchdog event handler with filtering."""

    def __init__(self, root: Path, callback: Callable[[ChangeEvent], None]):
        self._root = root
        self._callback = callback

    def _should_ignore(self, path: str) -> bool:
        parts = Path(path).parts
        for part in parts:
            if part in IGNORE_DIRS:
                return True
            # Allow .claude and .github but skip other dotfiles
            if part.startswith(".") and part not in (".claude", ".github"):
                return True
        ext = os.path.splitext(path)[1].lower()
        if ext in IGNORE_EXTS:
            return True
        return False

    def _make_event(self, event_type: str, src_path: str) -> ChangeEvent | None:
        try:
            rel = os.path.relpath(src_path, self._root).replace("\\", "/")
        except ValueError:
            return None

        if self._should_ignore(rel):
            return None

        name = os.path.basename(src_path)
        return ChangeEvent(event_type=event_type, path=rel, name=name)

    def on_created(self, event: FileSystemEvent):
        if event.is_directory:
            return
        ev = self._make_event("created", event.src_path)
        if ev:
            self._callback(ev)

    def on_modified(self, event: FileSystemEvent):
        if event.is_directory:
            return
        ev = self._make_event("modified", event.src_path)
        if ev:
            self._callback(ev)

    def on_deleted(self, event: FileSystemEvent):
        if event.is_directory:
            return
        ev = self._make_event("deleted", event.src_path)
        if ev:
            self._callback(ev)

    def on_moved(self, event: FileSystemEvent):
        if event.is_directory:
            return
        ev = self._make_event("moved", getattr(event, "dest_path", event.src_path))
        if ev:
            self._callback(ev)
