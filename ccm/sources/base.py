"""The contract every client reader implements, and the parts they share.

All but one of the clients store history as append-only JSONL, so the
byte-offset tailing, the partial-line handling and the truncation detection all
live here rather than being written out once per client. The exception
(OpenCode) keeps a SQLite database and supplies its own :meth:`Source.plan` /
:meth:`Source.ingest` against the same interface.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..pricing import split_model
from ..store import Store

READ_CHUNK = 8 << 20

_EPOCH = datetime(1970, 1, 1)


def parse_ts(raw: str | int | float | None) -> int | None:
    """Epoch milliseconds from an ISO-8601 string or a numeric timestamp.

    Clients mix the two freely -- Pi writes ISO on the envelope and epoch
    milliseconds inside the message it wraps.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        # Anything this small is seconds; real millisecond stamps are ~1.7e12.
        return int(value * 1000) if value < 1e11 else int(value)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return int((dt - _EPOCH).total_seconds() * 1000)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# project labelling


_project_cache: dict[str, str] = {}


def project_label(cwd: str | None, git_repo: str | None = None) -> str:
    """A short project name that means the same thing across clients.

    Only Codex records a git remote, and even it records two different ones for
    the same working tree, so remotes cannot be the join key. The working tree
    can: every client records a cwd. This walks up to the nearest ancestor
    holding a ``.git`` and names that, which also folds requests made from a
    subdirectory into the project they belong to.
    """
    if not cwd:
        if git_repo:
            return Path(git_repo.rstrip("/")).name.removesuffix(".git") or "unknown"
        return "unknown"
    cached = _project_cache.get(cwd)
    if cached is not None:
        return cached

    home = str(Path.home())
    path = Path(cwd)
    root: Path | None = None
    for candidate in (path, *path.parents):
        if str(candidate) == home or candidate == candidate.parent:
            break
        try:
            if (candidate / ".git").exists():
                root = candidate
                break
        except OSError:
            break
    if root is None:
        root = path
    label = "~" if str(root) == home else (root.name or str(root))
    _project_cache[cwd] = label
    return label


# ---------------------------------------------------------------------------
# canonical rows


#: Bind order matches ``ccm.store.REQUEST_COLUMNS``; the tests assert that.
def request_row(
    source: str,
    dk: str,
    *,
    ts: int,
    rank: int = 0,
    session_id: str | None = None,
    cum: tuple | None = None,
    rollout_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    input_tokens: int = 0,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    ctx_window: int | None = None,
    client_cost: float | None = None,
    rate_limit: tuple | None = None,
) -> tuple:
    """Build one ``requests`` row.

    ``input_tokens`` must already be the whole prompt, with ``cached_tokens``
    and the two cache-write counts as subsets of it. Normalising here rather
    than at query time means every consumer -- rollup, chart, invariant test --
    sees one convention.
    """
    provider, base = split_model(model)
    c = cum or (None, None, None, None)
    rl = rate_limit or (None, None, None, None, None)
    return (
        source,
        dk,
        rank,
        ts,
        session_id,
        c[0],
        c[1],
        c[2],
        c[3],
        rollout_id,
        model,
        base or None,
        provider,
        effort,
        input_tokens,
        cached_tokens,
        cache_write_tokens,
        cache_write_1h_tokens,
        output_tokens,
        reasoning_tokens,
        ctx_window,
        client_cost,
        rl[0],
        rl[1],
        rl[2],
        rl[3],
        rl[4],
    )


def session_row(
    rollout_id: str,
    source: str,
    *,
    session_id: str | None = None,
    parent_thread_id: str | None = None,
    path: str | None = None,
    first_ts: int | None = None,
    last_ts: int | None = None,
    cwd: str | None = None,
    git_repo: str | None = None,
    git_branch: str | None = None,
    originator: str | None = None,
    cli_version: str | None = None,
    thread_source: str | None = None,
    agent_role: str | None = None,
    agent_nickname: str | None = None,
    depth: int | None = None,
    is_subagent: bool = False,
) -> tuple:
    """Build one ``sessions`` row, deriving the shared project label."""
    return (
        rollout_id,
        source,
        session_id,
        parent_thread_id,
        path,
        first_ts,
        last_ts,
        cwd,
        git_repo,
        git_branch,
        project_label(cwd, git_repo),
        originator,
        cli_version,
        thread_source,
        agent_role,
        agent_nickname,
        depth,
        1 if is_subagent else 0,
    )


# ---------------------------------------------------------------------------
# work units


@dataclass
class Unit:
    """One schedulable piece of work: usually a file, sometimes a database."""

    key: str
    pending_bytes: int
    payload: object = None


@dataclass
class UnitResult:
    """What ingesting one unit contributed."""

    raw_events: int = 0
    bytes_read: int = 0
    new_requests: int = 0
    error: str | None = None


@dataclass
class ParseOutput:
    """Rows produced from one unit, before they reach the store."""

    requests: list[tuple] = field(default_factory=list)
    events: list[tuple] = field(default_factory=list)
    session: tuple | None = None
    anomalies: dict[str, int] = field(default_factory=dict)
    raw_events: int = 0
    bytes_read: int = 0
    offset: int = 0
    error: str | None = None
    #: Extra ``files`` columns the parser needs back to resume mid-file.
    carry: dict = field(default_factory=dict)

    def flag(self, kind: str, count: int = 1) -> None:
        self.anomalies[kind] = self.anomalies.get(kind, 0) + count


class Source:
    """A client whose history can be scanned incrementally."""

    #: Stable identifier, stored on every row and shown in the UI.
    name: str = ""
    #: Human-facing label.
    label: str = ""
    #: What to watch for changes. Empty when the source is not file-backed.
    watch_roots: tuple[Path, ...] = ()

    def available(self) -> bool:
        raise NotImplementedError

    def plan(self, store: Store) -> list[Unit]:
        """List the units with work outstanding, cheapest information first."""
        raise NotImplementedError

    def ingest(self, store: Store, unit: Unit) -> UnitResult:
        """Consume one unit and commit whatever it produced."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# shared JSONL machinery

_should_stop: Callable[[], bool] | None = None


@contextmanager
def cancellation(should_stop: Callable[[], bool] | None) -> Iterator[None]:
    """Let a reader abandon the file it is part-way through.

    Checking only between files is not enough to shut down promptly: a cold
    pass can spend minutes inside a single large rollout, which is longer than
    shutdown will wait for this thread. One module-level hook covers every
    reader because the scan is single-threaded by design -- see
    :mod:`ccm.engine`, where one worker owns all writes.
    """
    global _should_stop
    previous = _should_stop
    _should_stop = should_stop
    try:
        yield
    finally:
        _should_stop = previous


def read_new_lines(
    path: Path, start_offset: int, feed: Callable[[bytes], None]
) -> tuple[int, int, str | None]:
    """Feed every complete line from ``start_offset`` onwards.

    Returns ``(bytes_read, new_offset, error)``. The offset advances only over
    bytes that formed complete lines, so a record still being written is re-read
    intact next pass rather than being parsed in half. Stopping early is safe
    for exactly the same reason: the offset covers what was actually fed, and
    the next pass resumes from there.
    """
    pending = b""
    bytes_read = 0
    error: str | None = None
    try:
        with path.open("rb") as fh:
            fh.seek(start_offset)
            while True:
                if _should_stop is not None and _should_stop():
                    break
                chunk = fh.read(READ_CHUNK)
                if not chunk:
                    break
                bytes_read += len(chunk)
                data = pending + chunk
                nl = data.rfind(b"\n")
                if nl == -1:
                    pending = data
                    continue
                block, pending = data[: nl + 1], data[nl + 1 :]
                for line in block.splitlines():
                    if line:
                        feed(line)
    except OSError as exc:
        error = str(exc)
    return bytes_read, start_offset + bytes_read - len(pending), error


class JsonlSource(Source):
    """A source backed by a tree of append-only JSONL files.

    Subclasses supply :meth:`iter_files` and :meth:`parse`; everything about
    cursors, growth, truncation and inode reuse is handled here.
    """

    def __init__(self, root: Path):
        self.root = root
        self.watch_roots = (root,)

    def available(self) -> bool:
        return self.root.exists()

    def iter_files(self) -> Iterator[Path]:
        raise NotImplementedError

    def parse(self, path: Path, cursor: dict | None, start_offset: int) -> ParseOutput:
        raise NotImplementedError

    def plan(self, store: Store) -> list[Unit]:
        cursors = store.file_cursors()
        units: list[Unit] = []
        for path in self.iter_files():
            try:
                st = path.stat()
            except OSError:
                continue
            row = cursors.get(str(path))
            cursor = dict(row) if row is not None else None
            offset = int(cursor["offset"]) if cursor else 0
            if cursor and (st.st_size < offset or cursor.get("inode") != st.st_ino):
                # Truncated or replaced: start over. Re-ingesting is harmless
                # because every write is an idempotent upsert.
                cursor = None
                offset = 0
            if cursor is not None and offset >= st.st_size:
                continue
            units.append(
                Unit(
                    key=str(path),
                    pending_bytes=max(st.st_size - offset, 0),
                    payload=(path, cursor, offset, st),
                )
            )
        return units

    def ingest(self, store: Store, unit: Unit) -> UnitResult:
        path, cursor, offset, st = unit.payload  # type: ignore[misc]
        out = self.parse(path, cursor, offset)
        prior_raw = int(cursor["raw_events"] or 0) if cursor else 0
        written = store.write_batch(
            requests=out.requests,
            events=out.events,
            session=out.session,
            file_state=self.file_state(
                path,
                out,
                size=st.st_size,
                inode=st.st_ino,
                mtime=st.st_mtime,
                raw_events=prior_raw + out.raw_events,
            ),
            anomalies=out.anomalies,
        )
        return UnitResult(
            raw_events=out.raw_events,
            bytes_read=out.bytes_read,
            new_requests=written.get("requests", 0),
            error=out.error,
        )

    def file_state(
        self,
        path: Path,
        out: ParseOutput,
        *,
        size: int,
        inode: int,
        mtime: float,
        raw_events: int,
    ) -> dict:
        """Cursor row for one file, including whatever the parser must resume with."""
        return {
            "path": str(path),
            "source": self.name,
            "inode": inode,
            "size": size,
            "mtime": mtime,
            "offset": out.offset,
            "raw_events": raw_events,
            "new_requests": 0,
            "status": "error" if out.error else "ok",
            "error": out.error,
            "scanned_at": time.time(),
            **out.carry,
        }


def walk_jsonl(root: Path, *, match: Callable[[str], bool] | None = None) -> list[Path]:
    """Every ``.jsonl`` under ``root``, sorted.

    Sorted order is not needed for correctness -- ingest is order-independent --
    but scanning oldest-first means the common case resolves duplicates on first
    sight.
    """
    if not root.exists():
        return []
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.name.endswith(".jsonl") and (match is None or match(entry.name)):
                found.append(Path(entry.path))
    return sorted(found)
