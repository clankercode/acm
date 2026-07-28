"""Hermes agent history in SQLite.

Hermes (by Nous Research) keeps pre-aggregated per-session, per-model usage in a
table rather than per-request rows. Each ``session_model_usage`` entry folds
together the ``api_call_count`` API calls a session made against one model, so
the row here is a session+model aggregate -- coarser than every other source,
which each record one real API request.

That difference is the reason costing stays in the short tier here. A session's
input_tokens is the sum of every prompt it sent, which is millions; letting that
trip the long-context threshold would systematically over-charge, since no
individual request was that large. The per-request prompt size is unknowable
from this table, so the conservative choice -- short-tier rates only -- is also
the honest one. The ``multi_call_turn`` anomaly flags how many calls were folded
together, exactly the way Grok's does, so the Data Quality panel can say so.

The database is opened read-only; doing so still reads the write-ahead log, so a
session in progress is visible without being modified.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..store import Store
from .base import ParseOutput, Source, Unit, UnitResult, request_row, session_row

#: Read in slices so a very large history cannot build one enormous batch.
BATCH_ROWS = 5_000


def normalize_model(raw: str | None) -> str | None:
    """Strip a Hermes routing suffix: ``glm-5.2:cloud`` -> ``glm-5.2``.

    Everything from the first ``:`` onward is a routing/profile tag, not part of
    the model id that the rate table keys on.
    """
    if not raw:
        return None
    return raw.split(":", 1)[0] or None


class HermesSource(Source):
    name = "hermes"
    label = "Hermes"

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.watch_roots = (db_path.parent,) if db_path.parent.exists() else ()

    def available(self) -> bool:
        return self.db_path.exists()

    @property
    def cursor_key(self) -> str:
        return f"hermes:{self.db_path}"

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def plan(self, store: Store) -> list[Unit]:
        row = store.one("SELECT * FROM files WHERE path = ?", (self.cursor_key,))
        since = float(row["offset"]) if row and row["offset"] else 0.0
        try:
            conn = self._open()
        except sqlite3.Error:
            return []
        try:
            pending = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS bytes"
                " FROM session_model_usage WHERE last_seen >= ?",
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
            sessions = {
                r["id"]: r for r in conn.execute("SELECT * FROM sessions")
            }
            out = ParseOutput()
            high_water = since
            rows = conn.execute(
                "SELECT session_id, model, billing_provider, billing_base_url,"
                " billing_mode, task,"
                " api_call_count, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, reasoning_tokens,"
                " estimated_cost_usd, actual_cost_usd, first_seen, last_seen"
                " FROM session_model_usage"
                " WHERE last_seen >= ? ORDER BY last_seen LIMIT ?",
                (since, BATCH_ROWS),
            ).fetchall()
        except sqlite3.Error as exc:
            conn.close()
            return UnitResult(error=str(exc))
        conn.close()

        out.rows = len(rows)
        for row in rows:
            last_seen = float(row["last_seen"] or 0.0)
            high_water = max(high_water, last_seen)
            out.bytes_read += 1
            out.raw_events += 1

            fresh = int(row["input_tokens"] or 0)
            cache_read = int(row["cache_read_tokens"] or 0)
            cache_write = int(row["cache_write_tokens"] or 0)
            output = int(row["output_tokens"] or 0)
            reasoning = int(row["reasoning_tokens"] or 0)
            calls = int(row["api_call_count"] or 1)

            model = normalize_model(row["model"])

            # A session folds together `calls` API calls. The surplus is counted
            # as an anomaly so the Data Quality panel can say so -- the same
            # treatment Grok's multi-call turns get.
            if calls > 1:
                out.flag("multi_call_turn", calls - 1)

            session_id = row["session_id"] or ""
            model_key = row["model"] or "unknown"
            provider = row["billing_provider"] or ""
            base_url = row["billing_base_url"] or ""
            mode = row["billing_mode"] or ""
            # Dedup key: Hermes' own 6-column composite identity, stable
            # across re-reads.  All six are needed — the table has two rows
            # that differ only in billing_base_url (local vs cloud).
            dk = f"{session_id}|{model_key}|{provider}|{base_url}|{mode}|{row['task'] or ''}"
            ts = int(float(row["first_seen"] or last_seen) * 1000)

            cost = row["actual_cost_usd"]
            client_cost = float(cost) if cost is not None and cost > 0 else None

            out.requests.append(
                request_row(
                    self.name,
                    dk,
                    # Rows are mutable; rank by last_seen so the newest revision wins.
                    rank=int(last_seen * 1000),
                    ts=ts,
                    session_id=session_id,
                    rollout_id=f"hermes:{session_id}",
                    model=model,
                    input_tokens=fresh + cache_read + cache_write,
                    cached_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    # Reasoning bills as output but is reported separately here.
                    output_tokens=output + reasoning,
                    reasoning_tokens=reasoning,
                    client_cost=client_cost,
                )
            )

        # Skip the write only when the cursor didn't move AND we have no
        # rows to emit.  On a first pass (since == 0.0) every row passes
        # the WHERE filter; if high_water stays 0.0 because all rows have
        # NULL/zero last_seen, we must still write them.
        if high_water == since and not out.requests:
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
        """How many usage rows this source has contributed (recomputed, not
        accumulated, to stay correct under the boundary re-read)."""
        row = store.one(
            "SELECT COUNT(*) AS n FROM requests WHERE source = ?", (self.name,)
        )
        return int(row["n"]) if row else 0

    def _session_row(self, meta: sqlite3.Row) -> tuple:
        session_id = meta["id"]
        parent = meta["parent_session_id"]
        started = int(float(meta["started_at"] or 0) * 1000) if meta["started_at"] else None
        ended = int(float(meta["ended_at"] or 0) * 1000) if meta["ended_at"] else None
        return session_row(
            f"hermes:{session_id}",
            self.name,
            session_id=session_id,
            parent_thread_id=f"hermes:{parent}" if parent else None,
            path=str(self.db_path),
            first_ts=started,
            last_ts=ended,
            cwd=meta["cwd"],
            git_branch=meta["git_branch"],
            thread_source="subagent" if parent else "main",
            depth=1 if parent else 0,
            is_subagent=bool(parent),
        )
