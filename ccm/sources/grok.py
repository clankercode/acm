"""Grok CLI sessions under ``~/.grok/sessions``.

One directory per working tree (its path URL-encoded), one directory per session
inside that, and the session's own append-only ``updates.jsonl`` within that.
Token usage arrives on a ``turn_completed`` update; nothing else in the tree
records it.

Two things make Grok different from the other four clients.

**It reports per turn, not per request.** One record covers the ``modelCalls``
API calls the turn needed, and for a subagent it covers the whole run. Nothing
finer is written down -- per-call figures exist only in a rolling debug log with
no model attribution and no retention guarantee -- so a row here is a turn, and
the calls it folded together are counted as an anomaly rather than left to make
the request count quietly mean something different for one client.

**It names a routed model twice.** ``[model.llmp-glm-5-2]`` is the alias the
session records; ``model = "glm-5.2"`` is what the usage block reports. Only
``config.toml`` knows they are the same thing, and knowing it is what puts
gateway traffic beside direct traffic in the route comparison.

The token conventions match Codex and Pi: ``inputTokens`` is the whole prompt
with ``cachedReadTokens`` a subset of it, and reasoning sits inside output. Both
follow from Grok's own identity ``totalTokens == inputTokens + outputTokens``,
which leaves no room for either to be a separate addend, and are corroborated by
its debug log calling that same number ``prompt_tokens`` -- the OpenAI field that
has always included the cached part. Cache writes are neither billed nor
reported by xAI or by the gateways it routes through.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import orjson

from .base import (
    JsonlSource,
    ParseOutput,
    parse_ts,
    read_new_lines,
    request_row,
    session_row,
)

#: Cheap pre-filter: every line carrying usage names the update kind.
_MARKER = b'"turn_completed"'


@dataclass(frozen=True)
class ModelInfo:
    """One model as Grok is configured to reach it."""

    #: What the usage block calls it, e.g. ``glm-5.2``.
    name: str
    #: The gateway it goes through; empty means straight to the vendor.
    provider: str
    ctx_window: int | None

    @property
    def qualified(self) -> str:
        return f"{self.provider}/{self.name}" if self.provider else self.name


@dataclass(frozen=True)
class Catalogue:
    """Model lookup, by the two names Grok uses for the same thing."""

    by_alias: dict[str, ModelInfo]
    by_name: dict[str, ModelInfo]

    @classmethod
    def empty(cls) -> Catalogue:
        return cls({}, {})

    def resolve(self, name: str | None) -> ModelInfo | None:
        if not name:
            return None
        return self.by_name.get(name) or self.by_alias.get(name)


def read_catalogue(grok_home: Path) -> Catalogue:
    """Model routing as configured, from the two files that describe it.

    ``config.toml`` covers models reached through a gateway, which is where the
    provider prefix comes from. ``models_cache.json`` covers the vendor's own
    catalogue, and is only consulted for the context window -- a cache file is
    not something to depend on for anything that changes a figure.
    """
    by_alias: dict[str, ModelInfo] = {}
    by_name: dict[str, ModelInfo] = {}

    try:
        config = tomllib.loads((grok_home / "config.toml").read_text())
    except (OSError, ValueError):
        config = {}
    for alias, entry in (config.get("model") or {}).items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("model")
        if not name:
            continue
        window = entry.get("context_window")
        info = ModelInfo(
            name=str(name),
            provider=str(entry.get("model_provider") or ""),
            ctx_window=int(window) if isinstance(window, int) else None,
        )
        by_alias[str(alias)] = info
        # First alias wins: two aliases for one model through different
        # gateways is ambiguous, and guessing differently per turn would be
        # worse than picking one and being consistent.
        by_name.setdefault(info.name, info)

    try:
        cached = orjson.loads((grok_home / "models_cache.json").read_bytes())
    except (OSError, orjson.JSONDecodeError):
        cached = {}
    for model_id, entry in (cached.get("models") or {}).items():
        info_blob = entry.get("info") if isinstance(entry, dict) else None
        if not isinstance(info_blob, dict) or model_id in by_name:
            continue
        window = info_blob.get("context_window")
        info = ModelInfo(
            name=str(info_blob.get("model") or model_id),
            provider="",
            ctx_window=int(window) if isinstance(window, int) else None,
        )
        by_alias.setdefault(str(model_id), info)
        by_name.setdefault(info.name, info)

    return Catalogue(by_alias, by_name)


class GrokParser:
    source = "grok"

    def __init__(
        self,
        path: Path,
        cursor: dict | None = None,
        *,
        catalogue: Catalogue | None = None,
        parent_id: str | None = None,
    ):
        self.path = path
        self.dir = path.parent
        self.catalogue = catalogue or Catalogue.empty()
        cursor = cursor or {}
        self.session_id: str = cursor.get("session_id") or self.dir.name
        self.rollout_id: str = cursor.get("rollout_id") or f"grok:{self.session_id}"
        self.parent_id: str | None = parent_id or cursor.get("parent_thread_id")
        self.first_ts: int | None = cursor.get("first_ts")
        self.last_ts: int | None = cursor.get("last_ts")

    # -- parsing -----------------------------------------------------------

    def feed(self, line: bytes, out: ParseOutput) -> None:
        if _MARKER not in line:
            return
        try:
            record = orjson.loads(line)
        except orjson.JSONDecodeError:
            out.flag("parse_error")
            return

        params = record.get("params")
        if not isinstance(params, dict):
            return
        update = params.get("update")
        if not isinstance(update, dict) or update.get("sessionUpdate") != "turn_completed":
            return
        usage = update.get("usage")
        if not isinstance(usage, dict):
            return

        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        # The envelope stamp is whole seconds; the agent's own is milliseconds.
        ts = parse_ts(meta.get("agentTimestampMs")) or parse_ts(record.get("timestamp"))
        if ts is None:
            return
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

        # An event id is unique per emitted line and stable across re-reads, and
        # this log never replays -- so it deduplicates without the ordinal
        # bookkeeping the replaying formats need.
        anchor = meta.get("eventId") or f"{self.session_id}:{update.get('prompt_id')}"

        per_model = usage.get("modelUsage")
        if isinstance(per_model, dict) and per_model:
            entries = list(per_model.items())
        else:
            # No breakdown: attribute the turn to whatever the session was on.
            entries = [(self._session_model(), usage)]

        for name, block in entries:
            if isinstance(block, dict):
                self._emit(str(anchor), name, block, ts, out)

    def _emit(
        self, anchor: str, name: str | None, usage: dict, ts: int, out: ParseOutput
    ) -> None:
        prompt = int(usage.get("inputTokens") or 0)
        cached = int(usage.get("cachedReadTokens") or 0)
        output = int(usage.get("outputTokens") or 0)
        reasoning = int(usage.get("reasoningTokens") or 0)
        total = int(usage.get("totalTokens") or 0)
        calls = int(usage.get("modelCalls") or 1)

        # A turn that reported no usage at all is not a request. It reaches here
        # through the no-modelUsage fallback, where the outer block sometimes
        # carries no token fields either; recorded, it became a phantom request
        # with zero tokens, counted in requests-per-day and costing nothing.
        if not (prompt or cached or output or reasoning or total):
            return

        out.raw_events += 1

        if cached > prompt:
            out.flag("cached_gt_input")
            cached = prompt
        if reasoning > output:
            out.flag("reasoning_gt_output")
        if total and total != prompt + output:
            out.flag("total_mismatch")
        if calls > 1:
            out.flag("multi_call_turn", calls - 1)

        info = self.catalogue.resolve(name)
        model = info.qualified if info else name

        out.requests.append(
            request_row(
                self.source,
                f"{anchor}:{name or 'unknown'}",
                ts=ts,
                session_id=self.session_id,
                rollout_id=self.rollout_id,
                model=model,
                input_tokens=prompt,
                cached_tokens=cached,
                # xAI and the gateways bill cache reads only, and report
                # nothing at all for populating the cache.
                output_tokens=output,
                reasoning_tokens=reasoning,
                ctx_window=info.ctx_window if info else None,
            )
        )

    # -- session metadata --------------------------------------------------

    def summary(self) -> dict:
        """The session's own description of itself, or nothing.

        Read fresh rather than carried in the cursor: it is a few hundred bytes
        beside a log this only opens because it grew, and it is rewritten as the
        session goes on.
        """
        try:
            blob = orjson.loads((self.dir / "summary.json").read_bytes())
        except (OSError, orjson.JSONDecodeError):
            return {}
        return blob if isinstance(blob, dict) else {}

    def _session_model(self) -> str | None:
        alias = self.summary().get("current_model_id")
        info = self.catalogue.resolve(alias)
        return info.name if info else alias

    def session_row(self) -> tuple:
        summary = self.summary()
        info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
        remotes = summary.get("git_remotes")
        # The directory name is the working tree with its slashes escaped, so a
        # session whose summary is missing or half-written still lands in the
        # right project rather than in "unknown".
        cwd = info.get("cwd") or unquote(self.dir.parent.name)
        is_subagent = summary.get("session_kind") == "subagent" or self.parent_id is not None

        return session_row(
            self.rollout_id,
            self.source,
            session_id=self.session_id,
            parent_thread_id=f"grok:{self.parent_id}" if self.parent_id else None,
            path=str(self.path),
            first_ts=self.first_ts or parse_ts(summary.get("created_at")),
            last_ts=self.last_ts or parse_ts(summary.get("updated_at")),
            cwd=cwd,
            git_repo=remotes[0] if isinstance(remotes, list) and remotes else None,
            git_branch=summary.get("head_branch"),
            thread_source="subagent" if is_subagent else "main",
            agent_role=summary.get("agent_name"),
            depth=1 if is_subagent else 0,
            is_subagent=is_subagent,
        )

    def carry_columns(self) -> dict:
        return {
            "rollout_id": self.rollout_id,
            "session_id": self.session_id,
            "parent_thread_id": self.parent_id,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
        }


class GrokSource(JsonlSource):
    name = "grok"
    label = "Grok"

    def __init__(self, root: Path):
        super().__init__(root)
        #: Subagent session id -> the session that spawned it.
        self._parents: dict[str, str] = {}
        self._catalogue = Catalogue.empty()
        self._catalogue_key: tuple | None = None

    @property
    def grok_home(self) -> Path:
        return self.root.parent

    def iter_files(self) -> list[Path]:
        """Every session's update log, and who spawned each subagent.

        The tree records the parent link for free -- a subagent's id appears as
        a directory under its parent -- so the same walk that finds the logs
        builds the map, instead of opening a meta file per child that is mostly
        the subagent's prompt.
        """
        parents: dict[str, str] = {}
        found: list[Path] = []
        for project in _subdirs(self.root):
            for session in _subdirs(project):
                log = session / "updates.jsonl"
                if log.exists():
                    found.append(log)
                for child in _subdirs(session / "subagents"):
                    parents[child.name] = session.name
        self._parents = parents
        self._refresh_catalogue()
        return sorted(found)

    def parse(self, path: Path, cursor: dict | None, start_offset: int) -> ParseOutput:
        parser = GrokParser(
            path,
            cursor,
            catalogue=self._catalogue,
            parent_id=self._parents.get(path.parent.name),
        )
        out = ParseOutput(offset=start_offset)
        out.bytes_read, out.offset, out.error, out.rows = read_new_lines(
            path, start_offset, lambda line: parser.feed(line, out)
        )
        out.session = parser.session_row()
        out.carry = parser.carry_columns()
        return out

    def _refresh_catalogue(self) -> None:
        """Re-read the model config only when it has actually changed."""
        key = tuple(
            _mtime(self.grok_home / name)
            for name in ("config.toml", "models_cache.json")
        )
        if key != self._catalogue_key:
            self._catalogue = read_catalogue(self.grok_home)
            self._catalogue_key = key


def _subdirs(path: Path) -> list[Path]:
    try:
        return [Path(e.path) for e in os.scandir(path) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None
