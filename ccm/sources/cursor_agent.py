"""cursor-agent (the Cursor CLI) debug logs.

cursor-agent is the command-line agent from Cursor, distinct from the Cursor
IDE. Unlike every other client here it persists no usage to its transcript
store -- but when run with debug logging it writes ``analytics.track`` events
to rotating session logs under ``/tmp/cursor-agent-logs-<uid>/``. Those events
are the only place per-request token counts survive, so this source reads them.

The log line format is *not* JSONL: each line is

    [2026-07-23T15:15:31.275Z] analytics.track {"eventName": "...", "props": {...}}

so the parser strips the bracketed timestamp prefix and takes the JSON from
the first ``{``. Only two event names carry anything useful:

* ``cli.request.create`` -- ``model``, ``mode``, ``invocationID``,
  ``conversationId`` (no tokens).
* ``cli.request.completed`` -- ``estimated_tokens``, ``invocationID``,
  ``conversationId``.

A request is emitted when the *completed* event arrives, joining the *create*
event on ``invocationID`` to recover the model. ``invocationID`` is the dedup
key.

Fidelity caveat -- this is a deliberately lower-grade source than the others.
``estimated_tokens`` is a single undifferentiated count. Checking it against
the matching ``cli.request.create`` confirms it is the turn's full input
context, not the new prompt: a create with ``length: 228`` (characters) is
followed by a completed with ``estimated_tokens: 78231``. So it maps cleanly
to ``input_tokens`` and keeps the ``total == input + output`` invariant whole,
but cursor-agent reports no output, cache-read or cache-write breakdown, so
those stay zero. Cost computed from these rows therefore prices the turn as if
it were all input (the cheaper tier) and under-counts -- acceptable for a
best-effort source, called out here so no one mistakes the number for precise.

The ``model`` from ``create`` is frequently the literal ``"default"`` (the
configured model alias) which carries no rate; real model names such as
``grok-4.5`` or ``claude-opus-5`` do appear and price normally when present.

Durability -- ``/tmp`` rotates these logs (a newest-50 cap and ~7-day window),
so reading ``/tmp`` directly would lose history on the next rescan. Instead the
source keeps a stable mirror under ``~/.cache/ccm/cursor-logs/`` and reads that.
The mirror is refreshed by :func:`capture_live_logs`, which runs at the top of
:meth:`CursorAgentSource.plan` -- so no external daemon or hook is required.
The capture is driven by CCM's poll fallback (every ``poll_seconds``, default
2s): the inotify watcher only fires on ``.jsonl``/``.db`` files, and
cursor-agent logs are ``.log``, so the live dir is in :attr:`watch_roots` for
completeness but does not itself trigger a scan. An optional cursor-agent
``sessionEnd`` hook (see docs) closes the latency gap further for anyone who
wants closer-to-realtime capture, but nothing depends on it. Capture is
idempotent and inode-stable (it truncates-and-rewrites an existing mirror in
place, so the byte-offset cursor is not reset), and re-ingestion is harmless
regardless because every write is an upsert keyed on ``invocationID``.

The create→completed join is scoped per-file: a completion whose create landed
in a *different* session log resolves ``model = None``. In practice create and
completed for one invocation always co-locate (same session, same process), so
this is not a data-loss path, but it is a deliberate scoping choice noted here
so it is not mistaken for a global join.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import orjson

from .base import (
    JsonlSource,
    ParseOutput,
    parse_ts,
    read_new_lines,
    request_row,
    session_row,
)

#: Where cursor-agent writes rotating debug logs. One directory per uid.
LIVE_ROOT = Path("/tmp")

#: Cheap pre-filter: only analytics lines about a request can yield usage.
_MARKER = b"analytics.track"
_MARKER_REQ = b"cli.request."

#: Only these two event names carry request material.
_EV_CREATE = "cli.request.create"
_EV_COMPLETED = "cli.request.completed"


def live_session_logs(live_root: Path = LIVE_ROOT) -> list[Path]:
    """Every ``session-*.log`` under ``<live_root>/cursor-agent-logs-*``.

    ``latest.log`` is a symlink to the newest session (already covered) and
    ``oom`` is unrelated, so both are excluded by the name prefix.
    """
    found: list[Path] = []
    for d in live_root.glob("cursor-agent-logs-*"):
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if (
                p.is_file()
                and p.name.startswith("session-")
                and p.name.endswith(".log")
            ):
                found.append(p)
    return sorted(found)


def capture_live_logs(cache_dir: Path, live_root: Path = LIVE_ROOT) -> int:
    """Mirror new/changed cursor-agent session logs into ``cache_dir``.

    Returns the number of files copied. A file is copied only when the live
    copy is larger than the mirror, so a quiet, already-captured session costs
    a couple of cheap ``stat`` calls. The copy truncates-and-rewrites an
    existing mirror in place, which preserves its inode -- the byte-offset
    cursor therefore advances rather than resetting on every growth.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for live in live_session_logs(live_root):
        try:
            live_size = live.stat().st_size
        except OSError:
            continue
        # Prefix the uid dir name to avoid collisions across per-uid dirs
        # (each process restarts its session counter, so two uid dirs can
        # produce a session-<ts>-<n>.log with the same basename).
        dest = cache_dir / f"{live.parent.name}_{live.name}"
        try:
            dest_size = dest.stat().st_size if dest.exists() else -1
        except OSError:
            dest_size = -1
        if dest_size >= live_size:
            continue
        try:
            # copyfile opens an existing dest with 'wb' (truncate-in-place),
            # keeping the inode so the scanner's cursor is not invalidated.
            shutil.copyfile(live, dest)
            copied += 1
        except OSError:
            continue
    return copied


def _ts_from_prefix(line: bytes) -> int | None:
    """Epoch milliseconds from the ``[ISO8601]`` prefix, or None."""
    end = line.find(b"]")
    if end <= 0:
        return None
    return parse_ts(line[1:end].decode(errors="ignore"))


class CursorAgentParser:
    """Stateful parser for one cursor-agent session log.

    Buffers ``cli.request.create`` events (model/mode by ``invocationID``) and
    emits a request when the matching ``cli.request.completed`` arrives. The
    buffer is carried in the file cursor so a resumed scan still resolves
    completions whose create landed before the saved offset.
    """

    source = "cursor_agent"

    def __init__(self, path: Path, cursor: dict | None = None):
        self.path = path
        cursor = cursor or {}
        self.session_id: str = cursor.get("session_id") or path.stem
        self.rollout_id: str = (
            cursor.get("rollout_id") or f"cursor_agent:{self.session_id}"
        )
        carry: dict = {}
        if cursor.get("carry"):
            try:
                carry = json.loads(cursor["carry"])
            except (ValueError, TypeError):
                carry = {}
        self.models: dict[str, dict] = dict(carry.get("models") or {})
        self.conversation_id: str | None = carry.get("conversation_id")
        self.first_ts: int | None = cursor.get("first_ts")
        self.last_ts: int | None = cursor.get("last_ts")

    def _note_ts(self, ts: int | None, out: ParseOutput) -> None:
        if ts is None:
            return
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

    def feed(self, line: bytes, out: ParseOutput) -> None:
        if _MARKER not in line or _MARKER_REQ not in line:
            return
        brace = line.find(b"{")
        if brace == -1:
            return
        try:
            record = orjson.loads(line[brace:])
        except orjson.JSONDecodeError:
            out.flag("parse_error")
            return

        name = record.get("eventName")
        if name not in (_EV_CREATE, _EV_COMPLETED):
            return
        props = record.get("props")
        if not isinstance(props, dict):
            return
        inv = props.get("invocationID")
        if not inv:
            return

        ts = _ts_from_prefix(line)
        cid = props.get("conversationId")
        if cid:
            self.conversation_id = cid

        if name == _EV_CREATE:
            self._note_ts(ts, out)
            # Remember the model/mode until the completion arrives. Overwriting
            # is harmless: a request is created at most once per invocation.
            self.models[inv] = {
                "model": props.get("model"),
                "mode": props.get("mode"),
            }
            return

        # cli.request.completed -- this is where tokens live.
        self._note_ts(ts, out)
        out.raw_events += 1
        meta = self.models.pop(inv, None)
        model = meta["model"] if meta else None
        estimated = int(props.get("estimated_tokens") or 0)

        out.requests.append(
            request_row(
                self.source,
                str(inv),
                ts=ts if ts is not None else 0,
                session_id=self.session_id,
                rollout_id=self.rollout_id,
                model=model,
                # cursor-agent reports a single undifferentiated token count;
                # see module docstring for why it lands in input_tokens.
                input_tokens=estimated,
            )
        )

    def session_row(self) -> tuple:
        return session_row(
            self.rollout_id,
            self.source,
            session_id=self.session_id,
            path=str(self.path),
            first_ts=self.first_ts,
            last_ts=self.last_ts,
            thread_source="main",
        )

    def carry_columns(self) -> dict:
        return {
            "carry": json.dumps(
                {
                    "models": self.models,
                    "conversation_id": self.conversation_id,
                }
            ),
            "rollout_id": self.rollout_id,
            "session_id": self.session_id,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


class CursorAgentSource(JsonlSource):
    """cursor-agent usage, read from a stable mirror of its ``/tmp`` logs."""

    name = "cursor_agent"
    label = "cursor-agent"

    def __init__(
        self,
        cache_dir: Path,
        live_root: Path = LIVE_ROOT,
        *,
        capture_interval: float = 3600.0,
    ):
        super().__init__(cache_dir)
        self.live_root = live_root
        self.capture_interval = capture_interval
        #: monotonic timestamp of the last capture; None = never (run now).
        self._last_capture: float | None = None
        # The live dir is in watch_roots for completeness, but the inotify
        # watcher only fires on .jsonl/.db files, so the poll fallback (every
        # poll_seconds) is what actually drives capture. The capture itself is
        # throttled to once per capture_interval to avoid stat-storming /tmp.
        self.watch_roots = (cache_dir, live_root)

    def available(self) -> bool:
        # Available once there is anything to read: either a prior capture has
        # populated the cache, or the live logs exist to be captured.
        if self.root.exists() and any(
            f for f in self.root.iterdir() if "session-" in f.name
        ):
            return True
        return bool(live_session_logs(self.live_root))

    def iter_files(self):
        # Mirror files are named ``<uid_dir>_session-*.log`` (capture_live_logs
        # prefixes the uid dir to avoid cross-uid collisions), but matching on
        # ``session-`` covers both prefixed mirrors and bare fixtures written
        # straight into the cache dir in tests. cursor-agent logs are ``.log``,
        # not ``.jsonl``, so walk_jsonl's extension filter would hide them.
        if not self.root.exists():
            return []
        found: list[Path] = []
        stack = [self.root]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif "session-" in entry.name and entry.name.endswith(".log"):
                    found.append(Path(entry.path))
        return sorted(found)

    def plan(self, store):
        # Self-contained capture: mirror any new/changed live logs into the
        # stable cache before planning over it. Throttled to once per
        # capture_interval (default 1h) because the logs change slowly --
        # running it every poll cycle (2s) would stat-storm /tmp for nothing.
        # First call runs immediately (None → run now).
        now = time.monotonic()
        if (
            self._last_capture is None
            or now - self._last_capture >= self.capture_interval
        ):
            try:
                capture_live_logs(self.root, self.live_root)
            except OSError:
                pass
            self._last_capture = now
        return super().plan(store)

    def parse(self, path: Path, cursor: dict | None, start_offset: int) -> ParseOutput:
        parser = CursorAgentParser(path, cursor)
        out = ParseOutput(offset=start_offset)
        out.bytes_read, out.offset, out.error, out.rows = read_new_lines(
            path, start_offset, lambda line: parser.feed(line, out)
        )
        out.session = parser.session_row()
        out.carry = parser.carry_columns()
        return out
