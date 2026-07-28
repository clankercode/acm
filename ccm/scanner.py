"""The scan driver.

Everything format-specific lives in :mod:`ccm.sources`. What is left here is the
part that is the same for every client: work out what has outstanding work,
consume it, and keep an honest running account of progress while doing so.

Progress is reported in bytes rather than files because file sizes across a
corpus span four orders of magnitude, and a bar that jumps from 4% to 96% on one
file is worse than no bar at all.
"""

from __future__ import annotations

import threading
import time
from collections import deque
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
    "ErrorKind",
    "RolloutParser",
    "ScanProgress",
    "Scanner",
    "SourceProgress",
    "iter_rollouts",
    "parse_file",
    "parse_ts",
    "project_label",
]

#: How much recent history the live row rate is measured over. Short enough to
#: describe the file being read now, long enough that one small file finishing
#: does not spike it.
ROW_RATE_WINDOW = 5.0

#: Distinct failures kept for the header dropdown. A scan trips over kinds of
#: problem, not instances, so this is generous; anything past it is still
#: counted, just not itemised.
MAX_ERROR_KINDS = 16

#: Errors are grouped by message, so the message is truncated before it becomes
#: a key -- some carry a fragment of the offending line, which would make every
#: sighting unique and defeat the grouping entirely.
ERROR_MESSAGE_CHARS = 180


@dataclass
class ErrorKind:
    """One distinct failure, plus how often and where it has happened.

    Eleven identical messages in a dropdown say less than one message with an
    eleven beside it, so sightings are folded together and only the most recent
    file is kept as an example.
    """

    message: str
    count: int = 0
    sources: list[str] = field(default_factory=list)
    first_at: float = 0.0
    last_at: float = 0.0
    last_file: str | None = None

    def as_dict(self) -> dict:
        return {
            "message": self.message,
            "count": self.count,
            "sources": list(self.sources),
            "first_at": int(self.first_at * 1000),
            "last_at": int(self.last_at * 1000),
            "last_file": self.last_file,
        }


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
    rows: int = 0
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
            "rows": self.rows,
            "new_requests": self.new_requests,
            "errors": self.errors,
        }


@dataclass
class ScanProgress:
    """Live view of what the scanner is doing, streamed to the browser."""

    phase: str = "idle"  # idle | discovering | scanning | updating | tailing | paused
    #: Set while the operator has stopped the scan loop. Distinct from the phase
    #: because pausing cancels the pass rather than waiting for it: for the
    #: moment between the request and the worker noticing, the phase still
    #: honestly reads "scanning" while this already reads paused.
    paused: bool = False
    #: Set while the local corpus has been dropped for a rebuild that has not
    #: finished. Pausing part-way through one leaves the dashboard legitimately
    #: near-empty, which is worth saying out loud rather than letting it read as
    #: "you have no history".
    rebuild_pending: bool = False
    #: Whether the last pass gave up part-way (stop or pause) rather than
    #: finishing its plan. Read by the engine, which must not treat an abandoned
    #: cold scan as a corpus that has been read.
    interrupted: bool = False
    files_total: int = 0
    files_done: int = 0
    files_changed: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    raw_events: int = 0
    rows: int = 0
    new_requests: int = 0
    current_file: str | None = None
    current_source: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    last_error: str | None = None
    errors: int = 0
    sources: dict[str, SourceProgress] = field(default_factory=dict)
    error_kinds: dict[str, ErrorKind] = field(default_factory=dict)
    #: (wall clock, cumulative rows) samples backing the live rate. Bounded by
    #: both time and length, since a fast pass consumes hundreds of files a second.
    samples: deque[tuple[float, int]] = field(
        default_factory=lambda: deque(maxlen=4096), repr=False
    )
    #: The scan worker appends to both of the above while the web thread reads
    #: them for `/api/state`, and iterating either one mid-append raises. Every
    #: other field is a scalar the reader can only catch slightly stale.
    #: Reentrant so `as_dict` can hold it across `rows_per_sec`.
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def reset_diagnostics(self) -> None:
        """Clear the per-pass error groups and rate samples."""
        with self.lock:
            self.error_kinds = {}
            self.samples.clear()

    def note_error(
        self,
        message: str,
        *,
        source: str | None = None,
        file: str | None = None,
    ) -> None:
        """Count one failure against the pass, its client and its kind."""
        self.errors += 1
        self.last_error = f"{Path(file).name}: {message}" if file else message
        if source is not None and source in self.sources:
            self.sources[source].errors += 1

        key = " ".join(message.split())[:ERROR_MESSAGE_CHARS]
        now = time.time()
        with self.lock:
            kind = self.error_kinds.get(key)
            if kind is None:
                if len(self.error_kinds) >= MAX_ERROR_KINDS:
                    # Counted above but not itemised. The UI reports the
                    # shortfall between the total and the groups rather than
                    # letting this grow without bound.
                    return
                kind = ErrorKind(message=key, first_at=now)
                self.error_kinds[key] = kind
            kind.count += 1
            kind.last_at = now
            if file:
                kind.last_file = file
            if source is not None and source not in kind.sources:
                kind.sources.append(source)

    def note_sample(self) -> None:
        """Record where the event counter stands, for the live rate."""
        now = time.time()
        with self.lock:
            self.samples.append((now, self.rows))
            while (
                len(self.samples) > 2
                and now - self.samples[0][0] > ROW_RATE_WINDOW * 2
            ):
                self.samples.popleft()

    @property
    def rows_per_sec(self) -> float:
        """Session-history rows consumed per second over the last few seconds.

        Rows, not token events: only a small fraction of lines carry token
        counts, so an event rate sits at zero through thousands of files that are
        being read perfectly well, which is the opposite of what a liveness
        number is for.

        Windowed rather than the pass average on purpose: what it answers is
        whether the corpus is moving *now*, and an average over a 20 GB scan
        cannot say that -- it would still read healthy while the reader sat on
        one pathological file.

        The window ends at the current moment rather than at the last sample,
        which is what makes it decay. Counting is per completed file, so a pass
        ten seconds into a large one has legitimately banked nothing lately;
        measuring against the last sample instead would hold the previous value
        and then snap to zero, reporting a stall that is really just a big file.
        """
        with self.lock:
            if not self.samples:
                return 0.0
            now = time.time()
            cutoff = now - ROW_RATE_WINDOW
            # Where the counter stood as the window opened: the newest sample
            # from before the cutoff, or the oldest we still hold.
            base_at, base_rows = self.samples[0]
            for at, rows in self.samples:
                if at >= cutoff:
                    break
                base_at, base_rows = at, rows
            newest_rows = self.samples[-1][1]

        # A pass younger than one window is divided by its own age instead, or
        # its first seconds would read artificially low.
        span = min(ROW_RATE_WINDOW, now - base_at)
        if span <= 0.05:
            return 0.0
        return max(newest_rows - base_rows, 0) / span

    def as_dict(self) -> dict:
        elapsed = self.elapsed
        rate = self.bytes_done / elapsed if elapsed > 0.05 else 0.0
        remaining = max(self.bytes_total - self.bytes_done, 0)
        dedup = 1.0 - (self.new_requests / self.raw_events) if self.raw_events else 0.0
        with self.lock:
            rows_per_sec = round(self.rows_per_sec)
            # Loudest first: with a cap on how many are itemised, the kind that
            # is actually costing coverage should never be the one dropped.
            groups = [
                k.as_dict()
                for k in sorted(
                    self.error_kinds.values(), key=lambda k: (-k.count, -k.last_at)
                )
            ]
        return {
            "phase": self.phase,
            "paused": self.paused,
            "rebuild_pending": self.rebuild_pending,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "files_changed": self.files_changed,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "raw_events": self.raw_events,
            "rows": self.rows,
            "new_requests": self.new_requests,
            "current_file": self.current_file,
            "current_source": self.current_source,
            "elapsed": round(elapsed, 2),
            "bytes_per_sec": round(rate),
            "eta_seconds": round(remaining / rate, 1) if rate > 0 else None,
            "duplicate_fraction": round(dedup, 4),
            "rows_per_sec": rows_per_sec,
            "errors": self.errors,
            "last_error": self.last_error,
            "error_groups": groups,
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
        p.interrupted = False
        p.started_at = time.time()
        p.finished_at = None
        p.files_total = 0
        p.files_done = 0
        p.files_changed = 0
        p.bytes_total = 0
        p.bytes_done = 0
        p.raw_events = 0
        p.rows = 0
        p.new_requests = 0
        p.errors = 0
        p.current_file = None
        p.current_source = None
        p.sources = {
            s.name: SourceProgress(name=s.name, label=s.label) for s in self.sources
        }
        # Both are per-pass, like the counters they describe: carrying samples
        # across a reset would rate a fresh pass against the old event count.
        p.reset_diagnostics()

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
                p.interrupted = True
                p.phase = "tailing"
                p.finished_at = time.time()
                return p
            try:
                units = source.plan(self.store)
            except Exception as exc:  # a broken client must not stop the others
                p.note_error(f"{source.name}: {exc}", source=source.name)
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
                    p.interrupted = True
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
                    p.note_error(str(exc), source=source.name, file=unit.key)
                    p.files_done += 1
                    slice_.files_done += 1
                    p.note_sample()
                    if on_progress:
                        on_progress(p)
                    continue

                p.files_done += 1
                p.bytes_done += result.bytes_read
                p.raw_events += result.raw_events
                p.rows += result.rows
                p.new_requests += result.new_requests
                slice_.files_done += 1
                slice_.bytes_done += result.bytes_read
                slice_.raw_events += result.raw_events
                slice_.rows += result.rows
                slice_.new_requests += result.new_requests
                if result.error:
                    p.note_error(str(result.error), source=source.name, file=unit.key)
                p.note_sample()
                if on_progress:
                    on_progress(p)

        p.current_file = None
        p.current_source = None
        p.phase = "tailing"
        p.finished_at = time.time()
        if on_progress:
            on_progress(p)
        return p
