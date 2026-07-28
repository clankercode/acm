"""Each client reader, against synthetic fixtures.

The formats disagree about almost everything that matters for costing --
whether "input tokens" includes the cached ones, whether reasoning is part of
output, whether cache writes exist and are billed, and how the same request
comes to be recorded more than once. These tests pin down each of those
per-client decisions, because getting any of them backwards produces numbers
that look entirely plausible and are wrong.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from ccm import aggregate as A
from ccm.pricing import PricingTable, compute_tier
from ccm.scanner import Scanner
from ccm.sources import (
    ClaudeSource,
    GrokSource,
    HermesSource,
    KimiCliSource,
    KimiCodeSource,
    OpenCodeSource,
    PiSource,
    project_label,
)
from ccm.sources.base import parse_ts

from .conftest import (
    ClaudeTranscript,
    GrokSession,
    PiSession,
    build_opencode_db,
    opencode_message,
    write_grok_config,
)


def rows(store, source: str) -> list:
    return store.query(
        "SELECT * FROM requests WHERE source = ? ORDER BY ts", (source,)
    )


# ---------------------------------------------------------------------------
# Claude Code


def test_claude_reassembles_the_whole_prompt(tmp_path, store, clock):
    """Anthropic reports the uncached remainder; we store the total.

    Left as reported, a 98%-cached request would look like a 400-token prompt
    and the cache rate would be meaningless.
    """
    root = tmp_path / "claude"
    ClaudeTranscript("s1", clock).response(
        fresh=400, cache_read=20_000, cache_write_1h=5_000, output=300
    ).write(root)

    Scanner(store, sources=[ClaudeSource(root)]).scan_once()
    (row,) = rows(store, "claude")
    assert row["input_tokens"] == 25_400
    assert row["cached_tokens"] == 20_000
    assert row["cache_write_1h_tokens"] == 5_000
    assert row["cache_write_tokens"] == 0
    assert row["output_tokens"] == 300


def test_claude_collapses_the_per_block_rewrites(tmp_path, store, clock):
    """One response, five lines, one row -- carrying the final output count."""
    root = tmp_path / "claude"
    ClaudeTranscript("s1", clock).response(
        fresh=10, cache_read=1_000, output=900, blocks=5
    ).write(root)

    Scanner(store, sources=[ClaudeSource(root)]).scan_once()
    got = rows(store, "claude")
    assert len(got) == 1
    assert got[0]["output_tokens"] == 900

    quality = A.data_quality(store, PricingTable())
    assert quality["raw_token_events"] == 5
    assert quality["deduped_requests"] == 1


def test_claude_keeps_the_complete_sighting_whatever_the_order(tmp_path, store, clock):
    """Rank beats arrival order: a truncated copy must never win.

    A resume can copy an early, partial line of a response into a new
    transcript that the scanner happens to read first.
    """
    root = tmp_path / "claude"
    full = ClaudeTranscript("s1", clock)
    full.response(fresh=10, cache_read=1_000, output=900, message_id="msg_fixed")
    full.write(root, "b-later.jsonl")

    partial = ClaudeTranscript("s1", clock - timedelta(hours=1))
    partial.response(
        fresh=10, cache_read=1_000, output=3, message_id="msg_fixed"
    )
    partial.write(root, "a-earlier.jsonl")

    Scanner(store, sources=[ClaudeSource(root)]).scan_once()
    got = rows(store, "claude")
    assert len(got) == 1
    assert got[0]["output_tokens"] == 900


def test_claude_prices_cache_writes_above_the_input_rate(tmp_path, store, clock):
    """A 1-hour cache write costs 2x base input, not 1x and not 0.1x."""
    root = tmp_path / "claude"
    ClaudeTranscript("s1", clock).response(
        fresh=0, cache_read=0, cache_write_1h=1_000_000, output=0
    ).write(root)

    Scanner(store, sources=[ClaudeSource(root)]).scan_once()
    pricing = PricingTable()
    A.ensure_buckets_current(store, pricing)
    total = A.totals(store, pricing, A.Filters())
    # claude-opus-5: $5 base input, $10 for a 1h write.
    assert total["cost"] == pytest.approx(10.0)
    # The counterfactual is the same tokens at plain input rate.
    assert total["uncached_cost"] == pytest.approx(5.0)
    # Caching cost money here rather than saving it, and the tool says so.
    assert total["saved"] == pytest.approx(-5.0)


def test_claude_skips_synthetic_messages(tmp_path, store, clock):
    root = tmp_path / "claude"
    t = ClaudeTranscript("s1", clock)
    t.response(fresh=10, cache_read=100, output=5)
    t.model = "<synthetic>"
    t.response(fresh=0, cache_read=0, output=0)
    t.write(root)

    Scanner(store, sources=[ClaudeSource(root)]).scan_once()
    assert len(rows(store, "claude")) == 1
    anomalies = {r["kind"]: r["count"] for r in store.query("SELECT * FROM anomalies")}
    assert anomalies["synthetic_message"] == 1


def test_claude_marks_sidechain_transcripts_as_subagents(tmp_path, store, clock):
    root = tmp_path / "claude"
    ClaudeTranscript("s1", clock).response(fresh=1, cache_read=1, output=1).write(root)
    sub = ClaudeTranscript("s1", clock, sidechain=True)
    sub.response(fresh=1, cache_read=1, output=1)
    sub.write(root / "s1" / "subagents", "agent-abc.jsonl")

    Scanner(store, sources=[ClaudeSource(root)]).scan_once()
    flags = {
        r["rollout_id"]: r["is_subagent"]
        for r in store.query("SELECT rollout_id, is_subagent FROM sessions")
    }
    assert flags == {"claude:s1": 0, "claude:s1/subagents/agent-abc": 1}


# ---------------------------------------------------------------------------
# Pi


def test_pi_reads_usage_and_the_clients_own_cost(tmp_path, store, clock):
    root = tmp_path / "pi"
    PiSession("p1", clock).header().response(
        fresh=1_000, cache_read=9_000, output=500, reasoning=200, cost=0.0123
    ).write(root)

    Scanner(store, sources=[PiSource(root)]).scan_once()
    (row,) = rows(store, "pi")
    assert row["input_tokens"] == 10_000
    assert row["cached_tokens"] == 9_000
    # Reasoning is inside output for Pi, so it must not be added on top.
    assert row["output_tokens"] == 500
    assert row["reasoning_tokens"] == 200
    assert row["model"] == "minimax/MiniMax-M3"
    assert row["base_model"] == "MiniMax-M3"
    assert row["provider"] == "minimax"
    assert row["client_cost"] == pytest.approx(0.0123)


def test_pi_cost_audit_compares_against_the_client(tmp_path, store, clock):
    """The audit must exclude requests the client priced at zero.

    Pi records a flat zero for anything reached through a subscription proxy.
    Folding those in would make an exact match look like an overcharge.
    """
    root = tmp_path / "pi"
    session = PiSession("p1", clock).header()
    # MiniMax-M3 is $0.30/M input below its 512k long-context threshold.
    session.response(fresh=100_000, cache_read=0, output=0, cost=0.03)
    session.provider = "codex-pooler"
    session.model = "gpt-5.6-luna"
    session.response(fresh=100_000, cache_read=0, output=0, cost=0.0)
    session.write(root)

    Scanner(store, sources=[PiSource(root)]).scan_once()
    audit = A.client_cost_audit(store, PricingTable())["pi"]
    assert audit["requests"] == 1
    assert audit["ratio"] == pytest.approx(1.0)


def test_pi_anchors_compaction_to_the_preceding_request(tmp_path, store, clock):
    root = tmp_path / "pi"
    PiSession("p1", clock).header().response(
        fresh=10, cache_read=10, output=5, response_id="r1"
    ).compaction().write(root)

    Scanner(store, sources=[PiSource(root)]).scan_once()
    (event,) = store.query("SELECT * FROM events WHERE source = 'pi'")
    assert event["dk"] == "r1"
    assert event["kind"] == "context_compacted"


def test_pi_ingest_is_idempotent(tmp_path, store, clock):
    root = tmp_path / "pi"
    PiSession("p1", clock).header().response(
        fresh=10, cache_read=10, output=5, response_id="r1"
    ).write(root)

    scanner = Scanner(store, sources=[PiSource(root)])
    scanner.scan_once()
    scanner.store.reset_file(
        store.query("SELECT path FROM files")[0]["path"]
    )
    scanner.scan_once()
    assert len(rows(store, "pi")) == 1


# ---------------------------------------------------------------------------
# OpenCode


def test_opencode_folds_reasoning_into_billable_output(tmp_path, store, clock):
    """Reasoning is a sibling of output here, but it bills as output."""
    db = build_opencode_db(
        tmp_path / "oc.db",
        [
            opencode_message(
                "m1", 1_780_000_000_000, fresh=500, cache_read=49_000,
                output=200, reasoning=300,
            )
        ],
    )

    Scanner(store, sources=[OpenCodeSource(db)]).scan_once()
    (row,) = rows(store, "opencode")
    assert row["input_tokens"] == 49_500
    assert row["cached_tokens"] == 49_000
    assert row["output_tokens"] == 500
    assert row["reasoning_tokens"] == 300


def test_opencode_picks_up_a_row_edited_after_it_was_first_read(tmp_path, store, clock):
    """Rows are mutable while a response completes; the newest revision wins."""
    path = tmp_path / "oc.db"
    build_opencode_db(
        path,
        [opencode_message("m1", 1_780_000_000_000, fresh=10, cache_read=0, output=5)],
    )
    source = OpenCodeSource(path)
    scanner = Scanner(store, sources=[source])
    scanner.scan_once()
    assert rows(store, "opencode")[0]["output_tokens"] == 5

    build_opencode_db(
        path,
        [
            opencode_message(
                "m1", 1_780_000_000_000, fresh=10, cache_read=0, output=900,
                updated=1_780_000_009_000,
            )
        ],
    )
    scanner.scan_once()
    got = rows(store, "opencode")
    assert len(got) == 1
    assert got[0]["output_tokens"] == 900


def test_opencode_rescanning_unchanged_rows_adds_nothing(tmp_path, store):
    db = build_opencode_db(
        tmp_path / "oc.db",
        [
            opencode_message("m1", 1_780_000_000_000, fresh=10, cache_read=0, output=5),
            opencode_message("m2", 1_780_000_001_000, fresh=20, cache_read=5, output=6),
        ],
    )
    scanner = Scanner(store, sources=[OpenCodeSource(db)])
    scanner.scan_once()
    scanner.scan_once()
    scanner.scan_once()
    assert len(rows(store, "opencode")) == 2
    quality = A.data_quality(store, PricingTable())
    # Re-read rows must not inflate the raw counter, or the duplicate ratio
    # would creep upward on a database that never changed.
    assert quality["sources"][0]["raw_token_events"] == 2


def test_opencode_marks_child_sessions_as_subagents(tmp_path, store):
    import sqlite3

    db = build_opencode_db(
        tmp_path / "oc.db",
        [opencode_message("m1", 1_780_000_000_000, fresh=10, cache_read=0, output=5)],
    )
    conn = sqlite3.connect(db)
    conn.execute("UPDATE session SET parent_id = 'ses_parent'")
    conn.commit()
    conn.close()

    Scanner(store, sources=[OpenCodeSource(db)]).scan_once()
    (row,) = store.query("SELECT is_subagent, depth FROM sessions")
    assert row["is_subagent"] == 1
    assert row["depth"] == 1


# ---------------------------------------------------------------------------
# Grok


def grok_tree(tmp_path):
    """A Grok home with the session root inside it, as the real one is laid out."""
    home = tmp_path / "grok"
    root = home / "sessions"
    root.mkdir(parents=True)
    write_grok_config(
        home,
        llmp_glm_5_2={"model": "glm-5.2", "provider": "llmp", "ctx": 200_000},
    )
    return home, root


def test_grok_keeps_the_prompt_whole_and_reasoning_inside_output(tmp_path, store, clock):
    """Grok follows the OpenAI convention, not Anthropic's.

    ``inputTokens`` is the entire prompt with the cached part inside it, and
    reasoning is inside output -- both forced by its own identity
    ``totalTokens == inputTokens + outputTokens``. Reassembling either would
    double count.
    """
    home, root = grok_tree(tmp_path)
    GrokSession("s1", clock).turn(
        prompt=21_379, cached=2_816, output=6_703, reasoning=5_128
    ).write(root)

    Scanner(store, sources=[GrokSource(root)]).scan_once()
    (row,) = rows(store, "grok")
    assert row["input_tokens"] == 21_379
    assert row["cached_tokens"] == 2_816
    assert row["output_tokens"] == 6_703
    assert row["reasoning_tokens"] == 5_128
    # Nothing to bill for populating the cache: xAI does not charge for it and
    # never reports it either.
    assert row["cache_write_tokens"] == 0
    assert row["cache_write_1h_tokens"] == 0


def test_grok_names_the_gateway_a_routed_model_went_through(tmp_path, store, clock):
    """The usage block says "glm-5.2"; only the config knows it went via llmp.

    Without the join the routed traffic would sit in the "direct" bucket and the
    routed-versus-direct comparison would be quietly wrong.
    """
    home, root = grok_tree(tmp_path)
    GrokSession("s1", clock).turn(prompt=1_000, cached=0, output=10).write(root)

    Scanner(store, sources=[GrokSource(root)]).scan_once()
    (row,) = rows(store, "grok")
    assert row["model"] == "llmp/glm-5.2"
    assert row["provider"] == "llmp"
    assert row["base_model"] == "glm-5.2"
    assert row["ctx_window"] == 200_000


def test_grok_model_with_no_gateway_configured_reads_as_direct(tmp_path, store, clock):
    home, root = grok_tree(tmp_path)
    GrokSession("s1", clock, model="grok-4.5", alias="grok-4.5").turn(
        prompt=500, cached=0, output=5
    ).write(root)

    Scanner(store, sources=[GrokSource(root)]).scan_once()
    (row,) = rows(store, "grok")
    assert row["model"] == "grok-4.5"
    assert row["provider"] == ""


def test_grok_splits_a_turn_that_used_two_models(tmp_path, store, clock):
    """One turn, two models: two rows, so each is costed at its own rate."""
    home, root = grok_tree(tmp_path)
    write_grok_config(
        home,
        llmp_glm_5_2={"model": "glm-5.2", "provider": "llmp"},
        llmp_gpt_5_6_sol={"model": "gpt-5.6-sol", "provider": "llmp"},
    )
    GrokSession("s1", clock).turn(
        prompt=3_000,
        cached=1_000,
        output=300,
        calls=2,
        models={
            "glm-5.2": {
                "inputTokens": 2_000, "outputTokens": 200,
                "cachedReadTokens": 1_000, "reasoningTokens": 0, "modelCalls": 1,
            },
            "gpt-5.6-sol": {
                "inputTokens": 1_000, "outputTokens": 100,
                "cachedReadTokens": 0, "reasoningTokens": 0, "modelCalls": 1,
            },
        },
    ).write(root)

    Scanner(store, sources=[GrokSource(root)]).scan_once()
    got = {r["model"]: r["input_tokens"] for r in rows(store, "grok")}
    assert got == {"llmp/glm-5.2": 2_000, "llmp/gpt-5.6-sol": 1_000}


def test_grok_records_how_many_calls_a_turn_folded_together(tmp_path, store, clock):
    """A turn is not a request, and the difference is reported rather than hidden.

    Grok writes usage once per turn covering ``modelCalls`` API calls, so its
    request count means something different from every other client's. The
    surplus is counted as an anomaly so the Data Quality panel says so.
    """
    home, root = grok_tree(tmp_path)
    GrokSession("s1", clock).turn(prompt=1_000, cached=0, output=10, calls=3).write(root)

    Scanner(store, sources=[GrokSource(root)]).scan_once()
    assert len(rows(store, "grok")) == 1
    (row,) = store.query("SELECT * FROM anomalies WHERE kind = 'multi_call_turn'")
    assert row["count"] == 2


def test_grok_ignores_everything_that_is_not_a_completed_turn(tmp_path, store, clock):
    home, root = grok_tree(tmp_path)
    session = GrokSession("s1", clock)
    session.chatter().turn(prompt=100, cached=0, output=1).chatter().write(root)

    out = Scanner(store, sources=[GrokSource(root)]).scan_once()
    assert len(rows(store, "grok")) == 1
    assert out.raw_events == 1


def test_grok_ingest_is_idempotent(tmp_path, store, clock):
    """Re-reading a log from the top must not double the totals."""
    home, root = grok_tree(tmp_path)
    GrokSession("s1", clock).turn(prompt=1_000, cached=100, output=10).turn(
        prompt=2_000, cached=900, output=20
    ).write(root)

    scanner = Scanner(store, sources=[GrokSource(root)])
    scanner.scan_once()
    store.reset_file(store.query("SELECT path FROM files")[0]["path"])
    scanner.scan_once()

    got = rows(store, "grok")
    assert len(got) == 2
    assert sum(r["input_tokens"] for r in got) == 3_000


def test_grok_resumes_mid_file_after_a_partial_write(tmp_path, store, clock):
    home, root = grok_tree(tmp_path)
    session = GrokSession("s1", clock).turn(prompt=1_000, cached=0, output=10)
    path = session.write(root)

    scanner = Scanner(store, sources=[GrokSource(root)])
    scanner.scan_once()
    assert len(rows(store, "grok")) == 1

    session.turn(prompt=2_000, cached=500, output=20).write(root)
    scanner.scan_once()
    got = rows(store, "grok")
    assert len(got) == 2
    assert got[-1]["input_tokens"] == 2_000
    assert path.exists()


def test_grok_finds_a_subagent_and_its_parent_from_the_tree(tmp_path, store, clock):
    """The link is in the directory layout, so no meta file has to be opened."""
    home, root = grok_tree(tmp_path)
    GrokSession("parent", clock).turn(prompt=500, cached=0, output=5).write(root)
    GrokSession(
        "child", clock, kind="subagent", agent_name="general-purpose"
    ).turn(prompt=800, cached=0, output=9).write(root, parent="parent")

    Scanner(store, sources=[GrokSource(root)]).scan_once()
    by_id = {
        r["rollout_id"]: r
        for r in store.query("SELECT * FROM sessions WHERE source = 'grok'")
    }
    child = by_id["grok:child"]
    assert child["is_subagent"] == 1
    assert child["depth"] == 1
    assert child["parent_thread_id"] == "grok:parent"
    assert child["agent_role"] == "general-purpose"
    assert by_id["grok:parent"]["is_subagent"] == 0


def test_grok_labels_the_project_even_with_no_summary(tmp_path, store, clock):
    """The directory name is the working tree, escaped; that is enough."""
    home, root = grok_tree(tmp_path)
    path = GrokSession("s1", clock).turn(prompt=100, cached=0, output=1).write(root)
    (path.parent / "summary.json").unlink()

    Scanner(store, sources=[GrokSource(root)]).scan_once()
    (row,) = store.query("SELECT cwd, repo FROM sessions WHERE source = 'grok'")
    assert row["cwd"] == "/home/dev/project"
    assert row["repo"] == "project"


# ---------------------------------------------------------------------------
# Kimi Code


def test_kimi_code_reassembles_the_whole_prompt(tmp_path, store, clock):
    """``inputOther`` is the uncached remainder; cache reads/writes are addends."""
    from .conftest import KimiCodeWire

    root = tmp_path / "kc"
    KimiCodeWire("kc1", clock).request(
        fresh=400, cache_read=20_000, cache_write=3_000, output=300, message_id="msg_1"
    ).write(root)

    Scanner(store, sources=[KimiCodeSource(root)]).scan_once()
    (row,) = rows(store, "kimi_code")
    assert row["input_tokens"] == 23_400
    assert row["cached_tokens"] == 20_000
    assert row["cache_write_tokens"] == 3_000
    assert row["output_tokens"] == 300
    assert row["reasoning_tokens"] == 0


def test_kimi_code_attributes_model_from_the_preceding_request(tmp_path, store, clock):
    from .conftest import KimiCodeWire

    root = tmp_path / "kc"
    KimiCodeWire("kc1", clock).request(
        fresh=10, cache_read=10, output=5, message_id="msg_2"
    ).write(root)

    Scanner(store, sources=[KimiCodeSource(root)]).scan_once()
    (row,) = rows(store, "kimi_code")
    assert row["model"] == "kimi-code/kimi-for-coding"
    assert row["base_model"] == "kimi-for-coding"
    assert row["provider"] == "kimi-code"


def test_kimi_code_marks_subagent_threads_from_the_agent_directory(
    tmp_path, store, clock
):
    from .conftest import KimiCodeWire

    root = tmp_path / "kc"
    KimiCodeWire("kc1", clock, agent="main").request(
        fresh=10, cache_read=0, output=1, message_id="parent_req"
    ).write(root)
    KimiCodeWire("kc1", clock, agent="agent-0").request(
        fresh=20, cache_read=0, output=2, message_id="child_req"
    ).write(root)

    Scanner(store, sources=[KimiCodeSource(root)]).scan_once()
    by_id = {
        r["rollout_id"]: r
        for r in store.query("SELECT * FROM sessions WHERE source = 'kimi_code'")
    }
    flags = {rid: r["is_subagent"] for rid, r in by_id.items()}
    assert flags["kimi_code:kc1:main"] == 0
    assert flags["kimi_code:kc1:agent-0"] == 1
    assert by_id["kimi_code:kc1:agent-0"]["parent_thread_id"] == "kimi_code:kc1:main"


def test_kimi_code_ingest_is_idempotent(tmp_path, store, clock):
    from .conftest import KimiCodeWire

    root = tmp_path / "kc"
    KimiCodeWire("kc1", clock).request(
        fresh=10, cache_read=10, output=5, message_id="r1"
    ).write(root)

    scanner = Scanner(store, sources=[KimiCodeSource(root)])
    scanner.scan_once()
    store.reset_file(store.query("SELECT path FROM files")[0]["path"])
    scanner.scan_once()
    assert len(rows(store, "kimi_code")) == 1


def test_kimi_code_resumes_mid_file(tmp_path, store, clock):
    from .conftest import KimiCodeWire

    root = tmp_path / "kc"
    session = KimiCodeWire("kc1", clock).request(
        fresh=10, cache_read=0, output=1, message_id="r1"
    )
    path = session.write(root)

    scanner = Scanner(store, sources=[KimiCodeSource(root)])
    scanner.scan_once()
    assert len(rows(store, "kimi_code")) == 1

    session.request(fresh=20, cache_read=0, output=2, message_id="r2").write(root)
    scanner.scan_once()
    got = {r["dk"]: r for r in rows(store, "kimi_code")}
    assert set(got) == {"r1", "r2"}


# ---------------------------------------------------------------------------
# Kimi CLI


def test_kimi_cli_reassembles_the_whole_prompt(tmp_path, store, clock):
    from .conftest import KimiCliWire

    root = tmp_path / "kl"
    KimiCliWire("kl1", clock).request(
        fresh=400, cache_read=20_000, cache_write=3_000, output=300, message_id="m1"
    ).write(root)

    Scanner(store, sources=[KimiCliSource(root)]).scan_once()
    (row,) = rows(store, "kimi_cli")
    assert row["input_tokens"] == 23_400
    assert row["cached_tokens"] == 20_000
    assert row["cache_write_tokens"] == 3_000
    assert row["output_tokens"] == 300


def test_kimi_cli_records_no_model(tmp_path, store, clock):
    """The wire log never names a model; the row is unpriced by design."""
    from .conftest import KimiCliWire

    root = tmp_path / "kl"
    KimiCliWire("kl1", clock).request(
        fresh=10, cache_read=10, output=5, message_id="m2"
    ).write(root)

    Scanner(store, sources=[KimiCliSource(root)]).scan_once()
    (row,) = rows(store, "kimi_cli")
    assert row["model"] is None


def test_kimi_cli_ingest_is_idempotent(tmp_path, store, clock):
    from .conftest import KimiCliWire

    root = tmp_path / "kl"
    KimiCliWire("kl1", clock).request(
        fresh=10, cache_read=10, output=5, message_id="r1"
    ).write(root)

    scanner = Scanner(store, sources=[KimiCliSource(root)])
    scanner.scan_once()
    store.reset_file(store.query("SELECT path FROM files")[0]["path"])
    scanner.scan_once()
    assert len(rows(store, "kimi_cli")) == 1


# ---------------------------------------------------------------------------
# Hermes


def test_hermes_reassembles_the_whole_prompt(tmp_path, store):
    """Hermes reports cache splits separate from input_tokens (fresh-only)."""
    from .conftest import build_hermes_db

    db = build_hermes_db(
        tmp_path / "h.db",
        [{"model": "glm-5.2", "fresh": 500, "cache_read": 49_000, "output": 200}],
    )

    Scanner(store, sources=[HermesSource(db)]).scan_once()
    (row,) = rows(store, "hermes")
    assert row["input_tokens"] == 49_500
    assert row["cached_tokens"] == 49_000


def test_hermes_folds_reasoning_into_billable_output(tmp_path, store):
    from .conftest import build_hermes_db

    db = build_hermes_db(
        tmp_path / "h.db",
        [{"model": "glm-5.2", "fresh": 0, "output": 200, "reasoning": 300}],
    )

    Scanner(store, sources=[HermesSource(db)]).scan_once()
    (row,) = rows(store, "hermes")
    assert row["output_tokens"] == 500
    assert row["reasoning_tokens"] == 300


def test_hermes_normalizes_model_suffix(tmp_path, store):
    from .conftest import build_hermes_db

    db = build_hermes_db(
        tmp_path / "h.db",
        [{"model": "glm-5.2:cloud", "fresh": 10, "output": 1}],
    )

    Scanner(store, sources=[HermesSource(db)]).scan_once()
    (row,) = rows(store, "hermes")
    assert row["model"] == "glm-5.2"
    assert row["base_model"] == "glm-5.2"


def test_hermes_flags_multi_call_sessions(tmp_path, store):
    from .conftest import build_hermes_db

    db = build_hermes_db(
        tmp_path / "h.db",
        [{"model": "glm-5.2", "fresh": 10, "output": 1, "api_call_count": 5}],
    )

    Scanner(store, sources=[HermesSource(db)]).scan_once()
    (anom,) = store.query(
        "SELECT * FROM anomalies WHERE kind = 'multi_call_turn'"
    )
    assert anom["count"] == 4


def test_hermes_marks_child_sessions_as_subagents(tmp_path, store):
    from .conftest import build_hermes_db

    db = build_hermes_db(
        tmp_path / "h.db",
        [{"model": "glm-5.2", "fresh": 10, "output": 1}],
        parent_id="ses_parent",
    )

    Scanner(store, sources=[HermesSource(db)]).scan_once()
    (row,) = store.query("SELECT is_subagent, depth FROM sessions")
    assert row["is_subagent"] == 1
    assert row["depth"] == 1


def test_hermes_picks_up_an_edited_row(tmp_path, store):
    """Rows are mutated as a session progresses; the newest revision wins."""
    from .conftest import build_hermes_db

    path = tmp_path / "h.db"
    build_hermes_db(
        path,
        [{"model": "glm-5.2", "fresh": 10, "output": 5}],
    )
    source = HermesSource(path)
    scanner = Scanner(store, sources=[source])
    scanner.scan_once()
    assert rows(store, "hermes")[0]["output_tokens"] == 5

    build_hermes_db(
        path,
        [{"model": "glm-5.2", "fresh": 10, "output": 900, "last_seen": 1780000090.0}],
    )
    scanner.scan_once()
    got = rows(store, "hermes")
    assert len(got) == 1
    assert got[0]["output_tokens"] == 900


def test_hermes_rescanning_unchanged_rows_adds_nothing(tmp_path, store):
    from .conftest import build_hermes_db

    db = build_hermes_db(
        tmp_path / "h.db",
        [
            {"model": "glm-5.2", "fresh": 10, "output": 5},
            {
                "model": "glm-5.1",
                "fresh": 20,
                "cache_read": 5,
                "output": 6,
            },
        ],
    )
    scanner = Scanner(store, sources=[HermesSource(db)])
    scanner.scan_once()
    scanner.scan_once()
    assert len(rows(store, "hermes")) == 2


# ---------------------------------------------------------------------------
# cross-cutting


def test_sources_do_not_collide_in_the_store(tmp_path, store, clock, sessions_dir):
    """Two clients using the same dedup string must stay separate rows."""
    from .conftest import Thread

    claude_root = tmp_path / "claude"
    ClaudeTranscript("shared", clock).response(
        fresh=10, cache_read=10, output=5, message_id="collide"
    ).write(claude_root)

    pi_root = tmp_path / "pi"
    PiSession("shared", clock).header().response(
        fresh=20, cache_read=20, output=6, response_id="collide"
    ).write(pi_root)

    Scanner(
        store, sources=[ClaudeSource(claude_root), PiSource(pi_root)]
    ).scan_once()
    assert len(rows(store, "claude")) == 1
    assert len(rows(store, "pi")) == 1
    assert store.one("SELECT COUNT(*) AS n FROM requests")["n"] == 2


def test_one_broken_source_does_not_stop_the_others(tmp_path, store, clock):
    """A client that fails to plan must not take the scan down with it."""

    class Exploding(ClaudeSource):
        name = "claude"

        def plan(self, store):
            raise RuntimeError("corpus on fire")

    pi_root = tmp_path / "pi"
    PiSession("p1", clock).header().response(
        fresh=10, cache_read=10, output=5
    ).write(pi_root)

    progress = Scanner(
        store, sources=[Exploding(tmp_path / "claude"), PiSource(pi_root)]
    ).scan_once()
    assert progress.errors == 1
    assert "corpus on fire" in progress.last_error
    assert len(rows(store, "pi")) == 1


def test_progress_is_reported_per_client(tmp_path, store, clock):
    claude_root = tmp_path / "claude"
    ClaudeTranscript("s1", clock).response(fresh=10, cache_read=10, output=5).write(
        claude_root
    )
    pi_root = tmp_path / "pi"
    PiSession("p1", clock).header().response(
        fresh=10, cache_read=10, output=5
    ).write(pi_root)

    progress = Scanner(
        store, sources=[ClaudeSource(claude_root), PiSource(pi_root)]
    ).scan_once()
    per_source = {s["name"]: s for s in progress.as_dict()["sources"]}
    assert set(per_source) == {"claude", "pi"}
    assert per_source["claude"]["new_requests"] == 1
    assert per_source["pi"]["new_requests"] == 1
    assert progress.bytes_done == sum(
        s["bytes_done"] for s in per_source.values()
    )


def test_absent_corpora_are_skipped_not_errors(tmp_path, store):
    from ccm.config import Settings

    settings = Settings(
        sessions_dir=tmp_path / "nope",
        claude_dir=tmp_path / "nope2",
        pi_dir=tmp_path / "nope3",
        opencode_db=tmp_path / "nope.db",
        grok_dir=tmp_path / "nope4",
        sources=("codex", "claude", "pi", "opencode", "grok"),
        db_path=tmp_path / "db.sqlite",
        pricing_path=tmp_path / "pricing.toml",
        reference_path=tmp_path / "models-dev.json",
        debounce_seconds=0.1,
        poll_seconds=1.0,
        broadcast_hz=4.0,
        host="127.0.0.1",
        port=0,
    )
    scanner = Scanner(store, settings=settings)
    assert scanner.sources == []
    assert scanner.scan_once().errors == 0


# ---------------------------------------------------------------------------
# shared helpers


def test_project_label_names_the_worktree_not_the_subdirectory(tmp_path):
    """A request made from deep inside a repo belongs to that repo."""
    repo = tmp_path / "myproject"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "deep" / "place"
    nested.mkdir(parents=True)
    assert project_label(str(nested)) == "myproject"
    assert project_label(str(repo)) == "myproject"


def test_project_label_falls_back_to_the_directory_name(tmp_path):
    plain = tmp_path / "scratch"
    plain.mkdir()
    assert project_label(str(plain)) == "scratch"
    assert project_label(None) == "unknown"
    assert project_label(None, "ssh://git@host/max/thing.git") == "thing"


def test_parse_ts_accepts_both_shapes_clients_use():
    iso_ms = parse_ts("2026-07-01T12:00:00.000Z")
    assert parse_ts(iso_ms) == iso_ms
    assert parse_ts(iso_ms / 1000) == iso_ms
    assert parse_ts(None) is None
    assert parse_ts("not a date") is None


def test_bucket_costs_still_equal_per_request_costs_across_clients(
    tmp_path, store, clock
):
    """The rollup invariant has to survive cache writes and a mixed corpus."""
    claude_root = tmp_path / "claude"
    t = ClaudeTranscript("s1", clock)
    t.response(fresh=100, cache_read=50_000, cache_write_1h=9_000, output=400)
    t.response(fresh=100, cache_read=80_000, cache_write_5m=2_000, output=600)
    t.write(claude_root)

    pi_root = tmp_path / "pi"
    PiSession("p1", clock).header().response(
        fresh=5_000, cache_read=40_000, output=300
    ).write(pi_root)

    db = build_opencode_db(
        tmp_path / "oc.db",
        [opencode_message("m1", 1_780_000_000_000, fresh=500, cache_read=9_000, output=50)],
    )

    pricing = PricingTable()
    Scanner(
        store,
        sources=[ClaudeSource(claude_root), PiSource(pi_root), OpenCodeSource(db)],
    ).scan_once()
    A.ensure_buckets_current(store, pricing)

    direct = 0.0
    for row in store.query("SELECT * FROM requests"):
        rate = pricing.get(row["model"])
        direct += compute_tier(
            rate.tier_for(row["input_tokens"]),
            row["input_tokens"],
            row["cached_tokens"],
            row["output_tokens"],
            row["cache_write_tokens"],
            row["cache_write_1h_tokens"],
        ).cost
    assert A.totals(store, pricing, A.Filters())["cost"] == pytest.approx(
        direct, rel=1e-9
    )


def test_filtering_by_client_partitions_the_totals(tmp_path, store, clock):
    claude_root = tmp_path / "claude"
    ClaudeTranscript("s1", clock).response(
        fresh=1_000, cache_read=9_000, output=500
    ).write(claude_root)
    pi_root = tmp_path / "pi"
    PiSession("p1", clock).header().response(
        fresh=2_000, cache_read=8_000, output=400
    ).write(pi_root)

    pricing = PricingTable()
    Scanner(store, sources=[ClaudeSource(claude_root), PiSource(pi_root)]).scan_once()
    A.ensure_buckets_current(store, pricing)

    everything = A.totals(store, pricing, A.Filters())
    parts = [
        A.totals(store, pricing, A.Filters(sources=[s])) for s in ("claude", "pi")
    ]
    assert sum(p["cost"] for p in parts) == pytest.approx(everything["cost"])
    assert sum(p["requests"] for p in parts) == everything["requests"]
    assert parts[0]["requests"] == 1
