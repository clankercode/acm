"""The Update button's guards.

Nothing here runs an update. What is under test is the set of refusals, because
this is the one endpoint that runs a shell script: every test that "passes" by
starting an update would be a test that pulled and installed something.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ccm.config import Settings
from ccm.selfupdate import Updater
from ccm.server import create_app


def fake_checkout(tmp_path):
    """A directory that passes every structural check the updater makes."""
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "packaging").mkdir()
    (root / "justfile").write_text("update:\n    true\n")
    (root / "pyproject.toml").write_text('[project]\nname = "codex-cache-monitor"\n')
    (root / "packaging" / "self-update.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    return root


@pytest.fixture
def base(tmp_path, pricing) -> Settings:
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
        sources=(),
        db_path=tmp_path / "state" / "ccm.sqlite",
        pricing_path=pricing.path,
        reference_path=tmp_path / "models-dev.json",
        debounce_seconds=0.05,
        poll_seconds=0.2,
        broadcast_hz=20.0,
        host="127.0.0.1",
        port=0,
    )


def test_without_a_configured_checkout_there_is_nothing_to_update(base):
    status = Updater(base).status()
    assert status.available is False
    assert "CCM_CHECKOUT" in (status.reason or "")


def test_a_directory_that_is_not_a_ccm_checkout_is_refused(base, tmp_path):
    other = tmp_path / "somewhere-else"
    (other / ".git").mkdir(parents=True)
    updater = Updater(replace(base, checkout_path=other))
    # The point of the check: pulling and installing *some other* repo as this
    # program is worse than not updating.
    assert "does not look like a ccm checkout" in (updater.status().reason or "")

    missing = Updater(replace(base, checkout_path=tmp_path / "nope"))
    assert "not a directory" in (missing.status().reason or "")


def test_a_real_looking_checkout_is_available(base, tmp_path):
    updater = Updater(replace(base, checkout_path=fake_checkout(tmp_path)))
    status = updater.status()
    assert status.available is True
    assert status.reason is None
    assert status.running is False


def test_updates_are_refused_from_another_machine_unless_opted_in(base, tmp_path):
    settings = replace(base, checkout_path=fake_checkout(tmp_path))
    updater = Updater(settings)
    assert updater.may_start_from("127.0.0.1") is None
    assert updater.may_start_from("::1") is None
    # The whole reason the guard exists: the dashboard binds every interface and
    # has no authentication.
    refusal = updater.may_start_from("192.168.1.50")
    assert refusal and "only allowed from the machine" in refusal
    # Not an address at all: refused rather than assumed local, since "no peer"
    # and "a proxy in front of us" both arrive looking like this.
    assert updater.may_start_from(None)
    assert updater.may_start_from("localhost")
    assert updater.may_start_from("")

    opted_in = Updater(replace(settings, update_from_lan=True))
    assert opted_in.may_start_from("192.168.1.50") is None


def test_an_update_in_flight_blocks_a_second_one(base, tmp_path):
    updater = Updater(replace(base, checkout_path=fake_checkout(tmp_path)))
    updater.marker_path.parent.mkdir(parents=True, exist_ok=True)
    updater.marker_path.write_text(f"{time.time():.0f}\n")

    assert updater.running() is True
    assert updater.status().available is False
    with pytest.raises(RuntimeError, match="already running"):
        updater.start()

    # And the marker stops meaning anything once the script says it finished.
    updater.done_path.write_text("status=ok\n")
    assert updater.running() is False
    assert updater.outcome() == "ok"
    assert updater.status().available is True


def test_a_stale_marker_does_not_wedge_the_button(base, tmp_path):
    """A killed update must not leave the monitor unable to update forever."""
    updater = Updater(replace(base, checkout_path=fake_checkout(tmp_path)))
    updater.marker_path.parent.mkdir(parents=True, exist_ok=True)
    updater.marker_path.write_text("old\n")
    import os

    ancient = time.time() - 3600
    os.utime(updater.marker_path, (ancient, ancient))

    assert updater.running() is False
    assert updater.status().available is True


def test_the_endpoint_refuses_a_remote_caller(base, tmp_path):
    """The guard belongs to the server, not to the button that usually hides it."""
    settings = replace(base, checkout_path=fake_checkout(tmp_path))
    app = create_app(settings, watch=False)
    with TestClient(app, client=("192.168.1.50", 9999)) as client:
        assert client.get("/api/update").json()["available"] is False
        response = client.post("/api/update")
        assert response.status_code == 403
        assert "only allowed from the machine" in response.json()["detail"]
        # Nothing was started, so nothing is in flight.
        assert Updater(settings).running() is False


def test_a_page_on_another_site_cannot_spend_the_endpoint(base, tmp_path):
    """The attack the loopback guard cannot stop: a browser is on loopback.

    Any tab can POST to http://localhost:8808/api/update -- a simple request, so
    no preflight and no token to miss. Sec-Fetch-Site is what tells the two apart,
    and a page cannot set it.
    """
    settings = replace(base, checkout_path=fake_checkout(tmp_path))
    app = create_app(settings, watch=False)
    with TestClient(app, client=("127.0.0.1", 9999)) as client:
        evil = client.post("/api/update", headers={"sec-fetch-site": "cross-site"})
        assert evil.status_code == 403
        assert "another site" in evil.json()["detail"]

        framed = client.post(
            "/api/update",
            headers={"origin": "http://evil.example", "host": "localhost:8808"},
        )
        assert framed.status_code == 403

        assert Updater(settings).running() is False, "an update was started anyway"

    # A caller that is not a browser sends neither header, and is allowed: this
    # guard is not authentication, it only refuses foreign pages.
    updater = Updater(settings)
    assert updater.cross_site_problem({}) is None
    assert updater.cross_site_problem({"sec-fetch-site": "same-origin"}) is None


def test_the_dev_proxy_is_not_mistaken_for_another_site(base, tmp_path):
    """Vite rewrites Host but not Origin, and both name this machine.

    Without this, every write in `pnpm dev` is refused -- pause, rescan, pricing,
    delete -- which is a broken dev server rather than a security property.
    """
    updater = Updater(replace(base, checkout_path=fake_checkout(tmp_path)))
    assert (
        updater.cross_site_problem(
            {"origin": "http://localhost:5188", "host": "127.0.0.1:8808"}
        )
        is None
    )
    assert (
        updater.cross_site_problem({"origin": "http://[::1]:5188", "host": "localhost:8808"})
        is None
    )
    # The relaxation is loopback-to-loopback only.
    assert updater.cross_site_problem(
        {"origin": "http://evil.example", "host": "localhost:8808"}
    )
    assert updater.cross_site_problem({"origin": "null", "host": "localhost:8808"})


def test_an_update_must_be_asked_for_by_this_machines_name(base, tmp_path):
    """DNS rebinding forges nothing, so the other two guards cannot see it.

    A page on http://evil.example:8808 rebound to 127.0.0.1 is genuinely
    same-origin, genuinely matches its own Host, and genuinely arrives from a
    loopback peer. What it cannot do is call this machine localhost.
    """
    settings = replace(base, checkout_path=fake_checkout(tmp_path))
    updater = Updater(settings)
    assert updater.host_header_problem({"host": "localhost:8808"}) is None
    assert updater.host_header_problem({"host": "127.0.0.1:8808"}) is None
    assert updater.host_header_problem({"host": "[::1]:8808"}) is None
    assert updater.host_header_problem({}) is None  # not a browser
    refusal = updater.host_header_problem({"host": "evil.example:8808"})
    assert refusal and "localhost" in refusal

    app = create_app(settings, watch=False)
    with TestClient(app, client=("127.0.0.1", 9999)) as client:
        rebound = client.post(
            "/api/update",
            headers={
                "host": "evil.example:8808",
                "origin": "http://evil.example:8808",
                "sec-fetch-site": "same-origin",
            },
        )
        assert rebound.status_code == 403
        assert Updater(settings).running() is False, "an update was started anyway"

    # Opting into LAN updates opts out of this too -- it is the same trust.
    assert (
        Updater(replace(settings, update_from_lan=True)).host_header_problem(
            {"host": "evil.example:8808"}
        )
        is None
    )
    # Reads are not gated on the name: the dashboard is meant to be read from the
    # LAN under whatever hostname it has.
    with TestClient(app, client=("127.0.0.1", 9999)) as client:
        assert client.get("/api/update", headers={"host": "box.local:8808"}).status_code == 200


def test_a_refused_caller_is_told_nothing_about_the_checkout(base, tmp_path):
    """The verdict is public; the path and the transcript are not.

    Together they leak the username, the commits being deployed, and any paths a
    failed build printed -- straight to the caller just refused.
    """
    settings = replace(base, checkout_path=fake_checkout(tmp_path))
    updater = Updater(settings)
    updater.log_path.parent.mkdir(parents=True, exist_ok=True)
    updater.log_path.write_text("[12:00:00] updating /home/someone/src/ccm\n")

    app = create_app(settings, watch=False)
    with TestClient(app, client=("192.168.1.50", 9999)) as client:
        body = client.get("/api/update").json()
        assert body["available"] is False
        assert body["log"] == ""
        assert body["checkout"] is None

    with TestClient(app, client=("127.0.0.1", 9999)) as client:
        body = client.get("/api/update").json()
        assert "someone" in body["log"], "the local dashboard still needs the log"
        assert body["checkout"]


def test_two_clicks_cannot_start_two_updates(base, tmp_path):
    """The endpoint is sync, so two clicks land on two threadpool threads.

    Both can pass a check-then-act test and both can launch a script, leaving two
    `git pull`s and two `just install`s racing over one checkout.
    """
    updater = Updater(replace(base, checkout_path=fake_checkout(tmp_path)))
    updater.marker_path.parent.mkdir(parents=True, exist_ok=True)
    # Stand in for the winning thread: the marker exists and is live, but the
    # loser has already passed its own running() check.
    updater.marker_path.write_text(f"{time.time():.0f}\n")
    with pytest.raises(RuntimeError, match="already running"):
        updater.start()


def test_a_stale_marker_does_not_let_two_updates_through(base, tmp_path, monkeypatch):
    """The interleaving an exclusive-create lock could not stop.

    A killed update leaves its marker behind, so a lock made of "the file exists"
    also has to delete a stale one -- and that deletion is the hole: two threads
    both see the stale marker, both unlink it, both create it, and both proceed.
    Nobody loses that race, which is the opposite of what a lock is for.
    """
    import os

    updater = Updater(replace(base, checkout_path=fake_checkout(tmp_path)))
    updater.marker_path.parent.mkdir(parents=True, exist_ok=True)
    updater.marker_path.write_text("killed\n")
    ancient = time.time() - 3600
    os.utime(updater.marker_path, (ancient, ancient))
    assert updater.running() is False, "precondition: the marker is stale"

    launched: list[list[str]] = []
    monkeypatch.setattr(
        "ccm.selfupdate.subprocess.Popen", lambda argv, **kw: launched.append(argv)
    )

    before = updater.marker_path.stat().st_ino
    updater.start()
    assert len(launched) == 1

    # The marker is rewritten in place, never replaced. This is the whole fix, and
    # it is asserted on the inode because that is what distinguishes the two
    # implementations: an exclusive-create lock has to remove a stale marker before
    # it can claim, and *that* removal is the hole -- two callers both past their
    # staleness check both delete and both create, so both proceed. Racing threads
    # will not reliably reproduce that interleaving (the critical section is a few
    # syscalls long), so the invariant is tested instead of the timing.
    assert updater.marker_path.stat().st_ino == before, "the marker was replaced"
    assert updater.running() is True


def test_a_header_that_is_not_an_authority_is_not_this_machine(base, tmp_path):
    """`_host_only` decides who may deploy, so it may not be generous.

    Splitting on the last "//" read "evil.example//localhost" as localhost. No
    browser can send that -- Host and Origin come from a URL authority, which
    cannot contain a slash -- but a parse that is wrong by construction becomes a
    hole the moment it is reused.
    """
    from ccm.selfupdate import _host_only, _names_this_machine

    assert _host_only("http://localhost:5188") == "localhost"
    assert _host_only("127.0.0.1:8808") == "127.0.0.1"
    assert _host_only("[::1]:8808") == "::1"
    for hostile in ("evil.example//localhost", "localhost/../evil", "a@localhost", ""):
        assert not _names_this_machine(_host_only(hostile)), hostile

    updater = Updater(replace(base, checkout_path=fake_checkout(tmp_path)))
    assert updater.host_header_problem({"host": "evil.example//localhost"})


def test_a_slow_build_is_not_declared_dead(base, tmp_path):
    """The marker is written once, so it is a deadline; the log is the heartbeat.

    A cold `just setup && just build` can outlast STALE_AFTER, and declaring that
    update dead lets a second one stack on top of it.
    """
    updater = Updater(replace(base, checkout_path=fake_checkout(tmp_path)))
    updater.marker_path.parent.mkdir(parents=True, exist_ok=True)
    updater.marker_path.write_text("start\n")
    updater.log_path.write_text("[12:34:56] just build\n")
    import os

    ancient = time.time() - 3600
    os.utime(updater.marker_path, (ancient, ancient))

    assert updater.running() is True, "a talking update was declared dead"

    # And a silent one still times out.
    os.utime(updater.log_path, (ancient, ancient))
    assert updater.running() is False


def test_the_endpoint_reports_why_it_cannot_update(base):
    app = create_app(base, watch=False)
    # An explicit loopback address: TestClient's default is the host "testclient",
    # which the address guard rightly refuses.
    with TestClient(app, client=("127.0.0.1", 9999)) as client:
        body = client.get("/api/update").json()
        assert body["available"] is False
        assert "CCM_CHECKOUT" in body["reason"]
        # Addressed as localhost, so the rebinding guard has nothing to say and
        # the answer is the real one: nothing to update from.
        post = client.post("/api/update", headers={"host": "localhost:8808"})
        assert post.status_code == 409


def test_no_write_endpoint_accepts_a_cross_site_request(base, tmp_path):
    """The guard is not the update button's alone.

    A page in another tab could just as easily delete an imported machine or
    rewrite the rate table, and none of those endpoints authenticate either.
    """
    app = create_app(base, watch=False)
    with TestClient(app, client=("127.0.0.1", 9999)) as client:
        evil = {"sec-fetch-site": "cross-site"}
        assert client.post("/api/rescan", headers=evil).status_code == 403
        assert client.post("/api/scan/pause", headers=evil).status_code == 403
        assert client.delete("/api/machines/somebody", headers=evil).status_code == 403
        # Reads are untouched: they are already public to anything that can
        # reach the port, and this is not a login.
        assert client.get("/api/state", headers=evil).status_code == 200
