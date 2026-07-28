"""Gemini CLI sessions under ``~/.gemini/tmp``.

Each project gets a directory under ``tmp``; inside it, ``chats/`` holds one
``session-<timestamp>-<hash>.jsonl`` per conversation. The file is append-only
JSONL with a line per message.

Token usage rides on ``type == "gemini"`` lines in a ``tokens`` object whose
keys map cleanly to CCM's convention: ``input`` is the whole prompt (it grows
within a session as context accumulates), ``cached`` is the subset served from
the context cache, ``output`` is the visible response, and ``thoughts`` are
reasoning tokens billed at the output tier. The identity
``total == input + output + thoughts + tool`` holds, so folding ``thoughts``
into ``output_tokens`` (as Grok folds its ``reasoningTokens``) keeps the
``total == input + output`` invariant quiet.

Each API response is written twice -- once for the text part, once for the
tool-call part -- with the same ``id`` and identical ``tokens``. Using ``id``
as the dedup key lets the scanner drop the duplicate transparently.

No model other than ``gemini-3-flash-preview`` has appeared in the corpus, and
that model has no published rate card, so it lands in ``UNPRICED_BY_DESIGN``
rather than being guessed at.
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

#: Cheap pre-filter: only lines carrying a tokens block can yield usage.
_MARKER = b'"tokens"'


def session_id_from_path(path: Path) -> str:
    """The session-file stem, e.g. ``session-2026-05-30T16-08-6cf3e265``."""
    return path.stem


class GeminiParser:
    """Stateful parser for one Gemini CLI session log."""

    source = "gemini"

    def __init__(self, path: Path, cursor: dict | None = None):
        self.path = path
        cursor = cursor or {}
        self.session_id: str = cursor.get("session_id") or session_id_from_path(path)
        self.rollout_id: str = cursor.get("rollout_id") or f"gemini:{self.session_id}"
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

        if record.get("type") != "gemini":
            return
        tokens = record.get("tokens")
        if not isinstance(tokens, dict):
            return

        ts = parse_ts(record.get("timestamp"))
        if ts is not None:
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

        dk = record.get("id")
        if not dk:
            return

        out.raw_events += 1

        prompt = int(tokens.get("input") or 0)
        cached = int(tokens.get("cached") or 0)
        output = int(tokens.get("output") or 0)
        thoughts = int(tokens.get("thoughts") or 0)
        tool = int(tokens.get("tool") or 0)

        if cached > prompt:
            out.flag("cached_gt_input")
            cached = prompt

        out.requests.append(
            request_row(
                self.source,
                str(dk),
                ts=ts if ts is not None else 0,
                session_id=self.session_id,
                rollout_id=self.rollout_id,
                model=record.get("model"),
                input_tokens=prompt,
                cached_tokens=cached,
                # Fold thoughts and tool into output so the total invariant
                # (total == input + output) stays quiet.
                output_tokens=output + thoughts + tool,
                reasoning_tokens=thoughts,
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


class GeminiSource(JsonlSource):
    name = "gemini"
    label = "Gemini CLI"

    def iter_files(self):
        return walk_jsonl(self.root, match=lambda n: n.startswith("session-"))

    def parse(self, path: Path, cursor: dict | None, start_offset: int) -> ParseOutput:
        parser = GeminiParser(path, cursor)
        out = ParseOutput(offset=start_offset)
        out.bytes_read, out.offset, out.error, out.rows = read_new_lines(
            path, start_offset, lambda line: parser.feed(line, out)
        )
        out.session = parser.session_row()
        out.carry = parser.carry_columns()
        return out
