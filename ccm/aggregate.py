"""Rollups and query surface.

Stored rows hold token counts only. Every dollar figure in the product is
computed here, at read time, from the current rate table -- which is what lets a
pricing edit change the whole dashboard without touching the scan.

Costing an aggregate is only equal to summing its requests individually if every
request in it shares a rate. That is why ``long_ctx`` is part of the rollup key:
a bucket is homogeneous in model *and* context tier, so applying the tier rate to
the bucket's sums is exact rather than approximate.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .pricing import (
    Cost,
    PricingTable,
    compute,
    compute_tier,
    effective_rate,
    split_model,
)
from .store import Store

HOUR_MS = 3_600_000

#: Dimensions the rollup is keyed by, and therefore what can be filtered or
#: grouped without falling back to a scan of `requests`.
GROUPABLE = {
    "origin": "origin",
    "source": "source",
    "model": "model",
    "provider": "provider",
    "base_model": "base_model",
    "repo": "repo",
    "is_subagent": "is_subagent",
    "long_ctx": "long_ctx",
}

BUCKET_SECONDS = {
    "1m": 60,
    "5m": 300,
    "10m": 600,
    "30m": 1800,
    "hour": 3600,
    "6h": 21600,
    "day": 86400,
    "week": 604800,
}


def tier_expression(pricing: PricingTable, column: str = "base_model") -> str:
    """SQL that yields 1 when a request is billed at long-context rates.

    Thresholds are per model, so this compiles the rate table into a CASE rather
    than assuming one global cutoff.
    """
    thresholds = pricing.thresholds()
    default = pricing.default_threshold
    if not thresholds:
        return f"(CASE WHEN input_tokens > {default} THEN 1 ELSE 0 END)"
    cases = " ".join(
        f"WHEN {_sql_str(name)} THEN {int(limit)}" for name, limit in sorted(thresholds.items())
    )
    return (
        f"(CASE WHEN input_tokens > "
        f"(CASE {column} {cases} ELSE {int(default)} END) THEN 1 ELSE 0 END)"
    )


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# rollup maintenance


REBUILD_SELECT = """
SELECT
    r.ts / {hour_ms}                                AS hour,
    r.source                                        AS source,
    COALESCE(NULLIF(r.model, ''), 'unknown')        AS model,
    COALESCE(r.provider, '')                        AS provider,
    COALESCE(NULLIF(r.base_model, ''), 'unknown')   AS base_model,
    {repo}                                          AS repo,
    COALESCE(s.is_subagent, 0)                      AS is_subagent,
    {tier}                                          AS long_ctx,
    COUNT(*)                                        AS n,
    SUM(r.input_tokens)                             AS input_tokens,
    SUM(r.cached_tokens)                            AS cached_tokens,
    SUM(r.cache_write_tokens)                       AS cache_write_tokens,
    SUM(r.cache_write_1h_tokens)                    AS cache_write_1h_tokens,
    SUM(r.output_tokens)                            AS output_tokens,
    SUM(r.reasoning_tokens)                         AS reasoning_tokens,
    MAX(r.input_tokens)                             AS max_input
FROM requests r
LEFT JOIN sessions s ON s.rollout_id = r.rollout_id
WHERE r.ts / {hour_ms} IN ({hours})
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
"""

#: The project label, with the raw fields as a fallback for rows written before
#: a source learned to derive one.
REPO_EXPR = "COALESCE(NULLIF(s.repo, ''), NULLIF(s.cwd, ''), 'unknown')"

#: Where each filterable dimension lives in the rollup -- plain columns, since
#: the rollup is keyed by exactly these.
BUCKET_COLUMNS_SQL = {
    "origin": "origin",
    "source": "source",
    "model": "model",
    "provider": "provider",
    "repo": "repo",
    "is_subagent": "is_subagent",
}

#: ...and the same dimensions expressed against `requests` joined to `sessions`,
#: for the queries that need per-request resolution.
REQUEST_COLUMNS_SQL = {
    # Everything in `requests` is local by definition, so the origin of a row
    # here is always the empty string.
    "origin": "''",
    "source": "r.source",
    "model": "COALESCE(NULLIF(r.model, ''), 'unknown')",
    "provider": "COALESCE(r.provider, '')",
    "base_model": "COALESCE(NULLIF(r.base_model, ''), 'unknown')",
    "repo": REPO_EXPR,
    "is_subagent": "COALESCE(s.is_subagent, 0)",
}


def rebuild_buckets(
    store: Store, pricing: PricingTable, hours: Sequence[int] | None = None
) -> int:
    """Recompute the hourly rollup for the given hours (default: all dirty).

    Delete-then-insert per hour rather than incremental deltas, so the result
    cannot drift from `requests` no matter how rows were reordered or moved
    between hours by a late timestamp correction.
    """
    if hours is None:
        hours = store.take_dirty_hours()
    hours = [int(h) for h in hours]
    if not hours:
        return 0

    tier = tier_expression(pricing, "r.base_model")
    done = 0
    for start in range(0, len(hours), 500):
        chunk = hours[start : start + 500]
        placeholders = ",".join(str(h) for h in chunk)
        sql_select = REBUILD_SELECT.format(
            hour_ms=HOUR_MS, tier=tier, hours=placeholders, repo=REPO_EXPR
        )
        with store.lock:
            conn = store.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Scoped to the local origin. Without that clause, the first
                # dirty hour after an import would delete the imported rows for
                # that hour and reinsert only local data -- silently, and only
                # for the hours that happened to overlap.
                conn.execute(
                    f"DELETE FROM bucket_hour WHERE origin = '' AND hour IN ({placeholders})"
                )
                conn.execute(
                    "INSERT INTO bucket_hour (hour, source, model, provider,"
                    " base_model, repo, is_subagent, long_ctx, n, input_tokens,"
                    " cached_tokens, cache_write_tokens, cache_write_1h_tokens,"
                    f" output_tokens, reasoning_tokens, max_input) {sql_select}"
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        done += len(chunk)
    return done


def pricing_fingerprint(pricing: PricingTable) -> str:
    """Identifies the tier boundaries currently baked into the rollup.

    Only thresholds matter: rates are applied on read, but a threshold change
    moves requests between tiers and so invalidates the stored buckets.
    """
    parts = [f"default={pricing.default_threshold}"]
    parts += [f"{k}={v}" for k, v in sorted(pricing.thresholds().items())]
    return ";".join(parts)


def ensure_buckets_current(store: Store, pricing: PricingTable) -> int:
    """Rebuild everything if the tier boundaries moved, else just dirty hours."""
    fingerprint = pricing_fingerprint(pricing)
    if store.get_meta("bucket_fingerprint") != fingerprint:
        store.mark_all_hours_dirty()
        store.set_meta("bucket_fingerprint", fingerprint)
    return rebuild_buckets(store, pricing)


# ---------------------------------------------------------------------------
# filtering


@dataclass
class Filters:
    """Selection applied to every query. Empty lists mean "no restriction"."""

    start_ms: int | None = None
    end_ms: int | None = None
    origins: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    subagent: str = "all"  # all | main | sub

    def where(
        self,
        *,
        hour_column: str = "hour",
        ms: bool = False,
        columns: dict[str, str] | None = None,
    ) -> tuple[str, list[Any]]:
        """Build the WHERE clause against either the rollup or `requests`.

        ``columns`` maps each filterable dimension to the SQL that yields it in
        the query being built. Passing :data:`REQUEST_COLUMNS_SQL` targets the
        raw table with its joins; the default targets ``bucket_hour``, where
        every dimension is already a plain column.
        """
        cols = columns or BUCKET_COLUMNS_SQL
        clauses: list[str] = []
        params: list[Any] = []
        scale = 1 if ms else HOUR_MS
        if self.start_ms is not None:
            clauses.append(f"{hour_column} >= ?")
            params.append(self.start_ms if ms else self.start_ms // scale)
        if self.end_ms is not None:
            clauses.append(f"{hour_column} < ?")
            params.append(self.end_ms if ms else math.ceil(self.end_ms / scale))
        for values, key in (
            (self.origins, "origin"),
            (self.sources, "source"),
            (self.models, "model"),
            (self.providers, "provider"),
            (self.repos, "repo"),
        ):
            if values:
                clauses.append(f"{cols[key]} IN ({','.join('?' * len(values))})")
                params.extend(values)
        if self.subagent == "main":
            clauses.append(f"{cols['is_subagent']} = 0")
        elif self.subagent == "sub":
            clauses.append(f"{cols['is_subagent']} = 1")
        return (" AND ".join(clauses) if clauses else "1=1"), params


# ---------------------------------------------------------------------------
# metric assembly


@dataclass
class Totals:
    """Everything the KPI strip and every table row needs."""

    n: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    uncached: float = 0.0
    unpriced_tokens: int = 0
    fresh_cost: float = 0.0
    cached_cost: float = 0.0
    write_cost: float = 0.0
    output_cost: float = 0.0
    #: What the same tokens would have cost had none of them crossed a model's
    #: long-context threshold. The gap is the surcharge, and the prompt tokens
    #: that paid it are counted beside it.
    standard_cost: float = 0.0
    long_tokens: int = 0

    def add(self, row: Any, cost: Cost) -> None:
        self.n += row["n"]
        self.input_tokens += row["input_tokens"]
        self.cached_tokens += row["cached_tokens"]
        self.cache_write_tokens += row["cache_write_tokens"]
        self.cache_write_1h_tokens += row["cache_write_1h_tokens"]
        self.output_tokens += row["output_tokens"]
        self.reasoning_tokens += row["reasoning_tokens"]
        self.cost += cost.cost
        self.uncached += cost.uncached
        self.fresh_cost += cost.fresh_cost
        self.cached_cost += cost.cached_cost
        self.write_cost += cost.write_cost
        self.output_cost += cost.output_cost
        self.standard_cost += cost.standard
        if row["long_ctx"]:
            self.long_tokens += row["input_tokens"]
        if not cost.priced:
            self.unpriced_tokens += row["input_tokens"]

    def as_dict(self) -> dict:
        inp = self.input_tokens
        written = self.cache_write_tokens + self.cache_write_1h_tokens
        return {
            "requests": self.n,
            "input_tokens": inp,
            "cached_tokens": self.cached_tokens,
            "cache_write_tokens": written,
            "cache_write_1h_tokens": self.cache_write_1h_tokens,
            "fresh_tokens": max(inp - self.cached_tokens - written, 0),
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_rate": (self.cached_tokens / inp) if inp else 0.0,
            "cache_write_rate": (written / inp) if inp else 0.0,
            "cost": round(self.cost, 6),
            "cost_fresh": round(self.fresh_cost, 6),
            "cost_cached": round(self.cached_cost, 6),
            "cost_written": round(self.write_cost, 6),
            "cost_output": round(self.output_cost, 6),
            "uncached_cost": round(self.uncached, 6),
            "long_tokens": self.long_tokens,
            "long_fraction": (self.long_tokens / inp) if inp else 0.0,
            "long_surcharge": round(self.cost - self.standard_cost, 6),
            "saved": round(self.uncached - self.cost, 6),
            "saved_fraction": (1 - self.cost / self.uncached) if self.uncached else 0.0,
            "effective_rate": effective_rate(self.cost, inp),
            "list_rate": effective_rate(self.uncached, inp),
            # The generation side of the same ratio, and not comparable with the
            # rate above: output is never cached and is billed several times the
            # input price, so a few tokens of it can outweigh a large prompt.
            # Derived here rather than in the client so every surface that shows
            # a row of totals -- tables, sessions, calendar, heatmap -- agrees.
            "output_rate": effective_rate(self.output_cost, self.output_tokens),
            "efficiency": (self.cost / self.uncached) if self.uncached else 0.0,
            "avg_context": (inp / self.n) if self.n else 0.0,
            "unpriced_tokens": self.unpriced_tokens,
        }


def _row_cost(pricing: PricingTable, row: Any) -> Cost:
    return compute(
        pricing.get(row["model"]),
        row["input_tokens"],
        row["cached_tokens"],
        row["output_tokens"],
        long_context=bool(row["long_ctx"]),
        cache_write_tokens=row["cache_write_tokens"],
        cache_write_1h_tokens=row["cache_write_1h_tokens"],
    )


BUCKET_COLUMNS = (
    "n, input_tokens, cached_tokens, cache_write_tokens, cache_write_1h_tokens, "
    "output_tokens, reasoning_tokens, model, long_ctx"
)


def totals(store: Store, pricing: PricingTable, filters: Filters) -> dict:
    where, params = filters.where()
    rows = store.query(
        f"SELECT {BUCKET_COLUMNS} FROM bucket_hour WHERE {where}", params
    )
    agg = Totals()
    for row in rows:
        agg.add(row, _row_cost(pricing, row))
    return agg.as_dict()


def breakdown(
    store: Store, pricing: PricingTable, filters: Filters, dimension: str
) -> list[dict]:
    """Rollup grouped by one dimension, ordered by cost."""
    column = GROUPABLE.get(dimension)
    if column is None:
        raise ValueError(f"cannot group by {dimension!r}")
    where, params = filters.where()
    rows = store.query(
        f"SELECT {column} AS grp, {BUCKET_COLUMNS} FROM bucket_hour WHERE {where}",
        params,
    )
    groups: dict[Any, Totals] = {}
    for row in rows:
        groups.setdefault(row["grp"], Totals()).add(row, _row_cost(pricing, row))
    out = [{"key": str(k), **v.as_dict()} for k, v in groups.items()]
    out.sort(key=lambda d: -d["cost"])
    return out


def series(
    store: Store,
    pricing: PricingTable,
    filters: Filters,
    *,
    bucket: str = "hour",
    group: str | None = None,
    limit_groups: int = 12,
) -> dict:
    """Aligned time series, column-oriented for direct handoff to the charts.

    Token sums and both cost figures are returned per point; the client derives
    cache rate, effective rate and savings from those. One request therefore
    feeds every chart on the page.
    """
    seconds = BUCKET_SECONDS.get(bucket, 3600)
    column = GROUPABLE.get(group) if group else None

    if seconds < 3600:
        # Sub-hourly needs per-request resolution, so the rollup is bypassed.
        rows = _series_from_requests(store, pricing, filters, seconds, column)
    else:
        rows = _series_from_buckets(store, pricing, filters, seconds, column)

    # Bucket -> group -> Totals
    per_bucket: dict[int, dict[str, Totals]] = {}
    group_totals: dict[str, float] = {}
    for slot, key, row, cost in rows:
        bucket_groups = per_bucket.setdefault(slot, {})
        agg = bucket_groups.setdefault(key, Totals())
        agg.add(row, cost)
        group_totals[key] = group_totals.get(key, 0.0) + cost.cost

    if not per_bucket:
        return {"bucket_seconds": seconds, "t": [], "groups": [], "total": None}

    slots = sorted(per_bucket)
    # Continuous axis: gaps become explicit nulls rather than being drawn
    # through, so an idle hour never looks like a straight line of activity.
    axis = list(range(slots[0], slots[-1] + 1))
    times = [s * seconds for s in axis]

    ordered = sorted(group_totals, key=lambda k: -group_totals[k])
    kept = ordered[:limit_groups] if column else ordered
    other = set(ordered) - set(kept)

    def blank() -> dict[str, list]:
        return {
            k: [None] * len(axis)
            for k in (
                "n",
                "input",
                "cached",
                "written",
                "output",
                "reasoning",
                "cost",
                "cost_fresh",
                "cost_cached",
                "cost_written",
                "cost_output",
                "uncached",
                "surcharge",
                "long_input",
            )
        }

    out_groups: dict[str, dict[str, list]] = {k: blank() for k in kept}
    if other:
        out_groups["other"] = blank()
    total_cols = blank()

    index = {slot: i for i, slot in enumerate(axis)}
    for slot, groups in per_bucket.items():
        i = index[slot]
        running = Totals()
        for key, agg in groups.items():
            target = out_groups[key if key in out_groups else "other"]
            _accumulate(target, i, agg)
            running.n += agg.n
            running.input_tokens += agg.input_tokens
            running.cached_tokens += agg.cached_tokens
            running.cache_write_tokens += agg.cache_write_tokens
            running.cache_write_1h_tokens += agg.cache_write_1h_tokens
            running.output_tokens += agg.output_tokens
            running.reasoning_tokens += agg.reasoning_tokens
            running.cost += agg.cost
            running.uncached += agg.uncached
            running.fresh_cost += agg.fresh_cost
            running.cached_cost += agg.cached_cost
            running.write_cost += agg.write_cost
            running.output_cost += agg.output_cost
            running.standard_cost += agg.standard_cost
            running.long_tokens += agg.long_tokens
        _accumulate(total_cols, i, running)

    return {
        "bucket_seconds": seconds,
        "t": times,
        "groups": [
            {"key": key, **cols}
            for key, cols in sorted(
                out_groups.items(), key=lambda kv: -group_totals.get(kv[0], 0.0)
            )
        ],
        "total": total_cols,
    }


def _accumulate(cols: dict[str, list], i: int, agg: Totals) -> None:
    for name, value in (
        ("n", agg.n),
        ("input", agg.input_tokens),
        ("cached", agg.cached_tokens),
        ("written", agg.cache_write_tokens + agg.cache_write_1h_tokens),
        ("output", agg.output_tokens),
        ("reasoning", agg.reasoning_tokens),
        ("cost", round(agg.cost, 6)),
        ("cost_fresh", round(agg.fresh_cost, 6)),
        ("cost_cached", round(agg.cached_cost, 6)),
        ("cost_written", round(agg.write_cost, 6)),
        ("cost_output", round(agg.output_cost, 6)),
        ("uncached", round(agg.uncached, 6)),
        ("surcharge", round(agg.cost - agg.standard_cost, 6)),
        ("long_input", agg.long_tokens),
    ):
        cols[name][i] = value if cols[name][i] is None else cols[name][i] + value


def _series_from_buckets(store, pricing, filters, seconds, column):
    where, params = filters.where()
    rows = store.query(
        f"SELECT hour, {BUCKET_COLUMNS}"
        + (f", {column} AS grp" if column else "")
        + f" FROM bucket_hour WHERE {where}",
        params,
    )
    for row in rows:
        slot = (row["hour"] * 3600) // seconds
        key = str(row["grp"]) if column else "all"
        yield slot, key, row, _row_cost(pricing, row)


def _series_from_requests(store, pricing, filters, seconds, column):
    """Sub-hourly path: aggregate straight off `requests`.

    Slower than the rollup but only ever used over a short window, where the
    row count is small.
    """
    tier = tier_expression(pricing, "r.base_model")
    where, params = filters.where(
        hour_column="r.ts", ms=True, columns=REQUEST_COLUMNS_SQL
    )
    grp = {**REQUEST_COLUMNS_SQL, "long_ctx": tier}.get(column or "", "'all'")
    sql = f"""
        SELECT (r.ts / 1000) / {int(seconds)} AS slot,
               {grp} AS grp,
               COALESCE(NULLIF(r.model,''),'unknown') AS model,
               {tier} AS long_ctx,
               COUNT(*) AS n,
               SUM(r.input_tokens) AS input_tokens,
               SUM(r.cached_tokens) AS cached_tokens,
               SUM(r.cache_write_tokens) AS cache_write_tokens,
               SUM(r.cache_write_1h_tokens) AS cache_write_1h_tokens,
               SUM(r.output_tokens) AS output_tokens,
               SUM(r.reasoning_tokens) AS reasoning_tokens
        FROM requests r
        LEFT JOIN sessions s ON s.rollout_id = r.rollout_id
        WHERE {where}
        GROUP BY 1, 2, 3, 4
    """
    for row in store.query(sql, params):
        yield row["slot"], str(row["grp"]), row, _row_cost(pricing, row)


# ---------------------------------------------------------------------------
# specialised views


def heatmap(store: Store, pricing: PricingTable, filters: Filters, tz_offset_min: int = 0) -> dict:
    """Cache rate by weekday and hour of day, in the viewer's local time."""
    where, params = filters.where()
    rows = store.query(
        f"SELECT hour, {BUCKET_COLUMNS} FROM bucket_hour WHERE {where}", params
    )
    cells: dict[tuple[int, int], Totals] = {}
    for row in rows:
        local = row["hour"] * 3600 + tz_offset_min * 60
        day = ((local // 86400) + 4) % 7  # epoch day 0 was a Thursday
        hour_of_day = (local % 86400) // 3600
        cells.setdefault((day, hour_of_day), Totals()).add(row, _row_cost(pricing, row))
    return {
        "cells": [
            {"day": d, "hour": h, **agg.as_dict()} for (d, h), agg in sorted(cells.items())
        ]
    }


#: How many models a day's tooltip names before the rest become "other".
CALENDAR_TOP_MODELS = 4


def calendar(
    store: Store, pricing: PricingTable, filters: Filters, tz_offset_min: int = 0
) -> dict:
    """Per-calendar-day totals over the whole history, in the viewer's local time.

    Deliberately ignores the selected time range: the calendar carries its own
    month pagination, and applying a 7-day window on top of it would leave every
    other month blank. Every other filter still applies, because those describe
    *what* is being counted rather than *when*.
    """
    scope = replace(filters, start_ms=None, end_ms=None)
    where, params = scope.where()
    rows = store.query(
        f"SELECT hour, {BUCKET_COLUMNS} FROM bucket_hour WHERE {where}", params
    )

    days: dict[int, Totals] = {}
    per_model: dict[int, dict[str, float]] = {}
    for row in rows:
        # Epoch day in local time, so a late-night session lands on the day the
        # person doing it would call it.
        day = (row["hour"] * 3600 + tz_offset_min * 60) // 86400
        cost = _row_cost(pricing, row)
        days.setdefault(day, Totals()).add(row, cost)
        model = row["model"] or "unknown"
        models = per_model.setdefault(day, {})
        models[model] = models.get(model, 0.0) + cost.cost

    out = []
    for day, agg in sorted(days.items()):
        ranked = sorted(per_model[day].items(), key=lambda kv: -kv[1])
        top = [
            {"key": key, "cost": round(cost, 6)}
            for key, cost in ranked[:CALENDAR_TOP_MODELS]
        ]
        rest = sum(cost for _, cost in ranked[CALENDAR_TOP_MODELS:])
        if rest > 0:
            top.append({"key": "other", "cost": round(rest, 6)})
        out.append({"day": day, "top": top, **agg.as_dict()})
    return {"days": out, "tz_offset": tz_offset_min}


#: Fields the scatter needs to bin by any metric client-side. Slightly wider
#: than BUCKET_COLUMNS because prompt size and output are their own columns.
SCATTER_COLUMNS = (
    "hour, source, model, provider, base_model, repo, is_subagent, long_ctx,"
    " n, input_tokens, cached_tokens, cache_write_tokens,"
    " cache_write_1h_tokens, output_tokens, reasoning_tokens, max_input"
)


def context_scatter(
    store: Store, pricing: PricingTable, filters: Filters
) -> dict:
    """Per-bucket points for density plots of any metric against prompt size.

    Each hourly rollup bucket becomes a weighted point: its prompt size (the
    mean of the requests in it) on x, and every metric the dashboard knows on
    y. The client bins these into a 2D grid and can swap the y metric without a
    round trip.

    Reading from ``bucket_hour`` rather than ``requests`` is the fix for
    imported machines: an import has no per-request rows, only the rollup, so
    the old query found nothing. The rollup is also the 99:1 compression that
    makes a whole machine's history small enough to move between machines.
    """
    where, params = filters.where()
    rows = store.query(
        f"SELECT {SCATTER_COLUMNS} FROM bucket_hour WHERE {where} AND input_tokens > 0",
        params,
    )
    if not rows:
        return {"points": [], "max_input": 0, "count": 0}

    max_input = max(r["max_input"] for r in rows)
    # Log-x, because prompt sizes span three orders of magnitude. The range
    # runs from the smallest bucket's mean prompt to the largest single
    # request ever seen, so the x extent covers everything a point can land on.
    lo = math.log10(max(1, min(r["input_tokens"] / r["n"] for r in rows)))
    hi = math.log10(max(max_input, 10))
    span = max(hi - lo, 1e-9)

    points: list[dict] = []
    count = 0
    for r in rows:
        n = r["n"]
        inp = r["input_tokens"]
        cost = _row_cost(pricing, r)
        # A bucket is an aggregate, so prompt size is its mean -- good enough
        # for where the mass sits, which is what a density plot is for.
        mean_input = inp / n
        x_frac = (math.log10(max(mean_input, 1)) - lo) / span
        points.append(
            {
                "x": x_frac,
                # Every metric the client might want to bin on, precomputed
                # here so a metric switch is free.
                "n": n,
                "input": mean_input,
                "cache_rate": (r["cached_tokens"] / inp) if inp else 0.0,
                "effective_rate": effective_rate(cost.cost, inp),
                "output_rate": effective_rate(cost.output_cost, r["output_tokens"]),
                "cost": cost.cost,
                "output_tokens": r["output_tokens"],
            }
        )
        count += n

    return {
        "points": points,
        "x_log_min": lo,
        "x_log_max": hi,
        "count": count,
        "max_input": max_input,
    }


SESSION_SELECT = """
SELECT s.rollout_id, s.session_id, s.cwd, s.git_repo, s.git_branch,
       s.agent_role, s.agent_nickname, s.depth, s.is_subagent,
       s.thread_source, s.cli_version, s.first_ts, s.last_ts, s.path,
       COUNT(r.rowid)               AS n,
       COALESCE(SUM(r.input_tokens), 0)     AS input_tokens,
       COALESCE(SUM(r.cached_tokens), 0)    AS cached_tokens,
       COALESCE(SUM(r.output_tokens), 0)    AS output_tokens,
       COALESCE(SUM(r.reasoning_tokens), 0) AS reasoning_tokens,
       MIN(r.ts) AS req_first, MAX(r.ts) AS req_last
FROM sessions s
LEFT JOIN requests r ON r.rollout_id = s.rollout_id
GROUP BY s.rollout_id
"""


def sessions(
    store: Store, pricing: PricingTable, filters: Filters, *, include_empty: bool = False
) -> list[dict]:
    """Per-rollout rollup.

    A request belongs to whichever rollout recorded it first, which is the file
    that ran it live rather than any of the files that later replayed it.
    """
    tier = tier_expression(pricing, "r.base_model")
    where, params = filters.where(
        hour_column="r.ts", ms=True, columns=REQUEST_COLUMNS_SQL
    )
    rows = store.query(
        f"""
        SELECT s.rollout_id, s.source, s.session_id, s.cwd, s.git_repo,
               s.git_branch, s.repo AS project,
               s.agent_role, s.agent_nickname, s.depth, s.is_subagent,
               s.thread_source, s.cli_version, s.path,
               COALESCE(NULLIF(r.model,''),'unknown') AS model,
               {tier} AS long_ctx,
               COUNT(*) AS n,
               SUM(r.input_tokens) AS input_tokens,
               SUM(r.cached_tokens) AS cached_tokens,
               SUM(r.cache_write_tokens) AS cache_write_tokens,
               SUM(r.cache_write_1h_tokens) AS cache_write_1h_tokens,
               SUM(r.output_tokens) AS output_tokens,
               SUM(r.reasoning_tokens) AS reasoning_tokens,
               MIN(r.ts) AS first_ts, MAX(r.ts) AS last_ts
        FROM requests r
        JOIN sessions s ON s.rollout_id = r.rollout_id
        WHERE {where}
        GROUP BY s.rollout_id, model, long_ctx
        """,
        params,
    )
    merged: dict[str, dict] = {}
    for row in rows:
        entry = merged.get(row["rollout_id"])
        if entry is None:
            entry = {
                "rollout_id": row["rollout_id"],
                "origin": "",
                "source": row["source"],
                "session_id": row["session_id"],
                "cwd": row["cwd"],
                "repo": row["project"] or row["cwd"] or "unknown",
                "branch": row["git_branch"],
                "agent_role": row["agent_role"],
                "agent_nickname": row["agent_nickname"],
                "depth": row["depth"],
                "is_subagent": bool(row["is_subagent"]),
                "cli_version": row["cli_version"],
                "path": row["path"],
                "has_timeline": True,
                "models": {},
                "first_ts": row["first_ts"],
                "last_ts": row["last_ts"],
                "_totals": Totals(),
            }
            merged[row["rollout_id"]] = entry
        entry["_totals"].add(row, _row_cost(pricing, row))
        entry["models"][row["model"]] = (
            entry["models"].get(row["model"], 0) + row["input_tokens"]
        )
        entry["first_ts"] = min(entry["first_ts"], row["first_ts"])
        entry["last_ts"] = max(entry["last_ts"], row["last_ts"])

    _merge_imported_sessions(store, pricing, filters, merged)

    out = []
    for entry in merged.values():
        agg: Totals = entry.pop("_totals")
        models = entry.pop("models")
        entry["model"] = max(models, key=models.get) if models else None
        entry["models"] = sorted(models, key=models.get, reverse=True)
        # An imported bundle may omit timestamps; treat that as zero duration
        # rather than letting it raise.
        span = (entry["last_ts"] or 0) - (entry["first_ts"] or 0)
        entry["duration_ms"] = max(span, 0)
        entry.update(agg.as_dict())
        out.append(entry)
    out.sort(key=lambda d: -(d["first_ts"] or 0))
    return out


def _merge_imported_sessions(
    store: Store, pricing: PricingTable, filters: Filters, merged: dict[str, dict]
) -> None:
    """Fold sessions carried in from other machines into the same list.

    Imported rows arrive pre-grouped by ``(model, long_ctx)``, which is the same
    grouping the local query produces, so they cost identically -- and, because
    only tokens travelled, they re-cost with the rate table like everything
    else. What they cannot have is a per-request timeline, so the row is marked
    and the detail drawer says so rather than showing an empty chart.
    """
    clauses = ["1=1"]
    params: list[Any] = []
    if filters.origins:
        clauses.append(f"origin IN ({','.join('?' * len(filters.origins))})")
        params.extend(filters.origins)
    if filters.sources:
        clauses.append(f"source IN ({','.join('?' * len(filters.sources))})")
        params.extend(filters.sources)
    if filters.repos:
        clauses.append(f"repo IN ({','.join('?' * len(filters.repos))})")
        params.extend(filters.repos)
    if filters.models:
        clauses.append(f"model IN ({','.join('?' * len(filters.models))})")
        params.extend(filters.models)
    if filters.subagent == "main":
        clauses.append("is_subagent = 0")
    elif filters.subagent == "sub":
        clauses.append("is_subagent = 1")
    if filters.start_ms is not None:
        clauses.append("last_ts >= ?")
        params.append(filters.start_ms)
    if filters.end_ms is not None:
        clauses.append("first_ts < ?")
        params.append(filters.end_ms)

    for row in store.query(
        f"SELECT * FROM import_sessions WHERE {' AND '.join(clauses)}", params
    ):
        # Keyed by origin too: two machines can hold the same rollout id only if
        # one imported the other, and then they are genuinely the same session
        # under two names.
        key = f"{row['origin']}::{row['rollout_id']}"
        entry = merged.get(key)
        if entry is None:
            entry = {
                "rollout_id": row["rollout_id"],
                "origin": row["origin"],
                "source": row["source"],
                "session_id": None,
                "cwd": None,
                "repo": row["repo"] or "unknown",
                "branch": None,
                "agent_role": row["agent_role"],
                "agent_nickname": row["agent_nickname"],
                "depth": row["depth"],
                "is_subagent": bool(row["is_subagent"]),
                "cli_version": None,
                "path": None,
                "has_timeline": False,
                "models": {},
                "first_ts": row["first_ts"],
                "last_ts": row["last_ts"],
                "_totals": Totals(),
            }
            merged[key] = entry
        entry["_totals"].add(row, _row_cost(pricing, row))
        entry["models"][row["model"]] = (
            entry["models"].get(row["model"], 0) + row["input_tokens"]
        )
        if row["first_ts"] is not None:
            entry["first_ts"] = min(entry["first_ts"] or row["first_ts"], row["first_ts"])
        if row["last_ts"] is not None:
            entry["last_ts"] = max(entry["last_ts"] or row["last_ts"], row["last_ts"])


def session_detail(store: Store, pricing: PricingTable, rollout_id: str) -> dict:
    """Per-request timeline for one rollout, plus its event markers."""
    meta = store.one("SELECT * FROM sessions WHERE rollout_id = ?", (rollout_id,))
    rows = store.query(
        "SELECT ts, model, base_model, provider, effort, input_tokens, cached_tokens,"
        " cache_write_tokens, cache_write_1h_tokens,"
        " output_tokens, reasoning_tokens, ctx_window, rl_used_percent"
        " FROM requests WHERE rollout_id = ? ORDER BY ts",
        (rollout_id,),
    )
    agg = Totals()
    points = []
    for row in rows:
        rate = pricing.get(row["model"])
        cost = (
            compute_tier(
                rate.tier_for(row["input_tokens"]),
                row["input_tokens"],
                row["cached_tokens"],
                row["output_tokens"],
                row["cache_write_tokens"],
                row["cache_write_1h_tokens"],
            )
            if rate
            else Cost(0.0, 0.0, priced=False)
        )
        agg.n += 1
        agg.input_tokens += row["input_tokens"]
        agg.cached_tokens += row["cached_tokens"]
        agg.cache_write_tokens += row["cache_write_tokens"]
        agg.cache_write_1h_tokens += row["cache_write_1h_tokens"]
        agg.output_tokens += row["output_tokens"]
        agg.reasoning_tokens += row["reasoning_tokens"]
        agg.cost += cost.cost
        agg.uncached += cost.uncached
        points.append(
            {
                "ts": row["ts"],
                "model": row["model"],
                "input": row["input_tokens"],
                "cached": row["cached_tokens"],
                "written": row["cache_write_tokens"] + row["cache_write_1h_tokens"],
                "output": row["output_tokens"],
                "reasoning": row["reasoning_tokens"],
                "cache_rate": (row["cached_tokens"] / row["input_tokens"])
                if row["input_tokens"]
                else 0.0,
                "cost": round(cost.cost, 6),
                "ctx_window": row["ctx_window"],
                "quota_percent": row["rl_used_percent"],
            }
        )
    events = store.query(
        "SELECT ts, kind FROM events WHERE rollout_id = ? ORDER BY ts", (rollout_id,)
    )
    return {
        "meta": dict(meta) if meta else None,
        "totals": agg.as_dict(),
        "requests": points,
        "events": [{"ts": e["ts"], "kind": e["kind"]} for e in events],
    }


def event_markers(store: Store, filters: Filters) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if filters.start_ms is not None:
        clauses.append("ts >= ?")
        params.append(filters.start_ms)
    if filters.end_ms is not None:
        clauses.append("ts < ?")
        params.append(filters.end_ms)
    rows = store.query(
        f"SELECT ts, kind, rollout_id FROM events WHERE {' AND '.join(clauses)}"
        " ORDER BY ts",
        params,
    )
    return [dict(r) for r in rows]


def quota_series(store: Store, filters: Filters) -> list[dict]:
    """Plan quota consumption over time, sampled from the rate-limit snapshots."""
    clauses = ["rl_used_percent IS NOT NULL"]
    params: list[Any] = []
    if filters.start_ms is not None:
        clauses.append("ts >= ?")
        params.append(filters.start_ms)
    if filters.end_ms is not None:
        clauses.append("ts < ?")
        params.append(filters.end_ms)
    rows = store.query(
        f"""SELECT ts / 600000 * 600000 AS slot, rl_limit_id AS limit_id,
                   MAX(rl_used_percent) AS used_percent,
                   MAX(rl_window_minutes) AS window_minutes,
                   MAX(rl_resets_at) AS resets_at
            FROM requests WHERE {" AND ".join(clauses)}
            GROUP BY slot, limit_id ORDER BY slot""",
        params,
    )
    return [dict(r) for r in rows]


def dimensions(store: Store, pricing: PricingTable) -> dict:
    """Filter option lists, ordered by how much they cost.

    Cost, not token volume, because this ordering is also what assigns chart
    colours. The charts pick which series to draw by cost and fold the rest into
    one grey "other"; if colours were allocated on a different ranking, a model
    the chart chose to draw could still end up grey -- indistinguishable from
    the aggregate and from every other model in the same position.
    """

    def collect(column: str) -> list[str]:
        return [row["key"] for row in breakdown(store, pricing, Filters(), column)]

    span = store.one(
        "SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n FROM requests"
    )
    # Imported data has no rows in `requests`, so the span and count have to
    # take it in separately or the range picker would clip other machines out.
    imported = store.one(
        "SELECT MIN(first_ts) AS lo, MAX(last_ts) AS hi,"
        " COALESCE(SUM(requests), 0) AS n FROM imports"
    )
    lows = [v for v in ((span or {})["lo"], (imported or {})["lo"]) if v]
    highs = [v for v in ((span or {})["hi"], (imported or {})["hi"]) if v]
    return {
        "origins": collect("origin"),
        "sources": collect("source"),
        "models": collect("model"),
        "providers": collect("provider"),
        "base_models": collect("base_model"),
        "repos": collect("repo"),
        "first_ts": min(lows) if lows else None,
        "last_ts": max(highs) if highs else None,
        "requests": (span["n"] if span else 0) + (imported["n"] if imported else 0),
        "imported_requests": imported["n"] if imported else 0,
    }


def token_volumes(store: Store) -> dict[str, dict[str, int]]:
    """Tokens observed per model, split by billing category.

    Used to decide whether a disagreement with a reference price table could
    actually change a figure. A rate only matters if tokens were billed at it.
    """
    rows = store.query(
        "SELECT model,"
        " SUM(input_tokens - cached_tokens - cache_write_tokens"
        "     - cache_write_1h_tokens) AS fresh,"
        " SUM(cached_tokens) AS cached,"
        " SUM(cache_write_tokens + cache_write_1h_tokens) AS written,"
        " SUM(output_tokens) AS output"
        " FROM bucket_hour GROUP BY model"
    )
    volumes: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = {k: max(int(row[k] or 0), 0) for k in ("fresh", "cached", "written", "output")}
        # The rate table is keyed on the bare model, so a routed variant's
        # volume has to count toward the model it is priced as.
        for key in {row["model"], split_model(row["model"])[1]}:
            if not key:
                continue
            bucket = volumes.setdefault(key, {})
            for name, value in entry.items():
                bucket[name] = bucket.get(name, 0) + value
    return volumes


def data_quality(store: Store, pricing: PricingTable) -> dict:
    """What the scan could not fully account for."""
    anomalies = {r["kind"]: r["count"] for r in store.query("SELECT * FROM anomalies")}
    files = store.one(
        "SELECT COUNT(*) AS n, SUM(size) AS bytes, SUM(raw_events) AS raw,"
        " SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS failed FROM files"
    )
    requests = store.one("SELECT COUNT(*) AS n FROM requests")
    raw = (files["raw"] or 0) if files else 0
    deduped = requests["n"] if requests else 0
    unpriced_rows = store.query(
        "SELECT model, SUM(input_tokens) AS input_tokens, SUM(n) AS n"
        " FROM bucket_hour GROUP BY model"
    )
    unpriced = [
        {"model": r["model"], "input_tokens": r["input_tokens"], "requests": r["n"]}
        for r in unpriced_rows
        if pricing.get(r["model"]) is None
    ]
    estimated = [
        name
        for name, spec in pricing.as_dict()["models"].items()
        if spec["estimated"] or spec["long_tier_unknown"]
    ]
    return {
        "files": files["n"] if files else 0,
        "failed_files": (files["failed"] or 0) if files else 0,
        "bytes": (files["bytes"] or 0) if files else 0,
        "raw_token_events": raw,
        "deduped_requests": deduped,
        "replay_ratio": round(raw / deduped, 3) if deduped else 0.0,
        "replayed_events": max(raw - deduped, 0),
        "anomalies": anomalies,
        "unpriced_models": unpriced,
        "estimated_pricing": estimated,
        "sources": source_quality(store, pricing),
    }


def source_quality(store: Store, pricing: PricingTable) -> list[dict]:
    """Per-client ingest health, including a check against the client's own sums.

    Pi and OpenCode record what they believe each request cost. Comparing that
    with our figure is the closest thing to an independent audit available: a
    drift beyond rounding means either the rate table or the token
    normalisation is wrong for that client.
    """
    per_file = {
        r["source"]: r
        for r in store.query(
            "SELECT source, COUNT(*) AS files, SUM(size) AS bytes,"
            " SUM(raw_events) AS raw,"
            " SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS failed"
            " FROM files GROUP BY source"
        )
    }
    rows = store.query(
        "SELECT source, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts"
        " FROM requests GROUP BY source"
    )
    computed = {
        r["key"]: r for r in breakdown(store, pricing, Filters(), "source")
    }
    audit = client_cost_audit(store, pricing)
    out = []
    for row in rows:
        source = row["source"]
        files = per_file.get(source)
        raw = int(files["raw"] or 0) if files else 0
        ours = computed.get(source, {})
        entry = {
            "source": source,
            "files": int(files["files"]) if files else 0,
            "failed_files": int(files["failed"] or 0) if files else 0,
            "bytes": int(files["bytes"] or 0) if files else 0,
            "raw_token_events": raw,
            "requests": row["n"],
            "replay_ratio": round(raw / row["n"], 3) if row["n"] else 0.0,
            "first_ts": row["first_ts"],
            "last_ts": row["last_ts"],
            "cost": ours.get("cost", 0.0),
            "input_tokens": ours.get("input_tokens", 0),
            "cache_rate": ours.get("cache_rate", 0.0),
            "cache_write_rate": ours.get("cache_write_rate", 0.0),
            "effective_rate": ours.get("effective_rate", 0.0),
            "saved": ours.get("saved", 0.0),
            "audit": audit.get(source),
        }
        out.append(entry)
    out.sort(key=lambda d: -d["cost"])
    return out


def client_cost_audit(store: Store, pricing: PricingTable) -> dict[str, dict]:
    """Our cost against the client's own, over exactly the requests it priced.

    Restricted to rows the client actually put a non-zero price on. Pi records
    a flat zero for anything reached through a subscription proxy, and folding
    those in would make a perfect match look like a 33% overcharge.

    Reported twice: over everything priced, and over standard-tier requests
    only. The clients do not model long-context surcharges -- Pi bills a
    200k-token prompt at the same rate as a short one -- so on a corpus where
    half the prompts cross a threshold the headline ratio measures that
    disagreement and nothing else. The standard-tier figure is the one where a
    mismatch means *we* are wrong.
    """
    rows = store.query(
        "SELECT source, model, input_tokens, cached_tokens, cache_write_tokens,"
        " cache_write_1h_tokens, output_tokens, client_cost"
        " FROM requests WHERE client_cost IS NOT NULL AND client_cost > 0"
    )
    out: dict[str, dict] = {}
    for row in rows:
        entry = out.setdefault(
            row["source"],
            {
                "requests": 0,
                "ours": 0.0,
                "theirs": 0.0,
                "standard_requests": 0,
                "standard_ours": 0.0,
                "standard_theirs": 0.0,
            },
        )
        rate = pricing.get(row["model"])
        if rate is None:
            continue
        tier = rate.tier_for(row["input_tokens"])
        cost = compute_tier(
            tier,
            row["input_tokens"],
            row["cached_tokens"],
            row["output_tokens"],
            row["cache_write_tokens"],
            row["cache_write_1h_tokens"],
        ).cost
        entry["requests"] += 1
        entry["theirs"] += row["client_cost"]
        entry["ours"] += cost
        if tier is rate.tier_for(0):
            entry["standard_requests"] += 1
            entry["standard_theirs"] += row["client_cost"]
            entry["standard_ours"] += cost
    for entry in out.values():
        for prefix in ("", "standard_"):
            ours = entry[f"{prefix}ours"] = round(entry[f"{prefix}ours"], 6)
            theirs = entry[f"{prefix}theirs"] = round(entry[f"{prefix}theirs"], 6)
            entry[f"{prefix}ratio"] = (ours / theirs) if theirs else None
    return out
