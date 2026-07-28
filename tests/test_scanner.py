"""Scanner correctness: replay collapse, timestamp recovery, resumable reads."""

from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

from ccm.scanner import (
    MAX_ERROR_KINDS,
    ROW_RATE_WINDOW,
    CodexSource,
    ScanProgress,
    Scanner,
    iter_rollouts,
    parse_ts,
)
from ccm.store import Store

from .conftest import Thread


def scan(store: Store, sessions_dir: Path) -> dict:
    root = sessions_dir.parents[2]
    Scanner(store, root).scan_once()
    row = store.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS i,"
        " COALESCE(SUM(cached_tokens), 0) AS c, COALESCE(SUM(output_tokens), 0) AS o"
        " FROM requests"
    )
    return dict(row)


def build(sessions_dir: Path, clock, rollout_id="aaa", session_id="sess-1") -> Thread:
    t = Thread(session_id=session_id, rollout_id=rollout_id, clock=clock)
    t.meta().turn_context()
    t.request(1000, 0, 50).noise(2)
    t.request(1200, 900, 40).noise(2)
    t.request(1400, 1100, 60)
    return t


def test_basic_scan_counts_each_request_once(store, sessions_dir, clock):
    build(sessions_dir, clock).write(sessions_dir)
    got = scan(store, sessions_dir)
    assert got["n"] == 3
    assert got["i"] == 1000 + 1200 + 1400
    assert got["c"] == 0 + 900 + 1100
    assert got["o"] == 50 + 40 + 60


def test_replay_adds_nothing(store, sessions_dir, clock):
    """A resumed file replays the whole history and must contribute no rows."""
    original = build(sessions_dir, clock)
    original.write(sessions_dir, "rollout-2026-07-01T12-00-00-aaa.jsonl")
    baseline = scan(store, sessions_dir)

    resumed = original.replayed_into("bbb", clock + timedelta(hours=3))
    resumed.write(sessions_dir, "rollout-2026-07-01T15-00-00-bbb.jsonl")
    after = scan(store, sessions_dir)
    assert after == baseline

    # ...and the replay's own new work still lands.
    resumed.turn_context().request(2000, 1900, 70)
    resumed.write(sessions_dir, "rollout-2026-07-01T15-00-00-bbb.jsonl")
    grown = scan(store, sessions_dir)
    assert grown["n"] == baseline["n"] + 1
    assert grown["i"] == baseline["i"] + 2000


def test_replay_does_not_move_timestamps_forward(store, sessions_dir, clock):
    """The original's clock must win over the replay's, in either scan order."""
    original = build(sessions_dir, clock)
    resumed = original.replayed_into("bbb", clock + timedelta(hours=5))

    original.write(sessions_dir, "rollout-2026-07-01T12-00-00-aaa.jsonl")
    resumed.write(sessions_dir, "rollout-2026-07-01T17-00-00-bbb.jsonl")
    scan(store, sessions_dir)
    forward = [r["ts"] for r in store.query("SELECT ts FROM requests ORDER BY ts")]

    # Same corpus, opposite discovery order: names decide, so swap them.
    other = Store(store.path.parent / "reverse.sqlite")
    d2 = sessions_dir.parent / "02"
    d2.mkdir(parents=True, exist_ok=True)
    resumed.write(d2, "rollout-2026-07-01T01-00-00-bbb.jsonl")
    original.write(d2, "rollout-2026-07-01T02-00-00-aaa.jsonl")
    Scanner(other, d2.parents[2]).scan_once()
    reverse = [r["ts"] for r in other.query("SELECT ts FROM requests ORDER BY ts")]
    other.close()

    assert forward == reverse
    latest_original = max(parse_ts(l.split('"timestamp":"')[1][:24]) for l in original.lines)
    assert max(forward) <= latest_original


def test_attribution_follows_the_earliest_observation(store, sessions_dir, clock):
    """Requests belong to the file that ran them, not one that replayed them."""
    original = build(sessions_dir, clock)
    original.write(sessions_dir, "rollout-2026-07-01T12-00-00-aaa.jsonl")
    resumed = original.replayed_into("bbb", clock + timedelta(hours=3))
    resumed.turn_context().request(500, 400, 10)
    resumed.write(sessions_dir, "rollout-2026-07-01T15-00-00-bbb.jsonl")
    scan(store, sessions_dir)

    counts = {
        r["rollout_id"]: r["n"]
        for r in store.query(
            "SELECT rollout_id, COUNT(*) AS n FROM requests GROUP BY rollout_id"
        )
    }
    assert counts == {"aaa": 3, "bbb": 1}


def test_incremental_read_matches_one_shot(store, sessions_dir, clock, tmp_path):
    """Appending in pieces must land exactly where a single read would."""
    thread = build(sessions_dir, clock)
    full = thread.text()
    path = sessions_dir / "rollout-2026-07-01T12-00-00-aaa.jsonl"

    # Feed the file in awkward slices, including mid-line cuts.
    cuts = [len(full) // 7, len(full) // 3, len(full) // 2, len(full) - 5, len(full)]
    written = 0
    for cut in cuts:
        path.write_text(full[:cut])
        written = cut
        scan(store, sessions_dir)
    assert written == len(full)
    piecewise = scan(store, sessions_dir)

    oneshot_store = Store(tmp_path / "oneshot.sqlite")
    d2 = sessions_dir.parent / "09"
    d2.mkdir(parents=True, exist_ok=True)
    (d2 / path.name).write_text(full)
    Scanner(oneshot_store, d2.parents[2]).scan_once()
    row = oneshot_store.one(
        "SELECT COUNT(*) AS n, SUM(input_tokens) AS i, SUM(cached_tokens) AS c,"
        " SUM(output_tokens) AS o FROM requests"
    )
    oneshot_store.close()
    assert piecewise == dict(row)


def test_partial_trailing_line_is_not_consumed(store, sessions_dir, clock):
    """A half-written record must be re-read intact, not dropped."""
    thread = build(sessions_dir, clock)
    full = thread.text()
    body, last = full.rsplit("\n", 2)[0] + "\n", full.rsplit("\n", 2)[1] + "\n"
    path = sessions_dir / "rollout-2026-07-01T12-00-00-aaa.jsonl"

    path.write_text(body + last[: len(last) // 2])
    partial = scan(store, sessions_dir)
    path.write_text(body + last)
    complete = scan(store, sessions_dir)
    assert complete["n"] == partial["n"] + 1


def test_truncation_triggers_clean_reingest(store, sessions_dir, clock):
    thread = build(sessions_dir, clock)
    path = thread.write(sessions_dir, "rollout-2026-07-01T12-00-00-aaa.jsonl")
    first = scan(store, sessions_dir)

    rebuilt = build(sessions_dir, clock, rollout_id="aaa")
    rebuilt.request(9999, 9000, 5)
    path.write_text(rebuilt.text())
    # Shorter than the recorded offset, so the cursor must reset.
    path.write_text(rebuilt.text()[: len(rebuilt.text()) // 2])
    scan(store, sessions_dir)
    path.write_text(rebuilt.text())
    after = scan(store, sessions_dir)

    assert after["n"] == first["n"] + 1
    assert after["i"] == first["i"] + 9999


def test_model_switch_mid_file_is_attributed_per_request(store, sessions_dir, clock):
    t = Thread(session_id="s", rollout_id="m1", clock=clock)
    t.meta().turn_context("gpt-5.6-sol")
    t.request(100, 0, 10)
    t.turn_context("gpt-5.6-terra")
    t.request(200, 100, 20)
    t.write(sessions_dir)
    scan(store, sessions_dir)
    rows = {r["model"]: r["input_tokens"] for r in store.query(
        "SELECT model, input_tokens FROM requests")}
    assert rows == {"gpt-5.6-sol": 100, "gpt-5.6-terra": 200}


def test_model_attribution_survives_a_resume_mid_file(store, sessions_dir, clock):
    """The carried parser state must remember the model across passes."""
    t = Thread(session_id="s", rollout_id="m2", clock=clock)
    t.meta().turn_context("gpt-5.6-terra")
    head = t.text()
    t.request(300, 200, 30)
    full = t.text()

    path = sessions_dir / "rollout-2026-07-01T12-00-00-m2.jsonl"
    path.write_text(head)
    scan(store, sessions_dir)  # stops after turn_context, before any request
    path.write_text(full)
    scan(store, sessions_dir)

    row = store.one("SELECT model, provider, base_model FROM requests")
    assert row["model"] == "gpt-5.6-terra"
    assert row["provider"] == ""
    assert row["base_model"] == "gpt-5.6-terra"


def test_provider_prefix_is_split_out(store, sessions_dir, clock):
    t = Thread(session_id="s", rollout_id="p1", clock=clock)
    t.meta().turn_context("pooler/gpt-5.6-sol")
    t.request(100, 50, 10)
    t.write(sessions_dir)
    scan(store, sessions_dir)
    row = store.one("SELECT model, provider, base_model FROM requests")
    assert (row["model"], row["provider"], row["base_model"]) == (
        "pooler/gpt-5.6-sol",
        "pooler",
        "gpt-5.6-sol",
    )


def test_events_are_recorded_and_deduped_across_replay(store, sessions_dir, clock):
    t = Thread(session_id="s", rollout_id="e1", clock=clock)
    t.meta().turn_context()
    t.request(100, 0, 10).event("context_compacted")
    t.request(200, 100, 10).event("context_compacted").event("turn_aborted")
    t.write(sessions_dir, "rollout-2026-07-01T12-00-00-e1.jsonl")
    scan(store, sessions_dir)
    before = store.one("SELECT COUNT(*) AS n FROM events")["n"]
    assert before == 3

    t.replayed_into("e2", clock + timedelta(hours=2)).write(
        sessions_dir, "rollout-2026-07-01T14-00-00-e2.jsonl"
    )
    scan(store, sessions_dir)
    assert store.one("SELECT COUNT(*) AS n FROM events")["n"] == before


def test_anomalies_are_counted_not_corrected(store, sessions_dir, clock):
    t = Thread(session_id="s", rollout_id="a1", clock=clock)
    t.meta().turn_context()
    t.request(100, 0, 5, reasoning=40)  # reasoning <= output is violated
    t.write(sessions_dir)
    scan(store, sessions_dir)
    counts = {r["kind"]: r["count"] for r in store.query("SELECT * FROM anomalies")}
    assert counts.get("reasoning_gt_output") == 1
    # The row is still ingested at its reported values.
    row = store.one("SELECT output_tokens, reasoning_tokens FROM requests")
    assert (row["output_tokens"], row["reasoning_tokens"]) == (5, 40)


def test_session_dimensions_are_captured(store, sessions_dir, clock):
    t = Thread(
        session_id="s", rollout_id="d1", clock=clock,
        cwd="/srv/app", git_repo="/srv/app.git", thread_source="subagent",
    )
    t.meta().turn_context()
    t.request(100, 0, 5)
    t.write(sessions_dir)
    scan(store, sessions_dir)
    row = store.one("SELECT * FROM sessions")
    assert row["cwd"] == "/srv/app"
    assert row["git_repo"] == "/srv/app.git"
    assert row["is_subagent"] == 1


def test_rescanning_an_unchanged_corpus_is_a_no_op(store, sessions_dir, clock):
    build(sessions_dir, clock).write(sessions_dir)
    first = scan(store, sessions_dir)
    for _ in range(3):
        assert scan(store, sessions_dir) == first


def test_iter_rollouts_finds_nested_files_in_order(tmp_path):
    root = tmp_path / "sessions"
    for day, name in (("01", "rollout-2026-07-01T01-00-00-a.jsonl"),
                      ("02", "rollout-2026-07-02T01-00-00-b.jsonl")):
        d = root / "2026" / "07" / day
        d.mkdir(parents=True)
        (d / name).write_text("")
        (d / "notes.txt").write_text("ignored")
    found = [p.name for p in iter_rollouts(root)]
    assert found == [
        "rollout-2026-07-01T01-00-00-a.jsonl",
        "rollout-2026-07-02T01-00-00-b.jsonl",
    ]


def test_parse_ts_handles_the_corpus_format():
    from datetime import UTC, datetime as dt

    expected = int(dt(2026, 7, 12, 23, 21, 49, 498_000, tzinfo=UTC).timestamp() * 1000)
    assert parse_ts("2026-07-12T23:21:49.498Z") == expected
    # The trailing Z must be honoured rather than read as local time.
    assert parse_ts("2026-07-12T23:21:49.498Z") == 1783898509498
    assert parse_ts(None) is None
    assert parse_ts("not a date") is None


def test_scan_is_resumable_after_an_interrupted_pass(store, sessions_dir, clock, tmp_path):
    """A crash between files must not lose or duplicate anything."""
    for i in range(4):
        t = build(sessions_dir, clock + timedelta(hours=i), rollout_id=f"r{i}",
                  session_id=f"s{i}")
        t.write(sessions_dir, f"rollout-2026-07-01T1{i}-00-00-r{i}.jsonl")

    root = sessions_dir.parents[2]
    partial = Store(tmp_path / "partial.sqlite")
    scanner = Scanner(partial, root)
    calls = {"n": 0}

    def stop_early(progress):
        calls["n"] += 1
        if progress.files_done == 2:
            raise KeyboardInterrupt

    try:
        scanner.scan_once(on_progress=stop_early)
    except KeyboardInterrupt:
        pass
    mid = partial.one("SELECT COUNT(*) AS n FROM requests")["n"]
    assert 0 < mid < 12

    Scanner(partial, root).scan_once()
    resumed = partial.one("SELECT COUNT(*) AS n FROM requests")["n"]
    partial.close()

    scan(store, sessions_dir)
    assert resumed == store.one("SELECT COUNT(*) AS n FROM requests")["n"] == 12


def test_a_pass_with_nothing_to_read_is_never_announced(store, sessions_dir, clock):
    """A quiet poll must not surface as a scan.

    The header showed "scanning 0/0" for a frame every poll cycle, which read as
    a flicker rather than as liveness. Nothing new to read means nothing to say.
    """
    build(sessions_dir, clock).write(sessions_dir)
    root = sessions_dir.parents[2]
    scanner = Scanner(store, root)

    first: list[str] = []
    scanner.scan_once(on_progress=lambda p: first.append(p.phase))
    assert first, "a pass with real work still reports it"

    quiet: list[str] = []
    progress = scanner.scan_once(on_progress=lambda p: quiet.append(p.phase))
    assert quiet == []
    assert progress.files_total == 0
    # ...and the state a subscriber reads afterwards is the resting one.
    assert progress.phase == "tailing"
    assert progress.current_file is None


def test_offset_advances_only_over_complete_lines(store, sessions_dir, clock):
    thread = build(sessions_dir, clock)
    full = thread.text()
    path = sessions_dir / "rollout-2026-07-01T12-00-00-aaa.jsonl"
    path.write_text(full[:-3])
    scan(store, sessions_dir)
    offset = store.one("SELECT offset FROM files")["offset"]
    assert offset == full.rindex("\n", 0, len(full) - 3) + 1
    assert offset < os.path.getsize(path)


def test_rows_count_every_line_not_just_token_events(store, sessions_dir, clock):
    """The liveness number counts lines read, not token events found.

    Most of a corpus is not token counts -- during a real pass the event counter
    sat still through thousands of files that were being consumed perfectly well,
    so a rate built on it reported a stall that was not happening.
    """
    thread = build(sessions_dir, clock)
    thread.write(sessions_dir)
    progress = Scanner(store, sessions_dir.parents[2]).scan_once()

    assert progress.rows == len([line for line in thread.text().splitlines() if line])
    assert progress.rows > progress.raw_events
    assert progress.sources["codex"].rows == progress.rows


def test_row_rate_measures_only_the_recent_window():
    progress = ScanProgress()
    now = time.time()
    progress.samples.append((now - 1.0, 0))
    progress.rows = 400
    progress.samples.append((now, 400))
    assert 300 < progress.rows_per_sec < 500


def test_row_rate_decays_to_zero_when_nothing_is_being_read():
    """A finished or stalled pass must not leave a number that looks live."""
    progress = ScanProgress()
    now = time.time()
    progress.rows = 5000
    progress.samples.append((now - ROW_RATE_WINDOW * 3, 0))
    progress.samples.append((now - ROW_RATE_WINDOW * 2, 5000))
    assert progress.rows_per_sec == 0.0


def test_errors_are_grouped_by_kind(store, sessions_dir, clock):
    """The header shows a count; the dropdown behind it shows kinds.

    A reader that trips over one malformed line trips over it in every file that
    carries one, so sightings are folded together -- a dozen identical messages
    say less than one message with a dozen beside it.
    """
    for index in range(3):
        build(
            sessions_dir, clock, rollout_id=f"r{index}", session_id=f"s{index}"
        ).write(sessions_dir)

    class Broken(CodexSource):
        def ingest(self, store, unit):
            raise RuntimeError("'str' object has no attribute 'get'")

    progress = Scanner(
        store, sources=[Broken(sessions_dir.parents[2])]
    ).scan_once()

    assert progress.errors == 3
    groups = progress.as_dict()["error_groups"]
    assert len(groups) == 1
    assert groups[0]["count"] == 3
    assert "no attribute" in groups[0]["message"]
    assert groups[0]["sources"] == ["codex"]
    # An example to go and look at, and the client that hit it.
    assert groups[0]["last_file"].endswith(".jsonl")
    # A failed file commits no cursor, so its bytes stay pending for next pass.
    assert progress.bytes_done == 0


def test_error_kinds_are_capped_and_the_shortfall_stays_visible():
    """The dropdown is bounded; the count it hangs off is not.

    The UI shows `errors` minus what it can itemise as "+N more", so the two must
    disagree by exactly what was dropped rather than quietly matching.
    """
    progress = ScanProgress()
    for index in range(MAX_ERROR_KINDS + 5):
        progress.note_error(f"distinct failure number {index}")

    payload = progress.as_dict()
    assert payload["errors"] == MAX_ERROR_KINDS + 5
    assert len(payload["error_groups"]) == MAX_ERROR_KINDS
    itemised = sum(group["count"] for group in payload["error_groups"])
    assert payload["errors"] - itemised == 5


def test_each_pass_reports_its_own_errors_and_rate(store, sessions_dir, clock):
    """Both are per-pass, like every other counter in the progress payload."""
    build(sessions_dir, clock).write(sessions_dir)
    scanner = Scanner(store, sessions_dir.parents[2])
    scanner.progress.note_error("left over from an earlier pass")

    progress = scanner.scan_once()
    assert progress.errors == 0
    assert progress.as_dict()["error_groups"] == []
