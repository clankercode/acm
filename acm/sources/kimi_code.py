"""Kimi Code sessions under ``~/.kimi-code/sessions``.

The tree is one directory per working tree (``wd_<name>_<hash>``), one per
session (``session_<uuid>``), and one ``wire.jsonl`` per agent thread inside
``agents/<name>/`` -- ``main`` is the primary thread and ``agent-0`` …
``agent-N`` are the subagents it spawned.

Token usage lands on ``step.end`` events wrapped in ``context.append_loop_event``
records. Each such step is one real API call: it carries its own per-request
usage object and a ``messageId`` from the provider (``chatcmpl-…``), so the row
here is a request, exactly as it is for Codex and Claude Code. A separate
``usage.record`` event (``usageScope:"turn"``) sums a sparse subset of the steps
and would double-count if mixed in; it is ignored for usage and used only as a
cross-check in the corpus test.

Kimi reports tokens the Anthropic way, not the OpenAI way: ``inputOther`` is the
uncached remainder, with cache reads and cache creation reported alongside it as
separate addends. The whole prompt is reassembled here, as it is for Claude
Code and Pi, so the cache-rate columns stay meaningful.

No ``cwd`` is written to the wire log. The project label is decoded from the
working-tree directory name instead: ``wd_c2c_d6fdc22aef87`` becomes ``c2c``.
"""

from __future__ import annotations

import os
import re
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

#: A step that completed carries the only per-request usage in the tree.
_STEP_END_MARKER = b'"step.end"'
#: A request about to fire names the model (and effort) the steps will bill at.
_LLM_REQUEST_MARKER = b'"llm.request"'

#: ``wd_<name>_<12 hex>``, where <name> is the basename of the session's cwd.
_WD_DIR = re.compile(r"^wd_(.*)_[0-9a-f]{12}$")


def project_name_from_path(path: Path) -> str:
    """The session's working-tree basename, decoded from the directory layout.

    The path is ``.../wd_<name>_<hash>/session_<uuid>/agents/<agent>/wire.jsonl``.
    Kimi stores only the basename of the cwd plus a disambiguating hash, so the
    full path cannot be reconstructed -- but the basename is what
    :func:`~acm.sources.base.project_label` reduces to anyway.
    """
    for parent in path.parents:
        name = parent.name
        m = _WD_DIR.match(name)
        if m:
            return m.group(1)
    return "unknown"


def session_id_from_path(path: Path) -> str:
    """The ``session_<uuid>`` directory name, stripped of its prefix."""
    for parent in path.parents:
        if parent.name.startswith("session_"):
            return parent.name.removeprefix("session_")
    return path.parent.name


def agent_name_from_path(path: Path) -> str:
    """The agent directory immediately holding the wire log."""
    return path.parent.name


class KimiCodeParser:
    """Stateful parser for one agent's wire log."""

    source = "kimi_code"

    def __init__(self, path: Path, cursor: dict | None = None):
        self.path = path
        cursor = cursor or {}
        self.session_id: str = cursor.get("session_id") or session_id_from_path(path)
        agent = agent_name_from_path(path)
        self.agent: str = agent
        self.rollout_id: str = (
            cursor.get("rollout_id") or f"kimi_code:{self.session_id}:{agent}"
        )
        self.is_subagent: bool = agent != "main"
        self.parent_thread_id: str | None = (
            f"kimi_code:{self.session_id}:main" if self.is_subagent else None
        )
        self.project: str = cursor.get("cwd") or project_name_from_path(path)
        # Model and effort are set by llm.request and held until the next step.end.
        self.model: str | None = cursor.get("cur_model")
        self.effort: str | None = cursor.get("cur_effort")
        self.first_ts: int | None = cursor.get("first_ts")
        self.last_ts: int | None = cursor.get("last_ts")

    def feed(self, line: bytes, out: ParseOutput) -> None:
        if _STEP_END_MARKER not in line and _LLM_REQUEST_MARKER not in line:
            return
        try:
            record = orjson.loads(line)
        except orjson.JSONDecodeError:
            out.flag("parse_error")
            return

        rtype = record.get("type")
        ts = parse_ts(record.get("time"))
        if ts is not None:
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

        if rtype == "llm.request":
            self._track_model(record)
            return

        if rtype != "context.append_loop_event":
            return

        event = record.get("event")
        if not isinstance(event, dict) or event.get("type") != "step.end":
            return
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return
        dk = event.get("messageId")
        if not dk:
            return  # a step with no API response id carries nothing billable

        out.raw_events += 1

        fresh = int(usage.get("inputOther") or 0)
        cache_read = int(usage.get("inputCacheRead") or 0)
        cache_write = int(usage.get("inputCacheCreation") or 0)

        out.requests.append(
            request_row(
                self.source,
                str(dk),
                ts=ts if ts is not None else 0,
                session_id=self.session_id,
                rollout_id=self.rollout_id,
                model=self.model,
                effort=self.effort,
                input_tokens=fresh + cache_read + cache_write,
                cached_tokens=cache_read,
                cache_write_tokens=cache_write,
                output_tokens=int(usage.get("output") or 0),
            )
        )

    def _track_model(self, record: dict) -> None:
        # modelAlias carries the gateway the way Grok's config does; model is the
        # bare base name. Prefer the alias so routed traffic stays attributable.
        alias = record.get("modelAlias")
        if alias:
            self.model = str(alias)
        elif record.get("model"):
            self.model = str(record["model"])
        effort = record.get("thinkingEffort")
        if effort:
            self.effort = str(effort)

    def session_row(self) -> tuple:
        return session_row(
            self.rollout_id,
            self.source,
            session_id=self.session_id,
            parent_thread_id=self.parent_thread_id,
            path=str(self.path),
            first_ts=self.first_ts,
            last_ts=self.last_ts,
            cwd=self.project,
            thread_source="subagent" if self.is_subagent else "main",
            depth=1 if self.is_subagent else 0,
            is_subagent=self.is_subagent,
        )

    def carry_columns(self) -> dict:
        return {
            "rollout_id": self.rollout_id,
            "session_id": self.session_id,
            "cwd": self.project,
            "cur_model": self.model,
            "cur_effort": self.effort,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


class KimiCodeSource(JsonlSource):
    name = "kimi_code"
    label = "Kimi Code"

    def iter_files(self):
        return walk_jsonl(self.root, match=lambda n: n == "wire.jsonl")

    def parse(self, path: Path, cursor: dict | None, start_offset: int) -> ParseOutput:
        parser = KimiCodeParser(path, cursor)
        out = ParseOutput(offset=start_offset)
        out.bytes_read, out.offset, out.error, out.rows = read_new_lines(
            path, start_offset, lambda line: parser.feed(line, out)
        )
        out.session = parser.session_row()
        out.carry = parser.carry_columns()
        return out
