"""Moving stats between machines.

Everything the dashboard draws comes from the hourly rollup, and the rollup is
1,114 rows for 109,823 requests here -- a 99:1 compression. That is what makes a
whole machine's history a small file rather than a database dump.

Two things travel:

* **Buckets** -- the hourly rollup. Feeds every chart, KPI and breakdown.
* **Sessions** -- per-session rollups, split by ``(model, long_ctx)`` exactly as
  the local session query groups them. That split is not incidental: it is what
  lets an imported session be re-costed *exactly* when the rate table changes,
  instead of being frozen at whatever the exporting machine happened to charge.

No costs are stored. The importing machine prices imported tokens with its own
table, which is the only way figures from several machines can be compared.

Absolute paths and working directories are deliberately left out. They mean
nothing on another machine, and of everything recorded they are the most likely
to leak something. The normalised project label survives, so cross-machine repo
comparison still works.
"""

from __future__ import annotations

import json
import re
import socket
import time
from typing import Any

from .store import Store

FORMAT = "acm-export"
#: Formats produced by previous versions of this tool, still accepted on import
#: so a bundle exported by ccm can be read by acm. Exports always write FORMAT.
_LEGACY_FORMATS = frozenset({"ccm-export"})
VERSION = 1

#: Column order for the bucket rows. Positional to keep the file small; the
#: names travel with the data so a reader never has to guess.
BUCKET_COLUMNS = (
    "hour",
    "source",
    "model",
    "provider",
    "base_model",
    "repo",
    "is_subagent",
    "long_ctx",
    "n",
    "input_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "cache_write_1h_tokens",
    "output_tokens",
    "reasoning_tokens",
    "max_input",
)

SESSION_COLUMNS = (
    "rollout_id",
    "source",
    "repo",
    "agent_role",
    "agent_nickname",
    "depth",
    "is_subagent",
    "first_ts",
    "last_ts",
    "model",
    "long_ctx",
    "n",
    "input_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "cache_write_1h_tokens",
    "output_tokens",
    "reasoning_tokens",
)

#: Bucket columns that identify a row rather than measure it. Merging several
#: origins sums the rest across matching keys.
_BUCKET_KEY = BUCKET_COLUMNS[:8]
_BUCKET_SUM = BUCKET_COLUMNS[8:15]


class BundleError(ValueError):
    """The file is not a bundle this version can read."""


def default_label() -> str:
    return socket.gethostname() or "this machine"


def sanitise_label(label: str) -> str:
    """Trim to something usable as both a display name and a filter value."""
    cleaned = re.sub(r"\s+", " ", (label or "").strip())
    return cleaned[:60] or default_label()


def unique_label(store: Store, label: str) -> str:
    """A label not already taken by an import.

    Collisions are likely in the intended use -- two people exporting from
    machines both called `laptop` -- so they are resolved rather than rejected.
    """
    taken = {
        row["origin"] for row in store.query("SELECT origin FROM imports")
    } | {""}
    if label not in taken:
        return label
    for suffix in range(2, 1000):
        candidate = f"{label} ({suffix})"
        if candidate not in taken:
            return candidate
    return f"{label} ({int(time.time())})"


# ---------------------------------------------------------------------------
# export


def export_bundle(
    store: Store,
    pricing,
    *,
    label: str,
    origins: list[str] | None = None,
    tool_version: str = "",
) -> dict:
    """Build a bundle from the selected origins.

    ``origins`` uses ``""`` for this machine. ``None`` means every origin,
    which is how a machine that has already imported others re-exports the
    pooled set.
    """
    label = sanitise_label(label)
    known = [""] + [r["origin"] for r in store.query("SELECT origin FROM imports")]
    selected = known if origins is None else [o for o in origins if o in known]
    if not selected:
        selected = [""]

    placeholders = ",".join("?" * len(selected))
    bucket_rows = store.query(
        f"SELECT {', '.join(BUCKET_COLUMNS)} FROM bucket_hour"
        f" WHERE origin IN ({placeholders})",
        selected,
    )

    # Several origins can hold the same key -- two machines used the same model
    # in the same hour on the same repo. Merge rather than emit duplicates, so
    # the bundle is the same shape whether it came from one machine or ten.
    merged: dict[tuple, list[int]] = {}
    for row in bucket_rows:
        key = tuple(row[c] for c in _BUCKET_KEY)
        acc = merged.get(key)
        values = [row[c] for c in _BUCKET_SUM]
        if acc is None:
            merged[key] = [*values, row["max_input"]]
        else:
            for i, v in enumerate(values):
                acc[i] += v
            acc[-1] = max(acc[-1], row["max_input"])

    buckets = [[*key, *rest] for key, rest in sorted(merged.items())]
    sessions = _export_sessions(store, pricing, selected)
    contributors = _contributors(store, selected)

    total_requests = sum(row[8] for row in buckets)
    total_input = sum(row[9] for row in buckets)
    hours = [row[0] for row in buckets]
    clients = sorted({row[1] for row in buckets})

    return {
        "format": FORMAT,
        "version": VERSION,
        "label": label,
        "machine": socket.gethostname(),
        "exported_at": int(time.time() * 1000),
        "tool_version": tool_version,
        "origins": contributors,
        "summary": {
            "requests": total_requests,
            "input_tokens": total_input,
            "sessions": len({s[0] for s in sessions}),
            "clients": clients,
            "first_ts": min(hours) * 3_600_000 if hours else None,
            "last_ts": (max(hours) + 1) * 3_600_000 if hours else None,
        },
        "bucket_columns": list(BUCKET_COLUMNS),
        "buckets": buckets,
        "session_columns": list(SESSION_COLUMNS),
        "sessions": sessions,
    }


def _contributors(store: Store, selected: list[str]) -> list[str]:
    """Every machine whose data is in here, including through earlier imports.

    Provenance has to survive a second hop, or a bundle passed around a team
    stops saying where any of it came from.
    """
    names: list[str] = []
    for origin in selected:
        if origin == "":
            names.append(store.get_meta("local_label") or default_label())
            continue
        row = store.one(
            "SELECT contributors, origin FROM imports WHERE origin = ?", (origin,)
        )
        if row is None:
            continue
        try:
            inner = json.loads(row["contributors"] or "[]")
        except ValueError:
            inner = []
        names.extend(inner or [row["origin"]])
    return sorted(dict.fromkeys(names))


def _export_sessions(store: Store, pricing, selected: list[str]) -> list[list]:
    """Session rows for the selected origins, deduplicated.

    Buckets from two origins are summed -- two machines really can have worked
    on the same model in the same hour. Sessions are not: a rollout id is a
    UUID, so the same one appearing under two origins is one session that has
    been round-tripped, and adding it to itself would invent work nobody did.
    """
    from . import aggregate

    rows: list[list] = []
    seen: set[tuple] = set()

    def keep(row) -> bool:
        key = (row["rollout_id"], row["model"], row["long_ctx"])
        if key in seen:
            return False
        seen.add(key)
        return True

    if "" in selected:
        # Grouped by context tier as well as model. Costing a session's summed
        # tokens is only exact if every request in the group shares a rate, and
        # the tier decides that -- collapse it here and imported sessions would
        # be mispriced the moment the threshold matters.
        tier = aggregate.tier_expression(pricing, "r.base_model")
        for row in store.query(
            f"""
            SELECT s.rollout_id, s.source, COALESCE(NULLIF(s.repo,''), 'unknown') AS repo,
                   s.agent_role, s.agent_nickname, s.depth, s.is_subagent,
                   MIN(r.ts) AS first_ts, MAX(r.ts) AS last_ts,
                   COALESCE(NULLIF(r.model,''),'unknown') AS model,
                   {tier} AS long_ctx,
                   COUNT(*) AS n,
                   SUM(r.input_tokens) AS input_tokens,
                   SUM(r.cached_tokens) AS cached_tokens,
                   SUM(r.cache_write_tokens) AS cache_write_tokens,
                   SUM(r.cache_write_1h_tokens) AS cache_write_1h_tokens,
                   SUM(r.output_tokens) AS output_tokens,
                   SUM(r.reasoning_tokens) AS reasoning_tokens
            FROM requests r JOIN sessions s ON s.rollout_id = r.rollout_id
            GROUP BY s.rollout_id, model, long_ctx
            """
        ):
            if keep(row):
                rows.append([row[c] for c in SESSION_COLUMNS])
    imported = [o for o in selected if o]
    if imported:
        placeholders = ",".join("?" * len(imported))
        for row in store.query(
            f"SELECT {', '.join(SESSION_COLUMNS)} FROM import_sessions"
            f" WHERE origin IN ({placeholders})"
            " ORDER BY origin",
            imported,
        ):
            if keep(row):
                rows.append([row[c] for c in SESSION_COLUMNS])
    return rows


# ---------------------------------------------------------------------------
# import


def read_bundle(payload: Any) -> dict:
    """Validate a decoded bundle, or say precisely what is wrong with it."""
    if not isinstance(payload, dict):
        raise BundleError("not a JSON object")
    if payload.get("format") not in (FORMAT, *_LEGACY_FORMATS):
        raise BundleError(
            f"not an Agent Cache Monitor export (format={payload.get('format')!r})"
        )
    version = payload.get("version")
    if version != VERSION:
        raise BundleError(
            f"bundle version {version} cannot be read by this build (expects {VERSION})"
        )
    for field in ("bucket_columns", "buckets", "session_columns", "sessions"):
        if field not in payload:
            raise BundleError(f"missing {field!r}")
    if not isinstance(payload["buckets"], list):
        raise BundleError("buckets is not a list")
    return payload


def preview(store: Store, payload: Any) -> dict:
    """What importing this file would add, without adding it."""
    bundle = read_bundle(payload)
    proposed = sanitise_label(bundle.get("label") or "imported")
    resolved = unique_label(store, proposed)
    summary = dict(bundle.get("summary") or {})
    summary.setdefault("requests", sum(r[8] for r in bundle["buckets"]))
    return {
        "label": resolved,
        "suggested_label": proposed,
        "collision": resolved != proposed,
        "machine": bundle.get("machine"),
        "exported_at": bundle.get("exported_at"),
        "tool_version": bundle.get("tool_version"),
        "contributors": bundle.get("origins") or [],
        "buckets": len(bundle["buckets"]),
        "sessions": len(bundle["sessions"]),
        "summary": summary,
    }


def import_bundle(store: Store, payload: Any, label: str | None = None) -> dict:
    """Store a bundle under ``label``.

    An explicit label is taken at its word: if it names an import that already
    exists, that import is replaced. This is what re-importing a colleague's
    updated bundle under the same name has to mean, and the editable label in
    the import dialog would otherwise be unable to express it.

    With no label the bundle's own is used and uniquified, so importing two
    different files that happen to share a label keeps both.
    """
    bundle = read_bundle(payload)
    origin = (
        sanitise_label(label)
        if label
        else unique_label(store, sanitise_label(bundle.get("label") or "imported"))
    )

    bucket_index = {name: i for i, name in enumerate(bundle["bucket_columns"])}
    session_index = {name: i for i, name in enumerate(bundle["session_columns"])}

    def bucket_value(row: list, name: str, default=0):
        i = bucket_index.get(name)
        return default if i is None or i >= len(row) else row[i]

    def session_value(row: list, name: str, default=None):
        i = session_index.get(name)
        return default if i is None or i >= len(row) else row[i]

    bucket_rows = [
        (origin, *[bucket_value(row, c, 0 if c != "provider" else "") for c in BUCKET_COLUMNS])
        for row in bundle["buckets"]
    ]
    session_rows = [
        (origin, *[session_value(row, c) for c in SESSION_COLUMNS])
        for row in bundle["sessions"]
    ]

    summary = bundle.get("summary") or {}
    requests = int(summary.get("requests") or sum(r[8] for r in bundle["buckets"]))
    input_tokens = int(
        summary.get("input_tokens") or sum(r[9] for r in bundle["buckets"])
    )

    meta = (
        origin,
        sanitise_label(bundle.get("label") or origin),
        bundle.get("machine"),
        bundle.get("exported_at"),
        int(time.time() * 1000),
        bundle.get("tool_version"),
        bundle.get("version"),
        requests,
        input_tokens,
        summary.get("first_ts"),
        summary.get("last_ts"),
        json.dumps(summary.get("clients") or []),
        json.dumps(bundle.get("origins") or [origin]),
    )

    with store.lock:
        conn = store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM bucket_hour WHERE origin = ?", (origin,))
            conn.execute("DELETE FROM import_sessions WHERE origin = ?", (origin,))
            conn.executemany(
                "INSERT INTO bucket_hour (origin, "
                + ", ".join(BUCKET_COLUMNS)
                + ") VALUES ("
                + ",".join("?" * (len(BUCKET_COLUMNS) + 1))
                + ")",
                bucket_rows,
            )
            # OR REPLACE rather than plain INSERT: a bundle written by another
            # build could repeat a session key, and losing one copy is a far
            # better outcome than failing the whole import.
            conn.executemany(
                "INSERT OR REPLACE INTO import_sessions (origin, "
                + ", ".join(SESSION_COLUMNS)
                + ") VALUES ("
                + ",".join("?" * (len(SESSION_COLUMNS) + 1))
                + ")",
                session_rows,
            )
            conn.execute(
                "INSERT INTO imports (origin, source_label, machine, exported_at,"
                " imported_at, tool_version, bundle_version, requests, input_tokens,"
                " first_ts, last_ts, clients, contributors)"
                " VALUES (" + ",".join("?" * 13) + ")"
                " ON CONFLICT(origin) DO UPDATE SET"
                " source_label=excluded.source_label, machine=excluded.machine,"
                " exported_at=excluded.exported_at, imported_at=excluded.imported_at,"
                " tool_version=excluded.tool_version,"
                " bundle_version=excluded.bundle_version,"
                " requests=excluded.requests, input_tokens=excluded.input_tokens,"
                " first_ts=excluded.first_ts, last_ts=excluded.last_ts,"
                " clients=excluded.clients, contributors=excluded.contributors",
                meta,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {"origin": origin, "buckets": len(bucket_rows), "sessions": len(session_rows)}


def rename_import(store: Store, origin: str, label: str) -> str:
    """Rename an import, moving its rows with it.

    The label *is* the origin key, so this is three coordinated updates rather
    than one -- worth it to keep every query and filter value human-readable.
    """
    new = unique_label(store, sanitise_label(label))
    if new == origin:
        return origin
    with store.lock:
        conn = store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE imports SET origin = ? WHERE origin = ?", (new, origin))
            conn.execute(
                "UPDATE bucket_hour SET origin = ? WHERE origin = ?", (new, origin)
            )
            conn.execute(
                "UPDATE import_sessions SET origin = ? WHERE origin = ?", (new, origin)
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return new


def delete_import(store: Store, origin: str) -> bool:
    if not origin:
        return False
    with store.lock:
        conn = store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute("DELETE FROM imports WHERE origin = ?", (origin,))
            removed = cur.rowcount > 0
            conn.execute("DELETE FROM bucket_hour WHERE origin = ?", (origin,))
            conn.execute("DELETE FROM import_sessions WHERE origin = ?", (origin,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return removed


def list_origins(store: Store, pricing, aggregate) -> list[dict]:
    """Every machine's data now in this store, local first."""
    totals = {
        row["key"]: row
        for row in aggregate.breakdown(store, pricing, aggregate.Filters(), "origin")
    }
    local_label = store.get_meta("local_label") or default_label()
    out = [
        {
            "origin": "",
            "label": local_label,
            "local": True,
            "machine": socket.gethostname(),
            "imported_at": None,
            "exported_at": None,
            "contributors": [local_label],
            **_totals_of(totals.get("")),
        }
    ]
    for row in store.query("SELECT * FROM imports ORDER BY imported_at"):
        try:
            contributors = json.loads(row["contributors"] or "[]")
        except ValueError:
            contributors = []
        out.append(
            {
                "origin": row["origin"],
                "label": row["origin"],
                "local": False,
                "machine": row["machine"],
                "imported_at": row["imported_at"],
                "exported_at": row["exported_at"],
                "contributors": contributors,
                **_totals_of(totals.get(row["origin"])),
            }
        )
    return out


def _totals_of(row) -> dict:
    if row is None:
        return {
            "requests": 0,
            "input_tokens": 0,
            "cost": 0.0,
            "cache_rate": 0.0,
            "effective_rate": 0.0,
        }
    return {
        "requests": row["requests"],
        "input_tokens": row["input_tokens"],
        "cost": row["cost"],
        "cache_rate": row["cache_rate"],
        "effective_rate": row["effective_rate"],
    }
