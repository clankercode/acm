"""OpenCode history, which lives in SQLite rather than in files.

That changes how the incremental cursor works but not what it means. Instead of
a byte offset there is a high-water mark on ``message.time_updated``; instead of
appending, OpenCode edits rows in place as a response completes. Ranking by
``time_updated`` makes the newest revision of a row win, which is the same
mechanism Claude Code's streaming rewrites use.

The database is opened read-only. Doing so still reads the write-ahead log, so
a session in progress is visible; it just cannot be modified.

OpenCode's token accounting is its own again: ``input`` excludes cache traffic,
and ``reasoning`` is a sibling of ``output`` rather than a subset of it. Since
reasoning tokens bill as output, they are added in.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ..store import Store
from .base import ParseOutput, Source, Unit, UnitResult, request_row, session_row

#: Read in slices so a very large history cannot build one enormous batch.
BATCH_ROWS = 5_000


class OpenCodeSource(Source):
    name = "opencode"
    label = "OpenCode"

    def __init__(self, db_path: Path):
        self.db_path = db_path
        # The -wal and -shm siblings change on every write, so watching the
        # directory catches activity the main file alone would not.
        self.watch_roots = (db_path.parent,) if db_path.parent.exists() else ()

    def available(self) -> bool:
        return self.db_path.exists()

    @property
    def cursor_key(self) -> str:
        return f"opencode:{self.db_path}"

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def plan(self, store: Store) -> list[Unit]:
        row = store.one("SELECT * FROM files WHERE path = ?", (self.cursor_key,))
        since = int(row["offset"]) if row else 0
        try:
            conn = self._open()
        except sqlite3.Error:
            return []
        try:
            # `>=` rather than `>` so a row updated within the same millisecond
            # as the cursor is re-read. Re-reading is free; missing is not.
            pending = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(data)), 0) AS bytes"
                " FROM message WHERE time_updated >= ?",
                (since,),
            ).fetchone()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        if not pending["n"]:
            return []
        return [
            Unit(
                key=self.cursor_key,
                pending_bytes=int(pending["bytes"]),
                payload=(since, int(pending["n"])),
            )
        ]

    def ingest(self, store: Store, unit: Unit) -> UnitResult:
        since, _ = unit.payload  # type: ignore[misc]
        try:
            conn = self._open()
        except sqlite3.Error as exc:
            return UnitResult(error=str(exc))

        try:
            sessions = {r["id"]: r for r in conn.execute("SELECT * FROM session")}
            out = ParseOutput()
            high_water = since
            rows = conn.execute(
                "SELECT id, session_id, time_created, time_updated, data FROM message"
                " WHERE time_updated >= ? ORDER BY time_updated LIMIT ?",
                (since, BATCH_ROWS),
            ).fetchall()
        except sqlite3.Error as exc:
            conn.close()
            return UnitResult(error=str(exc))
        conn.close()

        out.rows = len(rows)
        for row in rows:
            out.bytes_read += len(row["data"] or "")
            high_water = max(high_water, int(row["time_updated"] or 0))
            try:
                data = json.loads(row["data"])
            except ValueError:
                out.flag("parse_error")
                continue
            if data.get("role") != "assistant":
                continue
            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                continue
            out.raw_events += 1

            cache = tokens.get("cache") or {}
            cache_read = int(cache.get("read") or 0)
            cache_write = int(cache.get("write") or 0)
            fresh = int(tokens.get("input") or 0)
            output = int(tokens.get("output") or 0)
            reasoning = int(tokens.get("reasoning") or 0)

            provider = data.get("providerID")
            model = data.get("modelID")
            if model and provider:
                model = f"{provider}/{model}"

            timing = data.get("time") or {}
            ts = int(timing.get("created") or row["time_created"] or 0)
            if not ts:
                continue

            out.requests.append(
                request_row(
                    self.name,
                    row["id"],
                    ts=ts,
                    # Rows are mutable, so the latest revision is the true one.
                    rank=int(row["time_updated"] or 0),
                    session_id=row["session_id"],
                    rollout_id=f"opencode:{row['session_id']}",
                    model=model,
                    input_tokens=fresh + cache_read + cache_write,
                    cached_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    # Reasoning is billed as output but reported separately, so
                    # it has to be folded in rather than treated as a subset.
                    output_tokens=output + reasoning,
                    reasoning_tokens=reasoning,
                    client_cost=data.get("cost"),
                )
            )

        # In the steady state this source re-reads exactly the boundary row on
        # every pass, by design -- `>=` is what stops a row written in the same
        # millisecond as the cursor from being lost. Committing an unchanged
        # cursor and unchanged session rows each time would then be a database
        # write every poll for the life of the process, so only write when the
        # high-water mark actually moved.
        if high_water == since:
            return UnitResult(
                raw_events=out.raw_events, bytes_read=out.bytes_read, rows=out.rows
            )

        written = store.write_batch(
            requests=out.requests,
            events=[],
            sessions=[self._session_row(meta) for meta in sessions.values()],
            anomalies=out.anomalies,
        )
        store.upsert_file(
            {
                "path": self.cursor_key,
                "source": self.name,
                "size": self.db_path.stat().st_size if self.db_path.exists() else 0,
                # `offset` doubles as the high-water mark on time_updated. It is
                # never read as a byte position for this source.
                "offset": high_water,
                "raw_events": self._raw_events(store),
                "status": "ok",
                "error": None,
                "scanned_at": time.time(),
            }
        )
        return UnitResult(
            raw_events=out.raw_events,
            bytes_read=out.bytes_read,
            new_requests=written.get("requests", 0),
            rows=out.rows,
        )

    def _raw_events(self, store: Store) -> int:
        """How many raw records this source has contributed.

        Recomputed rather than incremented: this cursor deliberately re-reads
        rows on the millisecond boundary, so an accumulating counter would drift
        upward on every pass and quietly inflate the replay ratio. One row here
        is one request, so the stored count is the honest figure.
        """
        row = store.one(
            "SELECT COUNT(*) AS n FROM requests WHERE source = ?", (self.name,)
        )
        return int(row["n"]) if row else 0

    def _session_row(self, meta: sqlite3.Row) -> tuple:
        model = meta["model"]
        if model:
            try:
                spec = json.loads(model)
                model = f"{spec.get('providerID')}/{spec.get('id')}"
            except (ValueError, AttributeError):
                pass
        return session_row(
            f"opencode:{meta['id']}",
            self.name,
            session_id=meta["id"],
            parent_thread_id=meta["parent_id"],
            path=str(self.db_path),
            first_ts=meta["time_created"],
            last_ts=meta["time_updated"],
            cwd=meta["directory"],
            cli_version=meta["version"],
            thread_source="subagent" if meta["parent_id"] else "main",
            agent_role=meta["agent"],
            agent_nickname=meta["slug"],
            depth=1 if meta["parent_id"] else 0,
            is_subagent=bool(meta["parent_id"]),
        )
