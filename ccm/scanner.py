"""The scan driver.

Everything format-specific lives in :mod:`ccm.sources`. What is left here is the
part that is the same for every client: work out what has outstanding work,
consume it, and keep an honest running account of progress while doing so.

Progress is reported in bytes rather than files because file sizes across a
corpus span four orders of magnitude, and a bar that jumps from 4% to 96% on one
file is worse than no bar at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .sources import Source, build_sources
from .sources.base import (  # parse_ts/project_label re-exported for convenience
    cancellation,
    parse_ts,
    project_label,
)
from .sources.codex import (  # noqa: F401  -- kept importable at the old path
    EVENT_KINDS,
    CodexSource,
    RolloutParser,
    iter_rollouts,
    parse_file,
)
from .store import Store

__all__ = [
    "EVENT_KINDS",
    "RolloutParser",
    "ScanProgress",
    "Scanner",
    "SourceProgress",
    "iter_rollouts",
    "parse_file",
    "parse_ts",
    "project_label",
]


@dataclass
class SourceProgress:
    """Per-client slice of the scan, so the UI can show which is moving."""

    name: str
    label: str
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    raw_events: int = 0
    new_requests: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "raw_events": self.raw_events,
            "new_requests": self.new_requests,
            "errors": self.errors,
        }


@dataclass
class ScanProgress:
    """Live view of what the scanner is doing, streamed to the browser."""

    phase: str = "idle"  # idle | discovering | scanning | tailing
    files_total: int = 0
    files_done: int = 0
    files_changed: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    raw_events: int = 0
    new_requests: int = 0
    current_file: str | None = None
    current_source: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    last_error: str | None = None
    errors: int = 0
    sources: dict[str, SourceProgress] = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def as_dict(self) -> dict:
        elapsed = self.elapsed
        rate = self.bytes_done / elapsed if elapsed > 0.05 else 0.0
        remaining = max(self.bytes_total - self.bytes_done, 0)
        dedup = 1.0 - (self.new_requests / self.raw_events) if self.raw_events else 0.0
        return {
            "phase": self.phase,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "files_changed": self.files_changed,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "raw_events": self.raw_events,
            "new_requests": self.new_requests,
            "current_file": self.current_file,
            "current_source": self.current_source,
            "elapsed": round(elapsed, 2),
            "bytes_per_sec": round(rate),
            "eta_seconds": round(remaining / rate, 1) if rate > 0 else None,
            "duplicate_fraction": round(dedup, 4),
            "errors": self.errors,
            "last_error": self.last_error,
            "sources": [s.as_dict() for s in self.sources.values()],
        }


#: How long discovery may run before it is worth telling the UI about. A poll
#: cycle stats a few thousand files in well under this; a first scan walking
#: four whole corpora takes many seconds and would otherwise look like a hang.
DISCOVERY_NOTICE_SECONDS = 1.5


class Scanner:
    """Walks every enabled client's history and feeds the store."""

    def __init__(
        self,
        store: Store,
        sessions_dir: Path | None = None,
        *,
        sources: Sequence[Source] | None = None,
        settings=None,
    ):
        """Accepts either an explicit source list or the legacy Codex root.

        Passing just a directory keeps the single-source call site working, and
        is what the scanner tests use to point at a fixture tree.
        """
        self.store = store
        if sources is not None:
            self.sources = list(sources)
        elif sessions_dir is not None:
            self.sources = [CodexSource(sessions_dir)]
        elif settings is not None:
            self.sources = build_sources(settings)
        else:
            raise ValueError("Scanner needs a sessions_dir, sources or settings")
        self.sessions_dir = sessions_dir
        self.progress = ScanProgress()

    @property
    def watch_roots(self) -> list[Path]:
        roots: list[Path] = []
        for source in self.sources:
            roots.extend(source.watch_roots)
        return roots

    def scan_once(
        self,
        *,
        on_progress: Callable[[ScanProgress], None] | None = None,
        phase: str = "scanning",
        should_stop: Callable[[], bool] | None = None,
    ) -> ScanProgress:
        """One full pass: ingest whatever is new since the last pass.

        ``should_stop`` is polled between files, and inside them by the readers
        (see :func:`ccm.sources.base.cancellation`). A cold scan of a large
        corpus runs for minutes, and shutdown waits on this thread, so without
        a way to leave early Ctrl-C would appear to hang for as long as the
        join allows. Whatever has been ingested so far is already committed,
        and the next pass resumes from the stored offsets.
        """
        p = self.progress
        p.phase = "discovering"
        p.started_at = time.time()
        p.finished_at = None
        p.files_total = 0
        p.files_done = 0
        p.files_changed = 0
        p.bytes_total = 0
        p.bytes_done = 0
        p.raw_events = 0
        p.new_requests = 0
        p.errors = 0
        p.current_file = None
        p.current_source = None
        p.sources = {
            s.name: SourceProgress(name=s.name, label=s.label) for s in self.sources
        }

        # Nothing is announced until there is something to announce. Most passes
        # are a quiet poll that plans zero units, and reporting those made the
        # header flicker "scanning 0/0" between two spells of "live".
        announced = False

        def announce() -> None:
            nonlocal announced
            announced = True
            if on_progress:
                on_progress(p)

        # Plan every source before ingesting any, so the progress bar has a
        # true denominator from the first byte rather than growing as it goes.
        plans: list[tuple[Source, list]] = []
        for source in self.sources:
            if should_stop is not None and should_stop():
                p.phase = "tailing"
                p.finished_at = time.time()
                return p
            try:
                units = source.plan(self.store)
            except Exception as exc:  # a broken client must not stop the others
                p.errors += 1
                p.last_error = f"{source.name}: {exc}"
                continue
            plans.append((source, units))
            slice_ = p.sources[source.name]
            slice_.files_total = len(units)
            slice_.bytes_total = sum(u.pending_bytes for u in units)
            p.files_total += len(units)
            p.bytes_total += slice_.bytes_total
            # A first scan walks the whole corpus, which is slow enough that
            # silence would read as a hang.
            if not announced and time.time() - p.started_at > DISCOVERY_NOTICE_SECONDS:
                announce()

        p.files_changed = p.files_total
        if not p.files_total:
            # Straight back to tailing without ever leaving it, as far as any
            # subscriber can tell. The caller still broadcasts this final state.
            p.phase = "tailing"
            p.finished_at = time.time()
            if announced:
                announce()
            return p

        p.phase = phase
        announce()

        for source, units in plans:
            slice_ = p.sources[source.name]
            p.current_source = source.name
            for unit in units:
                if should_stop is not None and should_stop():
                    p.current_file = None
                    p.current_source = None
                    p.phase = "tailing"
                    p.finished_at = time.time()
                    return p
                p.current_file = unit.key
                try:
                    with cancellation(should_stop):
                        result = source.ingest(self.store, unit)
                except Exception as exc:
                    p.errors += 1
                    slice_.errors += 1
                    p.last_error = f"{unit.key}: {exc}"
                    p.files_done += 1
                    slice_.files_done += 1
                    if on_progress:
                        on_progress(p)
                    continue

                p.files_done += 1
                p.bytes_done += result.bytes_read
                p.raw_events += result.raw_events
                p.new_requests += result.new_requests
                slice_.files_done += 1
                slice_.bytes_done += result.bytes_read
                slice_.raw_events += result.raw_events
                slice_.new_requests += result.new_requests
                if result.error:
                    p.errors += 1
                    slice_.errors += 1
                    p.last_error = f"{Path(unit.key).name}: {result.error}"
                if on_progress:
                    on_progress(p)

        p.current_file = None
        p.current_source = None
        p.phase = "tailing"
        p.finished_at = time.time()
        if on_progress:
            on_progress(p)
        return p
