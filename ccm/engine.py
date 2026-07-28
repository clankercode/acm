"""Background scan loop and live-update fan-out.

One worker thread owns all writes. It runs the initial pass, then sleeps until
the filesystem watcher (or the polling fallback) wakes it. Subscribers receive
coalesced snapshots rather than a message per file, so a cold scan of thousands
of files does not flood the browser.

Two kinds of update are pushed, because they differ enormously in size:

``scan``  progress only -- cheap, sent often, drives the progress bar.
``data``  a generation counter plus running totals -- sent when rows actually
          changed, telling the client its charts are stale.

Series data is pulled over HTTP rather than pushed. Pushing every chart's points
on every tick would be megabytes a second for numbers the user cannot read that
fast.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import aggregate as A
from .config import Settings
from .modelsdev import ModelsDev
from .portable import default_label, sanitise_label
from .pricing import PricingTable
from .scanner import ScanProgress, Scanner
from .store import DERIVED_TABLES, Store

log = logging.getLogger("ccm.engine")


@dataclass(eq=False)  # identity-hashed so it can live in a set
class Subscriber:
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop


class Engine:
    """Owns the store, the scanner and the set of live listeners."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.pricing = PricingTable(settings.pricing_path)
        self.reference = ModelsDev(settings.reference_path)
        self.scanner = Scanner(self.store, settings=settings)

        self._subscribers: set[Subscriber] = set()
        self._sub_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._watchers: list = []

        self._generation = 0
        self._broadcast_generation = -1
        self._totals: dict = {}
        self._quality: dict = {}
        self._dimensions: dict = {}
        self._last_broadcast = 0.0
        self._state_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self, *, watch: bool = True) -> None:
        self._worker = threading.Thread(target=self._run, name="ccm-scan", daemon=True)
        self._worker.start()
        if watch:
            self._start_watcher()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for watcher in self._watchers:
            watcher.stop()
        if self._worker is not None:
            self._worker.join(timeout=5)

    def _start_watcher(self) -> None:
        """One watcher per client root, sharing a single poll fallback.

        The roots are unrelated trees in different parts of the filesystem, so
        there is nothing to gain from a common parent -- watching ``~`` would
        pull in everything. Only the first watcher runs the poll loop, since a
        poll rescans every source anyway.
        """
        from .watcher import SessionWatcher

        roots = self.scanner.watch_roots
        for index, root in enumerate(roots):
            watcher = SessionWatcher(
                root,
                on_change=self.request_scan,
                debounce=self.settings.debounce_seconds,
                poll_interval=self.settings.poll_seconds if index == 0 else 0,
            )
            watcher.start()
            self._watchers.append(watcher)

    def request_scan(self) -> None:
        """Ask the worker to make another pass as soon as it can."""
        self._wake.set()

    def refresh_now(self) -> None:
        """Recompute the derived state immediately, off the worker thread.

        Used after an import: nothing was scanned, but the stored rows changed,
        so every open browser needs to be told to refetch.
        """
        self._refresh_derived(force=True)
        self._broadcast_data(force=True)

    # -- machine identity --------------------------------------------------

    @property
    def local_label(self) -> str:
        """What this machine calls itself in an export and in the UI."""
        return self.store.get_meta("local_label") or default_label()

    def set_local_label(self, label: str) -> str:
        self.store.set_meta("local_label", sanitise_label(label))
        return self.local_label

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._pass(initial=True)
        except Exception:
            log.exception("initial scan failed")
        while not self._stop.is_set():
            # The timeout doubles as a heartbeat, so a missed inotify event
            # delays an update rather than losing it.
            self._wake.wait(timeout=self.settings.poll_seconds)
            if self._stop.is_set():
                break
            self._wake.clear()
            try:
                self._pass()
            except Exception:
                log.exception("incremental scan failed")

    def _pass(self, *, initial: bool = False) -> None:
        if self.pricing.maybe_reload():
            self._refresh_derived(force=True)

        changed = {"any": False}
        last_tick = [0.0]

        def on_progress(progress: ScanProgress) -> None:
            now = time.time()
            if progress.new_requests:
                changed["any"] = True
            # Roll up and re-total periodically so the headline figures climb
            # during the first scan instead of appearing all at once at the end.
            if now - last_tick[0] > 1.0:
                last_tick[0] = now
                if changed["any"]:
                    self._refresh_derived()
                self._broadcast_scan(progress)
            else:
                self._broadcast_scan(progress, throttle=True)

        progress = self.scanner.scan_once(
            on_progress=on_progress, phase="scanning" if initial else "updating"
        )
        self._refresh_derived(force=changed["any"] or initial)
        self._broadcast_scan(progress, force=True)
        self._broadcast_data(force=True)

    def _refresh_derived(self, force: bool = False) -> None:
        rebuilt = A.ensure_buckets_current(self.store, self.pricing)
        if not rebuilt and not force:
            return
        filters = A.Filters()
        totals = A.totals(self.store, self.pricing, filters)
        quality = A.data_quality(self.store, self.pricing)
        dimensions = A.dimensions(self.store, self.pricing)
        with self._state_lock:
            self._generation += 1
            self._totals = totals
            self._quality = quality
            self._dimensions = dimensions
        self._broadcast_data()

    # -- fan-out -----------------------------------------------------------

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> Subscriber:
        sub = Subscriber(queue=asyncio.Queue(maxsize=64), loop=loop)
        with self._sub_lock:
            self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._sub_lock:
            self._subscribers.discard(sub)

    @property
    def subscriber_count(self) -> int:
        with self._sub_lock:
            return len(self._subscribers)

    def _emit(self, event: str, payload: dict) -> None:
        message = {"event": event, "data": payload}
        with self._sub_lock:
            targets = list(self._subscribers)
        for sub in targets:
            try:
                sub.loop.call_soon_threadsafe(self._offer, sub, message)
            except RuntimeError:
                # Loop already closed; the endpoint will clean the sub up.
                pass

    @staticmethod
    def _offer(sub: Subscriber, message: dict) -> None:
        try:
            sub.queue.put_nowait(message)
        except asyncio.QueueFull:
            # A slow client must not stall the scanner. Drop the oldest frame
            # and keep the newest, since these are snapshots, not deltas.
            try:
                sub.queue.get_nowait()
                sub.queue.put_nowait(message)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def _broadcast_scan(
        self, progress: ScanProgress, *, throttle: bool = False, force: bool = False
    ) -> None:
        now = time.time()
        interval = 1.0 / max(self.settings.broadcast_hz, 0.1)
        if throttle and not force and now - self._last_broadcast < interval:
            return
        self._last_broadcast = now
        self._emit("scan", progress.as_dict())

    def _broadcast_data(self, force: bool = False) -> None:
        """Announce that the stored data moved on.

        Sent only when the generation actually advances: the client refetches
        its charts on this event, and a quiet poll cycle is not a reason to make
        it do that. Liveness is already evident from the `scan` stream.
        """
        with self._state_lock:
            if not force and self._generation == self._broadcast_generation:
                return
            self._broadcast_generation = self._generation
            payload = {
                "generation": self._generation,
                "totals": self._totals,
                "quality": self._quality,
                "dimensions": self._dimensions,
            }
        self._emit("data", payload)

    # -- snapshots ---------------------------------------------------------

    def snapshot(self) -> dict:
        with self._state_lock:
            totals = self._totals
            quality = self._quality
            dimensions = self._dimensions
            generation = self._generation
        if not totals:
            # A client that connects before the first pass completes still gets
            # a well-formed, if empty, page.
            totals = A.totals(self.store, self.pricing, A.Filters())
            quality = A.data_quality(self.store, self.pricing)
            dimensions = A.dimensions(self.store, self.pricing)
        return {
            "generation": generation,
            "scan": self.scanner.progress.as_dict(),
            "totals": totals,
            "quality": quality,
            "dimensions": dimensions,
            "pricing": self.pricing.as_dict(),
            "reference": self.reference.status(),
            "local_label": self.local_label,
            "sessions_dir": str(self.settings.sessions_dir),
            "sources": [
                {
                    "name": s.name,
                    "label": s.label,
                    "root": str(s.watch_roots[0]) if s.watch_roots else "",
                }
                for s in self.scanner.sources
            ],
            "server_time": int(time.time() * 1000),
        }

    def rescan_from_scratch(self) -> None:
        """Drop locally derived state and re-read the corpus.

        Imported data survives: it came from another machine's corpus, which
        this one cannot re-read, so deleting it here would simply lose it.
        """
        with self.store.lock:
            conn = self.store.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table in DERIVED_TABLES:
                    if table == "bucket_hour":
                        conn.execute("DELETE FROM bucket_hour WHERE origin = ''")
                    else:
                        conn.execute(f"DELETE FROM {table}")
                conn.execute("DELETE FROM meta WHERE key = 'bucket_fingerprint'")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self.request_scan()


def local_addresses(port: int) -> list[str]:
    """Loopback plus the primary LAN address, for the startup banner."""
    import socket

    urls = [f"http://127.0.0.1:{port}"]
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        # No packet is sent; this just asks the routing table which local
        # address would be used to reach the outside world.
        probe.connect(("10.255.255.255", 1))
        lan = probe.getsockname()[0]
        probe.close()
        if lan and not lan.startswith("127."):
            urls.append(f"http://{lan}:{port}")
    except OSError:
        pass
    return urls
