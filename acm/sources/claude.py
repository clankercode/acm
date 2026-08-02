"""Claude Code transcripts under ``~/.claude/projects``.

Two things differ from Codex and both change the arithmetic.

**Streaming rewrites.** A response is written out once per content block as it
arrives -- text, thinking, each tool call -- and every one of those lines
carries the *whole* usage object as known so far. Prompt-side counts are settled
from the first line; ``output_tokens`` grows. 4,788 lines in this corpus
describe 2,057 real requests. Keying on the API message id collapses them, and
ranking by output tokens keeps the final, complete sighting rather than a
truncated early one.

**Cache writes are billed.** Anthropic charges 1.25x base input to store a
5-minute cache entry and 2x for a 1-hour one, against 0.1x to read one back. It
also reports ``input_tokens`` as the *uncached remainder* rather than the whole
prompt, which is the opposite of Codex. Both are normalised here: the prompt
total is reassembled, and the write tokens are split by TTL so they can be
priced apart.
"""

from __future__ import annotations

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

#: Only assistant records carry usage, and the marker is cheap to test for
#: before parsing. Transcripts are dominated by tool results and user turns.
_USAGE_MARKER = b'"usage"'

#: Claude Code emits this in place of a model name for locally generated
#: error placeholders. They report zero tokens and never hit the API.
_SYNTHETIC = "<synthetic>"


class ClaudeParser:
    """Stateful parser for one transcript file."""

    source = "claude"

    def __init__(self, path: Path, root: Path, cursor: dict | None = None):
        self.path = path
        cursor = cursor or {}
        self.rollout_id: str = cursor.get("rollout_id") or rollout_id_for(path, root)
        self.session_id: str | None = cursor.get("session_id")
        self.cwd: str | None = cursor.get("cwd")
        self.git_branch: str | None = cursor.get("git_branch")
        self.cli_version: str | None = cursor.get("cli_version")
        self.agent_nickname: str | None = cursor.get("agent_nickname")
        self.first_ts: int | None = cursor.get("first_ts")
        self.last_ts: int | None = cursor.get("last_ts")
        # Sidechain is a property of the transcript, not of a single line, and
        # subagent transcripts live in their own directory.
        self.is_subagent: bool = bool(cursor.get("depth")) or "subagents" in path.parts

    def feed(self, line: bytes, out: ParseOutput) -> None:
        if _USAGE_MARKER not in line:
            return
        try:
            record = orjson.loads(line)
        except orjson.JSONDecodeError:
            out.flag("parse_error")
            return
        message = record.get("message")
        if not isinstance(message, dict):
            return
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return

        ts = parse_ts(record.get("timestamp"))
        if ts is None:
            return
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

        self.session_id = record.get("sessionId") or self.session_id
        self.cwd = record.get("cwd") or self.cwd
        self.git_branch = record.get("gitBranch") or self.git_branch
        self.cli_version = record.get("version") or self.cli_version
        self.agent_nickname = record.get("slug") or self.agent_nickname
        if record.get("isSidechain"):
            self.is_subagent = True

        out.raw_events += 1

        model = message.get("model")
        if model == _SYNTHETIC:
            out.flag("synthetic_message")
            return

        fresh = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)

        # The TTL split is reported separately and occasionally disagrees with
        # the total. Trust the total -- it is the number that gets billed -- and
        # put any shortfall on the cheaper 5-minute rate.
        breakdown = usage.get("cache_creation")
        long_ttl = 0
        if isinstance(breakdown, dict):
            long_ttl = int(breakdown.get("ephemeral_1h_input_tokens") or 0)
            short_ttl = int(breakdown.get("ephemeral_5m_input_tokens") or 0)
            if long_ttl + short_ttl != cache_write:
                out.flag("cache_ttl_mismatch")
                long_ttl = min(long_ttl, cache_write)
        short_ttl = cache_write - long_ttl

        # dk must survive a resume copying this line into another transcript,
        # so it keys on the API's own identifiers, not on file position.
        dk = message.get("id") or record.get("requestId") or record.get("uuid")
        if not dk:
            out.flag("missing_message_id")
            return

        output = int(usage.get("output_tokens") or 0)
        out.requests.append(
            request_row(
                self.source,
                str(dk),
                ts=ts,
                # Later lines for the same response repeat the settled prompt
                # counts and a larger output count, so the largest output is
                # the complete one.
                rank=output,
                session_id=self.session_id,
                rollout_id=self.rollout_id,
                model=model,
                effort=record.get("effort"),
                input_tokens=fresh + cache_read + cache_write,
                cached_tokens=cache_read,
                cache_write_tokens=short_ttl,
                cache_write_1h_tokens=long_ttl,
                output_tokens=output,
            )
        )

    def session_row(self) -> tuple:
        return session_row(
            self.rollout_id,
            self.source,
            session_id=self.session_id,
            path=str(self.path),
            first_ts=self.first_ts,
            last_ts=self.last_ts,
            cwd=self.cwd,
            git_branch=self.git_branch,
            cli_version=self.cli_version,
            thread_source="subagent" if self.is_subagent else "main",
            agent_nickname=self.agent_nickname,
            depth=1 if self.is_subagent else 0,
            is_subagent=self.is_subagent,
        )

    def carry_columns(self) -> dict:
        return {
            "rollout_id": self.rollout_id,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "cli_version": self.cli_version,
            "agent_nickname": self.agent_nickname,
            "thread_source": "subagent" if self.is_subagent else "main",
            "depth": 1 if self.is_subagent else 0,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


def rollout_id_for(path: Path, root: Path) -> str:
    """A stable id for a transcript.

    The path relative to the projects root, because subagent transcripts are
    named for a content hash that is not unique across projects on its own.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return f"claude:{rel.as_posix().removesuffix('.jsonl')}"


class ClaudeSource(JsonlSource):
    name = "claude"
    label = "Claude Code"

    def iter_files(self):
        return walk_jsonl(self.root)

    def parse(self, path: Path, cursor: dict | None, start_offset: int) -> ParseOutput:
        parser = ClaudeParser(path, self.root, cursor)
        out = ParseOutput(offset=start_offset)
        out.bytes_read, out.offset, out.error, out.rows = read_new_lines(
            path, start_offset, lambda line: parser.feed(line, out)
        )
        out.session = parser.session_row()
        out.carry = parser.carry_columns()
        return out
