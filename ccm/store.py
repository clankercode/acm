"""SQLite schema and idempotent writes.

Every table here is derived state: deleting the database and rescanning
reproduces it exactly. Nothing is ever written back to any session corpus.

Four clients write history in four different shapes, and each one repeats
itself in its own way, so ingest is keyed on content rather than position and is
idempotent under arbitrary repetition and ordering. The shared contract is:

* A request is identified by ``(source, dk)``. Each reader mints ``dk`` from
  whatever its format guarantees to be stable and unique -- cumulative token
  counters for Codex, the API message id for Claude Code, the response id for
  Pi, the row id for OpenCode.
* ``rank`` decides which observation of a duplicate wins, highest first, with
  the earliest timestamp breaking ties. That one rule covers both duplication
  patterns: Codex replays whole histories with rewritten clocks and wants the
  earliest sighting (rank stays 0), while Claude Code writes a row per content
  block as a response streams in and wants the last, most complete one (rank
  counts output tokens).
* Non-token events are anchored to the preceding request's key, which inherits
  that same dedup correctness rather than inventing a second scheme.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

#: Bumped whenever the shape below changes -- or when the meaning of a value in
#: it does, as in 4, where the Codex dedup key gained the per-request usage.
#: Because everything here is derived, a mismatch is resolved by dropping and
#: rebuilding rather than by migrating.
SCHEMA_VERSION = "4"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Per-file scan cursor plus the parser state needed to resume mid-file.
-- Without the carry-over columns a resumed scan would not know which model the
-- next token_count belongs to. Sources with no files of their own (OpenCode)
-- keep a single synthetic row here holding their cursor.
CREATE TABLE IF NOT EXISTS files (
    path             TEXT PRIMARY KEY,
    source           TEXT NOT NULL DEFAULT 'codex',
    inode            INTEGER,
    size             INTEGER NOT NULL DEFAULT 0,
    mtime            REAL,
    offset           INTEGER NOT NULL DEFAULT 0,
    session_id       TEXT,
    rollout_id       TEXT,
    parent_thread_id TEXT,
    thread_source    TEXT,
    agent_role       TEXT,
    agent_nickname   TEXT,
    depth            INTEGER,
    cwd              TEXT,
    git_repo         TEXT,
    git_branch       TEXT,
    originator       TEXT,
    cli_version      TEXT,
    cur_model        TEXT,
    cur_effort       TEXT,
    carry            TEXT,
    first_ts         INTEGER,
    last_ts          INTEGER,
    raw_events       INTEGER NOT NULL DEFAULT 0,
    new_requests     INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'ok',
    error            TEXT,
    scanned_at       REAL
);

-- One row per real API request, after duplicate collapse.
-- Tokens only: cost is derived on read so a pricing edit needs no rescan.
--
-- input_tokens is always the WHOLE prompt. Clients disagree here -- Codex
-- reports a total that already includes cache hits, while Claude Code, Pi and
-- OpenCode report only the uncached remainder alongside separate cache
-- counters -- so each reader normalises to the total before it gets this far.
-- cached_tokens and the two cache-write columns are subsets of it.
--
-- The cum_* columns are Codex's dedup material, kept because they also carry
-- the per-lineage monotonicity check. Other sources leave them null.
CREATE TABLE IF NOT EXISTS requests (
    source           TEXT    NOT NULL,
    dk               TEXT    NOT NULL,
    rank             INTEGER NOT NULL DEFAULT 0,
    ts               INTEGER NOT NULL,
    session_id       TEXT,
    cum_in           INTEGER,
    cum_cached       INTEGER,
    cum_out          INTEGER,
    cum_reason       INTEGER,
    rollout_id       TEXT,
    model            TEXT,
    base_model       TEXT,
    provider         TEXT,
    effort           TEXT,
    input_tokens     INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,
    cache_write_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    ctx_window       INTEGER,
    -- What the client itself said the request cost, where it says so at all.
    -- Never used in any figure the dashboard reports; it exists purely so the
    -- Data Quality panel can hold our arithmetic up against the vendor's.
    client_cost      REAL,
    rl_used_percent  REAL,
    rl_window_minutes INTEGER,
    rl_resets_at     INTEGER,
    rl_limit_id      TEXT,
    rl_plan_type     TEXT,
    PRIMARY KEY (source, dk)
);

CREATE INDEX IF NOT EXISTS requests_ts       ON requests(ts);
CREATE INDEX IF NOT EXISTS requests_model_ts ON requests(model, ts);
CREATE INDEX IF NOT EXISTS requests_rollout  ON requests(rollout_id);

-- Notable non-token events, for overlaying on the time charts. Anchored to the
-- preceding request so duplicates collapse the same way requests do.
CREATE TABLE IF NOT EXISTS events (
    source     TEXT    NOT NULL,
    dk         TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    ordinal    INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    rollout_id TEXT,
    PRIMARY KEY (source, dk, kind, ordinal)
);

CREATE INDEX IF NOT EXISTS events_ts ON events(ts, kind);

-- One row per session file (or, for OpenCode, per session row): the dimension
-- table requests join against.
CREATE TABLE IF NOT EXISTS sessions (
    rollout_id       TEXT PRIMARY KEY,
    source           TEXT NOT NULL DEFAULT 'codex',
    session_id       TEXT,
    parent_thread_id TEXT,
    path             TEXT,
    first_ts         INTEGER,
    last_ts          INTEGER,
    cwd              TEXT,
    git_repo         TEXT,
    git_branch       TEXT,
    -- Normalised project label, shared across clients. git_repo above is the
    -- remote URL as the client recorded it, which is useless as a join key --
    -- the same working tree appears here under two different remotes, and only
    -- Codex records one at all. See ccm.sources.base.project_label.
    repo             TEXT,
    originator       TEXT,
    cli_version      TEXT,
    thread_source    TEXT,
    agent_role       TEXT,
    agent_nickname   TEXT,
    depth            INTEGER,
    is_subagent      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS sessions_repo ON sessions(repo);

-- Materialised hourly rollup. Holds token sums only; cost is applied on read.
-- long_ctx is part of the key so that costing a bucket at its tier's rate is
-- exactly equal to summing the per-request costs inside it.
-- `origin` is where the row came from: the empty string for this machine, an
-- import's label otherwise. Keeping imported data in the same table is what
-- lets every existing query treat another machine as one more dimension.
CREATE TABLE IF NOT EXISTS bucket_hour (
    hour             INTEGER NOT NULL,
    origin           TEXT    NOT NULL DEFAULT '',
    source           TEXT    NOT NULL,
    model            TEXT    NOT NULL,
    provider         TEXT    NOT NULL,
    base_model       TEXT    NOT NULL,
    repo             TEXT    NOT NULL,
    is_subagent      INTEGER NOT NULL,
    long_ctx         INTEGER NOT NULL,
    n                INTEGER NOT NULL,
    input_tokens     INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,
    cache_write_tokens    INTEGER NOT NULL,
    cache_write_1h_tokens INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    max_input        INTEGER NOT NULL,
    PRIMARY KEY (hour, origin, source, model, repo, is_subagent, long_ctx)
);

CREATE INDEX IF NOT EXISTS bucket_hour_hour ON bucket_hour(hour);
CREATE INDEX IF NOT EXISTS bucket_hour_origin ON bucket_hour(origin);

-- One row per imported bundle. `origin` is the label, unique, and is what the
-- imported bucket and session rows carry.
CREATE TABLE IF NOT EXISTS imports (
    origin         TEXT PRIMARY KEY,
    source_label   TEXT,
    machine        TEXT,
    exported_at    INTEGER,
    imported_at    INTEGER,
    tool_version   TEXT,
    bundle_version INTEGER,
    requests       INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    first_ts       INTEGER,
    last_ts        INTEGER,
    clients        TEXT,
    contributors   TEXT
);

-- Imported session rollups. Split by (model, long_ctx) exactly as the local
-- session query groups, so an imported session can still be re-costed against
-- whatever the rate table says later.
CREATE TABLE IF NOT EXISTS import_sessions (
    origin           TEXT    NOT NULL,
    rollout_id       TEXT    NOT NULL,
    model            TEXT    NOT NULL,
    long_ctx         INTEGER NOT NULL,
    source           TEXT,
    repo             TEXT,
    agent_role       TEXT,
    agent_nickname   TEXT,
    depth            INTEGER,
    is_subagent      INTEGER NOT NULL DEFAULT 0,
    first_ts         INTEGER,
    last_ts          INTEGER,
    n                INTEGER NOT NULL,
    input_tokens     INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,
    cache_write_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    PRIMARY KEY (origin, rollout_id, model, long_ctx)
);

-- Hours whose rollup is stale. Maintained by trigger so that a request whose
-- timestamp moves backwards into an earlier hour dirties both hours.
CREATE TABLE IF NOT EXISTS dirty_hours (hour INTEGER PRIMARY KEY);

-- These use NOT EXISTS rather than INSERT OR IGNORE deliberately: inside a
-- trigger, SQLite replaces the body's conflict-resolution algorithm with the
-- outer statement's, and the outer statement here is an upsert -- so OR IGNORE
-- would silently become ABORT and blow up the ingest.
CREATE TRIGGER IF NOT EXISTS requests_dirty_ai AFTER INSERT ON requests BEGIN
    INSERT INTO dirty_hours(hour) SELECT new.ts / 3600000
    WHERE NOT EXISTS (SELECT 1 FROM dirty_hours WHERE hour = new.ts / 3600000);
END;

CREATE TRIGGER IF NOT EXISTS requests_dirty_au AFTER UPDATE ON requests BEGIN
    INSERT INTO dirty_hours(hour) SELECT new.ts / 3600000
    WHERE NOT EXISTS (SELECT 1 FROM dirty_hours WHERE hour = new.ts / 3600000);
    INSERT INTO dirty_hours(hour) SELECT old.ts / 3600000
    WHERE NOT EXISTS (SELECT 1 FROM dirty_hours WHERE hour = old.ts / 3600000);
END;

CREATE TRIGGER IF NOT EXISTS requests_dirty_ad AFTER DELETE ON requests BEGIN
    INSERT INTO dirty_hours(hour) SELECT old.ts / 3600000
    WHERE NOT EXISTS (SELECT 1 FROM dirty_hours WHERE hour = old.ts / 3600000);
END;

-- Data-quality counters, incremented as anomalies are met during scanning.
CREATE TABLE IF NOT EXISTS anomalies (
    kind  TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);
"""

#: Columns written for each request, in bind order.
REQUEST_COLUMNS = (
    "source",
    "dk",
    "rank",
    "ts",
    "session_id",
    "cum_in",
    "cum_cached",
    "cum_out",
    "cum_reason",
    "rollout_id",
    "model",
    "base_model",
    "provider",
    "effort",
    "input_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "cache_write_1h_tokens",
    "output_tokens",
    "reasoning_tokens",
    "ctx_window",
    "client_cost",
    "rl_used_percent",
    "rl_window_minutes",
    "rl_resets_at",
    "rl_limit_id",
    "rl_plan_type",
)

_KEY_COLUMNS = ("source", "dk")
_UPDATABLE = tuple(c for c in REQUEST_COLUMNS if c not in _KEY_COLUMNS)

# The winning observation replaces the stored row outright -- not just its
# timestamp but its model and file attribution too, since a partial or replayed
# sighting is wrong about all of those in the same way.
#
# Rank first, then earliest timestamp. Readers that see the same request more
# than once in complete form leave rank at 0, which reduces this to "earliest
# wins" -- the rule Codex replay needs. A reader that sees a request grow as it
# streams sets rank to the growing quantity instead, and gets last-complete-wins
# without a second code path.
UPSERT_REQUEST = f"""
INSERT INTO requests ({", ".join(REQUEST_COLUMNS)})
VALUES ({", ".join("?" * len(REQUEST_COLUMNS))})
ON CONFLICT ({", ".join(_KEY_COLUMNS)}) DO UPDATE SET
    {", ".join(f"{c} = excluded.{c}" for c in _UPDATABLE)}
WHERE excluded.rank > requests.rank
   OR (excluded.rank = requests.rank AND excluded.ts < requests.ts)
"""

EVENT_COLUMNS = (
    "source",
    "dk",
    "kind",
    "ordinal",
    "ts",
    "rollout_id",
)

UPSERT_EVENT = f"""
INSERT INTO events ({", ".join(EVENT_COLUMNS)})
VALUES ({", ".join("?" * len(EVENT_COLUMNS))})
ON CONFLICT (source, dk, kind, ordinal)
DO UPDATE SET ts = excluded.ts, rollout_id = excluded.rollout_id
WHERE excluded.ts < events.ts
"""

SESSION_COLUMNS = (
    "rollout_id",
    "source",
    "session_id",
    "parent_thread_id",
    "path",
    "first_ts",
    "last_ts",
    "cwd",
    "git_repo",
    "git_branch",
    "repo",
    "originator",
    "cli_version",
    "thread_source",
    "agent_role",
    "agent_nickname",
    "depth",
    "is_subagent",
)

UPSERT_SESSION = f"""
INSERT INTO sessions ({", ".join(SESSION_COLUMNS)})
VALUES ({", ".join("?" * len(SESSION_COLUMNS))})
ON CONFLICT (rollout_id) DO UPDATE SET
    {", ".join(f"{c} = excluded.{c}" for c in SESSION_COLUMNS if c not in ("rollout_id", "first_ts", "last_ts"))},
    first_ts = MIN(COALESCE(sessions.first_ts, excluded.first_ts), COALESCE(excluded.first_ts, sessions.first_ts)),
    last_ts  = MAX(COALESCE(sessions.last_ts,  excluded.last_ts),  COALESCE(excluded.last_ts,  sessions.last_ts))
"""


#: Everything a rescan can rebuild from the local corpus.
DERIVED_TABLES = (
    "requests",
    "events",
    "sessions",
    "files",
    "bucket_hour",
    "dirty_hours",
    "anomalies",
)

#: Imported data. Not derived from anything on this machine, so a rescan must
#: leave it alone -- only a schema change, which cannot be migrated, drops it.
IMPORT_TABLES = ("imports", "import_sessions")


def _reset_if_stale(conn: sqlite3.Connection) -> bool:
    """Drop derived tables when the schema shape has moved on.

    Nothing here is a source of truth, so a version bump is cheaper to satisfy
    by rebuilding than by migrating -- and a rebuild cannot leave the database
    in a half-converted state that only shows up as wrong numbers later.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    stored = row[0] if row else None
    if stored == SCHEMA_VERSION:
        return False
    for table in DERIVED_TABLES + IMPORT_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("DELETE FROM meta WHERE key = 'bucket_fingerprint'")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    return stored is not None


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a tuned connection, creating the schema if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if read_only and path.exists():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")
        _reset_if_stale(conn)
        conn.executescript(SCHEMA)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class Store:
    """Thread-safe wrapper around the derived-state database.

    A single writer connection guarded by a lock is plenty here: ingest happens
    on one scanner thread and reads are short.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = connect(path)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- generic helpers ---------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- ingest ------------------------------------------------------------

    def write_batch(
        self,
        *,
        requests: Sequence[tuple],
        events: Sequence[tuple],
        session: tuple | None = None,
        sessions: Sequence[tuple] = (),
        file_state: dict | None = None,
        anomalies: dict[str, int] | None = None,
    ) -> dict[str, int]:
        """Commit one file's worth of parsed output atomically.

        Grouping the file cursor update into the same transaction as its rows is
        what makes an interrupted scan safe to resume: either the offset moved
        and the rows landed, or neither happened.

        Returns how many rows each upsert actually touched. For requests that is
        new rows plus rows whose timestamp was corrected earlier by a fresh
        observation -- in other words, everything the replay filter let through.
        """
        touched = {"requests": 0, "events": 0}
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                if requests:
                    # The dirty-hour triggers fire inside this statement and
                    # count toward total_changes, so discount them to leave the
                    # true request-row count.
                    before = conn.total_changes
                    dirty_before = conn.execute(
                        "SELECT COUNT(*) AS c FROM dirty_hours"
                    ).fetchone()["c"]
                    conn.executemany(UPSERT_REQUEST, requests)
                    dirty_after = conn.execute(
                        "SELECT COUNT(*) AS c FROM dirty_hours"
                    ).fetchone()["c"]
                    touched["requests"] = (
                        conn.total_changes - before - (dirty_after - dirty_before)
                    )
                if events:
                    before = conn.total_changes
                    conn.executemany(UPSERT_EVENT, events)
                    touched["events"] = conn.total_changes - before
                if session is not None:
                    conn.execute(UPSERT_SESSION, session)
                if sessions:
                    conn.executemany(UPSERT_SESSION, sessions)
                if anomalies:
                    conn.executemany(
                        "INSERT INTO anomalies(kind, count) VALUES (?, ?) "
                        "ON CONFLICT(kind) DO UPDATE SET count = count + excluded.count",
                        list(anomalies.items()),
                    )
                if file_state is not None:
                    self._upsert_file_locked(file_state)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return touched

    def _upsert_file_locked(self, state: dict) -> None:
        cols = list(state)
        placeholders = ", ".join("?" * len(cols))
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "path")
        self._conn.execute(
            f"INSERT INTO files ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(path) DO UPDATE SET {updates}",
            [state[c] for c in cols],
        )

    def upsert_file(self, state: dict) -> None:
        with self._lock:
            self._upsert_file_locked(state)

    def file_cursors(self) -> dict[str, sqlite3.Row]:
        return {row["path"]: row for row in self.query("SELECT * FROM files")}

    def reset_file(self, path: str) -> None:
        """Rewind a file to be re-read from the start.

        Safe at any time: re-ingesting rows that already exist is a no-op
        because every write is an idempotent upsert.
        """
        self.execute(
            "UPDATE files SET offset = 0, carry = NULL, raw_events = 0, "
            "new_requests = 0 WHERE path = ?",
            (path,),
        )

    # -- dirty hour bookkeeping -------------------------------------------

    def take_dirty_hours(self, limit: int | None = None) -> list[int]:
        """Claim the stale hours, clearing them from the queue."""
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                sql = "SELECT hour FROM dirty_hours ORDER BY hour"
                if limit:
                    sql += f" LIMIT {int(limit)}"
                hours = [r["hour"] for r in conn.execute(sql)]
                if hours:
                    conn.executemany(
                        "DELETE FROM dirty_hours WHERE hour = ?", [(h,) for h in hours]
                    )
                conn.execute("COMMIT")
                return hours
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mark_all_hours_dirty(self) -> int:
        """Force a full rollup rebuild, e.g. after a threshold change."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO dirty_hours(hour) "
                "SELECT DISTINCT ts / 3600000 FROM requests"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO dirty_hours(hour) SELECT DISTINCT hour FROM bucket_hour"
            )
            return self._conn.execute("SELECT COUNT(*) c FROM dirty_hours").fetchone()["c"]

    def bump_anomalies(self, counts: Iterable[tuple[str, int]]) -> None:
        rows = [(k, v) for k, v in counts if v]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO anomalies(kind, count) VALUES (?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET count = count + excluded.count",
                rows,
            )
