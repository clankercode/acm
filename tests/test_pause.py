"""Pausing the scan loop, and the build id a stale tab notices an upgrade by.

The interesting case is a pause that lands *inside* a pass, which is the one the
HTTP tests cannot arrange: they pause a loop that is already idle. A stub source
with a per-unit delay makes it deterministic -- the pass is guaranteed to still
be part-way through its plan when the pause arrives.
"""

from __future__ import annotations

import os
import threading
import time

from ccm import server
from ccm.config import Settings
from ccm.engine import Engine
from ccm.scanner import Scanner
from ccm.sources.base import Source, Unit, UnitResult
from ccm.store import Store


class SlowSource(Source):
    """A corpus of `count` units, each taking `delay` to ingest.

    Units already ingested are dropped from the plan, exactly as a file-backed
    source drops files whose stored cursor is at EOF -- which is what makes
    "resume does not re-read" a claim this stub can actually test.
    """

    name = "stub"
    label = "Stub"
    watch_roots = ()

    def __init__(self, count: int = 40, delay: float = 0.02):
        self.count = count
        self.delay = delay
        self.done: list[str] = []
        self.started = threading.Event()

    def available(self) -> bool:
        return True

    def plan(self, store: Store) -> list[Unit]:
        return [
            Unit(key=f"u{i}", pending_bytes=100)
            for i in range(self.count)
            if f"u{i}" not in self.done
        ]

    def ingest(self, store: Store, unit: Unit) -> UnitResult:
        self.started.set()
        time.sleep(self.delay)
        self.done.append(unit.key)
        return UnitResult(bytes_read=100, rows=1)


def wait_until(predicate, timeout=10.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_a_cancelled_pass_says_so_and_the_next_one_finishes_the_plan(tmp_path):
    """The scanner must report the difference between "done" and "gave up"."""
    store = Store(tmp_path / "ccm.sqlite")
    source = SlowSource(count=6, delay=0.0)
    scanner = Scanner(store, sources=[source])

    # Abandon the pass after the second unit, the way a pause does.
    first = scanner.scan_once(should_stop=lambda: len(source.done) >= 2)
    assert first.interrupted is True
    assert first.files_total == 6
    assert first.files_done == 2
    assert first.phase == "tailing"

    second = scanner.scan_once(should_stop=lambda: False)
    assert second.interrupted is False
    # Only what was left was planned, so nothing was read twice.
    assert second.files_total == 4
    assert source.done == [f"u{i}" for i in range(6)]


def engine_with(tmp_path, source) -> Engine:
    settings = Settings(
        sessions_dir=tmp_path / "sessions",
        claude_dir=tmp_path / "claude",
        pi_dir=tmp_path / "pi",
        opencode_db=tmp_path / "opencode.db",
        grok_dir=tmp_path / "grok",
        sources=(),
        db_path=tmp_path / "ccm.sqlite",
        pricing_path=tmp_path / "pricing.toml",
        reference_path=tmp_path / "models-dev.json",
        debounce_seconds=0.05,
        poll_seconds=0.1,
        broadcast_hz=50.0,
        host="127.0.0.1",
        port=0,
    )
    engine = Engine(settings)
    # The corpus is the stub's, not this machine's.
    engine.scanner = Scanner(engine.store, sources=[source])
    return engine


def test_pausing_mid_pass_stops_the_reading_and_resuming_carries_on(tmp_path):
    source = SlowSource(count=60, delay=0.02)
    engine = engine_with(tmp_path, source)
    engine.start(watch=False)
    try:
        assert source.started.wait(10), "the pass never started"
        engine.set_paused(True)
        assert wait_until(lambda: engine.scanner.progress.phase == "paused"), (
            "the worker did not come to rest"
        )

        # Reading really stopped: the count is still where it was a moment later.
        settled = len(source.done)
        assert settled < source.count, "the pass finished before it could be paused"
        time.sleep(0.3)
        assert len(source.done) == settled

        engine.set_paused(False)
        assert wait_until(lambda: len(source.done) == source.count, timeout=15)
        # Every unit exactly once, across the two passes.
        assert sorted(source.done) == sorted(f"u{i}" for i in range(source.count))
        assert engine.paused is False
    finally:
        engine.stop()


def test_a_paused_engine_still_shuts_down_promptly(tmp_path):
    engine = engine_with(tmp_path, SlowSource(count=200, delay=0.02))
    engine.start(watch=False)
    try:
        engine.set_paused(True)
        assert wait_until(lambda: engine.scanner.progress.phase == "paused")
    finally:
        started = time.time()
        engine.stop()
    assert time.time() - started < 5
    assert engine._worker is not None and not engine._worker.is_alive()


def test_pause_and_resume_racing_each_other_leave_one_consistent_answer(tmp_path):
    """The flag and the reported state are written together, or not at all."""
    engine = engine_with(tmp_path, SlowSource(count=1, delay=0.0))
    barrier = threading.Barrier(8)

    def toggle(want: bool) -> None:
        # Released together, so the eight calls genuinely overlap.
        barrier.wait()
        engine.set_paused(want)

    threads = [
        threading.Thread(target=toggle, args=(i % 2 == 0,)) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    # Whatever order they landed in, the two copies of the answer agree.
    assert engine.scanner.progress.paused == engine.paused
    assert engine.paused in (True, False)


def test_a_pause_survives_a_restart(tmp_path):
    """An update must not hand the disk back to a cold scan behind your back."""
    source = SlowSource(count=40, delay=0.02)
    engine = engine_with(tmp_path, source)
    engine.set_paused(True)
    engine.stop()

    # A second engine over the same store is what a restart looks like.
    restarted = engine_with(tmp_path, source)
    assert restarted.paused is True
    restarted.start(watch=False)
    try:
        assert wait_until(lambda: restarted.scanner.progress.phase == "paused")
        assert restarted.scanner.progress.as_dict()["paused"] is True
        # Nothing was read: a restored pause is a real one, not a label.
        time.sleep(0.3)
        assert source.done == []

        restarted.set_paused(False)
        assert wait_until(lambda: len(source.done) > 0, timeout=15)
    finally:
        restarted.stop()

    # And resuming is remembered too, or the next restart would re-pause.
    assert engine_with(tmp_path, source).paused is False


def test_a_from_scratch_rebuild_is_flagged_until_a_pass_completes(tmp_path):
    """A pause part-way through a rebuild must not look like an empty history."""
    engine = engine_with(tmp_path, SlowSource(count=1, delay=0.0))
    assert engine.scanner.progress.rebuild_pending is False
    engine.rescan_from_scratch()
    assert engine.scanner.progress.rebuild_pending is True
    assert engine.scanner.progress.as_dict()["rebuild_pending"] is True

    engine.start(watch=False)
    try:
        assert wait_until(lambda: engine.scanner.progress.rebuild_pending is False), (
            "a completed pass did not clear the flag"
        )
    finally:
        engine.stop()


# -- build identity --------------------------------------------------------


def fake_package(tmp_path, monkeypatch):
    """A stand-in package tree, so the test never writes to the real one."""
    pkg = tmp_path / "pkg"
    (pkg / "sources").mkdir(parents=True)
    (pkg / "engine.py").write_text("x = 1\n")
    (pkg / "sources" / "engine.py").write_text("x = 2\n")
    monkeypatch.setattr(server, "PACKAGE_ROOT", pkg)
    monkeypatch.setattr(server, "web_dist", lambda: None)
    return pkg


def test_build_id_follows_content_not_timestamps(tmp_path, monkeypatch):
    """A reinstall of identical code must not read as an upgrade, and vice versa.

    Wheel installs take their timestamps from the zip entries, which reproducible
    builds pin, so mtimes are wrong in both directions.
    """
    pkg = fake_package(tmp_path, monkeypatch)
    base = server.build_identity()["id"]

    os.utime(pkg / "engine.py", (0, 0))
    assert server.build_identity()["id"] == base

    (pkg / "engine.py").write_text("x = 3\n")
    assert server.build_identity()["id"] != base


def test_build_id_distinguishes_files_with_the_same_name(tmp_path, monkeypatch):
    """Two modules can hold identical bytes; which one holds them matters."""
    pkg = fake_package(tmp_path, monkeypatch)
    base = server.build_identity()["id"]
    (pkg / "engine.py").write_text("x = 2\n")
    (pkg / "sources" / "engine.py").write_text("x = 1\n")
    assert server.build_identity()["id"] != base


def test_build_id_reflects_the_built_dashboard(tmp_path, monkeypatch):
    """A UI-only rebuild changes the asset URLs the open tab is holding."""
    fake_package(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    dist.mkdir()
    index = dist / "index.html"
    index.write_text('<script src="/assets/index-aaaa.js"></script>')
    monkeypatch.setattr(server, "web_dist", lambda: dist)
    base = server.build_identity()["id"]
    index.write_text('<script src="/assets/index-bbbb.js"></script>')
    assert server.build_identity()["id"] != base
