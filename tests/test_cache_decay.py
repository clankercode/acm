"""Tests for cache TTL inference from inter-request gaps."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from acm import aggregate as A
from acm.cache_decay import cache_decay
from acm.config import Settings
from acm.scanner import Scanner
from acm.server import create_app
from acm.store import Store

from .conftest import Thread


def ingest(store: Store, sessions_dir, pricing, threads) -> None:
    for t in threads:
        t.write(sessions_dir, f"rollout-2026-07-01T00-00-00-{t.rollout_id}.jsonl")
    Scanner(store, sessions_dir.parents[2]).scan_once()
    A.ensure_buckets_current(store, pricing)


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def test_retention_curve_shows_cliff(store, sessions_dir, pricing):
    """A short idle gap (under the cache TTL) should show high retention, while
    a long gap (past the OpenAI ~5-10m TTL) should show a steep drop.

    Note: ``Thread.request`` advances the clock 2s internally before emitting its
    token_count event, so the measured gap between two consecutive requests is
    the explicit ``advance()`` plus 2s. A 1s advance therefore yields a 3s
    measured gap, which lands in the 0-5s bin.
    """
    clock = datetime(2026, 7, 1, tzinfo=UTC)

    t = Thread(session_id="s-gap", rollout_id="gap", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    t.request(800, 0, 40)  # first request: nothing cached yet
    t.advance(1.0)  # measured gap to next request: 3s -> 0-5s bin, still warm
    t.request(900, 800, 50)  # retention 800/800 = 1.0
    t.advance(900.0)  # measured gap: ~902s -> 10-30m bin, cache expired
    t.request(900, 100, 50)  # retention 100/900 ~= 0.11

    ingest(store, sessions_dir, pricing, [t])

    result = cache_decay(store, A.Filters())

    # Keyed by the actual source name, not a synthetic "all".
    assert "codex" in result["sources"]
    bins = result["sources"]["codex"]
    assert len(bins) > 0

    short = next(b for b in bins if b["gap_start_ms"] == 0)  # 0s-5s
    long_ = next(b for b in bins if b["gap_start_ms"] == 600_000)  # 10m-30m

    assert short["n"] >= 1
    assert short["mean_retention"] > 0.9, "short gap should have high retention"
    assert long_["n"] >= 1
    assert long_["mean_retention"] < 0.3, "15-min gap should show cache expiry"

    # Codex reports no cache writes, so it uses no write tiers.
    assert result["write_tiers"].get("codex", []) == []


def test_cache_decay_endpoint(tmp_path, pricing):
    """The ``/api/cache-decay`` endpoint returns retention bins per source."""
    clock = datetime(2026, 7, 1, tzinfo=UTC)

    sessions_dir = tmp_path / "sessions"
    day_dir = sessions_dir / "2026" / "07" / "01"
    day_dir.mkdir(parents=True)

    t = Thread(session_id="s-api", rollout_id="api", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    t.request(800, 0, 40)
    t.advance(1.0)
    t.request(900, 800, 50)
    t.write(day_dir, "rollout-2026-07-01T12-00-00-api.jsonl")

    settings = Settings(
        sessions_dir=sessions_dir,
        claude_dir=tmp_path / "claude",
        pi_dir=tmp_path / "pi",
        opencode_db=tmp_path / "opencode.db",
        grok_dir=tmp_path / "grok",
        kimi_code_dir=tmp_path / "kimi_code",
        kimi_dir=tmp_path / "kimi",
        hermes_db=tmp_path / "hermes.db",
        copilot_db=tmp_path / "copilot.db",
        gemini_dir=tmp_path / "gemini",
        cursor_agent_dir=tmp_path / "cursor-agent",
        cursor_agent_capture_interval=3600.0,
        sources=("codex", "claude", "pi", "opencode", "grok"),
        db_path=tmp_path / "acm.sqlite",
        pricing_path=pricing.path,
        reference_path=tmp_path / "models-dev.json",
        debounce_seconds=0.05,
        poll_seconds=0.2,
        broadcast_hz=20.0,
        host="127.0.0.1",
        port=0,
    )

    app = create_app(settings, watch=True)
    with TestClient(app) as c:
        assert wait_for(
            lambda: c.get("/api/state").json()["totals"]["requests"] == 2
        ), "initial scan did not complete"

        resp = c.get("/api/cache-decay")
        assert resp.status_code == 200
        body = resp.json()
        assert "codex" in body["sources"]
        bins = body["sources"]["codex"]
        assert len(bins) > 0
        for b in bins:
            assert {"mean_retention", "n", "label"}.issubset(b)


def test_claude_write_tiers_detected(store):
    """Claude writes to both the 5m and 1h cache tiers; ``write_tiers`` must
    report both. Rows are inserted directly to isolate the read path from the
    ingest layer.
    """
    base_ts = 1_800_000_000_000  # arbitrary epoch ms
    rollout = "claude-tier-test"

    store.execute(
        "INSERT INTO sessions (rollout_id, source, session_id, first_ts, last_ts, repo, is_subagent) "
        "VALUES (?, 'claude', ?, ?, ?, 'test', 0)",
        [rollout, "s-claude", base_ts, base_ts + 10_000],
    )
    # Two requests in the same rollout so the LAG window forms a pair. The
    # first carries writes on the 5m tier, the second on the 1h tier.
    store.execute(
        "INSERT INTO requests (source, dk, rank, ts, rollout_id, session_id, model, "
        "input_tokens, cached_tokens, cache_write_tokens, cache_write_1h_tokens, "
        "output_tokens, reasoning_tokens) "
        "VALUES (?, ?, 0, ?, ?, ?, 'claude-sonnet', 1000, 0, 5000, 0, 100, 0)",
        ["claude", "claude-0", base_ts, rollout, "s-claude"],
    )
    store.execute(
        "INSERT INTO requests (source, dk, rank, ts, rollout_id, session_id, model, "
        "input_tokens, cached_tokens, cache_write_tokens, cache_write_1h_tokens, "
        "output_tokens, reasoning_tokens) "
        "VALUES (?, ?, 1, ?, ?, ?, 'claude-sonnet', 1000, 800, 0, 3000, 100, 0)",
        ["claude", "claude-1", base_ts + 5_000, rollout, "s-claude"],
    )

    result = cache_decay(store, A.Filters())
    tiers = result["write_tiers"].get("claude", [])
    assert "5m" in tiers, f"expected 5m tier active, got {tiers}"
    assert "1h" in tiers, f"expected 1h tier active, got {tiers}"

