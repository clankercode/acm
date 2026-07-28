"""End-to-end checks against the real session corpus.

Marked ``corpus`` and skipped when the tree is absent. Run with::

    pytest -m corpus

The corpus is live -- the newest rollout is being appended to while the test
runs -- so nothing here asserts a hard-coded total. Instead the scanner is
checked against an independent reference implementation over *exactly the bytes
it consumed*, which the ``files`` table records as a per-file offset. That makes
the comparison exact regardless of how much the corpus grew mid-test.
"""

from __future__ import annotations

import collections
import json
import os
from pathlib import Path

import pytest

from ccm import aggregate as A
from ccm.pricing import PricingTable, compute_tier
from ccm.scanner import Scanner
from ccm.store import Store

from .conftest import UNPRICED_BY_DESIGN, unpriced_names

CORPUS = Path.home() / ".codex-shared" / "sessions"
REPO_PRICING = Path(__file__).resolve().parent.parent / "pricing.toml"

pytestmark = [
    pytest.mark.corpus,
    pytest.mark.skipif(not CORPUS.exists(), reason="no ~/.codex-shared/sessions"),
]


def reference_scan(offsets: dict[str, int]) -> dict:
    """A deliberately naive second implementation of the same rules.

    Written straight through with stdlib json and plain dicts so that agreeing
    with it means something: it shares no code with the scanner under test.
    """
    seen: dict[tuple, tuple] = {}
    raw = 0
    for path, limit in offsets.items():
        session_id = None
        model = None
        with open(path, "rb") as fh:
            data = fh.read(limit)
        for line in data.split(b"\n"):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind = obj.get("type")
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                if session_id is None:
                    session_id = payload.get("session_id") or ""
            elif kind == "turn_context":
                model = payload.get("model") or model
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                total = info.get("total_token_usage") or {}
                last = info.get("last_token_usage") or {}
                if not total:
                    continue
                raw += 1
                own = (
                    last.get("input_tokens") or 0,
                    last.get("cached_input_tokens") or 0,
                    last.get("output_tokens") or 0,
                    last.get("reasoning_output_tokens") or 0,
                )
                # Cumulative *and* own usage, as the scanner does: a compaction
                # restarts the counters, so the cumulative tuple is not unique
                # within a session.
                key = (
                    session_id or "",
                    total.get("input_tokens") or 0,
                    total.get("cached_input_tokens") or 0,
                    total.get("output_tokens") or 0,
                    total.get("reasoning_output_tokens") or 0,
                    *own,
                )
                ts = obj.get("timestamp") or ""
                value = (ts, *own, model)
                if key not in seen or ts < seen[key][0]:
                    seen[key] = value
    return {"requests": seen, "raw": raw}


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    db = tmp_path_factory.mktemp("corpus") / "ccm.sqlite"
    store = Store(db)
    pricing = PricingTable(REPO_PRICING)
    progress = Scanner(store, CORPUS).scan_once()
    A.ensure_buckets_current(store, pricing)
    yield store, pricing, progress
    store.close()


def test_scan_completes_without_errors(scanned):
    _, _, progress = scanned
    assert progress.errors == 0
    assert progress.files_total > 0
    assert progress.raw_events > progress.new_requests


def test_matches_an_independent_implementation(scanned):
    """The gate: identical bytes in, identical deduped requests out."""
    store, _, _ = scanned
    offsets = {
        row["path"]: row["offset"] for row in store.query("SELECT path, offset FROM files")
    }
    reference = reference_scan(offsets)

    ours = {
        (
            r["session_id"],
            r["cum_in"],
            r["cum_cached"],
            r["cum_out"],
            r["cum_reason"],
            r["input_tokens"],
            r["cached_tokens"],
            r["output_tokens"],
            r["reasoning_tokens"],
        ): r
        for r in store.query("SELECT * FROM requests WHERE source = 'codex'")
    }
    assert len(ours) == len(reference["requests"])
    assert set(ours) == set(reference["requests"])

    for key, ref in reference["requests"].items():
        mine = ours[key]
        assert (
            mine["input_tokens"],
            mine["cached_tokens"],
            mine["output_tokens"],
            mine["reasoning_tokens"],
        ) == ref[1:5], key
        assert mine["model"] == ref[5], key


def test_replay_collapse_is_substantial(scanned):
    """If this ever approaches 1.0 the dedup has stopped working."""
    store, pricing, _ = scanned
    dq = A.data_quality(store, pricing)
    assert dq["replay_ratio"] > 2.5
    assert dq["deduped_requests"] > 50_000


def test_token_invariants_hold_for_effectively_every_request(scanned):
    store, _, _ = scanned
    row = store.one(
        "SELECT COUNT(*) AS n,"
        " SUM(CASE WHEN cached_tokens > input_tokens THEN 1 ELSE 0 END) AS bad_cache,"
        " SUM(CASE WHEN reasoning_tokens > output_tokens THEN 1 ELSE 0 END) AS bad_reason"
        " FROM requests"
    )
    assert row["bad_cache"] == 0, "cached_input must be a subset of input"
    # Reasoning is a subset of output apart from a handful of malformed events.
    assert row["bad_reason"] / row["n"] < 0.005


def test_counter_resets_are_rare_and_lose_nothing(scanned):
    """Compactions restart Codex's counters, so resets are expected -- but rare.

    A ratio was the wrong instrument here: the flag used to fire for every
    request below a high-water mark, so one compaction in one long rollout
    produced 9537 of the corpus's 10286 "regressions" and the ratio measured
    that single file. What matters is that a reset stays exceptional; that the
    requests following one survive dedup rather than colliding with their
    pre-reset namesakes is checked where it can actually be seen, against the
    independent implementation.
    """
    store, _, _ = scanned
    resets = store.one(
        "SELECT COALESCE(SUM(count), 0) AS c FROM anomalies WHERE kind = 'cum_regression'"
    )["c"]
    total = store.one("SELECT COUNT(*) AS n FROM requests")["n"]
    assert resets / total < 0.01


def test_bucket_cost_equals_per_request_cost(scanned):
    """The aggregation invariant, verified on the full corpus rather than a fixture."""
    store, pricing, _ = scanned
    direct = 0.0
    for row in store.query(
        "SELECT model, input_tokens, cached_tokens, output_tokens FROM requests"
    ):
        rate = pricing.get(row["model"])
        if rate is None:
            continue
        direct += compute_tier(
            rate.tier_for(row["input_tokens"]),
            row["input_tokens"],
            row["cached_tokens"],
            row["output_tokens"],
        ).cost
    bucketed = A.totals(store, pricing, A.Filters())["cost"]
    assert bucketed == pytest.approx(direct, rel=1e-9)


def test_rollup_conserves_tokens(scanned):
    store, _, _ = scanned
    a = store.one(
        "SELECT COUNT(*) AS n, SUM(input_tokens) AS i, SUM(cached_tokens) AS c,"
        " SUM(output_tokens) AS o FROM requests"
    )
    b = store.one(
        "SELECT SUM(n) AS n, SUM(input_tokens) AS i, SUM(cached_tokens) AS c,"
        " SUM(output_tokens) AS o FROM bucket_hour"
    )
    assert (a["n"], a["i"], a["c"], a["o"]) == (b["n"], b["i"], b["c"], b["o"])


def test_the_only_unpriced_models_are_the_documented_ones(scanned):
    store, pricing, _ = scanned
    found = unpriced_names(A.data_quality(store, pricing))
    assert found <= UNPRICED_BY_DESIGN, "a new model has no rate"


def test_headline_figures_are_in_the_expected_range(scanned):
    """Bounds, not equalities -- the corpus grows while the suite runs."""
    store, pricing, _ = scanned
    tot = A.totals(store, pricing, A.Filters())
    assert 0.90 < tot["cache_rate"] < 0.96
    assert tot["input_tokens"] > 10e9
    assert tot["cost"] > 10_000
    assert tot["saved"] > tot["cost"]
    # Caching is doing most of the work: well under a quarter of list price.
    assert 0.10 < tot["efficiency"] < 0.30
    assert 0.5 < tot["effective_rate"] < 3.0


def test_long_context_tier_carries_real_volume(scanned):
    """Ignoring the tier would understate spend, so confirm it is not marginal."""
    store, _, _ = scanned
    rows = store.query(
        "SELECT long_ctx, SUM(input_tokens) AS i FROM bucket_hour GROUP BY long_ctx"
    )
    share = {r["long_ctx"]: r["i"] for r in rows}
    assert set(share) == {0, 1}
    long_fraction = share[1] / (share[0] + share[1])
    assert 0.2 < long_fraction < 0.7


def test_routed_traffic_caches_worse_than_direct(scanned):
    """The finding the dashboard exists to surface."""
    store, pricing, _ = scanned
    rows = {r["key"]: r for r in A.breakdown(store, pricing, A.Filters(), "model")}
    direct = rows.get("gpt-5.6-sol")
    routed = rows.get("pooler/gpt-5.6-sol")
    if not (direct and routed):
        pytest.skip("corpus has no pooler/direct pair for gpt-5.6-sol")
    assert routed["cache_rate"] < direct["cache_rate"]
    assert routed["effective_rate"] > direct["effective_rate"]


def test_rescan_of_an_unchanged_corpus_adds_nothing(scanned):
    """Second pass over already-consumed bytes must be inert."""
    store, _, _ = scanned
    before = store.one("SELECT COUNT(*) AS n, SUM(input_tokens) AS i FROM requests")
    offsets = {r["path"]: r["offset"] for r in store.query("SELECT path, offset FROM files")}
    unchanged = [p for p, off in offsets.items() if os.path.getsize(p) == off]
    assert unchanged, "expected at least some quiescent files"

    Scanner(store, CORPUS).scan_once()
    after = store.one("SELECT COUNT(*) AS n, SUM(input_tokens) AS i FROM requests")
    # Only genuinely new bytes may add rows.
    grown = sum(1 for p, off in offsets.items() if os.path.getsize(p) > off)
    if grown == 0:
        assert (after["n"], after["i"]) == (before["n"], before["i"])
    else:
        assert after["n"] >= before["n"]


def test_incremental_halfway_scan_converges(tmp_path):
    """Reading real rollouts in two halves must equal reading them whole.

    Files are copied into a private tree first. Pointing both scans at the live
    corpus would race the rollout currently being appended to, and compare two
    different sets of bytes.
    """
    quiescent = [
        p
        for p in sorted(CORPUS.rglob("rollout-*.jsonl"))
        if 20_000 < os.path.getsize(p) < 3_000_000
    ][:25]
    if len(quiescent) < 5:
        pytest.skip("not enough quiescent rollouts to compare")

    tree = tmp_path / "copy" / "2026" / "07" / "01"
    tree.mkdir(parents=True)
    payloads = {}
    for path in quiescent:
        data = path.read_bytes()
        payloads[tree / path.name] = data
        (tree / path.name).write_bytes(data)

    root = tmp_path / "copy"
    fresh = Store(tmp_path / "fresh.sqlite")
    Scanner(fresh, root).scan_once()
    expected = collections.Counter(
        (r["session_id"], r["cum_in"], r["cum_out"], r["ts"])
        for r in fresh.query("SELECT session_id, cum_in, cum_out, ts FROM requests")
    )
    fresh.close()
    assert expected, "expected the copied rollouts to yield requests"

    # Truncate every copy to just over half, deliberately mid-line.
    for target, data in payloads.items():
        target.write_bytes(data[: int(len(data) * 0.55)])

    staged = Store(tmp_path / "staged.sqlite")
    scanner = Scanner(staged, root)
    scanner.scan_once()
    partial = staged.one("SELECT COUNT(*) AS n FROM requests")["n"]
    assert 0 < partial < sum(expected.values()), "halfway scan should be incomplete"

    for target, data in payloads.items():
        target.write_bytes(data)
    scanner.scan_once()

    got = collections.Counter(
        (r["session_id"], r["cum_in"], r["cum_out"], r["ts"])
        for r in staged.query("SELECT session_id, cum_in, cum_out, ts FROM requests")
    )
    staged.close()
    assert got == expected
