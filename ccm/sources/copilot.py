"""GitHub Copilot CLI history in SQLite.

Copilot records per-request usage in ``assistant_usage_events``, one row per API
call, with an autoincrement ``id`` that is a clean monotonic cursor. The token
convention is OpenAI-style -- the same one Grok and Codex use -- which the
planning spike confirmed against the row's own ``token_details_json``: the
``input_tokens`` column is the whole prompt with ``cache_read_tokens`` inside it
(row id=2: input 32478 = fresh 4318 + cache_read 28160), and ``reasoning_tokens``
is a subset of ``output_tokens`` rather than an addend. Nothing is reassembled.

Cache writes are reported but GitHub does not bill for them, so they pass
through as-is. The database is opened read-only.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..store import Store
from .base import ParseOutput, Source, Unit, UnitResult, parse_ts, request_row, session_row

#: Read in slices so a very large history cannot build one enormous batch.
BATCH_ROWS = 5_000


class CopilotSource(Source):
    name = "copilot"
    label = "Copilot CLI"

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.watch_roots = (db_path.parent,) if db_path.parent.exists() else ()

    def available(self) -> bool:
        return self.db_path.exists()

    @property
    def cursor_key(self) -> str:
        return f"copilot:{self.db_path}"

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def plan(self, store: Store) -> list[Unit]:
        row = store.one("SELECT * FROM files WHERE path = ?", (self.cursor_key,))
        since = int(row["offset"]) if row and row["offset"] else 0
        try:
            conn = self._open()
        except sqlite3.Error:
            return []
        try:
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM assistant_usage_events WHERE id >= ?",
                (since,),
            ).fetchone()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        if not pending["n"]:
            return []
        # Bytes is a rough proxy here; the table has no row-size column.
        return [
            Unit(
                key=self.cursor_key,
                pending_bytes=int(pending["n"]),
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
            sessions = {r["id"]: r for r in conn.execute("SELECT * FROM sessions")}
            out = ParseOutput()
            high_water = since
            rows = conn.execute(
                "SELECT id, session_id, model, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, reasoning_tokens,"
                " reasoning_effort, created_at"
                " FROM assistant_usage_events WHERE id >= ? ORDER BY id LIMIT ?",
                (since, BATCH_ROWS),
            ).fetchall()
        except sqlite3.Error as exc:
            conn.close()
            return UnitResult(error=str(exc))
        conn.close()

        out.rows = len(rows)
        for row in rows:
            high_water = max(high_water, int(row["id"] or 0))
            out.bytes_read += 1
            out.raw_events += 1

            # OpenAI convention, verified against token_details_json:
            # input_tokens is the whole prompt with cache_read inside it;
            # reasoning is a subset of output, not an addend. No reassembly.
            input_tokens = int(row["input_tokens"] or 0)
            cache_read = int(row["cache_read_tokens"] or 0)
            cache_write = int(row["cache_write_tokens"] or 0)
            output = int(row["output_tokens"] or 0)
            reasoning = int(row["reasoning_tokens"] or 0)
            if reasoning > output:
                out.flag("reasoning_gt_output")

            session_id = row["session_id"] or ""
            ts = parse_ts(row["created_at"]) or 0

            out.requests.append(
                request_row(
                    self.name,
                    str(row["id"]),
                    ts=ts,
                    session_id=session_id,
                    rollout_id=f"copilot:{session_id}",
                    model=row["model"],
                    effort=row["reasoning_effort"],
                    input_tokens=input_tokens,
                    cached_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    output_tokens=output,
                    reasoning_tokens=reasoning,
                )
            )

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
                # `offset` doubles as the high-water mark on the autoincrement id.
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
        row = store.one(
            "SELECT COUNT(*) AS n FROM requests WHERE source = ?", (self.name,)
        )
        return int(row["n"]) if row else 0

    def _session_row(self, meta: sqlite3.Row) -> tuple:
        session_id = meta["id"]
        return session_row(
            f"copilot:{session_id}",
            self.name,
            session_id=session_id,
            path=str(self.db_path),
            cwd=meta["cwd"],
            git_repo=meta["repository"],
            git_branch=meta["branch"],
            thread_source="main",
        )
