# Agent Cache Monitor -- development, packaging and service lifecycle.
#
#   just setup      first time on a new machine
#   just serve      run it from this checkout
#   just install    put `acm` on PATH with the dashboard baked in
#   just enable     run it in the background under systemd, from boot
#   just update     pull, rebuild, reinstall, restart

set shell := ["bash", "-euo", "pipefail", "-c"]
set positional-arguments

package  := "agent-cache-monitor"
version  := `sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1`
unit     := "acm.service"
unit_dir := env("XDG_CONFIG_HOME", home_directory() / ".config") / "systemd/user"

# List the recipes.
default:
    @just --list --unsorted

# --- setup ------------------------------------------------------------------

# Install Python and web dependencies.
setup:
    uv sync --extra dev
    cd web && pnpm install --frozen-lockfile

# Refresh the lockfiles to the newest allowed versions.
upgrade:
    uv lock --upgrade
    cd web && pnpm update --latest

# --- build ------------------------------------------------------------------

# Build the dashboard and stage a copy inside the package.
build-web:
    cd web && pnpm build
    rm -rf acm/_web
    cp -r web/dist acm/_web
    @echo "staged $(find acm/_web -type f | wc -l) files in acm/_web"

# Build the wheel and sdist into dist/, dashboard included.
build: build-web
    rm -rf dist
    uv build
    @ls -1sh dist

# Fail unless the built wheel can actually run on its own.
verify-wheel: build
    #!/usr/bin/env bash
    set -euo pipefail
    wheel=$(ls dist/*.whl)
    # Listed once into a variable rather than piped per member: `grep -q` exits at
    # the first match and closes the pipe under it, so `unzip` takes a SIGPIPE and
    # `pipefail` reports the member missing -- a race that failed on whichever
    # member happened to lose it.
    listing=$(unzip -l "$wheel")
    for member in acm/_web/index.html acm/pricing.default.toml acm/server.py; do
        grep -q " $member\$" <<<"$listing" || { echo "wheel is missing $member"; exit 1; }
    done
    packaging/smoke-test.sh "$wheel"

# Preview the release notes the workflow would publish for the current version.
notes:
    @packaging/release-notes.sh "v{{version}}"

# --- run --------------------------------------------------------------------

# Run any acm subcommand from this checkout: `just acm export -o out.json`.
acm *args:
    uv run acm "$@"

# Serve the dashboard from this checkout.
serve *args:
    uv run acm serve "$@"

# Scan every corpus and print a summary.
scan *args:
    uv run acm scan "$@"

# API plus the Vite dev server with hot reload; Ctrl-C stops both.
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run acm serve --log-level info &
    api=$!
    trap 'kill $api 2>/dev/null || true' EXIT
    cd web && pnpm dev

# --- tests ------------------------------------------------------------------

# The unit and integration suite.
test *args:
    uv run pytest -m "not corpus" "$@"

# The slower suite that reads the real session corpora on this machine.
test-corpus *args:
    uv run pytest -m corpus "$@"

# Typecheck the dashboard without emitting.
typecheck:
    cd web && pnpm exec tsc -b --force

# Everything CI checks, in the same order.
check: test typecheck verify-wheel

# --- install ----------------------------------------------------------------

# Install `acm` on PATH as a standalone tool, dashboard included.
install: build
    uv tool install --force --from "$(ls dist/*.whl)" {{package}}
    @echo "installed $(command -v acm || echo '~/.local/bin/acm')"

# Remove the standalone tool. State and pricing are left alone.
uninstall:
    uv tool uninstall {{package}}

# Copy the systemd user unit into place without starting anything.
install-service:
    mkdir -p {{unit_dir}}
    install -m 644 packaging/{{unit}} {{unit_dir}}/{{unit}}
    systemctl --user daemon-reload
    @echo "installed {{unit_dir}}/{{unit}}"

# Build, install, start now, and start at boot. The one command for a new box.
enable: install install-service
    systemctl --user enable --now {{unit}}
    @sleep 1
    @just status

# Stop the service and leave it stopped across boots.
disable:
    systemctl --user disable --now {{unit}}

# Keep the service running when nobody is logged in. Needed once per machine.
linger:
    loginctl enable-linger "$USER"
    @loginctl show-user "$USER" | grep Linger

start:
    systemctl --user start {{unit}}

stop:
    systemctl --user stop {{unit}}

restart:
    systemctl --user restart {{unit}}

status:
    @systemctl --user --no-pager --lines=0 status {{unit}}

# Follow the service log.
logs *args:
    journalctl --user -u {{unit}} -f -n 100 "$@"

# Remove the unit file. Does not touch the installed tool.
uninstall-service:
    -systemctl --user disable --now {{unit}}
    rm -f {{unit_dir}}/{{unit}}
    systemctl --user daemon-reload

# --- maintenance ------------------------------------------------------------

# Pull, rebuild, reinstall, and restart the service if it is running.
update:
    git pull --ff-only
    just setup build install
    @if systemctl --user is-active --quiet {{unit}}; then \
        systemctl --user restart {{unit}} && echo "restarted {{unit}}"; \
    else \
        echo "{{unit}} is not running; nothing to restart"; \
    fi

# Delete the derived database. Rescanning rebuilds it.
reset:
    uv run acm reset

# Remove every build product.
clean:
    rm -rf dist acm/_web web/dist web/tsconfig.tsbuildinfo .pytest_cache
    find . -path ./.venv -prune -o -name __pycache__ -type d -print0 | xargs -0 rm -rf

# --- release ----------------------------------------------------------------

# Print the version in pyproject.toml.
version:
    @echo {{version}}

# Set the version in pyproject.toml and refresh the lockfile.
bump new:
    sed -i 's/^version = ".*"/version = "{{new}}"/' pyproject.toml
    uv lock
    @echo "version is now $(just version)"

# Refuse to tag anything that is not releasable. Run before `just tag`.
release-check: check
    #!/usr/bin/env bash
    set -euo pipefail
    test -z "$(git status --porcelain)" || { echo "working tree is dirty"; exit 1; }
    if git rev-parse -q --verify "refs/tags/v{{version}}" >/dev/null; then
        echo "tag v{{version}} already exists"; exit 1
    fi
    echo "ready to release v{{version}}"

# Tag this commit and push it, which builds the GitHub release.
tag: release-check
    git tag -a "v{{version}}" -m "{{package}} v{{version}}"
    git push origin "v{{version}}"
    @echo "pushed v{{version}}; watch the release workflow with: gh run watch"
