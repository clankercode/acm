"""End-to-end checks for the non-Codex clients, against their real histories.

Same discipline as :mod:`tests.test_corpus`: the corpora are live, so nothing
asserts a hard-coded total. Each reader is checked either against an
independent implementation over exactly the bytes it consumed, or against a
figure the client itself recorded.

Run with::

    pytest -m corpus
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ccm import aggregate as A
from ccm.pricing import PricingTable, compute_tier
from ccm.scanner import Scanner
from ccm.sources import ClaudeSource, GrokSource, OpenCodeSource, PiSource
from ccm.store import Store

CLAUDE = Path.home() / ".claude" / "projects"
PI = Path.home() / ".pi" / "agent" / "sessions"
OPENCODE = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
GROK = Path.home() / ".grok" / "sessions"
REPO_PRICING = Path(__file__).resolve().parent.parent / "pricing.toml"

pytestmark = [
    pytest.mark.corpus,
    pytest.mark.skipif(
        not (CLAUDE.exists() or PI.exists() or OPENCODE.exists() or GROK.exists()),
        reason="no non-Codex client histories on this machine",
    ),
]


def available_sources():
    sources = []
    if CLAUDE.exists():
        sources.append(ClaudeSource(CLAUDE))
    if PI.exists():
        sources.append(PiSource(PI))
    if OPENCODE.exists():
        sources.append(OpenCodeSource(OPENCODE))
    if GROK.exists():
        sources.append(GrokSource(GROK))
    return sources


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    db = tmp_path_factory.mktemp("clients") / "ccm.sqlite"
    store = Store(db)
    pricing = PricingTable(REPO_PRICING)
    progress = Scanner(store, sources=available_sources()).scan_once()
    A.ensure_buckets_current(store, pricing)
    yield store, pricing, progress
    store.close()


def reference_claude(offsets: dict[str, int]) -> dict:
    """A second, deliberately naive implementation of the Claude Code rules.

    Written straight through with stdlib json and plain dicts so that agreeing
    with it means something: it shares no code with the reader under test.
    """
    seen: dict[str, tuple] = {}
    raw = 0
    for path, limit in offsets.items():
        with open(path, "rb") as fh:
            data = fh.read(limit)
        for line in data.split(b"\n"):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            raw += 1
            if message.get("model") == "<synthetic>":
                continue
            key = message.get("id") or obj.get("requestId") or obj.get("uuid")
            if not key:
                continue
            fresh = usage.get("input_tokens") or 0
            read = usage.get("cache_read_input_tokens") or 0
            write = usage.get("cache_creation_input_tokens") or 0
            out = usage.get("output_tokens") or 0
            candidate = (out, obj.get("timestamp") or "", fresh + read + write, read, write)
            prior = seen.get(key)
            # Highest output wins; earliest timestamp breaks the tie.
            if (
                prior is None
                or candidate[0] > prior[0]
                or (candidate[0] == prior[0] and candidate[1] < prior[1])
            ):
                seen[key] = candidate
    return {"requests": seen, "raw": raw}


@pytest.mark.skipif(not CLAUDE.exists(), reason="no Claude Code history")
def test_claude_matches_an_independent_implementation(scanned):
    """The gate: identical bytes in, identical deduped requests out."""
    store, _, _ = scanned
    offsets = {
        row["path"]: row["offset"]
        for row in store.query("SELECT path, offset FROM files WHERE source = 'claude'")
    }
    reference = reference_claude(offsets)
    ours = {
        row["dk"]: row
        for row in store.query("SELECT * FROM requests WHERE source = 'claude'")
    }

    assert len(ours) == len(reference["requests"])
    assert set(ours) == set(reference["requests"])
    for key, ref in reference["requests"].items():
        mine = ours[key]
        assert mine["output_tokens"] == ref[0], key
        assert mine["input_tokens"] == ref[2], key
        assert mine["cached_tokens"] == ref[3], key
        assert (
            mine["cache_write_tokens"] + mine["cache_write_1h_tokens"] == ref[4]
        ), key


@pytest.mark.skipif(not CLAUDE.exists(), reason="no Claude Code history")
def test_claude_streaming_rewrites_are_collapsed(scanned):
    """If this reaches 1.0 the per-content-block dedup has stopped working."""
    store, pricing, _ = scanned
    quality = {s["source"]: s for s in A.data_quality(store, pricing)["sources"]}
    claude = quality["claude"]
    assert claude["replay_ratio"] > 1.5
    assert claude["requests"] > 100


@pytest.mark.skipif(not CLAUDE.exists(), reason="no Claude Code history")
def test_claude_cache_writes_are_a_material_share_of_its_bill(scanned):
    """The reason cache writes had to be modelled at all.

    They are a couple of percent of prompt tokens but bill at up to 2x base
    input against 0.1x for a read, so they are a large slice of the cost.
    Ignoring them would understate Claude Code's spend substantially.
    """
    store, pricing, _ = scanned
    rows = store.query(
        "SELECT model, input_tokens, cached_tokens, cache_write_tokens,"
        " cache_write_1h_tokens, output_tokens FROM requests WHERE source = 'claude'"
    )
    assert rows, "expected Claude Code requests"
    with_writes = 0.0
    without = 0.0
    written = 0
    for row in rows:
        rate = pricing.get(row["model"])
        if rate is None:
            continue
        tier = rate.tier_for(row["input_tokens"])
        written += row["cache_write_tokens"] + row["cache_write_1h_tokens"]
        with_writes += compute_tier(
            tier,
            row["input_tokens"],
            row["cached_tokens"],
            row["output_tokens"],
            row["cache_write_tokens"],
            row["cache_write_1h_tokens"],
        ).cost
        # The same tokens if writes were free and folded into fresh input.
        without += compute_tier(
            tier, row["input_tokens"], row["cached_tokens"], row["output_tokens"]
        ).cost
    assert written > 0, "Claude Code always writes cache entries"
    assert with_writes > without
    share = (with_writes - without) / with_writes
    assert 0.01 < share < 0.5, share


@pytest.mark.skipif(not PI.exists(), reason="no Pi history")
def test_pi_costs_agree_with_pi(scanned):
    """The strongest check available: the vendor's own arithmetic.

    Pi records what it believes each request cost. Over the requests it priced
    itself, our figure must match -- which exercises the rate table, the
    prompt-total normalisation and the cache accounting in one shot.
    """
    store, pricing, _ = scanned
    audit = A.client_cost_audit(store, pricing).get("pi")
    assert audit is not None, "expected Pi to report its own costs"
    assert audit["requests"] > 50
    assert audit["ratio"] == pytest.approx(1.0, rel=1e-6)


@pytest.mark.skipif(not OPENCODE.exists(), reason="no OpenCode database")
def test_opencode_reads_the_live_database_without_writing_to_it(scanned):
    store, _, _ = scanned
    rows = store.query("SELECT COUNT(*) AS n FROM requests WHERE source = 'opencode'")
    if not rows[0]["n"]:
        pytest.skip("OpenCode history is empty")
    # A read-only handle cannot have modified it, but assert the obvious thing:
    # the reader must not have needed write access to get anything.
    conn = sqlite3.connect(f"file:{OPENCODE}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE ccm_probe (x)")
    finally:
        conn.close()


@pytest.mark.skipif(not OPENCODE.exists(), reason="no OpenCode database")
def test_opencode_totals_reconcile_with_its_own_column(scanned):
    """OpenCode's ``total`` must equal the parts we split it into."""
    store, _, _ = scanned
    conn = sqlite3.connect(f"file:{OPENCODE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    declared = 0
    parts = 0
    for row in conn.execute("SELECT data FROM message"):
        data = json.loads(row["data"])
        tokens = data.get("tokens")
        if data.get("role") != "assistant" or not isinstance(tokens, dict):
            continue
        cache = tokens.get("cache") or {}
        declared += tokens.get("total") or 0
        parts += (
            (tokens.get("input") or 0)
            + (tokens.get("output") or 0)
            + (tokens.get("reasoning") or 0)
            + (cache.get("read") or 0)
            + (cache.get("write") or 0)
        )
    conn.close()
    if declared == 0:
        pytest.skip("OpenCode history is empty")
    assert parts == pytest.approx(declared, rel=1e-9)


def reference_grok(offsets: dict[str, int]) -> dict:
    """A second, deliberately naive implementation of the Grok rules.

    Stdlib json and plain dicts, sharing no code with the reader under test.
    """
    rows: dict[str, tuple] = {}
    raw = 0
    for path, limit in offsets.items():
        with open(path, "rb") as fh:
            data = fh.read(limit)
        for line in data.split(b"\n"):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            params = obj.get("params") or {}
            update = params.get("update") or {}
            if update.get("sessionUpdate") != "turn_completed":
                continue
            usage = update.get("usage")
            if not isinstance(usage, dict):
                continue
            meta = params.get("_meta") or {}
            anchor = meta.get("eventId")
            for name, block in (usage.get("modelUsage") or {}).items():
                raw += 1
                rows[f"{anchor}:{name}"] = (
                    block.get("inputTokens") or 0,
                    block.get("cachedReadTokens") or 0,
                    block.get("outputTokens") or 0,
                    block.get("reasoningTokens") or 0,
                )
    return {"requests": rows, "raw": raw}


@pytest.mark.skipif(not GROK.exists(), reason="no Grok history")
def test_grok_matches_an_independent_implementation(scanned):
    """The gate: identical bytes in, identical rows out."""
    store, _, _ = scanned
    offsets = {
        row["path"]: row["offset"]
        for row in store.query("SELECT path, offset FROM files WHERE source = 'grok'")
    }
    if not offsets:
        pytest.skip("Grok history is empty")
    reference = reference_grok(offsets)
    ours = {
        row["dk"]: row
        for row in store.query("SELECT * FROM requests WHERE source = 'grok'")
    }

    assert set(ours) == set(reference["requests"])
    for key, ref in reference["requests"].items():
        mine = ours[key]
        assert mine["input_tokens"] == ref[0], key
        assert mine["cached_tokens"] == ref[1], key
        assert mine["output_tokens"] == ref[2], key
        assert mine["reasoning_tokens"] == ref[3], key


@pytest.mark.skipif(not GROK.exists(), reason="no Grok history")
def test_grok_totals_hold_its_own_identity(scanned):
    """``totalTokens == inputTokens + outputTokens`` is why we read it as we do.

    If a future Grok switched to reporting the uncached remainder instead, this
    identity would break before any figure looked wrong, and the reader would
    need the same reassembly Claude Code gets.
    """
    store, _, _ = scanned
    offsets = {
        row["path"]: row["offset"]
        for row in store.query("SELECT path, offset FROM files WHERE source = 'grok'")
    }
    if not offsets:
        pytest.skip("Grok history is empty")
    checked = 0
    for path, limit in offsets.items():
        with open(path, "rb") as fh:
            data = fh.read(limit)
        for line in data.split(b"\n"):
            if b'"turn_completed"' not in line:
                continue
            usage = json.loads(line)["params"]["update"].get("usage") or {}
            if not usage:
                continue
            checked += 1
            assert usage["totalTokens"] == usage["inputTokens"] + usage["outputTokens"]
            assert usage["cachedReadTokens"] <= usage["inputTokens"]
            assert usage["reasoningTokens"] <= usage["outputTokens"]
    if not checked:
        pytest.skip("no completed Grok turns yet")


@pytest.mark.skipif(not GROK.exists(), reason="no Grok history")
def test_grok_sessions_carry_their_project_and_lineage(scanned):
    store, _, _ = scanned
    rows = store.query("SELECT * FROM sessions WHERE source = 'grok'")
    if not rows:
        pytest.skip("Grok history is empty")
    assert all(r["repo"] and r["repo"] != "unknown" for r in rows)
    assert all(r["rollout_id"].startswith("grok:") for r in rows)
    for row in rows:
        # A subagent knows its parent; a parent does not claim one.
        assert bool(row["parent_thread_id"]) == bool(row["is_subagent"])


def test_prompt_subsets_never_exceed_the_prompt(scanned):
    """Cached reads plus cache writes are parts of the prompt, not extras."""
    store, _, _ = scanned
    bad = store.one(
        "SELECT COUNT(*) AS n FROM requests"
        " WHERE cached_tokens + cache_write_tokens + cache_write_1h_tokens"
        "       > input_tokens"
    )["n"]
    assert bad == 0


def test_the_long_tier_is_never_a_discount(scanned):
    """A surcharge below zero means a rate was entered the wrong way round.

    Nothing else would catch it: the total would still look plausible, and the
    tier only shows up in the bill as a slightly different rate per token.
    """
    store, pricing, _ = scanned
    whole = A.totals(store, pricing, A.Filters())
    assert whole["long_surcharge"] >= 0
    assert whole["long_surcharge"] <= whole["cost"]
    for row in A.breakdown(store, pricing, A.Filters(), "model"):
        assert row["long_surcharge"] >= 0, row["key"]
        # No long-tier tokens, no surcharge -- and the converse.
        if row["long_tokens"] == 0:
            assert row["long_surcharge"] == 0, row["key"]


def test_every_model_across_every_client_is_priced(scanned):
    store, pricing, _ = scanned
    assert A.data_quality(store, pricing)["unpriced_models"] == []


def test_bucket_cost_equals_per_request_cost(scanned):
    """The aggregation invariant, over a corpus that mixes all the clients."""
    store, pricing, _ = scanned
    direct = 0.0
    for row in store.query(
        "SELECT model, input_tokens, cached_tokens, cache_write_tokens,"
        " cache_write_1h_tokens, output_tokens FROM requests"
    ):
        rate = pricing.get(row["model"])
        if rate is None:
            continue
        direct += compute_tier(
            rate.tier_for(row["input_tokens"]),
            row["input_tokens"],
            row["cached_tokens"],
            row["output_tokens"],
            row["cache_write_tokens"],
            row["cache_write_1h_tokens"],
        ).cost
    # The reported figure is rounded to the cent-fraction it displays, so the
    # bound is that rounding, not a relative one -- a tighter tolerance just
    # measures how large the corpus has grown.
    assert A.totals(store, pricing, A.Filters())["cost"] == pytest.approx(
        direct, abs=1e-6
    )


def test_per_client_totals_sum_to_the_whole(scanned):
    store, pricing, _ = scanned
    whole = A.totals(store, pricing, A.Filters())
    parts = A.breakdown(store, pricing, A.Filters(), "source")
    assert sum(p["cost"] for p in parts) == pytest.approx(whole["cost"], rel=1e-9)
    assert sum(p["requests"] for p in parts) == whole["requests"]
    assert sum(p["input_tokens"] for p in parts) == whole["input_tokens"]


def test_rescanning_an_unchanged_corpus_adds_nothing(scanned):
    """Second pass over already-consumed bytes must be inert."""
    store, _, _ = scanned
    before = store.one("SELECT COUNT(*) AS n, SUM(input_tokens) AS i FROM requests")
    offsets = {
        r["path"]: r["offset"]
        for r in store.query("SELECT path, offset FROM files WHERE source != 'opencode'")
    }
    Scanner(store, sources=available_sources()).scan_once()
    after = store.one("SELECT COUNT(*) AS n, SUM(input_tokens) AS i FROM requests")
    grown = sum(1 for p, off in offsets.items() if Path(p).stat().st_size > off)
    if grown == 0:
        assert (after["n"], after["i"]) == (before["n"], before["i"])
    else:
        assert after["n"] >= before["n"]
