"""Runtime paths and settings.

Everything is overridable by environment variable so the tool can be pointed at
a fixture corpus in tests without touching the real one.

Writable state lives inside the checkout when run from one, and under the XDG
directories when run from an installed wheel -- where the checkout's ``data/``
would be somewhere unwritable in ``site-packages``.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

#: A source checkout has the build file one level above the package; an
#: installed copy has ``site-packages`` there instead.
DEV_LAYOUT = (PROJECT_ROOT / "pyproject.toml").is_file()

#: The rate table shipped inside the wheel, copied out on first run.
BUNDLED_PRICING = PACKAGE_ROOT / "pricing.default.toml"


def _xdg(var: str, fallback: str) -> Path:
    raw = os.environ.get(var)
    return (Path(raw).expanduser() if raw else Path.home() / fallback) / "ccm"


#: Derived state, safe to delete.
STATE_DIR = PROJECT_ROOT / "data" if DEV_LAYOUT else _xdg("XDG_STATE_HOME", ".local/state")
#: Things the user edits, directly or through the dashboard.
CONFIG_DIR = PROJECT_ROOT if DEV_LAYOUT else _xdg("XDG_CONFIG_HOME", ".config")
#: Downloaded copies of things that live upstream.
CACHE_DIR = PROJECT_ROOT / "data" if DEV_LAYOUT else _xdg("XDG_CACHE_HOME", ".cache")


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_optional_path(name: str, default: Path | None) -> Path | None:
    """Like :func:`_env_path`, but an empty value means "nowhere", not "default"."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return Path(raw).expanduser() if raw.strip() else None


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


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

    #: Git checkout the dashboard's Update button builds from, or None when there
    #: is nothing to build: an installed copy has no idea where it came from, so
    #: this is configuration rather than a guess. A checkout run knows its own.
    #: Defaulted so that a test, or any other caller building settings by hand,
    #: gets a monitor that cannot update itself rather than one that might.
    checkout_path: Path | None = None
    #: Whether an update may be requested from another machine. Off, because the
    #: dashboard is unauthenticated and this endpoint runs a shell script.
    update_from_lan: bool = False

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
            db_path=_env_path("CCM_DB", STATE_DIR / "ccm.sqlite"),
            pricing_path=_env_path("CCM_PRICING", CONFIG_DIR / "pricing.toml"),
            reference_path=_env_path("CCM_REFERENCE", CACHE_DIR / "models-dev.json"),
            debounce_seconds=_env_float("CCM_DEBOUNCE", 0.25),
            poll_seconds=_env_float("CCM_POLL", 2.0),
            broadcast_hz=_env_float("CCM_BROADCAST_HZ", 4.0),
            # Bound on all interfaces so the dashboard is reachable from other
            # machines on the LAN, not just loopback.
            host=os.environ.get("CCM_HOST", "0.0.0.0"),
            # 8787 is taken by the pre-existing codex-session-monitor on this
            # host, so the default sits clear of it.
            port=int(os.environ.get("CCM_PORT", "8808")),
            checkout_path=_env_optional_path(
                "CCM_CHECKOUT", PROJECT_ROOT if DEV_LAYOUT else None
            ),
            update_from_lan=_env_flag("CCM_UPDATE_FROM_LAN"),
        )


settings = Settings.from_env()


def bootstrap(cfg: Settings) -> None:
    """Put a rate table where an installed copy expects to find one.

    Called from the CLI rather than at import so that constructing ``Settings``
    stays free of side effects, which the tests rely on.
    """
    if cfg.pricing_path.exists() or not BUNDLED_PRICING.is_file():
        return
    cfg.pricing_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUNDLED_PRICING, cfg.pricing_path)
