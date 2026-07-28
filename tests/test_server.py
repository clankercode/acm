"""HTTP surface and the live-update path.

The important test here is `test_appending_to_a_rollout_reaches_a_live_client`:
it appends to a session file while a stream is open and asserts the numbers move
without anyone asking. That is the whole product promise.

The streaming tests run against a real uvicorn server rather than Starlette's
TestClient. The SSE endpoint only ends when the client disconnects, and
TestClient's portal waits for the generator to finish on block exit -- so the
two deadlock each other. A real socket also exercises the path we actually ship.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from ccm.config import Settings
from ccm.server import create_app

from .conftest import Thread


@pytest.fixture
def settings(tmp_path, pricing) -> Settings:
    """A fully isolated corpus.

    Every client root points inside tmp_path, including the ones a given test
    does not use. Settings has no defaults for these on purpose -- an omitted
    root would otherwise fall back to the developer's real session history and
    the test would silently start reading it.
    """
    sessions = tmp_path / "sessions" / "2026" / "07" / "01"
    sessions.mkdir(parents=True)
    return Settings(
        sessions_dir=tmp_path / "sessions",
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
        db_path=tmp_path / "ccm.sqlite",
        pricing_path=pricing.path,
        reference_path=tmp_path / "models-dev.json",
        debounce_seconds=0.05,
        poll_seconds=0.2,
        broadcast_hz=20.0,
        host="127.0.0.1",
        port=0,
    )


@pytest.fixture
def day_dir(settings):
    return settings.sessions_dir / "2026" / "07" / "01"


def seed(day_dir, clock, rollout_id="a", model="gpt-5.6-sol") -> Thread:
    t = Thread(session_id="s-" + rollout_id, rollout_id=rollout_id, clock=clock)
    t.meta().turn_context(model)
    t.request(1_000, 800, 50)
    t.request(1_200, 1_000, 60)
    t.write(day_dir, f"rollout-2026-07-01T12-00-00-{rollout_id}.jsonl")
    return t


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


@pytest.fixture
def client(settings, day_dir, clock):
    seed(day_dir, clock)
    app = create_app(settings, watch=True)
    with TestClient(app) as c:
        # Wait for the generation to advance too, not just for totals to be
        # right. /api/state computes totals on demand before the first rollup
        # lands, so requests==2 can be true while generation is still 0 -- and a
        # test that then asserts the generation is stable would race it.
        assert wait_for(lambda: settled(c.get("/api/state").json()))
        yield c


def settled(state: dict) -> bool:
    return state["generation"] >= 1 and state["totals"]["requests"] == 2


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(settings, day_dir, clock):
    """A real uvicorn instance, for tests that hold an SSE connection open."""
    import uvicorn

    seed(day_dir, clock)
    port = free_port()
    app = create_app(replace(settings, port=port), watch=True)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert wait_for(lambda: server.started, timeout=20), "server did not start"

    base = f"http://127.0.0.1:{port}"
    assert wait_for(
        lambda: settled(httpx.get(f"{base}/api/state", timeout=5).json())
    ), "initial scan did not complete"
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def test_state_reports_a_completed_scan(client):
    state = client.get("/api/state").json()
    # "discovering" belongs in the set: the fixture waits for the totals, not for
    # the worker to settle, so the next poll cycle's walk can already be under way
    # by the time this request is answered. Without it the test fails about one
    # run in six, which is worse than useless.
    assert state["scan"]["phase"] in ("tailing", "updating", "scanning", "discovering")
    assert state["totals"]["requests"] == 2
    assert state["totals"]["cache_rate"] == pytest.approx(1800 / 2200)
    assert state["dimensions"]["models"] == ["gpt-5.6-sol"]
    assert "gpt-5.6-sol" in state["pricing"]["models"]


def test_series_endpoint_shapes(client):
    body = client.get("/api/series?bucket=hour&group=model").json()
    assert body["bucket_seconds"] == 3600
    assert [g["key"] for g in body["groups"]] == ["gpt-5.6-sol"]
    assert len(body["t"]) == len(body["total"]["cost"])


def test_series_rejects_bad_arguments(client):
    assert client.get("/api/series?bucket=fortnight").status_code == 400
    assert client.get("/api/series?group=colour").status_code == 400
    assert client.get("/api/breakdown/colour").status_code == 400


def test_filters_apply_over_http(client, day_dir, clock):
    everything = client.get("/api/totals").json()
    filtered = client.get("/api/totals?model=nonexistent").json()
    assert everything["requests"] == 2
    assert filtered["requests"] == 0


def test_direct_provider_filter_uses_the_sentinel(client):
    """Provider "" means direct routing, which an empty query param cannot say."""
    body = client.get("/api/totals?provider=direct").json()
    assert body["requests"] == 2


def test_session_detail_and_404(client):
    listing = client.get("/api/sessions").json()
    assert listing["total"] == 1
    rollout_id = listing["rows"][0]["rollout_id"]
    detail = client.get(f"/api/sessions/{rollout_id}").json()
    assert len(detail["requests"]) == 2
    assert client.get("/api/sessions/does-not-exist").status_code == 404


def test_pricing_edit_changes_costs_without_a_rescan(client):
    """Editing the short tier must leave the long tier and output rate alone.

    The seeded rollout straddles the fixture's 1000-token threshold: the
    1000-token request bills short, the 1200-token one bills long.
    """
    short = (200 * 5.0 + 800 * 0.5 + 50 * 30.0) / 1e6
    long = (200 * 10.0 + 1000 * 1.0 + 60 * 45.0) / 1e6
    before = client.get("/api/totals").json()["cost"]
    assert before == pytest.approx(short + long)
    # A corpus total, not the live pass's counters: the worker keeps tailing while
    # the test runs, so `scan.files_done` legitimately moves without anything
    # having been re-read, and asserting on it fails about one run in six.
    read_before = client.get("/api/state").json()["quality"]["raw_token_events"]

    client.put(
        "/api/pricing",
        json={"models": {"gpt-5.6-sol": {"input": 50.0, "cached_input": 5.0}}},
    ).raise_for_status()

    dearer_short = (200 * 50.0 + 800 * 5.0 + 50 * 30.0) / 1e6
    after = client.get("/api/totals").json()["cost"]
    assert after == pytest.approx(dearer_short + long)
    # The corpus was not re-read; only the rate table changed.
    assert client.get("/api/state").json()["quality"]["raw_token_events"] == read_before


def test_long_tier_can_be_edited_independently(client):
    before = client.get("/api/totals").json()["cost"]
    client.put(
        "/api/pricing",
        json={"models": {"gpt-5.6-sol": {"long": {"input": 20.0, "cached_input": 2.0}}}},
    ).raise_for_status()
    after = client.get("/api/totals").json()["cost"]
    # Only the single long-tier request repriced.
    delta = (200 * (20.0 - 10.0) + 1000 * (2.0 - 1.0)) / 1e6
    assert after == pytest.approx(before + delta)


def test_raising_the_threshold_moves_a_request_between_tiers(client):
    """A threshold edit invalidates the rollup, unlike a rate edit."""
    before = client.get("/api/totals").json()["cost"]
    client.put(
        "/api/pricing",
        json={"models": {"gpt-5.6-sol": {"long_context_threshold": 100_000}}},
    ).raise_for_status()
    after = client.get("/api/totals").json()["cost"]
    both_short = (
        (200 * 5.0 + 800 * 0.5 + 50 * 30.0) + (200 * 5.0 + 1000 * 0.5 + 60 * 30.0)
    ) / 1e6
    assert after == pytest.approx(both_short)
    assert after < before


def test_pricing_edit_preserves_comments_in_the_file(client, settings):
    settings.pricing_path.write_text(
        "# keep me\n" + settings.pricing_path.read_text()
    )
    client.put("/api/pricing", json={"models": {"gpt-5.6-sol": {"input": 7.0}}})
    text = settings.pricing_path.read_text()
    assert "# keep me" in text
    assert "7" in text


def test_pricing_endpoint_rejects_junk(client):
    assert client.put("/api/pricing", json={"nope": 1}).status_code == 400


def test_stream_opens_with_a_hello_snapshot(live_server):
    with httpx.stream("GET", f"{live_server}/api/stream", timeout=20) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        event, data = event_reader(response)()
        assert event == "hello"
        assert data["totals"]["requests"] == 2
        assert "scan" in data and "pricing" in data


def test_appending_to_a_rollout_reaches_a_live_client(live_server, day_dir, clock):
    """The product promise: new usage shows up without a refresh."""
    thread = Thread(session_id="s-a", rollout_id="a", clock=clock)
    thread.meta().turn_context("gpt-5.6-sol")
    thread.request(1_000, 800, 50)
    thread.request(1_200, 1_000, 60)
    path = day_dir / "rollout-2026-07-01T12-00-00-a.jsonl"

    with httpx.stream("GET", f"{live_server}/api/stream", timeout=30) as response:
        read = event_reader(response)
        event, hello = read()
        assert event == "hello"
        baseline = hello["totals"]["requests"]

        # A badly cached request lands while the client is listening.
        thread.request(5_000, 100, 400)
        with path.open("a") as fh:
            fh.write(thread.lines[-1] + "\n")

        deadline = time.time() + 25
        seen = None
        while time.time() < deadline and seen is None:
            event, data = read()
            if event == "data" and data["totals"]["requests"] > baseline:
                seen = data
        assert seen is not None, "no data event carried the appended request"
        assert seen["totals"]["requests"] == baseline + 1
        assert seen["totals"]["input_tokens"] == 2_200 + 5_000
        # The poorly cached request must drag the headline numbers the right way.
        assert seen["totals"]["cache_rate"] < 1800 / 2200
        assert seen["totals"]["effective_rate"] > hello["totals"]["effective_rate"]


def test_stream_ends_when_the_client_disconnects(live_server):
    """Closing the connection must let the generator finish, not leak a task."""
    with httpx.stream("GET", f"{live_server}/api/stream", timeout=20) as response:
        event, _ = event_reader(response)()
        assert event == "hello"
    # A second stream still works, so the first was cleaned up.
    with httpx.stream("GET", f"{live_server}/api/stream", timeout=20) as response:
        event, _ = event_reader(response)()
        assert event == "hello"


def test_a_new_rollout_file_is_picked_up(client, day_dir, clock):
    before = client.get("/api/state").json()["totals"]["requests"]
    seed(day_dir, clock + timedelta(hours=1), rollout_id="b")
    state = wait_for(
        lambda: (
            s := client.get("/api/state").json()
        )["totals"]["requests"] > before and s
    )
    assert state is not None
    assert state["totals"]["requests"] == before + 2
    assert len(client.get("/api/sessions").json()["rows"]) == 2


def test_generation_advances_only_on_real_change(client, day_dir, clock):
    first = client.get("/api/state").json()["generation"]
    time.sleep(1.0)  # several poll cycles with nothing new
    assert client.get("/api/state").json()["generation"] == first

    seed(day_dir, clock + timedelta(hours=2), rollout_id="c")
    assert wait_for(lambda: client.get("/api/state").json()["generation"] > first)


def test_quality_endpoint_exposes_replay_stats(client):
    dq = client.get("/api/quality").json()
    assert dq["deduped_requests"] == 2
    assert dq["raw_token_events"] == 2
    assert dq["unpriced_models"] == []


def test_rescan_rebuilds_from_scratch(client):
    before = client.get("/api/totals").json()
    client.post("/api/rescan?full=true").raise_for_status()
    restored = wait_for(
        lambda: (t := client.get("/api/totals").json())["requests"] == before["requests"]
        and t
    )
    assert restored is not None
    assert restored["cost"] == pytest.approx(before["cost"])


def test_pausing_holds_new_sessions_back_until_resumed(client, day_dir, clock):
    """A paused server must ignore the corpus, and lose nothing by doing so."""
    assert client.post("/api/scan/pause").json() == {"paused": True}
    assert client.get("/api/state").json()["scan"]["paused"] is True
    # The worker settles into the paused phase rather than looking busy forever.
    assert wait_for(
        lambda: client.get("/api/state").json()["scan"]["phase"] == "paused"
    ), "the scan loop did not come to rest"

    seed(day_dir, clock, rollout_id="b")
    client.post("/api/rescan")  # refused, so this cannot be what unblocks it
    # Several poll intervals: if the loop were still running, this is where it
    # would pick the new rollout up.
    time.sleep(settings_poll(client) * 5)
    assert client.get("/api/totals").json()["requests"] == 2

    assert client.post("/api/scan/resume").json() == {"paused": False}
    assert wait_for(lambda: client.get("/api/totals").json()["requests"] == 4)
    assert client.get("/api/state").json()["scan"]["paused"] is False


def settings_poll(client) -> float:
    return client.app.state.engine.settings.poll_seconds


def test_rescan_is_refused_while_paused(client):
    """Dropping the derived tables with no worker to rebuild them would empty
    the dashboard, so a full rescan is rejected rather than queued."""
    client.post("/api/scan/pause").raise_for_status()
    try:
        assert client.post("/api/rescan?full=true").status_code == 409
        assert client.get("/api/totals").json()["requests"] == 2
    finally:
        client.post("/api/scan/resume").raise_for_status()


def test_state_names_the_build_it_was_served_by(client):
    """The dashboard compares this across reconnects to spot an upgrade."""
    build = client.get("/api/state").json()["build"]
    assert build["version"]
    assert len(build["id"]) == 16
    # Stable within a process: a changed id has to mean changed code.
    assert client.get("/api/state").json()["build"] == build


def test_ui_placeholder_when_not_built(client):
    response = client.get("/")
    # Either the built UI is served, or a clear instruction to build it.
    assert response.status_code in (200, 503)
    if response.status_code == 503:
        assert "just build-web" in response.text


def event_reader(response):
    """Return a callable that pulls one SSE frame at a time off an open stream.

    The line iterator is created once and captured: httpx allows a response body
    to be iterated only once, so calling `iter_lines()` per frame raises
    StreamConsumed on the second read.
    """
    lines = response.iter_lines()

    def read() -> tuple[str, dict]:
        event = None
        for raw in lines:
            line = raw if isinstance(raw, str) else raw.decode()
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                return event or "message", json.loads(line[5:])
        raise AssertionError("stream ended before a complete event")

    return read
