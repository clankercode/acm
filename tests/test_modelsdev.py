"""The models.dev cross-check.

`pricing.toml` remains the source of truth. What is tested here is that the
second opinion is useful rather than noisy: a difference that could change a
figure must be flagged, and a difference that provably cannot must not be.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from ccm.modelsdev import ModelsDev
from ccm.pricing import PricingTable

CATALOGUE = {
    "openai": {
        "models": {
            "gpt-5.6-sol": {
                # models.dev quotes a cache-write rate here; OpenAI does not
                # charge one and Codex never reports the tokens.
                "cost": {"input": 5, "output": 30, "cache_read": 0.5, "cache_write": 6.25},
                "limit": {"context": 1_050_000},
            },
            "gpt-5.6-terra": {
                "cost": {"input": 9.99, "output": 15, "cache_read": 0.25},
                "limit": {"context": 1_050_000},
            },
        }
    },
    # A reseller listing the same model at a different price. First-party wins.
    "some-reseller": {
        "models": {
            "gpt-5.6-sol": {"cost": {"input": 7, "output": 40, "cache_read": 0.7}}
        }
    },
}


@pytest.fixture
def rates(tmp_path):
    path = tmp_path / "pricing.toml"
    path.write_text(
        """
[models."gpt-5.6-sol"]
input = 5.0
cached_input = 0.5
output = 30.0
source = "vendor page"

[models."gpt-5.6-terra"]
input = 2.5
cached_input = 0.25
output = 15.0
source = "vendor page"
"""
    )
    return PricingTable(path)


@pytest.fixture
def catalogue(tmp_path):
    path = tmp_path / "models-dev.json"
    path.write_text(json.dumps({"fetched_at": 1785000000.0, "providers": CATALOGUE}))
    return ModelsDev(path)


def by_model(rows):
    return {r["model"]: r for r in rows}


def field(row, name):
    return next(f for f in row["fields"] if f["field"] == name)


def test_a_disagreement_that_carries_tokens_is_flagged(catalogue, rates):
    volumes = {"gpt-5.6-terra": {"fresh": 5_000_000, "cached": 1, "output": 1}}
    row = by_model(catalogue.compare(rates, volumes))["gpt-5.6-terra"]
    assert row["status"] == "differs"
    assert field(row, "input")["state"] == "differs"
    assert field(row, "input")["theirs"] == 9.99


def test_a_disagreement_with_no_tokens_behind_it_is_inert(catalogue, rates):
    """The OpenAI cache-write case, which would otherwise cry wolf forever."""
    volumes = {"gpt-5.6-sol": {"fresh": 10_000, "cached": 5_000, "written": 0, "output": 900}}
    row = by_model(catalogue.compare(rates, volumes))["gpt-5.6-sol"]
    assert field(row, "cache_write")["state"] == "inert"
    assert field(row, "cache_write")["theirs"] == 6.25
    assert field(row, "input")["state"] == "match"
    # Inert never escalates the model's overall status to a discrepancy.
    assert row["status"] == "inert"


def test_the_same_difference_becomes_real_once_tokens_appear(catalogue, rates):
    quiet = {"gpt-5.6-sol": {"written": 0}}
    busy = {"gpt-5.6-sol": {"written": 4_000_000}}
    assert by_model(catalogue.compare(rates, quiet))["gpt-5.6-sol"]["status"] == "inert"
    assert by_model(catalogue.compare(rates, busy))["gpt-5.6-sol"]["status"] == "differs"


def test_first_party_pricing_wins_over_a_reseller(catalogue, rates):
    row = by_model(catalogue.compare(rates, {}))["gpt-5.6-sol"]
    assert row["provider"] == "openai"
    assert row["offers"] == 2
    assert field(row, "input")["theirs"] == 5


def test_a_model_the_catalogue_omits_says_so(catalogue, rates):
    rates.path.write_text(
        rates.path.read_text()
        + '\n[models."private-model"]\ninput = 1.0\ncached_input = 0.1\noutput = 2.0\nsource = "internal"\n'
    )
    rates.reload()
    row = by_model(catalogue.compare(rates, {}))["private-model"]
    assert row["status"] == "unlisted"
    assert row["reference"] is None


def test_real_discrepancies_sort_above_everything_else(catalogue, rates):
    volumes = {
        "gpt-5.6-terra": {"fresh": 5_000_000},
        "gpt-5.6-sol": {"fresh": 900_000_000},
    }
    rows = catalogue.compare(rates, volumes)
    assert rows[0]["model"] == "gpt-5.6-terra"
    assert rows[0]["status"] == "differs"


def test_an_unreachable_network_leaves_the_cache_intact(catalogue, rates, monkeypatch):
    """Offline must degrade the comparison to stale, never lose it."""

    def explode(*args, **kwargs):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    status = catalogue.refresh()

    assert status["available"] is True
    assert "no route to host" in status["error"]
    assert catalogue.rates_for("gpt-5.6-sol") is not None


def test_a_missing_cache_file_is_not_an_error(tmp_path, rates):
    absent = ModelsDev(tmp_path / "never-fetched.json")
    assert absent.status()["available"] is False
    assert absent.rates_for("gpt-5.6-sol") is None
    # The comparison still renders, just with nothing to compare against.
    rows = absent.compare(rates, {})
    assert {r["status"] for r in rows} == {"unlisted"}


def test_a_corrupt_cache_file_is_ignored(tmp_path, rates):
    path = tmp_path / "models-dev.json"
    path.write_text("{ not json")
    assert ModelsDev(path).status()["available"] is False
