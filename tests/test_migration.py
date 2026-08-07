"""Upgrading must not lose what an upgrade cannot rebuild.

Two separate losses are covered here, both of which happened in practice:

* The ccm -> acm rename skipped its own migration whenever the target directory
  already existed -- which, under the systemd unit, is always, because
  ``StateDirectory=`` creates it before the process starts. The history was left
  behind in the old directory and the service came up on an empty database.
* A ``SCHEMA_VERSION`` bump dropped the imported tables along with the derived
  ones. Derived state is rebuilt by rescanning; imported state came from another
  machine's corpus and is simply gone.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from acm import config
from acm.store import IMPORT_SCHEMA_VERSION, SCHEMA_VERSION, Store, connect


# ---------------------------------------------------------------------------
# path migration


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the three XDG bases at a scratch tree."""
    for var, name in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        base = tmp_path / name
        base.mkdir()
        monkeypatch.setenv(var, str(base))
    # Otherwise a checkout layout also migrates its own data/ directory, which
    # is a different assertion and belongs in its own test.
    monkeypatch.setattr(config, "DEV_LAYOUT", False)
    return tmp_path


def _legacy_state(tmp_path: Path) -> Path:
    old = tmp_path / "state" / "ccm"
    old.mkdir(parents=True)
    (old / "ccm.sqlite").write_text("history")
    (old / "ccm.sqlite-wal").write_text("wal")
    return old


def test_migrates_when_the_target_is_absent(xdg: Path) -> None:
    _legacy_state(xdg)
    config.migrate_legacy_ccm_paths()
    new = xdg / "state" / "acm"
    assert (new / "acm.sqlite").read_text() == "history"
    assert (new / "acm.sqlite-wal").read_text() == "wal"
    assert not (xdg / "state" / "ccm").exists()


def test_migrates_into_an_empty_target(xdg: Path) -> None:
    """The systemd case: StateDirectory= created the target before we ran."""
    _legacy_state(xdg)
    (xdg / "state" / "acm").mkdir()
    (xdg / "config" / "acm").mkdir()
    (xdg / "cache" / "acm").mkdir()

    config.migrate_legacy_ccm_paths()

    assert (xdg / "state" / "acm" / "acm.sqlite").read_text() == "history"
    assert not (xdg / "state" / "ccm").exists()


def test_migrates_config_and_cache_into_empty_targets(xdg: Path) -> None:
    old = xdg / "config" / "ccm"
    old.mkdir(parents=True)
    (old / "pricing.toml").write_text("rates")
    (old / "env").write_text("ACM_PORT=9999")
    (xdg / "config" / "acm").mkdir()
    (xdg / "cache" / "ccm").mkdir(parents=True)
    (xdg / "cache" / "ccm" / "models-dev.json").write_text("{}")
    (xdg / "cache" / "acm").mkdir()

    config.migrate_legacy_ccm_paths()

    assert (xdg / "config" / "acm" / "pricing.toml").read_text() == "rates"
    assert (xdg / "config" / "acm" / "env").read_text() == "ACM_PORT=9999"
    assert (xdg / "cache" / "acm" / "models-dev.json").read_text() == "{}"


def test_leaves_a_target_that_already_holds_state(xdg: Path) -> None:
    """Both versions have run and written. Guessing a winner loses the other."""
    _legacy_state(xdg)
    new = xdg / "state" / "acm"
    new.mkdir()
    (new / "acm.sqlite").write_text("newer history")

    config.migrate_legacy_ccm_paths()

    assert (new / "acm.sqlite").read_text() == "newer history"
    assert (xdg / "state" / "ccm" / "ccm.sqlite").read_text() == "history"


def test_migrates_across_a_device_boundary(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-created target is a bind mount in the service's sandbox.

    ``rename`` cannot cross that boundary; the move has to fall back to copy and
    delete. This came up on the first real cutover, on ``~/.cache/acm``.
    """
    _legacy_state(xdg)
    (xdg / "state" / "acm").mkdir()

    real_rename = Path.rename

    def rename_across_devices(self: Path, target):
        if self.parent.name == "ccm":
            raise OSError(18, "Invalid cross-device link")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename_across_devices)
    config.migrate_legacy_ccm_paths()
    monkeypatch.undo()

    assert (xdg / "state" / "acm" / "acm.sqlite").read_text() == "history"
    assert not (xdg / "state" / "ccm").exists()


def test_refuses_a_target_systemd_turned_into_a_symlink(xdg: Path) -> None:
    """StateDirectory= missing + ConfigurationDirectory= present makes one.

    Following it would file the database under ~/.config, so the migration
    stops and says which symlink to remove.
    """
    _legacy_state(xdg)
    (xdg / "config" / "acm").mkdir(parents=True)
    (xdg / "state" / "acm").symlink_to(xdg / "config" / "acm")

    config.migrate_legacy_ccm_paths()

    assert (xdg / "state" / "ccm" / "ccm.sqlite").read_text() == "history"
    assert not (xdg / "config" / "acm" / "acm.sqlite").exists()
    assert not (xdg / "config" / "acm" / "ccm.sqlite").exists()


def test_migration_is_idempotent(xdg: Path) -> None:
    _legacy_state(xdg)
    config.migrate_legacy_ccm_paths()
    config.migrate_legacy_ccm_paths()
    assert (xdg / "state" / "acm" / "acm.sqlite").read_text() == "history"


def test_checkout_layout_renames_its_own_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout keeps state in data/ and never went near XDG."""
    for var, name in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(var, str(tmp_path / name))
    project = tmp_path / "checkout"
    (project / "data").mkdir(parents=True)
    (project / "data" / "ccm.sqlite").write_text("checkout history")
    monkeypatch.setattr(config, "DEV_LAYOUT", True)
    monkeypatch.setattr(config, "PROJECT_ROOT", project)

    config.migrate_legacy_ccm_paths()

    assert (project / "data" / "acm.sqlite").read_text() == "checkout history"
    assert not (project / "data" / "ccm.sqlite").exists()


# ---------------------------------------------------------------------------
# schema bumps


def _seed_import(store: Store, origin: str = "x-left") -> None:
    store.execute(
        "INSERT INTO imports(origin, machine, requests, input_tokens) VALUES (?, ?, ?, ?)",
        (origin, origin, 116491, 9_000_000),
    )
    store.execute(
        "INSERT INTO import_sessions(origin, rollout_id, model, long_ctx, n, "
        "input_tokens, cached_tokens, output_tokens, reasoning_tokens) "
        "VALUES (?, 'r1', 'gpt-5.6-sol', 0, 4, 100, 50, 20, 5)",
        (origin,),
    )
    store.execute(
        "INSERT INTO bucket_hour(hour, origin, source, model, provider, base_model, "
        "repo, is_subagent, long_ctx, n, input_tokens, cached_tokens, "
        "cache_write_tokens, cache_write_1h_tokens, output_tokens, reasoning_tokens, "
        "max_input) VALUES (7, ?, 'codex', 'gpt-5.6-sol', 'openai', 'gpt-5.6-sol', "
        "'repo', 0, 0, 4, 100, 50, 0, 0, 20, 5, 40)",
        (origin,),
    )
    # Local rollups are derived, and must not survive a bump.
    store.execute(
        "INSERT INTO bucket_hour(hour, origin, source, model, provider, base_model, "
        "repo, is_subagent, long_ctx, n, input_tokens, cached_tokens, "
        "cache_write_tokens, cache_write_1h_tokens, output_tokens, reasoning_tokens, "
        "max_input) VALUES (7, '', 'codex', 'gpt-5.6-sol', 'openai', 'gpt-5.6-sol', "
        "'repo', 0, 0, 9, 900, 100, 0, 0, 90, 9, 100)"
    )


def _stale(path: Path, version: str = "3") -> None:
    raw = sqlite3.connect(path)
    raw.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (version,))
    raw.commit()
    raw.close()


def _counts(store: Store) -> dict[str, int]:
    return {
        "imports": store.one("SELECT COUNT(*) c FROM imports")["c"],
        "import_sessions": store.one("SELECT COUNT(*) c FROM import_sessions")["c"],
        "imported_buckets": store.one(
            "SELECT COUNT(*) c FROM bucket_hour WHERE origin <> ''"
        )["c"],
        "local_buckets": store.one(
            "SELECT COUNT(*) c FROM bucket_hour WHERE origin = ''"
        )["c"],
    }


def test_schema_bump_keeps_imports_and_drops_derived(tmp_path: Path) -> None:
    db = tmp_path / "acm.sqlite"
    store = Store(db)
    _seed_import(store)
    store.close()

    _stale(db)

    store = Store(db)
    counts = _counts(store)
    store.close()
    assert counts["imports"] == 1
    assert counts["import_sessions"] == 1
    assert counts["imported_buckets"] == 1
    assert counts["local_buckets"] == 0, "locally derived rollups are rebuilt, not kept"


def test_schema_bump_preserves_imported_values(tmp_path: Path) -> None:
    db = tmp_path / "acm.sqlite"
    store = Store(db)
    _seed_import(store)
    store.close()
    _stale(db)

    store = Store(db)
    row = store.one("SELECT * FROM imports WHERE origin = 'x-left'")
    bucket = store.one("SELECT * FROM bucket_hour WHERE origin = 'x-left'")
    store.close()
    assert row is not None and row["requests"] == 116491
    assert row["machine"] == "x-left"
    assert bucket is not None and bucket["input_tokens"] == 100


def test_schema_bump_flags_a_rebuild(tmp_path: Path) -> None:
    db = tmp_path / "acm.sqlite"
    Store(db).close()
    _stale(db)
    store = Store(db)
    assert store.get_meta("rebuild_pending") == "1"
    store.close()


def test_a_new_database_is_not_flagged_for_rebuild(tmp_path: Path) -> None:
    store = Store(tmp_path / "acm.sqlite")
    assert store.get_meta("rebuild_pending") is None
    assert store.get_meta("schema_version") == SCHEMA_VERSION
    assert store.get_meta("import_schema_version") == IMPORT_SCHEMA_VERSION
    store.close()


def test_reopening_at_the_same_version_changes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "acm.sqlite"
    store = Store(db)
    _seed_import(store)
    store.close()

    store = Store(db)
    counts = _counts(store)
    store.close()
    assert counts["local_buckets"] == 1, "an ordinary restart must not drop anything"
    assert counts["imports"] == 1


def test_an_import_shape_change_parks_the_data_in_a_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case imports cannot survive still leaves them on disk."""
    db = tmp_path / "acm.sqlite"
    store = Store(db)
    _seed_import(store)
    store.close()

    raw = sqlite3.connect(db)
    raw.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    raw.execute("UPDATE meta SET value = '0' WHERE key = 'import_schema_version'")
    raw.commit()
    raw.close()

    store = Store(db)
    counts = _counts(store)
    store.close()
    assert counts["imports"] == 0

    sidecar = tmp_path / "acm.sqlite.imports.json"
    assert sidecar.is_file()
    saved = json.loads(sidecar.read_text())
    assert saved["tables"]["imports"]["rows"][0][0] == "x-left"
    assert len(saved["tables"]["import_sessions"]["rows"]) == 1


def test_a_dropped_column_does_not_block_the_carry(tmp_path: Path) -> None:
    """A rollup column the new schema lost is left behind; the rows still arrive.

    ``bucket_hour`` is the table this can happen to: it is dropped and recreated
    on every bump, and the imported rows in it have to be put back into whatever
    shape the new schema declares.
    """
    db = tmp_path / "acm.sqlite"
    store = Store(db)
    _seed_import(store)
    store.close()

    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE bucket_hour ADD COLUMN gone TEXT")
    raw.execute("UPDATE bucket_hour SET gone = 'x'")
    raw.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()

    store = Store(db)
    row = store.one("SELECT * FROM bucket_hour WHERE origin = 'x-left'")
    store.close()
    assert row is not None, "the imported rollup survived the column change"
    assert row["input_tokens"] == 100
    assert "gone" not in row.keys()


# ---------------------------------------------------------------------------
# an unfinished rebuild outliving the process


def _engine(tmp_path: Path):
    from acm.config import Settings
    from acm.engine import Engine

    settings = Settings(
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
        db_path=tmp_path / "acm.sqlite",
        pricing_path=tmp_path / "pricing.toml",
        reference_path=tmp_path / "models-dev.json",
        debounce_seconds=0.05,
        poll_seconds=0.1,
        broadcast_hz=50.0,
        host="127.0.0.1",
        port=0,
    )
    return Engine(settings)


def test_an_unfinished_rebuild_survives_a_restart(tmp_path: Path) -> None:
    """Otherwise a half-read corpus reads as a history that lost its rows."""
    engine = _engine(tmp_path)
    engine.rescan_from_scratch()
    assert engine.scanner.progress.rebuild_pending
    engine.store.close()

    restarted = _engine(tmp_path)
    assert restarted.scanner.progress.rebuild_pending, (
        "the dashboard stopped saying the corpus was mid-rebuild"
    )
    restarted.store.close()


def test_a_finished_rebuild_does_not_come_back(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.rescan_from_scratch()
    # What the worker does at the end of a pass that read the plan through.
    engine._pass()
    engine.store.close()

    restarted = _engine(tmp_path)
    assert not restarted.scanner.progress.rebuild_pending
    restarted.store.close()


def test_read_only_open_never_resets(tmp_path: Path) -> None:
    db = tmp_path / "acm.sqlite"
    store = Store(db)
    _seed_import(store)
    store.close()
    _stale(db)

    conn = connect(db, read_only=True)
    assert (
        conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        == "3"
    )
    assert conn.execute("SELECT COUNT(*) FROM bucket_hour").fetchone()[0] == 2
    conn.close()
