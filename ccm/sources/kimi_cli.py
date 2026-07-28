"""Kimi CLI sessions under ``~/.kimi/sessions``.

A different product from Kimi Code, sharing a family but not a format. The tree
is one directory per working tree (a hash of the cwd) then one per session
(a uuid), with a single ``wire.jsonl`` at the session root.

Usage lands on ``StatusUpdate`` messages inside the ``message`` envelope, in a
``token_usage`` object with snake_case keys: ``input_other`` is the uncached
remainder, ``input_cache_read`` and ``input_cache_creation`` are addends -- the
same convention as Kimi Code, only cased differently. Each carries a
``message_id`` (``chatcmpl-…``) that is the per-request dedup key.

No model is recorded anywhere in this format. The wire log names providers and
protocols but never the model id, so every request is emitted with ``model=None``
and lands as unpriced. The token and cache-ratio columns are still valuable, and
the absence is documented rather than papered over with a guess.
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

#: Cheap pre-filter: only StatusUpdate lines can carry token_usage.
_MARKER = b'"StatusUpdate"'


def session_id_from_path(path: Path) -> str:
    """The session directory name (a uuid), one level above the wire log."""
    return path.parent.name


class KimiCliParser:
    """Stateful parser for one Kimi CLI session's wire log."""

    source = "kimi_cli"

    def __init__(self, path: Path, cursor: dict | None = None):
        self.path = path
        cursor = cursor or {}
        self.session_id: str = cursor.get("session_id") or session_id_from_path(path)
        self.rollout_id: str = (
            cursor.get("rollout_id") or f"kimi_cli:{self.session_id}"
        )
        self.first_ts: int | None = cursor.get("first_ts")
        self.last_ts: int | None = cursor.get("last_ts")

    def feed(self, line: bytes, out: ParseOutput) -> None:
        if _MARKER not in line:
            return
        try:
            record = orjson.loads(line)
        except orjson.JSONDecodeError:
            out.flag("parse_error")
            return

        # The wire log is {timestamp, message: {type, payload: {...}}}.
        message = record.get("message")
        if not isinstance(message, dict) or message.get("type") != "StatusUpdate":
            return
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        usage = payload.get("token_usage")
        if not isinstance(usage, dict):
            return
        dk = payload.get("message_id")
        if not dk:
            return

        # timestamp is epoch seconds (a float ~1.7e9); parse_ts handles it.
        ts = parse_ts(record.get("timestamp"))
        if ts is not None:
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

        out.raw_events += 1

        fresh = int(usage.get("input_other") or 0)
        cache_read = int(usage.get("input_cache_read") or 0)
        cache_write = int(usage.get("input_cache_creation") or 0)

        out.requests.append(
            request_row(
                self.source,
                str(dk),
                ts=ts if ts is not None else 0,
                session_id=self.session_id,
                rollout_id=self.rollout_id,
                # No model is recorded anywhere in this format.
                model=None,
                input_tokens=fresh + cache_read + cache_write,
                cached_tokens=cache_read,
                cache_write_tokens=cache_write,
                output_tokens=int(usage.get("output") or 0),
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
            thread_source="main",
        )

    def carry_columns(self) -> dict:
        return {
            "rollout_id": self.rollout_id,
            "session_id": self.session_id,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


class KimiCliSource(JsonlSource):
    name = "kimi_cli"
    label = "Kimi CLI"

    def iter_files(self):
        return walk_jsonl(self.root, match=lambda n: n == "wire.jsonl")

    def parse(self, path: Path, cursor: dict | None, start_offset: int) -> ParseOutput:
        parser = KimiCliParser(path, cursor)
        out = ParseOutput(offset=start_offset)
        out.bytes_read, out.offset, out.error, out.rows = read_new_lines(
            path, start_offset, lambda line: parser.feed(line, out)
        )
        out.session = parser.session_row()
        out.carry = parser.carry_columns()
        return out
