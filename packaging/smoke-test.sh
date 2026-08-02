#!/usr/bin/env bash
# Prove a built wheel runs on a machine that has nothing else on it.
#
#   packaging/smoke-test.sh dist/agent_cache_monitor-0.1.0-py3-none-any.whl
#
# Installs the wheel into a throwaway venv under a throwaway HOME, so the
# dashboard it serves and the state it writes can only have come out of the
# wheel itself -- not out of the checkout it was built from.
set -euo pipefail

wheel="${1:?usage: smoke-test.sh <wheel>}"
port="${SMOKE_PORT:-8809}"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

uv venv --quiet "$root/venv"
uv pip install --quiet --python "$root/venv/bin/python" "$wheel"
acm="$root/venv/bin/acm"

# Installing happens first so that uv still has its usual cache; only acm runs
# under the throwaway HOME. No corpora exist there, so every client is skipped
# and the scan finds nothing -- what is under test is that the program runs.
export HOME="$root/home"
mkdir -p "$HOME"
# Overriding HOME is not enough: an XDG_*_HOME inherited from the caller wins over
# it, so state and cache would land in the real user's directories and the checks
# below would look for them in the throwaway one and not find them. Some terminals
# and agent harnesses set these.
unset XDG_STATE_HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME

echo "== acm scan"
"$acm" scan -q

test -f "$HOME/.config/acm/pricing.toml" || { echo "no rate table was written"; exit 1; }
test -f "$HOME/.local/state/acm/acm.sqlite" || { echo "no database was written"; exit 1; }

echo "== acm serve"
ACM_HOST=127.0.0.1 ACM_PORT="$port" "$acm" serve --no-watch >"$root/serve.log" 2>&1 &
pid=$!
trap 'kill $pid 2>/dev/null || true; rm -rf "$root"' EXIT

for _ in $(seq 60); do
    curl -sf -o /dev/null "http://127.0.0.1:$port/api/totals" && break
    sleep 0.5
done

curl -sf "http://127.0.0.1:$port/api/totals" >/dev/null || { cat "$root/serve.log"; exit 1; }
# The dashboard has to come from inside the wheel, not from a 503 placeholder.
curl -sf "http://127.0.0.1:$port/" | grep -qi '<title>' || {
    echo "the wheel does not carry a built dashboard"
    cat "$root/serve.log"
    exit 1
}

kill "$pid"
wait "$pid" 2>/dev/null || true
echo "== ok: $(basename "$wheel") scans, serves the API and serves the dashboard"
