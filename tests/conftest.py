"""Fixture builders for synthetic session files.

The generators mirror the shape of each client's real output closely enough to
exercise the parts that matter: Codex's cumulative counters and replayed
history, Claude Code's per-content-block rewrites and cache-write TTLs, Pi's
self-reported costs, OpenCode's mutable rows -- plus mid-file model switches and
partially written trailing lines.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest

from ccm.pricing import PricingTable
from ccm.store import Store


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


@dataclass
class Thread:
    """Builds a rollout line-by-line, tracking cumulative counters as Codex does."""

    session_id: str
    rollout_id: str
    clock: datetime
    model: str = "gpt-5.6-sol"
    effort: str = "high"
    cwd: str = "/home/dev/project"
    git_repo: str | None = "/home/dev/project.git"
    thread_source: str = "user"
    lines: list[str] = field(default_factory=list)
    cum: list[int] = field(default_factory=lambda: [0, 0, 0, 0])

    def _emit(self, obj: dict) -> None:
        self.lines.append(json.dumps(obj, separators=(",", ":")))

    def advance(self, seconds: float = 1.0) -> None:
        self.clock += timedelta(seconds=seconds)

    def meta(self) -> Thread:
        self._emit(
            {
                "timestamp": iso(self.clock),
                "type": "session_meta",
                "payload": {
                    "session_id": self.session_id,
                    "id": self.rollout_id,
                    "parent_thread_id": None,
                    "timestamp": iso(self.clock),
                    "cwd": self.cwd,
                    "originator": "codex-tui",
                    "cli_version": "0.144.1",
                    "thread_source": self.thread_source,
                    "agent_role": None,
                    "agent_nickname": None,
                    "source": {},
                    "git": {"repository_url": self.git_repo, "branch": "main"},
                },
            }
        )
        return self

    def turn_context(self, model: str | None = None, effort: str | None = None) -> Thread:
        if model:
            self.model = model
        if effort:
            self.effort = effort
        self._emit(
            {
                "timestamp": iso(self.clock),
                "type": "turn_context",
                "payload": {
                    "model": self.model,
                    "effort": self.effort,
                    "cwd": self.cwd,
                },
            }
        )
        return self

    def request(self, inp: int, cached: int, out: int, reasoning: int = 0) -> Thread:
        self.advance(2)
        self.cum[0] += inp
        self.cum[1] += cached
        self.cum[2] += out
        self.cum[3] += reasoning
        self._emit(
            {
                "timestamp": iso(self.clock),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": self.cum[0],
                            "cached_input_tokens": self.cum[1],
                            "output_tokens": self.cum[2],
                            "reasoning_output_tokens": self.cum[3],
                            "total_tokens": self.cum[0] + self.cum[2],
                        },
                        "last_token_usage": {
                            "input_tokens": inp,
                            "cached_input_tokens": cached,
                            "output_tokens": out,
                            "reasoning_output_tokens": reasoning,
                            "total_tokens": inp + out,
                        },
                        "model_context_window": 353400,
                    },
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {
                            "used_percent": 12.5,
                            "window_minutes": 10080,
                            "resets_at": 1784487518,
                        },
                        "plan_type": "pro",
                    },
                },
            }
        )
        return self

    def event(self, kind: str = "context_compacted") -> Thread:
        self.advance(1)
        self._emit(
            {
                "timestamp": iso(self.clock),
                "type": "event_msg",
                "payload": {"type": kind},
            }
        )
        return self

    def noise(self, count: int = 3) -> Thread:
        """Lines the scanner must skip: bulky payloads with no statistics."""
        for i in range(count):
            self._emit(
                {
                    "timestamp": iso(self.clock),
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "x" * 500}],
                        "seq": i,
                    },
                }
            )
        return self

    def text(self) -> str:
        return "".join(line + "\n" for line in self.lines)

    def write(self, directory: Path, name: str | None = None) -> Path:
        path = directory / (name or f"rollout-2026-07-01T00-00-00-{self.rollout_id}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.text())
        return path

    def replayed_into(self, rollout_id: str, at: datetime) -> Thread:
        """A resume: the whole history rewritten with the resume's clock.

        This is the behaviour that makes naive summation of the corpus wrong by
        roughly a factor of four, so it is the central thing the tests exercise.
        """
        clone = Thread(
            session_id=self.session_id,
            rollout_id=rollout_id,
            clock=at,
            model=self.model,
            effort=self.effort,
            cwd=self.cwd,
            git_repo=self.git_repo,
            thread_source=self.thread_source,
        )
        stamp = iso(at)
        for raw in self.lines:
            obj = json.loads(raw)
            obj["timestamp"] = stamp
            if obj.get("type") == "session_meta":
                # The replayed copy keeps the original's identity; the resuming
                # file announces itself in a fresh header written first.
                clone.lines.append(
                    json.dumps(
                        {
                            "timestamp": stamp,
                            "type": "session_meta",
                            "payload": {
                                **obj["payload"],
                                "id": rollout_id,
                            },
                        },
                        separators=(",", ":"),
                    )
                )
                continue
            clone.lines.append(json.dumps(obj, separators=(",", ":")))
        clone.cum = list(self.cum)
        return clone


@dataclass
class ClaudeTranscript:
    """Builds a Claude Code transcript.

    The important detail it reproduces is that one API response is written as
    several lines -- one per content block -- each repeating the whole usage
    object with a larger output count than the last.
    """

    session_id: str
    clock: datetime
    cwd: str = "/home/dev/project"
    model: str = "claude-opus-5"
    branch: str = "main"
    sidechain: bool = False
    lines: list[str] = field(default_factory=list)

    def _emit(self, obj: dict) -> None:
        self.lines.append(json.dumps(obj, separators=(",", ":")))

    def advance(self, seconds: float = 1.0) -> None:
        self.clock += timedelta(seconds=seconds)

    def response(
        self,
        *,
        fresh: int,
        cache_read: int,
        cache_write_1h: int = 0,
        cache_write_5m: int = 0,
        output: int,
        blocks: int = 1,
        message_id: str | None = None,
    ) -> ClaudeTranscript:
        """One API response, split across ``blocks`` transcript lines."""
        mid = message_id or f"msg_{uuid.uuid4().hex[:16]}"
        rid = f"req_{uuid.uuid4().hex[:16]}"
        for i in range(blocks):
            self.advance(0.5)
            # Output grows as the response streams; the prompt side is settled
            # from the first line.
            partial = output if i == blocks - 1 else max(1, output * i // blocks)
            self._emit(
                {
                    "type": "assistant",
                    "uuid": str(uuid.uuid4()),
                    "requestId": rid,
                    "timestamp": iso(self.clock),
                    "sessionId": self.session_id,
                    "cwd": self.cwd,
                    "gitBranch": self.branch,
                    "version": "2.1.220",
                    "isSidechain": self.sidechain,
                    "message": {
                        "id": mid,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "usage": {
                            "input_tokens": fresh,
                            "cache_read_input_tokens": cache_read,
                            "cache_creation_input_tokens": cache_write_1h
                            + cache_write_5m,
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": cache_write_1h,
                                "ephemeral_5m_input_tokens": cache_write_5m,
                            },
                            "output_tokens": partial,
                            "service_tier": "standard",
                        },
                    },
                }
            )
        return self

    def user_turn(self, text: str = "hello") -> ClaudeTranscript:
        """A line with no usage, which the reader must skip."""
        self.advance(1)
        self._emit(
            {
                "type": "user",
                "uuid": str(uuid.uuid4()),
                "timestamp": iso(self.clock),
                "sessionId": self.session_id,
                "cwd": self.cwd,
                "message": {"role": "user", "content": text},
            }
        )
        return self

    def write(self, root: Path, name: str | None = None) -> Path:
        path = root / (name or f"{self.session_id}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(line + "\n" for line in self.lines))
        return path


@dataclass
class PiSession:
    """Builds a Pi session file, including its self-reported cost."""

    session_id: str
    clock: datetime
    cwd: str = "/home/dev/project"
    provider: str = "minimax"
    model: str = "MiniMax-M3"
    lines: list[str] = field(default_factory=list)

    def _emit(self, obj: dict) -> None:
        self.lines.append(json.dumps(obj, separators=(",", ":")))

    def advance(self, seconds: float = 1.0) -> None:
        self.clock += timedelta(seconds=seconds)

    def header(self) -> PiSession:
        self._emit(
            {
                "type": "session",
                "version": 3,
                "id": self.session_id,
                "timestamp": iso(self.clock),
                "cwd": self.cwd,
            }
        )
        self._emit(
            {
                "type": "model_change",
                "id": uuid.uuid4().hex[:8],
                "timestamp": iso(self.clock),
                "provider": self.provider,
                "modelId": self.model,
            }
        )
        return self

    def response(
        self,
        *,
        fresh: int,
        cache_read: int,
        output: int,
        reasoning: int = 0,
        cost: float | None = None,
        response_id: str | None = None,
    ) -> PiSession:
        self.advance(2)
        self._emit(
            {
                "type": "message",
                "id": uuid.uuid4().hex[:8],
                "timestamp": iso(self.clock),
                "message": {
                    "role": "assistant",
                    "content": [],
                    "provider": self.provider,
                    "model": self.model,
                    "responseId": response_id or uuid.uuid4().hex[:12],
                    "timestamp": int(self.clock.timestamp() * 1000),
                    "usage": {
                        "input": fresh,
                        "output": output,
                        "cacheRead": cache_read,
                        "cacheWrite": 0,
                        "cacheWrite1h": 0,
                        "reasoning": reasoning,
                        "totalTokens": fresh + output + cache_read,
                        "cost": {"total": cost} if cost is not None else {},
                    },
                },
            }
        )
        return self

    def compaction(self) -> PiSession:
        self.advance(1)
        self._emit(
            {
                "type": "compaction",
                "id": uuid.uuid4().hex[:8],
                "timestamp": iso(self.clock),
                "summary": "...",
            }
        )
        return self

    def write(self, root: Path) -> Path:
        stamp = self.clock.strftime("%Y-%m-%dT%H-%M-%S-000Z")
        path = root / f"--home-dev-project--" / f"{stamp}_{self.session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(line + "\n" for line in self.lines))
        return path


def build_opencode_db(
    path: Path, messages: list[dict], *, session_id: str = "ses_test"
) -> Path:
    """A minimal OpenCode database with just the columns the reader touches."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS session (
            id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, parent_id TEXT,
            slug TEXT, directory TEXT, path TEXT, title TEXT, version TEXT,
            agent TEXT, model TEXT, time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE IF NOT EXISTS message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO session (id, project_id, slug, directory, title,"
        " version, agent, model, time_created, time_updated)"
        " VALUES (?, 'p', 'brisk-otter', '/home/dev/project', 't', '1.0', 'build',"
        " '{\"id\":\"big-pickle\",\"providerID\":\"opencode\"}', ?, ?)",
        (session_id, messages[0]["time_created"], messages[-1]["time_updated"]),
    )
    for m in messages:
        conn.execute(
            "INSERT OR REPLACE INTO message (id, session_id, time_created,"
            " time_updated, data) VALUES (?, ?, ?, ?, ?)",
            (
                m["id"],
                session_id,
                m["time_created"],
                m["time_updated"],
                json.dumps(m["data"]),
            ),
        )
    conn.commit()
    conn.close()
    return path


def opencode_message(
    mid: str,
    ts: int,
    *,
    fresh: int,
    cache_read: int,
    output: int,
    reasoning: int = 0,
    cost: float = 0.0,
    updated: int | None = None,
) -> dict:
    """One OpenCode assistant row.

    Note that ``reasoning`` sits alongside ``output`` rather than inside it, and
    ``input`` excludes cache traffic -- both the opposite of Codex.
    """
    return {
        "id": mid,
        "time_created": ts,
        "time_updated": updated if updated is not None else ts,
        "data": {
            "role": "assistant",
            "providerID": "opencode",
            "modelID": "big-pickle",
            "cost": cost,
            "tokens": {
                "input": fresh,
                "output": output,
                "reasoning": reasoning,
                "total": fresh + output + reasoning + cache_read,
                "cache": {"read": cache_read, "write": 0},
            },
            "time": {"created": ts, "completed": ts + 1000},
        },
    }


@dataclass
class GrokSession:
    """Builds one Grok session directory: its update log and its summary.

    Grok reports at turn granularity, so ``turn`` writes a whole turn's usage --
    optionally covering several API calls, and optionally split across models.
    """

    session_id: str
    clock: datetime
    cwd: str = "/home/dev/project"
    model: str = "glm-5.2"
    alias: str = "llmp-glm-5-2"
    kind: str = "primary"
    agent_name: str = "grok-build-plan"
    lines: list[str] = field(default_factory=list)
    seq: int = 0

    def advance(self, seconds: float = 1.0) -> None:
        self.clock += timedelta(seconds=seconds)

    def _emit(self, update: dict) -> None:
        self.seq += 1
        self.lines.append(
            json.dumps(
                {
                    "timestamp": int(self.clock.timestamp()),
                    "method": "_x.ai/session/update",
                    "params": {
                        "sessionId": self.session_id,
                        "update": update,
                        "_meta": {
                            "eventId": f"{self.session_id}-{self.seq}",
                            "agentTimestampMs": int(self.clock.timestamp() * 1000),
                        },
                    },
                },
                separators=(",", ":"),
            )
        )

    def chatter(self) -> GrokSession:
        """A line with no usage on it, which the reader must skip."""
        self.advance(1)
        self._emit({"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}})
        return self

    def turn(
        self,
        *,
        prompt: int,
        cached: int,
        output: int,
        reasoning: int = 0,
        calls: int = 1,
        models: dict[str, dict] | None = None,
    ) -> GrokSession:
        self.advance(5)
        usage = {
            "inputTokens": prompt,
            "outputTokens": output,
            # Grok's own identity: the cached part is inside inputTokens, and
            # reasoning is inside outputTokens, so neither is added again here.
            "totalTokens": prompt + output,
            "cachedReadTokens": cached,
            "reasoningTokens": reasoning,
            "modelCalls": calls,
            "modelUsage": models
            if models is not None
            else {
                self.model: {
                    "inputTokens": prompt,
                    "outputTokens": output,
                    "totalTokens": prompt + output,
                    "cachedReadTokens": cached,
                    "reasoningTokens": reasoning,
                    "modelCalls": calls,
                }
            },
            "numTurns": 1,
        }
        self._emit(
            {
                "sessionUpdate": "turn_completed",
                "prompt_id": uuid.uuid4().hex[:12],
                "stop_reason": "end_turn",
                "usage": usage,
            }
        )
        return self

    def write(self, root: Path, *, parent: str | None = None) -> Path:
        project = root / quote(self.cwd, safe="")
        directory = project / self.session_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "updates.jsonl").write_text(
            "".join(line + "\n" for line in self.lines)
        )
        (directory / "summary.json").write_text(
            json.dumps(
                {
                    "info": {"id": self.session_id, "cwd": self.cwd},
                    "created_at": iso(self.clock),
                    "updated_at": iso(self.clock),
                    "current_model_id": self.alias,
                    "session_kind": self.kind,
                    "agent_name": self.agent_name,
                    "head_branch": "master",
                    "git_remotes": ["ssh://git@example.com/dev/project.git"],
                }
            )
        )
        if parent:
            (project / parent / "subagents" / self.session_id).mkdir(
                parents=True, exist_ok=True
            )
        return directory / "updates.jsonl"


def write_grok_config(root: Path, **models: dict) -> Path:
    """The ``config.toml`` beside the session tree, naming each model's gateway."""
    body = ["[cli]", 'installer = "internal"', ""]
    for alias, spec in models.items():
        body.append(f"[model.{alias}]")
        body.append(f'model = "{spec["model"]}"')
        if spec.get("provider"):
            body.append(f'model_provider = "{spec["provider"]}"')
        if spec.get("ctx"):
            body.append(f"context_window = {spec['ctx']}")
        body.append("")
    path = root / "config.toml"
    path.write_text("\n".join(body))
    return path


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions" / "2026" / "07" / "01"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "ccm.sqlite")
    yield s
    s.close()


@pytest.fixture
def pricing(tmp_path: Path) -> PricingTable:
    path = tmp_path / "pricing.toml"
    path.write_text(
        """
[defaults]
long_context_threshold = 1000

[models."gpt-5.6-sol"]
input = 5.0
cached_input = 0.5
output = 30.0
[models."gpt-5.6-sol".long]
input = 10.0
cached_input = 1.0
output = 45.0

[models."gpt-5.6-terra"]
input = 2.5
cached_input = 0.25
output = 15.0

[models."cheap"]
inherit = "gpt-5.6-terra"
input = 1.0
"""
    )
    return PricingTable(path)


@pytest.fixture
def clock() -> datetime:
    # Timezone-aware so that the "Z" written into fixtures and the epoch
    # arithmetic in assertions agree; a naive value would be read as UTC on the
    # way in and as local time on the way out.
    return datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
