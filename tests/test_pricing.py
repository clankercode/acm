"""Rate resolution and cost arithmetic, including the two context tiers."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ccm.pricing import (
    PricingTable,
    Tier,
    compute,
    compute_tier,
    effective_rate,
    split_model,
)

REPO_PRICING = Path(__file__).resolve().parent.parent / "pricing.toml"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("gpt-5.6-sol", ("", "gpt-5.6-sol")),
        ("pooler/gpt-5.6-sol", ("pooler", "gpt-5.6-sol")),
        ("xai/grok-4.5", ("xai", "grok-4.5")),
        (None, ("", "")),
        ("", ("", "")),
    ],
)
def test_split_model(raw, expected):
    assert split_model(raw) == expected


def test_cached_tokens_are_billed_at_the_cache_rate(pricing):
    # 1000 prompt tokens of which 800 were cache hits, 100 output.
    cost = pricing.cost("gpt-5.6-sol", 1000, 800, 100)
    expected = (200 * 5.0 + 800 * 0.5 + 100 * 30.0) / 1e6
    assert cost.cost == pytest.approx(expected)
    # The counterfactual charges every prompt token at the full rate.
    assert cost.uncached == pytest.approx((1000 * 5.0 + 100 * 30.0) / 1e6)
    assert cost.saved == pytest.approx(cost.uncached - cost.cost)


def test_long_context_uses_the_upper_tier(pricing):
    # The fixture threshold is 1000, so 1001 prompt tokens crosses it.
    short = pricing.cost("gpt-5.6-sol", 1000, 0, 0)
    long = pricing.cost("gpt-5.6-sol", 1001, 0, 0)
    assert short.cost == pytest.approx(1000 * 5.0 / 1e6)
    assert long.cost == pytest.approx(1001 * 10.0 / 1e6)


def test_tier_can_be_forced_for_aggregates(pricing):
    """A bucket knows its tier from the grouping key, not its summed size."""
    # Summed tokens look "long", but these were short-tier requests.
    forced = pricing.cost("gpt-5.6-sol", 50_000, 0, 0, long_context=False)
    assert forced.cost == pytest.approx(50_000 * 5.0 / 1e6)
    inferred = pricing.cost("gpt-5.6-sol", 50_000, 0, 0)
    assert inferred.cost == pytest.approx(50_000 * 10.0 / 1e6)


def test_model_without_a_long_tier_keeps_short_rates(pricing):
    small = pricing.cost("gpt-5.6-terra", 100, 0, 0)
    big = pricing.cost("gpt-5.6-terra", 100_000, 0, 0)
    assert small.cost / 100 == pytest.approx(big.cost / 100_000)


def test_provider_prefix_falls_back_to_the_base_model(pricing):
    assert pricing.get("pooler/gpt-5.6-sol") == pricing.get("gpt-5.6-sol")
    assert pricing.cost("pooler/gpt-5.6-sol", 100, 0, 0).cost == pytest.approx(
        pricing.cost("gpt-5.6-sol", 100, 0, 0).cost
    )


def test_inherit_copies_then_overrides(pricing):
    base = pricing.get("gpt-5.6-terra")
    cheap = pricing.get("cheap")
    assert cheap.short.input == 1.0  # overridden
    assert cheap.short.output == base.short.output  # inherited


def test_unknown_model_is_reported_not_guessed(pricing):
    assert pricing.get("some-new-model") is None
    cost = pricing.cost("some-new-model", 1000, 500, 100)
    assert cost.cost == 0.0 and cost.priced is False
    assert "some-new-model" in pricing.unpriced()


def test_effective_rate_is_dollars_per_million_input():
    assert effective_rate(2.0, 1_000_000) == pytest.approx(2.0)
    assert effective_rate(1.0, 4_000_000) == pytest.approx(0.25)
    assert effective_rate(5.0, 0) == 0.0


def test_effective_rate_ranks_caching_correctly():
    """The metric the dashboard leads with: lower is better, regardless of scale."""
    worse = effective_rate(4_445.35, 2_376_756_293)
    better = effective_rate(20_003.58, 31_608_513_390)
    assert better < worse


def test_hot_reload_picks_up_an_edit(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('[models."m"]\ninput = 1.0\ncached_input = 0.1\noutput = 2.0\n')
    table = PricingTable(path)
    first = table.cost("m", 1_000_000, 0, 0).cost
    assert first == pytest.approx(1.0)

    import os, time
    path.write_text('[models."m"]\ninput = 3.0\ncached_input = 0.3\noutput = 2.0\n')
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert table.maybe_reload() is True
    assert table.cost("m", 1_000_000, 0, 0).cost == pytest.approx(3.0)
    assert table.maybe_reload() is False


def test_compute_tier_is_linear_and_additive():
    tier = Tier(input=5.0, cached_input=0.5, output=30.0)
    whole = compute_tier(tier, 1000, 600, 80)
    halves = [compute_tier(tier, 500, 300, 40) for _ in range(2)]
    assert whole.cost == pytest.approx(sum(h.cost for h in halves))
    assert whole.uncached == pytest.approx(sum(h.uncached for h in halves))


def test_compute_with_no_rate_is_zero_and_unpriced():
    assert compute(None, 100, 50, 10).priced is False


# -- the shipped table -----------------------------------------------------


def test_shipped_rates_match_the_published_figures():
    table = PricingTable(REPO_PRICING)
    sol = table.get("gpt-5.6-sol")
    assert (sol.short.input, sol.short.cached_input, sol.short.output) == (5.0, 0.5, 30.0)
    assert (sol.long.input, sol.long.cached_input, sol.long.output) == (10.0, 1.0, 45.0)
    assert sol.threshold == 272_000

    terra = table.get("gpt-5.6-terra")
    assert (terra.short.input, terra.short.output) == (2.5, 15.0)
    assert (terra.long.input, terra.long.output) == (5.0, 22.5)

    # gpt-5.4-mini publishes no long tier, so it must not invent one.
    mini = table.get("gpt-5.4-mini")
    assert mini.long == mini.short


def test_shipped_table_covers_every_model_in_the_corpus():
    """Guards against a new model silently costing zero."""
    table = PricingTable(REPO_PRICING)
    seen = [
        "gpt-5.5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "pooler/gpt-5.6-sol",
        "pooler/gpt-5.6-terra",
        "pooler/gpt-5.4-mini",
        "grok-4.5",
        "xai/grok-4.5",
        "xai/grok-composer-2.5-fast",
    ]
    missing = [m for m in seen if table.get(m) is None]
    assert missing == []


def test_estimated_rates_are_declared_as_such():
    """Anything inferred rather than published must be flagged for the UI."""
    raw = tomllib.loads(REPO_PRICING.read_text())
    composer = raw["models"]["grok-composer-2.5-fast"]
    assert composer["estimated"] is True
    assert composer["long_tier_unknown"] is True
    table = PricingTable(REPO_PRICING)
    assert table.get("grok-composer-2.5-fast").estimated is True


def test_every_shipped_model_cites_a_source():
    """Rates must be traceable, whether stated directly or inherited."""
    table = PricingTable(REPO_PRICING)
    raw = tomllib.loads(REPO_PRICING.read_text())
    for name in raw["models"]:
        assert table.get(name).source, f"{name} has no pricing source"
