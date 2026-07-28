"""Filesystem watching with a polling fallback.

inotify tells us about a write within milliseconds, but Codex appends to the
active rollout continuously, so raw events arrive in a stream. They are
coalesced into at most one wake-up per debounce window.

The poll loop is not just a fallback for missing inotify: watches are only
established on directories that exist at start-up, and a new day's directory
appears without warning, so a slow sweep guarantees eventual pickup regardless.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("ccm.watcher")


class SessionWatcher:
    """Calls ``on_change`` at most once per debounce window."""

    def __init__(
        self,
        root: Path,
        on_change: Callable[[], None],
        *,
        debounce: float = 0.25,
        poll_interval: float = 2.0,
    ):
        self.root = root
        self.on_change = on_change
        self.debounce = debounce
        self.poll_interval = poll_interval
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._observer = None
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        self.backend = "none"

    def start(self) -> None:
        self._start_inotify()
        # A poll interval of zero opts out: with several roots watched, one poll
        # loop already rescans every source, so the rest would be pure noise.
        if self.poll_interval > 0:
            self._poller = threading.Thread(
                target=self._poll_loop, name="ccm-poll", daemon=True
            )
            self._poller.start()

    def _start_inotify(self) -> None:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            log.info("watchdog unavailable; polling only")
            return

        watcher = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:
                if event.is_directory:
                    return
                path = getattr(event, "dest_path", None) or event.src_path
                # ``.db-wal`` covers OpenCode, whose main database file can sit
                # untouched for a whole session while the log absorbs writes.
                if path.endswith((".jsonl", ".db", ".db-wal")):
                    watcher.trigger()

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            observer = Observer()
            observer.schedule(Handler(), str(self.root), recursive=True)
            observer.start()
            self._observer = observer
            self.backend = "inotify"
        except OSError as exc:
            # Typically the per-user watch limit. Polling still covers us.
            log.warning("filesystem watch unavailable (%s); polling only", exc)

    def trigger(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        try:
            self.on_change()
        except Exception:
            log.exception("scan trigger failed")

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.on_change()
            except Exception:
                log.exception("poll trigger failed")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
