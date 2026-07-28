"""Rollup maintenance and the derived metrics built on top of it."""

from __future__ import annotations

import os
import time
from datetime import timedelta

import pytest

from ccm import aggregate as A
from ccm.pricing import PricingTable, compute_tier
from ccm.scanner import Scanner
from ccm.store import Store

from .conftest import Thread


def ingest(store: Store, sessions_dir, pricing, threads) -> None:
    for t in threads:
        t.write(sessions_dir, f"rollout-2026-07-01T00-00-00-{t.rollout_id}.jsonl")
    Scanner(store, sessions_dir.parents[2]).scan_once()
    A.ensure_buckets_current(store, pricing)


def simple(clock, rollout_id="a", model="gpt-5.6-sol") -> Thread:
    t = Thread(session_id="s-" + rollout_id, rollout_id=rollout_id, clock=clock)
    t.meta().turn_context(model)
    t.request(800, 600, 40)
    t.request(900, 700, 50)
    return t


def sum_requests_directly(store: Store, pricing: PricingTable) -> float:
    """Cost every request on its own, as the reference for bucket costing."""
    total = 0.0
    for row in store.query(
        "SELECT model, input_tokens, cached_tokens, output_tokens FROM requests"
    ):
        rate = pricing.get(row["model"])
        if rate is None:
            continue
        total += compute_tier(
            rate.tier_for(row["input_tokens"]),
            row["input_tokens"],
            row["cached_tokens"],
            row["output_tokens"],
        ).cost
    return total


def test_bucket_cost_equals_per_request_cost(store, sessions_dir, pricing, clock):
    """The invariant that justifies aggregating at all.

    Buckets are keyed by context tier precisely so that applying one rate to a
    bucket's summed tokens gives the same answer as costing each request.
    """
    t = Thread(session_id="s", rollout_id="mix", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    for size in (100, 900, 1_000, 1_001, 5_000, 40_000):  # straddles the 1000 threshold
        t.request(size, size // 2, 25)
    ingest(store, sessions_dir, pricing, [t])

    bucketed = A.totals(store, pricing, A.Filters())["cost"]
    assert bucketed == pytest.approx(sum_requests_directly(store, pricing), rel=1e-9)


def test_mixed_tiers_land_in_separate_buckets(store, sessions_dir, pricing, clock):
    t = Thread(session_id="s", rollout_id="tiers", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    t.request(500, 0, 0)
    t.request(5_000, 0, 0)
    ingest(store, sessions_dir, pricing, [t])
    tiers = {r["long_ctx"]: r["input_tokens"] for r in store.query(
        "SELECT long_ctx, input_tokens FROM bucket_hour")}
    assert tiers == {0: 500, 1: 5_000}


def test_rebuild_is_idempotent(store, sessions_dir, pricing, clock):
    ingest(store, sessions_dir, pricing, [simple(clock)])
    snapshot = [tuple(r) for r in store.query("SELECT * FROM bucket_hour ORDER BY hour, model")]
    for _ in range(3):
        A.rebuild_buckets(store, pricing, hours=[r["hour"] for r in store.query(
            "SELECT DISTINCT hour FROM bucket_hour")])
    assert [tuple(r) for r in store.query(
        "SELECT * FROM bucket_hour ORDER BY hour, model")] == snapshot


def test_buckets_match_requests_after_rebuild(store, sessions_dir, pricing, clock):
    ingest(store, sessions_dir, pricing, [simple(clock, "a"), simple(clock, "b")])
    a = store.one("SELECT COUNT(*) n, SUM(input_tokens) i FROM requests")
    b = store.one("SELECT SUM(n) n, SUM(input_tokens) i FROM bucket_hour")
    assert (a["n"], a["i"]) == (b["n"], b["i"])


def test_a_timestamp_moving_back_dirties_both_hours(store, sessions_dir, pricing, clock):
    """A replay-corrected timestamp must not leave a stale bucket behind."""
    original = simple(clock, "a")
    late = original.replayed_into("b", clock + timedelta(hours=6))
    # Scan the late copy first so the early one corrects it downward later.
    late.write(sessions_dir, "rollout-2026-07-01T18-00-00-b.jsonl")
    Scanner(store, sessions_dir.parents[2]).scan_once()
    A.ensure_buckets_current(store, pricing)
    late_hours = {r["hour"] for r in store.query("SELECT DISTINCT hour FROM bucket_hour")}

    original.write(sessions_dir, "rollout-2026-07-01T12-00-00-a.jsonl")
    Scanner(store, sessions_dir.parents[2]).scan_once()
    A.rebuild_buckets(store, pricing)

    now_hours = {r["hour"] for r in store.query("SELECT DISTINCT hour FROM bucket_hour")}
    assert now_hours != late_hours
    # No double counting: the tokens exist once, at the earlier hour.
    assert store.one("SELECT SUM(n) n FROM bucket_hour")["n"] == 2


def test_threshold_change_forces_a_full_rebuild(store, sessions_dir, pricing, clock, tmp_path):
    t = Thread(session_id="s", rollout_id="thr", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    t.request(1_500, 0, 0)
    ingest(store, sessions_dir, pricing, [t])
    assert store.one("SELECT long_ctx FROM bucket_hour")["long_ctx"] == 1
    before = A.totals(store, pricing, A.Filters())["cost"]

    # Raise the threshold above the request: it should fall back to short rates.
    pricing.path.write_text(
        pricing.path.read_text().replace(
            "long_context_threshold = 1000", "long_context_threshold = 100000"
        )
    )
    os.utime(pricing.path, (time.time() + 1, time.time() + 1))
    pricing.reload()
    A.ensure_buckets_current(store, pricing)

    assert store.one("SELECT long_ctx FROM bucket_hour")["long_ctx"] == 0
    assert A.totals(store, pricing, A.Filters())["cost"] == pytest.approx(before / 2)


def test_pricing_edit_alone_needs_no_rebuild(store, sessions_dir, pricing, clock):
    """Rates apply on read, so only thresholds can invalidate the rollup."""
    ingest(store, sessions_dir, pricing, [simple(clock)])
    fingerprint = A.pricing_fingerprint(pricing)
    before = A.totals(store, pricing, A.Filters())["cost"]

    pricing.path.write_text(pricing.path.read_text().replace("input = 5.0", "input = 50.0"))
    os.utime(pricing.path, (time.time() + 1, time.time() + 1))
    pricing.reload()

    assert A.pricing_fingerprint(pricing) == fingerprint
    assert A.totals(store, pricing, A.Filters())["cost"] != before


def test_cache_savings_are_the_gap_to_the_uncached_counterfactual(
    store, sessions_dir, pricing, clock
):
    t = Thread(session_id="s", rollout_id="save", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    t.request(1_000, 900, 0)  # 90% cached, short tier
    ingest(store, sessions_dir, pricing, [t])
    tot = A.totals(store, pricing, A.Filters())
    assert tot["cache_rate"] == pytest.approx(0.9)
    assert tot["cost"] == pytest.approx((100 * 5.0 + 900 * 0.5) / 1e6)
    assert tot["uncached_cost"] == pytest.approx(1000 * 5.0 / 1e6)
    assert tot["saved"] == pytest.approx(tot["uncached_cost"] - tot["cost"])
    assert tot["efficiency"] == pytest.approx(tot["cost"] / tot["uncached_cost"])


def test_effective_rate_reflects_cache_quality(store, sessions_dir, pricing, clock):
    """Same model and volume, different cache rate, different $/Mtok."""
    good = Thread(session_id="g", rollout_id="good", clock=clock)
    good.meta().turn_context("gpt-5.6-sol")
    good.request(1_000, 950, 0)
    bad = Thread(session_id="b", rollout_id="bad", clock=clock)
    bad.meta().turn_context("pooler/gpt-5.6-sol")
    bad.request(1_000, 200, 0)
    ingest(store, sessions_dir, pricing, [good, bad])

    rows = {r["key"]: r for r in A.breakdown(store, pricing, A.Filters(), "model")}
    assert rows["pooler/gpt-5.6-sol"]["effective_rate"] > rows["gpt-5.6-sol"]["effective_rate"]
    assert rows["gpt-5.6-sol"]["input_tokens"] == rows["pooler/gpt-5.6-sol"]["input_tokens"]


def test_output_rate_prices_generation_not_the_prompt(store, sessions_dir, pricing, clock):
    """The two rates answer different questions and must not track each other.

    Same prompt volume, wildly different amounts written back: the effective rate
    barely moves while the output rate stays pinned to the model's output price,
    which is the point of showing it separately.
    """
    terse = Thread(session_id="t", rollout_id="terse", clock=clock)
    terse.meta().turn_context("gpt-5.6-sol")
    terse.request(1_000, 900, 10)
    windy = Thread(session_id="w", rollout_id="windy", clock=clock)
    windy.meta().turn_context("gpt-5.6-terra")
    windy.request(1_000, 900, 5_000)
    ingest(store, sessions_dir, pricing, [terse, windy])

    rows = {r["key"]: r for r in A.breakdown(store, pricing, A.Filters(), "model")}
    assert rows["gpt-5.6-sol"]["output_rate"] == pytest.approx(30.0)
    assert rows["gpt-5.6-terra"]["output_rate"] == pytest.approx(15.0)
    for row in rows.values():
        assert row["output_rate"] == pytest.approx(
            row["cost_output"] / (row["output_tokens"] / 1e6)
        )


def test_output_rate_is_zero_when_nothing_was_generated(
    store, sessions_dir, pricing, clock
):
    """A window of pure prompt replay divides by zero otherwise."""
    t = Thread(session_id="s", rollout_id="silent", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    t.request(1_000, 900, 0)
    ingest(store, sessions_dir, pricing, [t])
    tot = A.totals(store, pricing, A.Filters())
    assert tot["output_tokens"] == 0
    assert tot["output_rate"] == 0.0


def test_breakdown_by_provider_separates_routed_from_direct(
    store, sessions_dir, pricing, clock
):
    direct = Thread(session_id="d", rollout_id="d", clock=clock)
    direct.meta().turn_context("gpt-5.6-sol")
    direct.request(500, 450, 0)
    routed = Thread(session_id="p", rollout_id="p", clock=clock)
    routed.meta().turn_context("pooler/gpt-5.6-sol")
    routed.request(500, 100, 0)
    ingest(store, sessions_dir, pricing, [direct, routed])

    rows = {r["key"]: r for r in A.breakdown(store, pricing, A.Filters(), "provider")}
    assert set(rows) == {"", "pooler"}
    assert rows[""]["cache_rate"] > rows["pooler"]["cache_rate"]


def test_filters_narrow_the_result(store, sessions_dir, pricing, clock):
    a = simple(clock, "a", "gpt-5.6-sol")
    b = simple(clock, "b", "gpt-5.6-terra")
    ingest(store, sessions_dir, pricing, [a, b])
    everything = A.totals(store, pricing, A.Filters())
    only_sol = A.totals(store, pricing, A.Filters(models=["gpt-5.6-sol"]))
    assert only_sol["requests"] == 2
    assert everything["requests"] == 4
    assert only_sol["input_tokens"] < everything["input_tokens"]


def test_time_filter_excludes_out_of_range_hours(store, sessions_dir, pricing, clock):
    early = simple(clock, "early")
    late = simple(clock + timedelta(hours=10), "late")
    ingest(store, sessions_dir, pricing, [early, late])
    cutoff = int((clock + timedelta(hours=5)).timestamp() * 1000)
    assert A.totals(store, pricing, A.Filters(start_ms=cutoff))["requests"] == 2
    assert A.totals(store, pricing, A.Filters(end_ms=cutoff))["requests"] == 2


def test_series_axis_is_continuous_with_gaps_as_null(store, sessions_dir, pricing, clock):
    """An idle hour must read as absent, not as a line drawn straight through."""
    a = simple(clock, "a")
    b = simple(clock + timedelta(hours=4), "b")
    ingest(store, sessions_dir, pricing, [a, b])
    s = A.series(store, pricing, A.Filters(), bucket="hour")
    assert len(s["t"]) == 5
    costs = s["total"]["cost"]
    assert costs[0] is not None and costs[-1] is not None
    assert costs[1] is None and costs[2] is None and costs[3] is None


def test_series_totals_match_the_scalar_totals(store, sessions_dir, pricing, clock):
    ingest(store, sessions_dir, pricing, [simple(clock, "a"), simple(clock, "b")])
    scalar = A.totals(store, pricing, A.Filters())
    s = A.series(store, pricing, A.Filters(), bucket="hour")
    assert sum(v for v in s["total"]["cost"] if v) == pytest.approx(scalar["cost"])
    assert sum(v for v in s["total"]["input"] if v) == scalar["input_tokens"]


def test_series_grouped_by_model_partitions_the_total(store, sessions_dir, pricing, clock):
    ingest(
        store, sessions_dir, pricing,
        [simple(clock, "a", "gpt-5.6-sol"), simple(clock, "b", "gpt-5.6-terra")],
    )
    s = A.series(store, pricing, A.Filters(), bucket="hour", group="model")
    keys = {g["key"] for g in s["groups"]}
    assert keys == {"gpt-5.6-sol", "gpt-5.6-terra"}
    per_group = sum(v for g in s["groups"] for v in g["cost"] if v)
    assert per_group == pytest.approx(sum(v for v in s["total"]["cost"] if v))


def test_sub_hourly_series_bypasses_the_rollup(store, sessions_dir, pricing, clock):
    ingest(store, sessions_dir, pricing, [simple(clock, "a")])
    fine = A.series(store, pricing, A.Filters(), bucket="1m")
    coarse = A.series(store, pricing, A.Filters(), bucket="hour")
    assert fine["bucket_seconds"] == 60
    assert sum(v for v in fine["total"]["cost"] if v) == pytest.approx(
        sum(v for v in coarse["total"]["cost"] if v)
    )


def test_sessions_view_attributes_tokens_to_the_originating_rollout(
    store, sessions_dir, pricing, clock
):
    original = simple(clock, "a")
    original.write(sessions_dir, "rollout-2026-07-01T12-00-00-a.jsonl")
    replay = original.replayed_into("b", clock + timedelta(hours=2))
    replay.turn_context().request(300, 100, 5)
    replay.write(sessions_dir, "rollout-2026-07-01T14-00-00-b.jsonl")
    Scanner(store, sessions_dir.parents[2]).scan_once()
    A.ensure_buckets_current(store, pricing)

    rows = {r["rollout_id"]: r for r in A.sessions(store, pricing, A.Filters())}
    assert rows["a"]["requests"] == 2
    assert rows["b"]["requests"] == 1
    assert rows["b"]["input_tokens"] == 300


def test_session_detail_returns_a_timeline(store, sessions_dir, pricing, clock):
    t = simple(clock, "a")
    t.event("context_compacted")
    ingest(store, sessions_dir, pricing, [t])
    detail = A.session_detail(store, pricing, "a")
    assert len(detail["requests"]) == 2
    assert detail["requests"][0]["ts"] <= detail["requests"][1]["ts"]
    assert detail["events"][0]["kind"] == "context_compacted"
    assert detail["totals"]["requests"] == 2


def test_data_quality_reports_the_replay_ratio(store, sessions_dir, pricing, clock):
    original = simple(clock, "a")
    original.write(sessions_dir, "rollout-2026-07-01T12-00-00-a.jsonl")
    original.replayed_into("b", clock + timedelta(hours=1)).write(
        sessions_dir, "rollout-2026-07-01T13-00-00-b.jsonl"
    )
    Scanner(store, sessions_dir.parents[2]).scan_once()
    A.ensure_buckets_current(store, pricing)
    dq = A.data_quality(store, pricing)
    assert dq["deduped_requests"] == 2
    assert dq["raw_token_events"] == 4
    assert dq["replay_ratio"] == pytest.approx(2.0)
    assert dq["replayed_events"] == 2


def test_data_quality_flags_unpriced_models(store, sessions_dir, pricing, clock):
    t = Thread(session_id="s", rollout_id="u", clock=clock)
    t.meta().turn_context("brand-new-model")
    t.request(100, 0, 5)
    ingest(store, sessions_dir, pricing, [t])
    dq = A.data_quality(store, pricing)
    assert [u["model"] for u in dq["unpriced_models"]] == ["brand-new-model"]


def test_dimensions_lists_filter_options(store, sessions_dir, pricing, clock):
    ingest(
        store, sessions_dir, pricing,
        [simple(clock, "a", "gpt-5.6-sol"), simple(clock, "b", "pooler/gpt-5.6-sol")],
    )
    dims = A.dimensions(store, pricing)
    assert set(dims["models"]) == {"gpt-5.6-sol", "pooler/gpt-5.6-sol"}
    assert set(dims["providers"]) == {"", "pooler"}
    assert dims["requests"] == 4


def test_heatmap_covers_weekday_and_hour(store, sessions_dir, pricing, clock):
    ingest(store, sessions_dir, pricing, [simple(clock, "a")])
    cells = A.heatmap(store, pricing, A.Filters())["cells"]
    assert len(cells) == 1
    assert 0 <= cells[0]["day"] <= 6 and 0 <= cells[0]["hour"] <= 23


def test_calendar_groups_by_local_day(store, sessions_dir, pricing, clock):
    """Late-night work belongs to the day the person doing it would name."""
    t = Thread(session_id="s", rollout_id="cal", clock=clock.replace(hour=22))
    t.meta().turn_context("gpt-5.6-sol")
    t.request(800, 600, 40)
    ingest(store, sessions_dir, pricing, [t])

    utc = A.calendar(store, pricing, A.Filters())["days"]
    # +10 pushes 22:00 UTC into the small hours of the next local day.
    plus_ten = A.calendar(store, pricing, A.Filters(), 600)["days"]
    assert len(utc) == len(plus_ten) == 1
    assert plus_ten[0]["day"] == utc[0]["day"] + 1
    assert plus_ten[0]["input_tokens"] == utc[0]["input_tokens"]


def test_calendar_ignores_the_selected_range_but_not_the_other_filters(
    store, sessions_dir, pricing, clock
):
    """Month pagination is its own time control; a 7-day window would blank it.

    Everything else still applies, because those say what is being counted
    rather than when.
    """
    ingest(
        store,
        sessions_dir,
        pricing,
        [simple(clock, "a"), simple(clock + timedelta(days=40), "b")],
    )
    at = int(clock.timestamp() * 1000)
    windowed = A.Filters(start_ms=at - 3600_000, end_ms=at + 3600_000)
    # The window really does bite everywhere else...
    assert A.totals(store, pricing, windowed)["requests"] < (
        A.totals(store, pricing, A.Filters())["requests"]
    )
    # ...and is deliberately ignored here.
    assert len(A.calendar(store, pricing, windowed)["days"]) == 2

    narrowed = A.calendar(store, pricing, A.Filters(models=["nothing-uses-this"]))
    assert narrowed["days"] == []


def test_calendar_names_the_models_behind_a_day(store, sessions_dir, pricing, clock):
    a = Thread(session_id="s1", rollout_id="m1", clock=clock)
    a.meta().turn_context("gpt-5.6-sol")
    a.request(5_000, 0, 100)
    b = Thread(session_id="s2", rollout_id="m2", clock=clock)
    b.meta().turn_context("gpt-5.6-terra")
    b.request(1_000, 0, 10)
    ingest(store, sessions_dir, pricing, [a, b])

    (day,) = A.calendar(store, pricing, A.Filters())["days"]
    assert [m["key"] for m in day["top"]] == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert sum(m["cost"] for m in day["top"]) == pytest.approx(day["cost"], abs=1e-6)


def test_long_context_surcharge_is_the_gap_to_standard_rates(
    store, sessions_dir, pricing, clock
):
    """The markup for crossing the threshold, which no invoice itemises.

    It is knowable only where both tiers are in hand: the bucket keeps the tier
    the request landed in and nothing about the one it escaped.
    """
    t = Thread(session_id="s", rollout_id="lc", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    t.request(500, 0, 0)  # standard tier
    t.request(5_000, 0, 0)  # over the fixture's 1000-token threshold
    ingest(store, sessions_dir, pricing, [t])

    got = A.totals(store, pricing, A.Filters())
    rate = pricing.get("gpt-5.6-sol")
    expected = 5_000 * (rate.long.input - rate.short.input) / 1e6
    assert got["long_surcharge"] == pytest.approx(expected, abs=1e-9)
    assert got["long_tokens"] == 5_000
    assert got["long_fraction"] == pytest.approx(5_000 / 5_500)


def test_a_standard_tier_corpus_has_no_surcharge(store, sessions_dir, pricing, clock):
    t = Thread(session_id="s", rollout_id="sm", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    t.request(500, 100, 10)
    ingest(store, sessions_dir, pricing, [t])
    got = A.totals(store, pricing, A.Filters())
    assert got["long_surcharge"] == 0.0
    assert got["long_tokens"] == 0


def test_series_surcharge_sums_to_the_scalar_one(store, sessions_dir, pricing, clock):
    t = Thread(session_id="s", rollout_id="ss", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    for hours in range(3):
        t.advance(3600)
        t.request(5_000, 1_000, 50)
    ingest(store, sessions_dir, pricing, [t])

    total = A.series(store, pricing, A.Filters(), bucket="hour")["total"]
    scalar = A.totals(store, pricing, A.Filters())
    assert sum(v or 0 for v in total["surcharge"]) == pytest.approx(
        scalar["long_surcharge"], abs=1e-6
    )
    assert sum(v or 0 for v in total["long_input"]) == scalar["long_tokens"]


def test_context_scatter_bins_every_request(store, sessions_dir, pricing, clock):
    t = Thread(session_id="s", rollout_id="sc", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    for size in (100, 1_000, 10_000, 100_000):
        t.request(size, size // 3, 10)
    ingest(store, sessions_dir, pricing, [t])
    grid = A.context_scatter(store, pricing, A.Filters(), bins=10)
    assert grid["count"] == 4
    assert sum(b["n"] for b in grid["bins"]) == 4


def test_quota_series_tracks_plan_usage(store, sessions_dir, pricing, clock):
    ingest(store, sessions_dir, pricing, [simple(clock, "a")])
    rows = A.quota_series(store, A.Filters())
    assert rows and rows[0]["used_percent"] == pytest.approx(12.5)


def test_tier_expression_respects_per_model_thresholds(pricing):
    sql = A.tier_expression(pricing, "base_model")
    assert "gpt-5.6-sol" in sql and "1000" in sql
    assert sql.startswith("(CASE WHEN input_tokens >")
