"""Carrying stats between machines.

The property that matters is that an import is indistinguishable from having
scanned the same corpus locally: every figure the dashboard shows must come out
the same, and must keep coming out the same after the rate table changes or the
rollup is rebuilt.
"""

from __future__ import annotations

import json

import pytest

from acm import aggregate as A
from acm.pricing import PricingTable
from acm.scanner import Scanner
from acm.store import Store

from .conftest import Thread

METRICS = (
    "requests",
    "input_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "output_tokens",
    "cost",
    "saved",
    "effective_rate",
)


def seed(store, sessions_dir, pricing, clock, *, rollouts=("a", "b")) -> None:
    for i, name in enumerate(rollouts):
        t = Thread(session_id=f"s-{name}", rollout_id=name, clock=clock)
        t.meta().turn_context("gpt-5.6-sol" if i % 2 == 0 else "gpt-5.6-terra")
        t.request(800, 600, 40)
        t.request(1_500, 900, 60)  # over the fixture's 1000-token long threshold
        t.write(sessions_dir, f"rollout-2026-07-01T00-00-00-{name}.jsonl")
    Scanner(store, sessions_dir.parents[2]).scan_once()
    A.ensure_buckets_current(store, pricing)


@pytest.fixture
def seeded(store, sessions_dir, pricing, clock):
    seed(store, sessions_dir, pricing, clock)
    return store


def other_store(tmp_path, name="other.sqlite") -> Store:
    return Store(tmp_path / name)


# ---------------------------------------------------------------------------
# round trip


def test_import_reproduces_every_headline_figure(seeded, pricing, tmp_path):
    """The gate: a bundle must carry the whole dashboard, not an approximation."""
    from acm import portable

    before = A.totals(seeded, pricing, A.Filters())
    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")

    fresh = other_store(tmp_path)
    portable.import_bundle(fresh, bundle)
    after = A.totals(fresh, pricing, A.Filters())

    for key in METRICS:
        assert after[key] == pytest.approx(before[key]), key

    for dimension in ("model", "source", "repo", "provider"):
        a = {r["key"]: round(r["cost"], 9) for r in A.breakdown(seeded, pricing, A.Filters(), dimension)}
        b = {r["key"]: round(r["cost"], 9) for r in A.breakdown(fresh, pricing, A.Filters(), dimension)}
        assert a == b, dimension
    fresh.close()


def test_imported_sessions_survive_with_their_stats(seeded, pricing, tmp_path):
    from acm import portable

    before = {s["rollout_id"]: s for s in A.sessions(seeded, pricing, A.Filters())}
    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    fresh = other_store(tmp_path)
    portable.import_bundle(fresh, bundle)
    after = {s["rollout_id"]: s for s in A.sessions(fresh, pricing, A.Filters())}

    assert set(after) == set(before)
    for key, row in before.items():
        assert after[key]["cost"] == pytest.approx(row["cost"]), key
        assert after[key]["requests"] == row["requests"]
        assert after[key]["input_tokens"] == row["input_tokens"]
        assert after[key]["agent_nickname"] == row["agent_nickname"]
    # ...but they carry no per-request timeline, and say so rather than
    # pretending to have one.
    assert all(not s["has_timeline"] for s in after.values())
    fresh.close()


def test_imported_sessions_recost_when_prices_change(seeded, pricing, tmp_path):
    """Why sessions are exported split by (model, tier) rather than flattened.

    Only tokens travel. If the split were collapsed, a later price edit could
    not be applied correctly to the imported rows.
    """
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    fresh = other_store(tmp_path)
    portable.import_bundle(fresh, bundle)
    original = A.totals(fresh, pricing, A.Filters())["cost"]

    doubled = pricing.path.read_text().replace("input = 5.0", "input = 10.0")
    pricing.path.write_text(doubled)
    pricing.reload()
    A.ensure_buckets_current(fresh, pricing)

    after = A.totals(fresh, pricing, A.Filters())["cost"]
    assert after > original
    fresh.close()


def test_exclusions_are_deliberate(seeded, pricing):
    """Paths and working directories must not leave the machine."""
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    text = json.dumps(bundle)
    assert "/home/dev/project" not in text
    assert "rollout-2026" not in text
    assert "cwd" not in bundle["session_columns"]
    assert "path" not in bundle["session_columns"]
    # The normalised project label does travel -- it is the cross-machine key.
    assert "repo" in bundle["session_columns"]


# ---------------------------------------------------------------------------
# living alongside local data


def test_a_rollup_rebuild_leaves_imported_rows_alone(seeded, pricing, tmp_path):
    """The regression the origin-scoped DELETE exists to prevent.

    Without it the first dirty hour after an import quietly removes the
    imported rows for that hour -- and only for the hours that overlap, so the
    damage is partial and easy to miss.
    """
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    local_cost = A.totals(seeded, pricing, A.Filters())["cost"]
    portable.import_bundle(seeded, bundle, "other-pc")

    seeded.mark_all_hours_dirty()
    A.rebuild_buckets(seeded, pricing)

    by = {r["key"]: r["cost"] for r in A.breakdown(seeded, pricing, A.Filters(), "origin")}
    assert "other-pc" in by, "imported rows were deleted by the rebuild"
    assert by[""] == pytest.approx(local_cost)
    assert by["other-pc"] == pytest.approx(local_cost)


def test_origin_filter_partitions_the_totals(seeded, pricing):
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    portable.import_bundle(seeded, bundle, "other-pc")

    combined = A.totals(seeded, pricing, A.Filters())
    parts = [
        A.totals(seeded, pricing, A.Filters(origins=[o])) for o in ("", "other-pc")
    ]
    assert sum(p["requests"] for p in parts) == combined["requests"]
    assert sum(p["input_tokens"] for p in parts) == combined["input_tokens"]
    assert sum(p["cost"] for p in parts) == pytest.approx(combined["cost"], rel=1e-9)


def test_rescan_keeps_imports_but_rebuilds_local(seeded, pricing, tmp_path, sessions_dir):
    """A rescan cannot re-read another machine's corpus, so it must not drop it."""
    from acm import portable
    from acm.store import DERIVED_TABLES

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    portable.import_bundle(seeded, bundle, "other-pc")

    # What Engine.rescan_from_scratch does.
    for table in DERIVED_TABLES:
        if table == "bucket_hour":
            seeded.execute("DELETE FROM bucket_hour WHERE origin = ''")
        else:
            seeded.execute(f"DELETE FROM {table}")

    assert seeded.one("SELECT COUNT(*) AS n FROM requests")["n"] == 0
    assert seeded.one("SELECT COUNT(*) AS n FROM bucket_hour")["n"] > 0
    assert A.totals(seeded, pricing, A.Filters(origins=["other-pc"]))["requests"] > 0


def test_deleting_a_machine_removes_all_of_it(seeded, pricing):
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    portable.import_bundle(seeded, bundle, "other-pc")
    assert portable.delete_import(seeded, "other-pc") is True

    assert seeded.one(
        "SELECT COUNT(*) AS n FROM bucket_hour WHERE origin = 'other-pc'"
    )["n"] == 0
    assert seeded.one(
        "SELECT COUNT(*) AS n FROM import_sessions WHERE origin = 'other-pc'"
    )["n"] == 0
    assert seeded.one("SELECT COUNT(*) AS n FROM imports")["n"] == 0
    # Local data untouched.
    assert A.totals(seeded, pricing, A.Filters())["requests"] > 0


def test_renaming_a_machine_moves_its_rows(seeded, pricing):
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    portable.import_bundle(seeded, bundle, "laptop")
    portable.rename_import(seeded, "laptop", "the laptop")

    keys = {r["key"] for r in A.breakdown(seeded, pricing, A.Filters(), "origin")}
    assert "the laptop" in keys and "laptop" not in keys
    assert A.totals(seeded, pricing, A.Filters(origins=["the laptop"]))["requests"] > 0


# ---------------------------------------------------------------------------
# labels


def test_an_explicit_label_replaces_rather_than_duplicating(seeded, pricing):
    """Re-importing a colleague's updated bundle under the same name."""
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    portable.import_bundle(seeded, bundle, "laptop")
    portable.import_bundle(seeded, bundle, "laptop")

    origins = [r["origin"] for r in seeded.query("SELECT origin FROM imports")]
    assert origins == ["laptop"]
    assert A.totals(seeded, pricing, A.Filters())["requests"] == 2 * A.totals(
        seeded, pricing, A.Filters(origins=[""])
    )["requests"]


def test_an_unlabelled_import_is_made_unique(seeded, pricing):
    """Two different bundles that happen to share a label must both survive."""
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="laptop")
    portable.import_bundle(seeded, bundle)
    portable.import_bundle(seeded, bundle)

    origins = sorted(r["origin"] for r in seeded.query("SELECT origin FROM imports"))
    assert origins == ["laptop", "laptop (2)"]


def test_preview_reports_a_collision_without_importing(seeded, pricing):
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="laptop")
    first = portable.preview(seeded, bundle)
    assert first["collision"] is False
    assert first["label"] == "laptop"
    assert first["summary"]["requests"] > 0

    portable.import_bundle(seeded, bundle, "laptop")
    second = portable.preview(seeded, bundle)
    assert second["collision"] is True
    assert second["suggested_label"] == "laptop"
    assert second["label"] == "laptop (2)"
    # Preview must not have written anything.
    assert seeded.one("SELECT COUNT(*) AS n FROM imports")["n"] == 1


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"format": "something-else", "version": 1}, "not an Agent Cache Monitor"),
        ({"format": "ccm-export", "version": 99}, "cannot be read"),
        ({"format": "ccm-export", "version": 1}, "missing"),
        ("just a string", "not a JSON object"),
    ],
)
def test_unreadable_bundles_say_why(store, payload, message):
    from acm import portable

    with pytest.raises(portable.BundleError, match=message):
        portable.read_bundle(payload)


def test_a_legacy_ccm_export_bundle_can_still_be_read(seeded, pricing):
    """The ccm-export format is accepted on import for backward compat."""
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="old-pc")
    bundle["format"] = "ccm-export"  # simulate a bundle from the old tool
    info = portable.preview(seeded, bundle)
    assert info["label"] == "old-pc"


# ---------------------------------------------------------------------------
# pooling


def test_a_combined_export_equals_the_sum_of_its_parts(seeded, pricing, tmp_path):
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    one = A.totals(seeded, pricing, A.Filters())
    portable.import_bundle(seeded, bundle, "other-pc")

    combined = portable.export_bundle(seeded, pricing, label="team", origins=None)
    assert combined["summary"]["requests"] == 2 * one["requests"]

    fresh = other_store(tmp_path)
    portable.import_bundle(fresh, combined)
    pooled = A.totals(fresh, pricing, A.Filters())
    assert pooled["requests"] == 2 * one["requests"]
    assert pooled["cost"] == pytest.approx(2 * one["cost"])
    fresh.close()


def test_pooling_never_counts_one_session_twice(seeded, pricing):
    """Buckets from two origins sum; sessions must not.

    A rollout id is a UUID, so the same one under two origins is one session
    that has been round-tripped -- adding it to itself would invent work nobody
    did, and would break the session table's key on the way back in.
    """
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    portable.import_bundle(seeded, bundle, "other-pc")

    combined = portable.export_bundle(seeded, pricing, label="team")
    keys = [(row[0], row[9], row[10]) for row in combined["sessions"]]
    assert len(keys) == len(set(keys))
    assert len(combined["sessions"]) == len(bundle["sessions"])


def test_a_combined_export_merges_matching_keys(seeded, pricing):
    """Two machines in the same hour on the same model become one row.

    Otherwise a pooled bundle would grow linearly with the number of machines
    even when they did identical work.
    """
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    rows_before = len(bundle["buckets"])
    portable.import_bundle(seeded, bundle, "other-pc")

    combined = portable.export_bundle(seeded, pricing, label="team")
    assert len(combined["buckets"]) == rows_before
    assert combined["buckets"][0][8] == 2 * bundle["buckets"][0][8]  # n doubled


def test_provenance_survives_a_second_hop(seeded, pricing, tmp_path):
    from acm import portable

    seeded.set_meta("local_label", "desk")
    first = portable.export_bundle(seeded, pricing, label="desk")

    middle = other_store(tmp_path, "middle.sqlite")
    middle.set_meta("local_label", "laptop")
    portable.import_bundle(middle, first, "desk")
    pooled = portable.export_bundle(middle, pricing, label="both")

    assert sorted(pooled["origins"]) == ["desk", "laptop"]
    middle.close()


def test_exporting_a_selection_leaves_the_rest_out(seeded, pricing):
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    local = A.totals(seeded, pricing, A.Filters())["requests"]
    portable.import_bundle(seeded, bundle, "other-pc")

    just_local = portable.export_bundle(seeded, pricing, label="mine", origins=[""])
    assert just_local["summary"]["requests"] == local

    just_theirs = portable.export_bundle(
        seeded, pricing, label="theirs", origins=["other-pc"]
    )
    assert just_theirs["summary"]["requests"] == local


def test_machine_listing_puts_this_one_first(seeded, pricing):
    from acm import portable

    bundle = portable.export_bundle(seeded, pricing, label="origin-pc")
    portable.import_bundle(seeded, bundle, "other-pc")

    rows = portable.list_origins(seeded, pricing, A)
    assert rows[0]["local"] is True
    assert rows[0]["origin"] == ""
    assert [r["origin"] for r in rows[1:]] == ["other-pc"]
    assert rows[1]["requests"] == rows[0]["requests"]
