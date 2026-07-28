"""Readers for each coding client's session history.

Every client records the same underlying thing -- one API request, its token
counts, and which model served it -- but agrees on almost nothing else: the
storage format, the dedup hazard, even whether "input tokens" includes the
cached ones. A source is the thin layer that turns one client's history into
the canonical rows in :mod:`ccm.store`, and it is where all of that
disagreement is confined.

The driver in :mod:`ccm.scanner` treats every source identically:
:meth:`~ccm.sources.base.Source.plan` lists the units of work with their
pending byte counts, and :meth:`~ccm.sources.base.Source.ingest` consumes one.
"""

from __future__ import annotations

from ..config import Settings
from .base import Source, Unit, UnitResult, project_label
from .claude import ClaudeSource
from .codex import CodexSource
from .copilot import CopilotSource
from .cursor_agent import CursorAgentSource
from .gemini import GeminiSource
from .grok import GrokSource
from .hermes import HermesSource
from .kimi_cli import KimiCliSource
from .kimi_code import KimiCodeSource
from .opencode import OpenCodeSource
from .pi import PiSource

__all__ = [
    "ClaudeSource",
    "CodexSource",
    "CopilotSource",
    "CursorAgentSource",
    "GeminiSource",
    "GrokSource",
    "HermesSource",
    "KimiCliSource",
    "KimiCodeSource",
    "OpenCodeSource",
    "PiSource",
    "Source",
    "Unit",
    "UnitResult",
    "build_sources",
    "project_label",
]

_BUILDERS = {
    "codex": lambda s: CodexSource(s.sessions_dir),
    "claude": lambda s: ClaudeSource(s.claude_dir),
    "pi": lambda s: PiSource(s.pi_dir),
    "opencode": lambda s: OpenCodeSource(s.opencode_db),
    "grok": lambda s: GrokSource(s.grok_dir),
    "kimi_code": lambda s: KimiCodeSource(s.kimi_code_dir),
    "kimi_cli": lambda s: KimiCliSource(s.kimi_dir),
    "hermes": lambda s: HermesSource(s.hermes_db),
    "copilot": lambda s: CopilotSource(s.copilot_db),
    "gemini": lambda s: GeminiSource(s.gemini_dir),
    "cursor_agent": lambda s: CursorAgentSource(s.cursor_agent_dir),
}


def build_sources(settings: Settings) -> list[Source]:
    """Instantiate the enabled sources whose corpus actually exists.

    Skipping absent roots rather than erroring is what lets the same build run
    on a machine with one client installed and on one with four.
    """
    out: list[Source] = []
    for name in settings.sources:
        builder = _BUILDERS.get(name)
        if builder is None:
            continue
        source = builder(settings)
        if source.available():
            out.append(source)
    return out
