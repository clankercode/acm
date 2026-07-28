#!/usr/bin/env bash
# Pull, rebuild, reinstall and restart, from the dashboard's Update button.
#
#   packaging/self-update.sh <checkout> <logfile> [restart|norestart]
#
# Runs detached from the server that asked for it, because the last thing it
# does is restart that server. Everything it says goes to the log, which the
# dashboard tails -- there is no terminal to print to, and the browser needs to
# see why an update failed as much as that it did.
set -uo pipefail

checkout="${1:?usage: self-update.sh <checkout> <logfile>}"
log="${2:?usage: self-update.sh <checkout> <logfile>}"
# "restart" only when the caller launched us somewhere that survives it.
restart="${3:-norestart}"
unit="ccm.service"

mkdir -p "$(dirname "$log")"
# Truncated, not appended: the log is a transcript of *this* attempt, and the
# dashboard shows the tail of it. Old attempts are of no interest once a new one
# starts, and an append-only log would grow forever unwatched.
exec >"$log" 2>&1

# A systemd --user unit inherits almost no PATH, and the toolchain lives in the
# user's own bin directories. Without this, `just` is not found and the update
# fails for a reason that has nothing to do with the update.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin"

say() { echo "[$(date +%H:%M:%S)] $*"; }

fail() {
    say "FAILED: $*"
    # Left behind deliberately: the dashboard reads this to know the attempt
    # ended, and the exit status is the only honest summary of it.
    echo "status=failed" >"$log.done"
    exit 1
}

cd "$checkout" || fail "no checkout at $checkout"
command -v just >/dev/null || fail "just is not on PATH"

say "updating $checkout"
say "$(git -C "$checkout" log --oneline -1)"

# Not `just update`: that recipe restarts the unit as its last step, and this
# script has more to say afterwards. The steps are the same ones.
git pull --ff-only || fail "git pull --ff-only (local commits, or a dirty tree?)"
say "at $(git log --oneline -1)"
just setup || fail "just setup"
just build || fail "just build"
just install || fail "just install"

if ! systemctl --user is-active --quiet "$unit"; then
    # Not a failure, but not a finished update either: whatever is serving the
    # dashboard right now is still the old build, and saying "done" would be a
    # lie the panel repeats.
    say "installed, but $unit is not running -- the process serving this page is"
    say "still the old build. Restart however you started it."
    say "done"
    echo "status=partial" >"$log.done"
    exit 0
elif [ "$restart" = norestart ]; then
    # No systemd-run, so this script lives inside the service's own cgroup and
    # `systemctl restart` would kill it here -- no outcome written, the panel
    # stuck on "Updating..." forever, after a successful update.
    say "installed, but not restarting: without systemd-run this script would be"
    say "killed by the restart. Run: systemctl --user restart $unit"
    say "done"
    # Not "ok": the new code is on disk but the old code is still serving, and a
    # panel reading "Last update" would be claiming otherwise. Nothing else here
    # would correct it either -- the build id never changes, so no reload prompt
    # fires.
    echo "status=partial" >"$log.done"
    exit 0
else
    say "restarting $unit"
    # The server dies here; the browser reconnects on its own and notices the
    # new build. The outcome is written first, because nothing after this line is
    # guaranteed to run: the restart may take this script's cgroup with it.
    say "done"
    echo "status=ok" >"$log.done"
    systemctl --user restart "$unit" || fail "systemctl --user restart $unit"
    say "restarted $unit"
    exit 0
fi

say "done"
echo "status=ok" >"$log.done"
