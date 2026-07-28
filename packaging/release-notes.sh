#!/usr/bin/env bash
# Compose the body of a GitHub release.
#
#   packaging/release-notes.sh v0.2.0 [previous-tag] > notes.md
#
# Run it locally to see exactly what the release workflow will publish. The
# commit subjects since the previous tag are the changelog, so they are worth
# writing well; anything they do not say belongs in a summary the release
# workflow cannot invent, added by hand afterwards.
set -euo pipefail

tag="${1:?usage: release-notes.sh <tag> [previous-tag]}"
version="${tag#v}"
# Previewing before the tag exists is the normal way to use this, so fall back
# to HEAD -- which is what the tag is about to point at anyway.
ref="$tag"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null || ref=HEAD
prev="${2:-$(git describe --tags --abbrev=0 "$ref^" 2>/dev/null || true)}"
repo="${GITHUB_REPOSITORY:-xertrov/codex-cache-monitor}"
base="https://github.com/$repo"

cat <<EOF
Reads every coding agent's session history on this machine -- Codex, Claude
Code, Pi, OpenCode, Grok -- and serves a live dashboard of token usage,
prompt-cache performance and notional cost, costed at list prices so the
clients are comparable.

## Install

\`\`\`
uv tool install $base/releases/download/$tag/codex_cache_monitor-$version-py3-none-any.whl
ccm serve
\`\`\`

The wheel carries the built dashboard and a default rate table, so nothing else
has to be installed and nothing has to be built. On first run it writes its
database to \`~/.local/state/ccm\` and an editable copy of the rate table to
\`~/.config/ccm/pricing.toml\`; every session corpus it reads is opened
read-only. The dashboard binds \`0.0.0.0:8808\`.

To run it in the background instead, from a checkout of this tag:

\`\`\`
just enable      # build, install, start now and at boot, under systemd --user
\`\`\`
EOF

echo
if [ -n "$prev" ]; then
    echo "## Changes since $prev"
    echo
    git log --no-merges --pretty=format:'- %s' "$prev..$ref"
    echo
    echo
    echo "[Full diff]($base/compare/$prev...$tag)"
else
    echo "## Changes"
    echo
    git log --no-merges --pretty=format:'- %s' "$ref"
    echo
fi

if compgen -G "dist/*" >/dev/null; then
    echo
    echo "## Artifacts"
    echo
    echo "| File | SHA-256 |"
    echo "|---|---|"
    for f in dist/*; do
        case "$f" in *.sha256|*SHA256SUMS*) continue ;; esac
        printf '| `%s` | `%s` |\n' "$(basename "$f")" "$(sha256sum "$f" | cut -d' ' -f1)"
    done
fi
