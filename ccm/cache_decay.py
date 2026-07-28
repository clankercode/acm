"""Infer prompt-cache expiry (TTL) from inter-request idle-time gaps.

Cache providers (OpenAI, Anthropic, etc.) keep recent prompt prefixes warm for a
provider-defined duration, then evict them. We cannot observe eviction directly,
but we can infer it: within a session, the fraction of the previous request's
prompt that is still cached at the *next* request decays as the idle gap grows.
A cliff in that retention-vs-gap curve marks the TTL.

Method
------
For each source, within each session (``rollout_id``), order requests by
timestamp. For each consecutive pair (i-1, i):

* **gap**       = ``ts[i] - ts[i-1]`` (idle time between requests, in ms)
* **retention** = ``cached_tokens[i] / input_tokens[i-1]`` (fraction of the
  previous prompt that survived in cache)

Gaps are binned into time buckets and mean/median/p25/p75 retention is computed
per bin. The cliff(s) in the resulting curve reveal the cache TTL(s).

Caveats
-------
1. **Gap measures idle time within one session**, not provider-side eviction
   caused by competing sessions sharing the same cache namespace.
2. **Retention is a lower bound** -- context truncation, prefix changes, or
   compaction events can also reduce ``cached_tokens``, masquerading as expiry.
3. **Codex does not report cache_write tokens** (OpenAI populates the cache for
   free), so only the read-side cliff is visible. Claude explicitly distinguishes
   ``cache_write_tokens`` (5m tier) and ``cache_write_1h_tokens`` (1h tier),
   enabling richer analysis of the write side.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any, TypedDict

from .aggregate import Filters, REQUEST_COLUMNS_SQL
from .store import Store

#: Default gap-bin edges in milliseconds, chosen to straddle known TTLs (5m,
#: 10m, 1h). Boundaries are time-aligned so the cliffs land cleanly.
DEFAULT_GAP_BINS_MS: tuple[int, ...] = (
    0,
    5_000,
    15_000,
    30_000,
    60_000,
    120_000,
    300_000,    # 5 min
    600_000,    # 10 min
    1_800_000,  # 30 min
    3_600_000,  # 1 hour
    7_200_000,  # 2 hours
    14_400_000, # 4 hours
)

#: Sentinel for the open-ended final bin; every real gap is below this.
INF = 9_999_999_999


class CacheDecayResult(TypedDict):
    """Return shape of :func:`cache_decay`."""

    sources: dict[str, list[dict]]
    write_tiers: dict[str, list[str]]


_GAP_SQL = """
WITH ordered AS (
    SELECT r.source,
           r.rollout_id,
           r.ts,
           r.input_tokens,
           r.cached_tokens,
           LAG(r.ts)           OVER w AS prev_ts,
           LAG(r.input_tokens) OVER w AS prev_input
    FROM requests r
    LEFT JOIN sessions s ON s.rollout_id = r.rollout_id
    WHERE {where}
      AND r.input_tokens > 0
    WINDOW w AS (
        PARTITION BY r.rollout_id
        ORDER BY r.ts
        ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
    )
)
SELECT source,
       (ts - prev_ts)  AS gap_ms,
       cached_tokens,
       prev_input
FROM ordered
WHERE prev_ts IS NOT NULL AND prev_input > 0
"""


def _bin_label(start_ms: int, end_ms: int) -> str:
    """Human-readable bin label, e.g. ``"0s-5s"``, ``"5m-10m"``, ``"1h+"``."""

    def humanize(ms: int) -> str:
        if ms >= 3_600_000:
            return f"{ms // 3_600_000}h"
        if ms >= 60_000:
            return f"{ms // 60_000}m"
        return f"{ms // 1000}s"

    right = "+" if end_ms == INF else humanize(end_ms)
    return f"{humanize(start_ms)}-{right}"


def cache_decay(
    store: Store,
    filters: Filters,
    *,
    gap_bins_ms: Sequence[int] | None = None,
) -> CacheDecayResult:
    """Compute cache-retention-vs-idle-gap curves, per source.

    Returns a dict with two top-level keys:

    * ``"sources"`` -- the per-source retention curves, keyed by source name
      (e.g. ``"codex"``, ``"claude"``). Each value is a list of bin dicts
      ordered by gap. Each bin aggregates the consecutive-request pairs whose
      gap falls in ``[gap_start_ms, gap_end_ms)`` and carries ``n``,
      ``mean_retention``, ``median_retention``, ``p25_retention`` and
      ``p75_retention``.
    * ``"write_tiers"`` -- per-source list of active cache-write tier names
      (``"5m"`` and/or ``"1h"``). A tier is active when the source has any
      nonzero writes against it in the filtered window. This annotates which
      retention cliff corresponds to which write-tier expiry; Codex (which
      reports no cache writes) always returns ``[]``.
    """
    edges = list(gap_bins_ms) if gap_bins_ms is not None else list(DEFAULT_GAP_BINS_MS)
    # Dedup, sort, and append the open-ended tail.
    edges = sorted(set(edges))
    boundaries = edges + [INF]

    where, params = filters.where(
        hour_column="r.ts", ms=True, columns=REQUEST_COLUMNS_SQL
    )
    rows = store.query(_GAP_SQL.format(where=where), params)

    # Collect raw (gap, retention) pairs per source.
    raw: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for row in rows:
        prev_input = row["prev_input"]
        retention = row["cached_tokens"] / prev_input if prev_input else 0.0
        raw[row["source"]].append((row["gap_ms"], retention))

    return {"sources": _build_bins(raw, boundaries), "write_tiers": _write_tiers(store, where, params)}


_WRITE_TIERS_SQL = """
SELECT source,
       SUM(cache_write_tokens)   AS total_5m,
       SUM(cache_write_1h_tokens) AS total_1h
FROM requests r
WHERE {where}
GROUP BY source
"""


def _write_tiers(store: Store, where: str, params: list[Any]) -> dict[str, list[str]]:
    """Detect which cache-write tiers each source has used (sum > 0)."""
    rows = store.query(_WRITE_TIERS_SQL.format(where=where), params)
    tiers: dict[str, list[str]] = {}
    for row in rows:
        active: list[str] = []
        if (row["total_5m"] or 0) > 0:
            active.append("5m")
        if (row["total_1h"] or 0) > 0:
            active.append("1h")
        tiers[row["source"]] = active
    return tiers


def _build_bins(
    raw: dict[str, list[tuple[int, float]]],
    boundaries: list[int],
) -> dict[str, list[dict]]:
    """Bin per-source retention ratios into the gap boundaries."""
    result: dict[str, list[dict]] = {}
    for source, pairs in raw.items():
        bins: list[dict] = []
        for i in range(len(boundaries) - 1):
            start = boundaries[i]
            end = boundaries[i + 1]
            ratios = [r for gap, r in pairs if start <= gap < end]
            label = _bin_label(start, end)
            if not ratios:
                bins.append(
                    {
                        "gap_start_ms": start,
                        "gap_end_ms": end,
                        "label": label,
                        "n": 0,
                        "mean_retention": 0.0,
                        "median_retention": 0.0,
                        "p25_retention": 0.0,
                        "p75_retention": 0.0,
                    }
                )
                continue
            ratios.sort()
            n = len(ratios)
            mean = sum(ratios) / n
            bins.append(
                {
                    "gap_start_ms": start,
                    "gap_end_ms": end,
                    "label": label,
                    "n": n,
                    "mean_retention": round(mean, 4),
                    "median_retention": round(ratios[n // 2], 4),
                    "p25_retention": round(ratios[n // 4], 4),
                    "p75_retention": round(ratios[3 * n // 4], 4),
                }
            )
        result[source] = bins
    return result
