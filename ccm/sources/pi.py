"""Pi agent sessions under ``~/.pi/agent/sessions``.

The friendliest of the four formats: one file per session, one line per
message, no replay, and each assistant message carries a settled usage object
exactly once. Deduplication still keys on the provider's response id rather than
file position, because a session that is resumed can repeat its tail.

Pi is also the only client that records what it thinks each request cost. That
figure is stored but never used in any reported number -- it is kept so the Data
Quality panel can check our arithmetic against the vendor's.
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

_WANTED = (b'"message"', b'"session"', b'"compaction"', b'"model_change"')

#: Pi records these against the preceding request, for the chart overlay.
_EVENT_TYPES = {"compaction": "context_compacted"}


class PiParser:
    source = "pi"

    def __init__(self, path: Path, cursor: dict | None = None):
        self.path = path
        cursor = cursor or {}
        self.rollout_id: str = cursor.get("rollout_id") or f"pi:{path.stem}"
        self.session_id: str | None = cursor.get("session_id")
        self.cwd: str | None = cursor.get("cwd")
        self.cli_version: str | None = cursor.get("cli_version")
        self.model: str | None = cursor.get("cur_model")
        self.effort: str | None = cursor.get("cur_effort")
        self.first_ts: int | None = cursor.get("first_ts")
        self.last_ts: int | None = cursor.get("last_ts")
        self.last_dk: str | None = cursor.get("carry") or None
        self.ordinals: dict[str, int] = {}

    def feed(self, line: bytes, out: ParseOutput) -> None:
        if not any(marker in line[:200] for marker in _WANTED):
            return
        try:
            record = orjson.loads(line)
        except orjson.JSONDecodeError:
            out.flag("parse_error")
            return

        kind = record.get("type")
        ts = parse_ts(record.get("timestamp"))
        if ts is not None:
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

        if kind == "session":
            self.session_id = record.get("id") or self.session_id
            self.cwd = record.get("cwd") or self.cwd
            self.cli_version = record.get("version") and str(record["version"])
            return
        if kind == "model_change":
            provider = record.get("provider")
            model = record.get("modelId")
            if model:
                self.model = f"{provider}/{model}" if provider else model
            return
        if kind == "thinking_level_change":
            self.effort = record.get("thinkingLevel") or self.effort
            return
        if kind in _EVENT_TYPES:
            self._event(_EVENT_TYPES[kind], ts, out)
            return
        if kind != "message":
            return

        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        if ts is None:
            ts = parse_ts(message.get("timestamp"))
        if ts is None:
            return

        out.raw_events += 1

        provider = message.get("provider")
        model = message.get("model") or self.model
        if model and provider and "/" not in model:
            model = f"{provider}/{model}"
        self.model = model or self.model

        fresh = int(usage.get("input") or 0)
        cache_read = int(usage.get("cacheRead") or 0)
        cache_write = int(usage.get("cacheWrite") or 0)
        # cacheWrite1h is reported alongside cacheWrite, and the file's own
        # totalTokens identity balances with cacheWrite alone -- so the long-TTL
        # figure is a subset of it, not an addition to it.
        long_ttl = min(int(usage.get("cacheWrite1h") or 0), cache_write)

        output = int(usage.get("output") or 0)
        reasoning = int(usage.get("reasoning") or 0)
        if reasoning > output:
            out.flag("reasoning_gt_output")

        cost = usage.get("cost")
        client_cost = cost.get("total") if isinstance(cost, dict) else None

        dk = message.get("responseId") or record.get("id")
        if not dk:
            out.flag("missing_response_id")
            return

        out.requests.append(
            request_row(
                self.source,
                str(dk),
                ts=ts,
                session_id=self.session_id,
                rollout_id=self.rollout_id,
                model=model,
                effort=self.effort,
                input_tokens=fresh + cache_read + cache_write,
                cached_tokens=cache_read,
                cache_write_tokens=cache_write - long_ttl,
                cache_write_1h_tokens=long_ttl,
                # Reasoning is inside output here, as it is for Codex.
                output_tokens=output,
                reasoning_tokens=reasoning,
                client_cost=client_cost,
            )
        )
        self.last_dk = str(dk)
        self.ordinals = {}

    def _event(self, kind: str, ts: int | None, out: ParseOutput) -> None:
        if ts is None or self.last_dk is None:
            return
        ordinal = self.ordinals.get(kind, 0)
        self.ordinals[kind] = ordinal + 1
        out.events.append((self.source, self.last_dk, kind, ordinal, ts, self.rollout_id))

    def session_row(self) -> tuple:
        return session_row(
            self.rollout_id,
            self.source,
            session_id=self.session_id,
            path=str(self.path),
            first_ts=self.first_ts,
            last_ts=self.last_ts,
            cwd=self.cwd,
            cli_version=self.cli_version,
            thread_source="main",
        )

    def carry_columns(self) -> dict:
        return {
            "rollout_id": self.rollout_id,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "cli_version": self.cli_version,
            "cur_model": self.model,
            "cur_effort": self.effort,
            "carry": self.last_dk,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


class PiSource(JsonlSource):
    name = "pi"
    label = "Pi"

    def iter_files(self):
        return walk_jsonl(self.root)

    def parse(self, path: Path, cursor: dict | None, start_offset: int) -> ParseOutput:
        parser = PiParser(path, cursor)
        out = ParseOutput(offset=start_offset)
        out.bytes_read, out.offset, out.error = read_new_lines(
            path, start_offset, lambda line: parser.feed(line, out)
        )
        out.session = parser.session_row()
        out.carry = parser.carry_columns()
        return out
