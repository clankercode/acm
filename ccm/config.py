"""Runtime paths and settings.

Everything is overridable by environment variable so the tool can be pointed at
a fixture corpus in tests without touching the real one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


#: Every client the tool knows how to read, in display order. A source is only
#: scanned if its root exists, so an unused client costs nothing.
ALL_SOURCES = ("codex", "claude", "pi", "opencode", "grok")


def _env_sources(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return ALL_SOURCES
    wanted = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in ALL_SOURCES]
    if unknown:
        raise ValueError(
            f"{name}: unknown source(s) {', '.join(unknown)}; "
            f"known sources are {', '.join(ALL_SOURCES)}"
        )
    return tuple(wanted)


@dataclass(frozen=True)
class Settings:
    """Where to read sessions from, where to keep derived state.

    Each client keeps its history somewhere different and in a different
    format; every corpus is opened read-only and never written to.
    """

    #: Root of the Codex rollout tree.
    sessions_dir: Path
    #: Claude Code transcripts, one directory per project.
    claude_dir: Path
    #: Pi agent sessions, one directory per working directory.
    pi_dir: Path
    #: OpenCode keeps its history in SQLite rather than files.
    opencode_db: Path
    #: Grok sessions, one directory per working tree then one per session.
    grok_dir: Path
    #: Which of :data:`ALL_SOURCES` to scan.
    sources: tuple[str, ...]

    #: SQLite database holding all derived state. Safe to delete.
    db_path: Path
    #: Editable model rate table.
    pricing_path: Path
    #: Cached copy of the models.dev catalogue, used only to cross-check rates.
    reference_path: Path

    #: Coalescing window for filesystem events before a scan is triggered.
    debounce_seconds: float
    #: Interval for the polling fallback when inotify is unavailable.
    poll_seconds: float
    #: Upper bound on SSE broadcast frequency.
    broadcast_hz: float

    host: str
    port: int

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            sessions_dir=_env_path(
                "CCM_SESSIONS_DIR", Path.home() / ".codex-shared" / "sessions"
            ),
            claude_dir=_env_path("CCM_CLAUDE_DIR", Path.home() / ".claude" / "projects"),
            pi_dir=_env_path("CCM_PI_DIR", Path.home() / ".pi" / "agent" / "sessions"),
            opencode_db=_env_path(
                "CCM_OPENCODE_DB",
                Path.home() / ".local" / "share" / "opencode" / "opencode.db",
            ),
            grok_dir=_env_path("CCM_GROK_DIR", Path.home() / ".grok" / "sessions"),
            sources=_env_sources("CCM_SOURCES"),
            db_path=_env_path("CCM_DB", PROJECT_ROOT / "data" / "ccm.sqlite"),
            pricing_path=_env_path("CCM_PRICING", PROJECT_ROOT / "pricing.toml"),
            reference_path=_env_path(
                "CCM_REFERENCE", PROJECT_ROOT / "data" / "models-dev.json"
            ),
            debounce_seconds=_env_float("CCM_DEBOUNCE", 0.25),
            poll_seconds=_env_float("CCM_POLL", 2.0),
            broadcast_hz=_env_float("CCM_BROADCAST_HZ", 4.0),
            # Bound on all interfaces so the dashboard is reachable from other
            # machines on the LAN, not just loopback.
            host=os.environ.get("CCM_HOST", "0.0.0.0"),
            # 8787 is taken by the pre-existing codex-session-monitor on this
            # host, so the default sits clear of it.
            port=int(os.environ.get("CCM_PORT", "8808")),
        )


settings = Settings.from_env()
