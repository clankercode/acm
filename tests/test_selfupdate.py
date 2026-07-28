"""The Update button's guards.

Nothing here runs an update. What is under test is the set of refusals, because
this is the one endpoint that runs a shell script: every test that "passes" by
starting an update would be a test that pulled and installed something.
"""

from __future__ import annotations

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


def test_the_endpoint_reports_why_it_cannot_update(base):
    app = create_app(base, watch=False)
    # An explicit loopback address: TestClient's default is the host "testclient",
    # which the address guard rightly refuses.
    with TestClient(app, client=("127.0.0.1", 9999)) as client:
        body = client.get("/api/update").json()
        assert body["available"] is False
        assert "CCM_CHECKOUT" in body["reason"]
        assert client.post("/api/update").status_code == 409
