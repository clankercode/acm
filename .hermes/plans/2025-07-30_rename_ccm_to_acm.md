# Rename ccm → acm (Agent Cache Monitor) Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Rename the entire project — Python package, distribution name, console script, env vars, XDG directories, systemd unit, logger names, web localStorage keys, and all docs — from `ccm` to `acm`, with a one-time migration that moves existing users' state from the old `ccm` paths to the new `acm` paths.

**Architecture:** A mechanical rename across ~30 files, plus a migration shim in `config.py` that detects legacy `~/.local/state/ccm`, `~/.config/ccm`, `~/.cache/ccm` directories and renames them to their `acm` equivalents on first run of the new version. The portable bundle format string gains backward compatibility (accepts both `ccm-export` and `acm-export` on import, writes `acm-export` on export).

**Tech Stack:** Python 3.12+, uv, hatchling, systemd user units, React/TypeScript dashboard (pnpm/vite), pytest.

---

## Current State

- Git repo already renamed on GitHub: `clankercode/ccm` → `clankercode/acm` (done, remote updated).
- Package import dir: `ccm/` → must become `acm/`.
- Distribution name in pyproject.toml: `codex-cache-monitor` → `agent-cache-monitor`.
- Console script: `ccm` → `acm`.
- Env vars: `CCM_*` → `ACM_*` (15 distinct vars).
- XDG dirs: `ccm` → `acm` (state, config, cache).
- systemd unit: `ccm.service` → `acm.service`.
- No existing install on this machine (`uv tool list` shows no ccm; no `~/.local/state/ccm` etc.).

## Scope of "ccm" references (from rg)

**Must rename (functional):**
- `ccm/` directory (17 .py files) → `acm/`
- `pyproject.toml`: dist name, script entry, hatch packages/include paths, artifacts
- `ccm/config.py`: `_xdg()` suffix `"ccm"`, all `CCM_*` env var names, `cursor_agent_dir` default path
- `ccm/portable.py`: `FORMAT = "ccm-export"` (see Task 7 for compat)
- `ccm/server.py`: `DIST_NAME`, logger name, export filename prefix
- `ccm/selfupdate.py`: logger name, error message strings, transient unit name `ccm-self-update-*`
- `ccm/__main__.py`: thread name `ccm-announce`, argparse `prog="ccm"`
- `ccm/engine.py`, `ccm/watcher.py`: thread names `ccm-scan`, `ccm-poll`
- `justfile`: `package`, `unit`, `unit_dir` vars; all `ccm` recipe names and `uv run ccm` calls; `build-web` paths `ccm/_web`
- `packaging/ccm.service` → `packaging/acm.service` (rename file + update contents)
- `packaging/ccm.env.example` → `packaging/acm.env.example` (rename file + update contents)
- `packaging/smoke-test.sh`: `$root/venv/bin/ccm`, path checks, env vars
- `packaging/self-update.sh`: `unit="ccm.service"`
- `packaging/release-notes.sh`: repo default, install command, path references
- `.github/workflows/release.yml`, `.github/workflows/ci.yml`: `ccm/_web` paths, release title
- `web/package.json`: `"name": "ccm-web"`
- `web/src/lib/live.ts`: localStorage keys `ccm-output`, `ccm-theme`
- `web/src/App.tsx`: `syncKey="ccm"` (internal chart sync, not user-visible — but rename for consistency)
- `web/src/lib/types.ts`: comment reference
- All `tests/*.py`: `from ccm...` imports → `from acm...`, `ccm.sqlite` fixture names, env var names in Settings construction, assertion strings
- `tests/conftest.py`: imports, `store` fixture db name
- `tests/test_selfupdate.py`: `fake_checkout` pyproject name, assertion strings

**Rename in docs (cosmetic):**
- `README.md` (67 occurrences)
- `CLIENT_CANDIDATES.md` (15 occurrences)
- `docs/long-context-thresholds.md` (14 occurrences)
- `pricing.toml` (1 comment)
- `RELEASE.md` (check for references)

**Do NOT rename:**
- `~/.codex-shared/sessions` — that's Codex's own directory, not ours.
- Any client corpus paths (`.claude`, `.pi`, `.grok`, etc.) — those belong to other tools.
- The `cursor-logs` subdirectory name under cache — only the parent `ccm` → `acm`.

---

## Migration Strategy (the critical part)

Users who installed `ccm` have state at:
- `~/.local/state/ccm/ccm.sqlite` (derived DB — safe to delete, but rescanning is expensive)
- `~/.config/ccm/pricing.toml` (user-edited rate table — MUST preserve)
- `~/.config/ccm/env` (systemd EnvironmentFile — MUST preserve)
- `~/.cache/ccm/models-dev.json` (downloaded cache — safe to re-download)
- `~/.cache/ccm/cursor-logs/` (mirrored cursor-agent logs — SHOULD preserve)
- `~/.local/bin/ccm` (old binary — should be uninstalled)
- `~/.config/systemd/user/ccm.service` (old unit — should be removed)

**Migration approach:** A `migrate_legacy_ccm_paths()` function in `config.py`, called from `bootstrap()` before anything else. For each of the three XDG roots (state, config, cache):
1. If the new `acm` dir does NOT exist but the old `ccm` dir DOES: `os.rename(old, new)`.
2. If both exist (user ran both somehow): do nothing — don't clobber, log a warning.

The DB filename stays `ccm.sqlite` inside the dir? No — rename to `acm.sqlite` for full consistency. The migration renames the file too. Actually: the DB filename default is `STATE_DIR / "ccm.sqlite"`. We change the default to `STATE_DIR / "acm.sqlite"`. During migration, if we rename the whole state dir, the file inside is still called `ccm.sqlite`. So either:
- (a) Keep the DB filename as `ccm.sqlite` (it's internal, nobody references it by name), or
- (b) Rename it during migration too.

Option (b) is cleaner. The migration function, after renaming the state directory, also renames `acm/ccm.sqlite` → `acm/acm.sqlite` if present.

**Env var migration:** Old `CCM_*` env vars in the user's shell profile or the migrated `env` file won't be read by the new code. Two options:
- (a) Clean break: document in release notes that env vars are now `ACM_*`.
- (b) Backward-compat fallback: in `from_env()`, check `ACM_*` first, fall back to `CCM_*` if unset.

Option (b) is low-cost and prevents silent breakage. The plan includes it as Task 9. The `env` file itself migrates with the config dir rename, but its contents still say `CCM_*` — so the fallback is what makes a migrated `env` file work until the user edits it.

**Old binary/unit cleanup:** The migration prints a notice if `~/.local/bin/ccm` still exists, telling the user to run `uv tool uninstall codex-cache-monitor`. It does NOT auto-uninstall (too surprising). The old `ccm.service` is left in place — the user must `systemctl --user disable --now ccm.service` and remove it manually (documented).

---

### Task 1: Establish baseline — install current ccm

**Objective:** Install the existing `ccm` tool and create real state on disk, so the migration can be tested against it.

**Files:** None (read-only setup).

**Step 1: Build and install the current version**

```bash
cd /home/xertrov/src/acm
just build          # builds web + wheel
just install        # uv tool install -> ~/.local/bin/ccm
```

**Step 2: Run it once to create state**

```bash
ccm scan -q         # creates ~/.local/state/ccm/ccm.sqlite, ~/.config/ccm/pricing.toml
ccm serve --no-watch &  # start briefly, confirm it works, then kill
sleep 2 && kill %1
```

**Step 3: Verify baseline state exists**

```bash
ls ~/.local/state/ccm/    # ccm.sqlite
ls ~/.config/ccm/         # pricing.toml
ls ~/.cache/ccm/          # (may be empty if no models.dev fetch)
which ccm                 # ~/.local/bin/ccm
```

Expected: all paths exist, `ccm` is on PATH.

---

### Task 2: Rename the Python package directory

**Objective:** `ccm/` → `acm/` — the import package.

**Files:**
- Rename: `ccm/` → `acm/` (git mv)
- Modify: all internal imports within the package (relative imports like `from .config` stay valid; absolute imports like `from ccm.x` must change)

**Step 1: Rename the directory**

```bash
cd /home/xertrov/src/acm
git mv ccm acm
```

**Step 2: Find and fix absolute imports inside the package**

```bash
rg -n 'from ccm\.|import ccm' acm/
```

Expected: any `from ccm.xxx import` → `from acm.xxx import`. Relative imports (`from .config`) are unchanged.

**Step 3: Verify no absolute `ccm` imports remain**

```bash
rg -n 'from ccm\.|import ccm' acm/   # should be empty
```

---

### Task 3: Update pyproject.toml

**Objective:** Rename distribution, script entry, and build paths.

**Files:**
- Modify: `pyproject.toml`

**Changes:**
- `name = "codex-cache-monitor"` → `name = "agent-cache-monitor"`
- `[project.scripts]` `ccm = "ccm.__main__:main"` → `acm = "acm.__main__:main"`
- `artifacts = ["ccm/_web/**"]` → `["acm/_web/**"]`
- `packages = ["ccm"]` → `["acm"]`
- `force-include`: `"pricing.toml" = "ccm/pricing.default.toml"` → `"acm/pricing.default.toml"`
- `include = ["ccm", ...]` → `["acm", ...]`

**Step: Verify**

```bash
uv lock          # regenerate lockfile with new dist name
grep agent-cache-monitor uv.lock   # confirm it's there
```

---

### Task 4: Update config.py — XDG dirs, env vars, migration

**Objective:** Rename the `_xdg` suffix, all `CCM_*` env vars to `ACM_*`, the cursor-agent default path, the DB filename, and add the migration function.

**Files:**
- Modify: `acm/config.py`

**Changes:**

1. `_xdg()` line 31: `... / "ccm"` → `... / "acm"`

2. All env var names in `from_env()`:
   - `CCM_SESSIONS_DIR` → `ACM_SESSIONS_DIR`
   - `CCM_CLAUDE_DIR` → `ACM_CLAUDE_DIR`
   - `CCM_PI_DIR` → `ACM_PI_DIR`
   - `CCM_OPENCODE_DB` → `ACM_OPENCODE_DB`
   - `CCM_GROK_DIR` → `ACM_GROK_DIR`
   - `CCM_KIMI_CODE_DIR` → `ACM_KIMI_CODE_DIR`
   - `CCM_KIMI_DIR` → `ACM_KIMI_DIR`
   - `CCM_HERMES_DB` → `ACM_HERMES_DB`
   - `CCM_COPILOT_DB` → `ACM_COPILOT_DB`
   - `CCM_GEMINI_DIR` → `ACM_GEMINI_DIR`
   - `CCM_CURSOR_AGENT_DIR` → `ACM_CURSOR_AGENT_DIR`
   - `CCM_CURSOR_AGENT_CAPTURE_INTERVAL` → `ACM_CURSOR_AGENT_CAPTURE_INTERVAL`
   - `CCM_SOURCES` → `ACM_SOURCES`
   - `CCM_DB` → `ACM_DB`
   - `CCM_PRICING` → `ACM_PRICING`
   - `CCM_REFERENCE` → `ACM_REFERENCE`
   - `CCM_DEBOUNCE` → `ACM_DEBOUNCE`
   - `CCM_POLL` → `ACM_POLL`
   - `CCM_BROADCAST_HZ` → `ACM_BROADCAST_HZ`
   - `CCM_HOST` → `ACM_HOST`
   - `CCM_PORT` → `ACM_PORT`
   - `CCM_CHECKOUT` → `ACM_CHECKOUT`
   - `CCM_UPDATE_FROM_LAN` → `ACM_UPDATE_FROM_LAN`

3. `cursor_agent_dir` default: `Path.home() / ".cache" / "ccm" / "cursor-logs"` → `... / "acm" / "cursor-logs"`

4. `db_path` default: `STATE_DIR / "ccm.sqlite"` → `STATE_DIR / "acm.sqlite"`

5. Add `migrate_legacy_ccm_paths()` function (full code below) and call it at the top of `bootstrap()`.

```python
import logging

_log = logging.getLogger("acm.config")

_LEGACY_DIR = "ccm"
_CURRENT_DIR = "acm"
_LEGACY_DB_NAME = "ccm.sqlite"
_CURRENT_DB_NAME = "acm.sqlite"


def migrate_legacy_ccm_paths() -> None:
    """One-time rename of ccm-era state/config/cache dirs to acm.

    Runs before anything reads or writes those locations. If the new ``acm``
    directory already exists, the old one is left alone -- the user has run
    both versions and we will not guess which state wins.
    """
    for var, fallback in (
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        raw = os.environ.get(var)
        base = Path(raw).expanduser() if raw else Path.home() / fallback
        old = base / _LEGACY_DIR
        new = base / _CURRENT_DIR
        if not old.is_dir() or new.exists():
            continue
        try:
            old.rename(new)
            _log.info("migrated %s -> %s", old, new)
        except OSError as exc:
            _log.warning("could not migrate %s -> %s: %s", old, new, exc)

    # Inside the state dir, the DB file itself was renamed too.
    raw = os.environ.get("XDG_STATE_HOME")
    state = Path(raw).expanduser() if raw else Path.home() / ".local/state"
    old_db = state / _CURRENT_DIR / _LEGACY_DB_NAME
    new_db = state / _CURRENT_DIR / _CURRENT_DB_NAME
    if old_db.is_file() and not new_db.exists():
        try:
            old_db.rename(new_db)
            _log.info("migrated db %s -> %s", old_db, new_db)
        except OSError as exc:
            _log.warning("could not migrate db %s: %s", old_db, exc)
```

Then in `bootstrap()`:

```python
def bootstrap(cfg: Settings) -> None:
    migrate_legacy_ccm_paths()
    # ... existing pricing copy logic ...
```

**Step: Verify** — run a quick import check:

```bash
uv run python -c "from acm.config import Settings; print(Settings.from_env().db_path)"
# should show .../acm/acm.sqlite
```

---

### Task 5: Update env var backward-compat fallback

**Objective:** Read `CCM_*` as a fallback when `ACM_*` is unset, so a migrated `env` file or shell profile keeps working silently.

**Files:**
- Modify: `acm/config.py`

**Approach:** Add a helper that checks `ACM_*` first, then `CCM_*`:

```python
def _env(name: str) -> str | None:
    """ACM_ first, CCM_ as a legacy fallback (one rename behind)."""
    val = os.environ.get(name)
    if val is not None:
        return val
    legacy = name.replace("ACM_", "CCM_", 1)
    if legacy != name:
        return os.environ.get(legacy)
    return None
```

Then replace `_env_path`, `_env_optional_path`, `_env_float`, `_env_sources`, `_env_flag` to use this helper internally instead of `os.environ.get(name)` directly. (They already call `os.environ.get(name)` — swap for `_env(name)`.)

Add a one-time deprecation log: if any `CCM_*` var is actually being used (fallback hit), log a warning once telling the user to rename to `ACM_*`.

**Step: Verify**

```bash
uv run python -c "
import os; os.environ.pop('ACM_PORT', None); os.environ['CCM_PORT'] = '9999'
from acm.config import Settings; print(Settings.from_env().port)
"  # should print 9999
```

---

### Task 6: Rename logger names and thread names

**Objective:** `ccm.*` loggers → `acm.*`; `ccm-*` thread names → `acm-*`.

**Files:**
- Modify: `acm/server.py` line 39: `logging.getLogger("ccm.server")` → `"acm.server"`
- Modify: `acm/selfupdate.py` line 36: `"ccm.selfupdate"` → `"acm.selfupdate"`
- Modify: `acm/engine.py` line 37: `"ccm.engine"` → `"acm.engine"`; line 100 thread name `"ccm-scan"` → `"acm-scan"`
- Modify: `acm/watcher.py` line 19: `"ccm.watcher"` → `"acm.watcher"`; line 50 thread name `"ccm-poll"` → `"acm-poll"`
- Modify: `acm/__main__.py` line 249: `"ccm-announce"` → `"acm-announce"`; line 258 `prog="ccm"` → `prog="acm"`

**Step: Verify**

```bash
rg -n '"ccm\.|ccm-' acm/   # should be empty
```

---

### Task 7: Update portable.py bundle format with backward compat

**Objective:** Export format string becomes `acm-export`; import accepts both `ccm-export` and `acm-export`.

**Files:**
- Modify: `acm/portable.py`

**Changes:**
- Line 34: `FORMAT = "acm-export"`
- Add `_LEGACY_FORMATS = frozenset({"ccm-export"})` 
- In the format check (around line 287): accept `FORMAT` OR any legacy format:

```python
if payload.get("format") not in (FORMAT, *_LEGACY_FORMATS):
    raise BundleError(...)
```

**Step: Write test**

```python
# in tests/test_portable.py (or wherever portable tests live)
def test_imports_legacy_ccm_export_format(tmp_path):
    # A bundle with "format": "ccm-export" should still import.
    ...
```

**Step: Verify**

```bash
uv run pytest tests/ -k portable -v
```

---

### Task 8: Update server.py DIST_NAME and export filename

**Objective:** Distribution name lookup and export download filename.

**Files:**
- Modify: `acm/server.py`
  - Line 28: `DIST_NAME = "agent-cache-monitor"` (was `"codex-cache-monitor"`)
  - Line 368: `filename="ccm-{name}.json"` → `filename="acm-{name}.json"`
  - Line 39 comment: update `version("ccm")` reference in comment → `version("agent-cache-monitor")`

---

### Task 9: Update selfupdate.py message strings and unit name

**Objective:** User-facing error messages and the transient systemd unit name.

**Files:**
- Modify: `acm/selfupdate.py`
  - Line 141-142: `"Set CCM_CHECKOUT"` → `"Set ACM_CHECKOUT"`; `"~/.config/ccm/env"` → `"~/.config/acm/env"`
  - Line 151: `"does not look like a ccm checkout"` → `"does not look like an acm checkout"`
  - Line 231: `"Set CCM_UPDATE_FROM_LAN=1"` → `"Set ACM_UPDATE_FROM_LAN=1"`
  - Line 358: `f"--unit=ccm-self-update-{int(time.time())}"` → `f"--unit=acm-self-update-{int(time.time())}"`
  - Line 18 docstring: `ccm.service` → `acm.service`
  - Line 351 comment: `ccm.service` → `acm.service`

---

### Task 10: Update justfile

**Objective:** All recipe vars and commands.

**Files:**
- Modify: `justfile`

**Changes:**
- Line 5 comment: `` `ccm` `` → `` `acm` ``
- Line 12: `package := "agent-cache-monitor"` (was `"codex-cache-monitor"`)
- Line 14: `unit := "acm.service"` (was `"ccm.service"`)
- Line 38-40: `ccm/_web` → `acm/_web` (three occurrences)
- Line 58: `ccm/_web/index.html ccm/pricing.default.toml ccm/server.py` → `acm/_web/index.html acm/pricing.default.toml acm/server.py`
- Line 69-71: recipe name `ccm` → `acm`, `uv run ccm` → `uv run acm`
- Line 75: `uv run ccm serve` → `uv run acm serve`
- Line 79: `uv run ccm scan` → `uv run acm scan`
- Line 85: `uv run ccm serve` → `uv run acm serve`
- Line 109 comment: `` `ccm` `` → `` `acm` ``
- Line 112: `command -v ccm` and `'~/.local/bin/ccm'` → `acm` / `'~/.local/bin/acm'`
- Line 176: `uv run ccm reset` → `uv run acm reset`
- Line 180: `ccm/_web` → `acm/_web`

---

### Task 11: Rename and update packaging files

**Objective:** systemd unit, env example, smoke test, self-update script, release notes.

**Files:**
- Rename: `packaging/ccm.service` → `packaging/acm.service` (git mv)
- Rename: `packaging/ccm.env.example` → `packaging/acm.env.example` (git mv)
- Modify: `packaging/acm.service`
- Modify: `packaging/acm.env.example`
- Modify: `packaging/smoke-test.sh`
- Modify: `packaging/self-update.sh`
- Modify: `packaging/release-notes.sh`

**Changes in `acm.service`:**
- Line 10-11: `packaging/ccm.service` → `packaging/acm.service`; `ccm.service` → `acm.service`
- Line 15: `Documentation=https://github.com/clankercode/acm` (update repo URL — was xertrov/codex-cache-monitor)
- Line 21: `%h/src/codex-cache-monitor/.venv/bin/ccm` → `%h/src/acm/.venv/bin/acm`
- Line 22: `ExecStart=%h/.local/bin/ccm serve` → `ExecStart=%h/.local/bin/acm serve`
- Line 23: `SyslogIdentifier=ccm` → `acm`
- Line 25-27: `CCM_*` → `ACM_*` in comments; `EnvironmentFile=-%E/ccm/env` → `-%E/acm/env`
- Line 34-42: all `ccm` → `acm` in comments and `StateDirectory`/`CacheDirectory`/`ConfigurationDirectory`

**Changes in `acm.env.example`:**
- Line 1: `~/.config/ccm/env` → `~/.config/acm/env`
- All `CCM_*` → `ACM_*`
- All `~/.local/state/ccm`, `~/.cache/ccm`, `~/.config/ccm` → `acm`
- Line 30: `CCM_CHECKOUT=/home/you/src/ccm` → `ACM_CHECKOUT=/home/you/src/acm`

**Changes in `smoke-test.sh`:**
- Line 4 comment: `codex_cache_monitor` → `agent_cache_monitor`
- Line 18: `ccm="$root/venv/bin/ccm"` → `acm="$root/venv/bin/acm"`
- Line 20-38: all `$ccm` → `$acm`, all `CCM_*` → `ACM_*`
- Line 34-35: `~/.config/ccm/`, `~/.local/state/ccm/ccm.sqlite` → `acm`

**Changes in `self-update.sh`:**
- Line 16: `unit="ccm.service"` → `unit="acm.service"`

**Changes in `release-notes.sh`:**
- Line 19: `repo="${GITHUB_REPOSITORY:-clankercode/acm}"` (was `xertrov/codex-cache-monitor`)
- Line 31: `codex_cache_monitor-$version` → `agent_cache_monitor-$version`
- Line 32: `ccm serve` → `acm serve`
- Line 37-38: `~/.local/state/ccm`, `~/.config/ccm/pricing.toml` → `acm`

---

### Task 12: Update GitHub Actions workflows

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `.github/workflows/ci.yml`

**Changes:**
- `release.yml` line 66-67: `ccm/_web` → `acm/_web`
- `release.yml` line 87: `"ccm ${{ ... }}"` → `"acm ${{ ... }}"`
- `ci.yml` line 91-92: `ccm/_web` → `acm/_web`

---

### Task 13: Update web frontend

**Objective:** package.json name, localStorage keys, syncKey, comments.

**Files:**
- Modify: `web/package.json` line 2: `"ccm-web"` → `"acm-web"`
- Modify: `web/src/lib/live.ts`:
  - Line 194: `'ccm-output'` → `'acm-output'`
  - Line 197: `'ccm-output'` → `'acm-output'`
  - Line 210: `'ccm-theme'` → `'acm-theme'`
  - Line 219: `'ccm-theme'` → `'acm-theme'`
- Modify: `web/src/App.tsx`: all `syncKey="ccm"` → `syncKey="acm"` (~14 occurrences)
- Modify: `web/src/lib/types.ts` line 254: `ccm serve` → `acm serve` (comment)

**Note on localStorage migration:** localStorage keys changing means users lose their theme/output toggle settings. This is cosmetic and not worth a migration shim — they'll re-toggle once. (If we wanted to, we could add a one-time `localStorage.getItem('ccm-theme')` fallback, but the settings are trivial.)

**Step: Verify** web still builds:

```bash
cd web && pnpm build
```

---

### Task 14: Update all tests

**Objective:** Fix imports, env var names, fixture names, assertion strings.

**Files:**
- Modify: `tests/conftest.py`: `from ccm.pricing` → `from acm.pricing`, `from ccm.store` → `from acm.store`; `store` fixture `tmp_path / "ccm.sqlite"` → `"acm.sqlite"`
- Modify: `tests/test_sources.py`: all `from ccm...` → `from acm...`; any `CCM_*` env vars → `ACM_*`
- Modify: `tests/test_server.py`: imports; `CCM_*` → `ACM_*`; `db_path=tmp_path / "ccm.sqlite"` → `"acm.sqlite"`
- Modify: `tests/test_selfupdate.py`:
  - imports
  - `fake_checkout` line 28: `name = "codex-cache-monitor"` → `name = "agent-cache-monitor"`
  - Line 63: `"CCM_CHECKOUT"` → `"ACM_CHECKOUT"`
  - Line 66-72: `"does not look like a ccm checkout"` → `"does not look like an acm checkout"`
  - Line 253: `src/ccm` → `src/acm` (in log fixture text)
  - Line 302: `"ccm.selfupdate.subprocess.Popen"` → `"acm.selfupdate.subprocess.Popen"`
  - Line 328: imports
- Modify: `tests/test_scanner.py`: imports
- Modify: `tests/test_pricing.py`: imports
- Modify: `tests/test_aggregate.py`: imports
- Modify: `tests/test_portable.py`: imports, FORMAT assertions
- Modify: `tests/test_pause.py`: imports
- Modify: `tests/test_cache_decay.py`: imports
- Modify: `tests/test_corpus.py`: imports
- Modify: `tests/test_corpus_clients.py`: imports
- Modify: `tests/test_modelsdev.py`: imports

**Step: Find all remaining references**

```bash
rg -n 'from ccm\.|import ccm|CCM_|ccm\.sqlite|ccm checkout|ccm\.selfupdate' tests/
```

Fix every hit.

**Step: Verify**

```bash
just test
```

Expected: all tests pass.

---

### Task 15: Update documentation

**Objective:** All user-facing docs.

**Files:**
- Modify: `README.md` — replace all `ccm` with `acm`, `CCM_` with `ACM_`, `codex-cache-monitor` with `agent-cache-monitor`, `ccm.service` with `acm.service`, path references, install commands, repo URL. Add a migration note for existing users.
- Modify: `CLIENT_CANDIDATES.md` — replace `ccm` references.
- Modify: `docs/long-context-thresholds.md` — replace `ccm` references.
- Modify: `pricing.toml` line 40 comment — `ccm` → `acm`.
- Modify: `RELEASE.md` — check and update any `ccm`/`codex-cache-monitor` references.

**Add a migration section to README.md** (near the install section):

```markdown
## Upgrading from ccm

If you installed the previous `ccm` tool, the new `acm` automatically migrates
your state on first run: `~/.local/state/ccm`, `~/.config/ccm`, and
`~/.cache/ccm` are renamed to their `acm` equivalents. Your database, pricing
table, and env file are preserved.

To clean up the old install:

    uv tool uninstall codex-cache-monitor
    systemctl --user disable --now ccm.service   # if you used the service
    rm ~/.config/systemd/user/ccm.service

Environment variables renamed from `CCM_*` to `ACM_*`; the old names still work
as a fallback for now.
```

---

### Task 16: Build, install, and test the migration end-to-end

**Objective:** Prove the whole rename works and the migration picks up the baseline state from Task 1.

**Step 1: Uninstall old tool (but leave state dirs in place)**

```bash
uv tool uninstall codex-cache-monitor
# Do NOT delete ~/.local/state/ccm etc. -- the migration needs them.
```

**Step 2: Build and install the new version**

```bash
just build
just install
which acm           # ~/.local/bin/acm
```

**Step 3: Run acm once — triggers migration**

```bash
acm scan -q
```

**Step 4: Verify migration happened**

```bash
# Old dirs should be gone (renamed):
test ! -d ~/.local/state/ccm && echo "state migrated"
test ! -d ~/.config/ccm && echo "config migrated"

# New dirs should exist with the old data:
ls ~/.local/state/acm/acm.sqlite    # the migrated DB
ls ~/.config/acm/pricing.toml       # the migrated rate table
```

**Step 5: Verify env var fallback works**

```bash
CCM_PORT=9999 acm serve --no-watch &
sleep 2
curl -sf http://127.0.0.1:9999/api/totals >/dev/null && echo "CCM_ fallback works"
kill %1
```

**Step 6: Run the full test suite**

```bash
just check    # test + typecheck + verify-wheel
```

Expected: all green.

**Step 7: Verify the wheel smoke test**

```bash
just verify-wheel
```

This runs `packaging/smoke-test.sh` against a throwaway HOME, proving the wheel is self-contained and all internal paths are correct.

---

### Task 17: Final sweep — grep for stragglers

**Objective:** Catch any `ccm`/`CCM` reference that was missed.

```bash
rg -in 'ccm' --type-not binary \
  -g '!uv.lock' -g '!.git/**' -g '!web/node_modules/**' -g '!web/dist/**' \
  -g '!acm/_web/**' -g '!.hermes/**'
```

Review every hit. Legitimate hits at this point:
- `ccm-export` in `portable.py` `_LEGACY_FORMATS` (backward compat — intentional).
- `ccm` in `config.py` migration function (intentional — it reads old paths).
- `ccm` in migration-related test assertions.
- Historical references in git commit messages (not in working tree).

Anything else is a missed rename — fix it.

---

## Risks and Tradeoffs

1. **Breaking existing users' env vars.** Mitigated by the `CCM_*` → `ACM_*` fallback (Task 5). The fallback logs a deprecation warning.

2. **localStorage keys lost.** Theme/output toggle settings reset. Acceptable — trivial to re-toggle, not worth a migration shim.

3. **`syncKey` change breaks cross-chart sync if a user has two tabs open during upgrade.** Vanishingly unlikely and self-corrects on reload.

4. **Bundle format compat.** Old `ccm-export` bundles still import (Task 7). New exports use `acm-export`. No data loss.

5. **systemd unit name change.** Users must reinstall the service under the new name. Documented in README migration section and release notes. The old `ccm.service` is left in place (not auto-removed) to avoid surprising the user.

6. **Directory rename races.** `os.rename` is atomic on the same filesystem (all these are under `$HOME`). If new dir already exists, we skip — no clobbering.

7. **The `env` file migrates but its contents still say `CCM_*`.** The fallback (Task 5) handles this. The user can update it at their leisure.

## Open Questions

- Should the DB filename change from `ccm.sqlite` to `acm.sqlite`? **Decision: yes** — full consistency, and the migration renames it. (The DB is internal; no user references it by name.)
- Should we keep `FORMAT = "ccm-export"` for exports too (not just imports)? **Decision: no** — export the new name; only import accepts the old. This signals the rename to anyone inspecting a bundle.

## Verification Summary

| Check | Command |
|---|---|
| No `ccm` imports remain | `rg 'from ccm\.|import ccm' acm/ tests/` → empty |
| No `CCM_` in source (except migration/fallback) | `rg 'CCM_' acm/` → only migration + fallback |
| Tests pass | `just test` |
| Web builds | `cd web && pnpm build` |
| Wheel is self-contained | `just verify-wheel` |
| Migration works | Task 16 steps 3-4 |
| Env fallback works | Task 16 step 5 |
| No stragglers | Task 17 grep |
