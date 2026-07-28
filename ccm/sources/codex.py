"""Codex rollout files.

Codex rewrites a thread's entire prior history into a new rollout file whenever
a session resumes or a subagent forks, stamping every replayed line with the
moment of the rewrite. One observed file holds 41,619 token_count events
covering six minutes of wall clock. Summing naively over-counts the corpus
roughly fourfold.

Deduplication therefore keys on the cumulative token counters, which advance
exactly once per real request, and keeps the earliest timestamp so the true hour
survives. Rank stays 0 throughout: every sighting of a request is complete, so
the tie-break on earliest timestamp is the whole rule.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import orjson

from .base import (
    JsonlSource,
    ParseOutput,
    parse_ts,
    read_new_lines,
    request_row,
    session_row,
    walk_jsonl,
)

#: Event payload types worth recording for chart overlays and diagnostics.
EVENT_KINDS = frozenset(
    {
        "context_compacted",
        "turn_aborted",
        "task_started",
        "task_complete",
        "stream_error",
        "error",
    }
)

#: Byte markers used to reject lines before paying for a JSON parse. The bulk of
#: the corpus is response_item and mcp_tool_call_end payloads that carry no
#: statistics, and skipping them without parsing is the single biggest win in
#: scan throughput.
_WANTED_MARKERS: tuple[bytes, ...] = tuple(
    b'"%s"' % k.encode() for k in sorted(EVENT_KINDS | {"token_count"})
)
_META_MARKERS = (b'"session_meta"', b'"turn_context"')

#: How far into a line the discriminating `"type"` fields are found. The record
#: prefix is a fixed-order serde struct, so the payload type lands well inside
#: this window; the margin is generous enough to absorb format drift.
_HEAD = 256


def iter_rollouts(root: Path) -> Iterator[Path]:
    """Yield every rollout file under the sessions tree, oldest name first.

    Filenames embed a timestamp, so lexical order is chronological.
    """
    yield from walk_jsonl(root, match=lambda name: name.startswith("rollout-"))


def dedup_key(session_id: str, cum: tuple[int, int, int, int]) -> str:
    return "|".join((session_id, *(str(c) for c in cum)))


class RolloutParser:
    """Stateful parser for a single rollout file.

    State is carried across incremental passes so that resuming mid-file
    attributes requests to the right model and anchors events to the right
    request.
    """

    source = "codex"

    def __init__(self, path: Path, cursor: dict | None = None):
        self.path = path
        cursor = cursor or {}
        self.session_id: str | None = cursor.get("session_id")
        self.rollout_id: str | None = cursor.get("rollout_id")
        self.parent_thread_id: str | None = cursor.get("parent_thread_id")
        self.thread_source: str | None = cursor.get("thread_source")
        self.agent_role: str | None = cursor.get("agent_role")
        self.agent_nickname: str | None = cursor.get("agent_nickname")
        self.depth: int | None = cursor.get("depth")
        self.cwd: str | None = cursor.get("cwd")
        self.git_repo: str | None = cursor.get("git_repo")
        self.git_branch: str | None = cursor.get("git_branch")
        self.originator: str | None = cursor.get("originator")
        self.cli_version: str | None = cursor.get("cli_version")
        self.model: str | None = cursor.get("cur_model")
        self.effort: str | None = cursor.get("cur_effort")

        carry = {}
        if cursor.get("carry"):
            try:
                carry = json.loads(cursor["carry"])
            except (ValueError, TypeError):
                carry = {}
        #: Cumulative counters of the most recent request, used to anchor events.
        self.last_cum: tuple[int, int, int, int] = tuple(  # type: ignore[assignment]
            carry.get("last_cum") or (0, 0, 0, 0)
        )
        #: Count of each event kind seen since that request.
        self.ordinals: dict[str, int] = dict(carry.get("ordinals") or {})
        #: Highest cumulative total seen, to detect counter regressions.
        self.max_cum_total: int = int(carry.get("max_cum_total") or 0)
        self.first_ts: int | None = cursor.get("first_ts")
        self.last_ts: int | None = cursor.get("last_ts")
        self.have_meta: bool = bool(cursor.get("rollout_id"))

    def carry_state(self) -> dict:
        return {
            "last_cum": list(self.last_cum),
            "ordinals": self.ordinals,
            "max_cum_total": self.max_cum_total,
        }

    # -- line handling -----------------------------------------------------

    def feed(self, line: bytes, result: ParseOutput) -> None:
        """Process one complete JSONL line."""
        head = line[:_HEAD]
        is_event = b'"event_msg"' in head
        if is_event:
            if not any(m in head for m in _WANTED_MARKERS):
                return
        elif not any(m in head for m in _META_MARKERS):
            return

        try:
            record = orjson.loads(line)
        except orjson.JSONDecodeError:
            result.flag("parse_error")
            return

        rtype = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return

        if rtype == "session_meta":
            # Only the first one describes this file. Later copies are replayed
            # parent metadata and would misattribute every row after them.
            if not self.have_meta:
                self._read_meta(payload)
                self.have_meta = True
            return

        if rtype == "turn_context":
            self.model = payload.get("model") or self.model
            self.effort = payload.get("effort") or self.effort
            return

        if rtype != "event_msg":
            return

        ptype = payload.get("type")
        ts = parse_ts(record.get("timestamp"))
        if ts is not None:
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

        if ptype == "token_count":
            self._read_token_count(payload, ts, result)
        elif ptype in EVENT_KINDS:
            self._read_event(ptype, ts, result)

    def _read_meta(self, payload: dict) -> None:
        self.session_id = payload.get("session_id")
        self.rollout_id = payload.get("id")
        self.parent_thread_id = payload.get("parent_thread_id")
        self.thread_source = payload.get("thread_source")
        self.agent_role = payload.get("agent_role")
        self.agent_nickname = payload.get("agent_nickname")
        self.cwd = payload.get("cwd")
        self.originator = payload.get("originator")
        self.cli_version = payload.get("cli_version")
        git = payload.get("git") or {}
        if isinstance(git, dict):
            self.git_repo = git.get("repository_url")
            self.git_branch = git.get("branch")
        source = payload.get("source") or {}
        spawn = (
            ((source.get("subagent") or {}).get("thread_spawn") or {})
            if isinstance(source, dict)
            else {}
        )
        if isinstance(spawn, dict):
            self.depth = spawn.get("depth")

    def _read_token_count(self, payload: dict, ts: int | None, result: ParseOutput) -> None:
        info = payload.get("info") or {}
        total = info.get("total_token_usage") or {}
        last = info.get("last_token_usage") or {}
        if not total or ts is None:
            return

        result.raw_events += 1

        cum = (
            int(total.get("input_tokens") or 0),
            int(total.get("cached_input_tokens") or 0),
            int(total.get("output_tokens") or 0),
            int(total.get("reasoning_output_tokens") or 0),
        )
        cum_total = int(total.get("total_tokens") or 0)
        if cum_total < self.max_cum_total:
            result.flag("cum_regression")
        self.max_cum_total = max(self.max_cum_total, cum_total)

        inp = int(last.get("input_tokens") or 0)
        cached = int(last.get("cached_input_tokens") or 0)
        out = int(last.get("output_tokens") or 0)
        reason = int(last.get("reasoning_output_tokens") or 0)
        ltotal = int(last.get("total_tokens") or 0)

        if cached > inp:
            result.flag("cached_gt_input")
        if reason > out:
            result.flag("reasoning_gt_output")
        if ltotal and ltotal != inp + out:
            result.flag("total_mismatch")

        limits = payload.get("rate_limits") or {}
        primary = (limits.get("primary") or {}) if isinstance(limits, dict) else {}
        session_id = self.session_id or ""

        result.requests.append(
            request_row(
                self.source,
                dedup_key(session_id, cum),
                ts=ts,
                session_id=session_id,
                cum=cum,
                rollout_id=self.rollout_id,
                model=self.model,
                effort=self.effort,
                # Codex already reports the whole prompt here, cache hits
                # included, and populates its cache for free.
                input_tokens=inp,
                cached_tokens=cached,
                output_tokens=out,
                reasoning_tokens=reason,
                ctx_window=info.get("model_context_window"),
                rate_limit=(
                    primary.get("used_percent"),
                    primary.get("window_minutes"),
                    primary.get("resets_at"),
                    limits.get("limit_id") if isinstance(limits, dict) else None,
                    limits.get("plan_type") if isinstance(limits, dict) else None,
                ),
            )
        )
        self.last_cum = cum
        self.ordinals = {}

    def _read_event(self, kind: str, ts: int | None, result: ParseOutput) -> None:
        if ts is None:
            return
        # Anchoring to the preceding request means event dedup inherits the
        # request dedup's correctness instead of needing a scheme of its own --
        # these payloads carry nothing else unique.
        ordinal = self.ordinals.get(kind, 0)
        self.ordinals[kind] = ordinal + 1
        result.events.append(
            (
                self.source,
                dedup_key(self.session_id or "", self.last_cum),
                kind,
                ordinal,
                ts,
                self.rollout_id,
            )
        )

    def session_row(self) -> tuple | None:
        if not self.rollout_id:
            return None
        return session_row(
            self.rollout_id,
            self.source,
            session_id=self.session_id,
            parent_thread_id=self.parent_thread_id,
            path=str(self.path),
            first_ts=self.first_ts,
            last_ts=self.last_ts,
            cwd=self.cwd,
            git_repo=self.git_repo,
            git_branch=self.git_branch,
            originator=self.originator,
            cli_version=self.cli_version,
            thread_source=self.thread_source,
            agent_role=self.agent_role,
            agent_nickname=self.agent_nickname,
            depth=self.depth,
            is_subagent=self.thread_source == "subagent",
        )

    def carry_columns(self) -> dict:
        return {
            "session_id": self.session_id,
            "rollout_id": self.rollout_id,
            "parent_thread_id": self.parent_thread_id,
            "thread_source": self.thread_source,
            "agent_role": self.agent_role,
            "agent_nickname": self.agent_nickname,
            "depth": self.depth,
            "cwd": self.cwd,
            "git_repo": self.git_repo,
            "git_branch": self.git_branch,
            "originator": self.originator,
            "cli_version": self.cli_version,
            "cur_model": self.model,
            "cur_effort": self.effort,
            "carry": json.dumps(self.carry_state()),
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


def parse_file(path: Path, cursor: dict | None, start_offset: int) -> ParseOutput:
    """Read a rollout from ``start_offset`` to the last complete line."""
    parser = RolloutParser(path, cursor)
    out = ParseOutput(offset=start_offset)
    out.bytes_read, out.offset, out.error = read_new_lines(
        path, start_offset, lambda line: parser.feed(line, out)
    )
    out.session = parser.session_row()
    out.carry = parser.carry_columns()
    return out


class CodexSource(JsonlSource):
    name = "codex"
    label = "Codex"

    def iter_files(self):
        return iter_rollouts(self.root)

    def parse(self, path: Path, cursor: dict | None, start_offset: int) -> ParseOutput:
        return parse_file(path, cursor, start_offset)
