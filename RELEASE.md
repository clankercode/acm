# Releasing

A release is a git tag. Pushing `vX.Y.Z` makes
[`.github/workflows/release.yml`](.github/workflows/release.yml) build the
dashboard, build the wheel and sdist, prove the wheel runs on a machine with
nothing on it, and publish a GitHub release with both artifacts attached and
notes composed by [`packaging/release-notes.sh`](packaging/release-notes.sh).

The notes are generated, not written: an install block, the commit subjects
since the previous tag, and a checksum table. That is a good page whenever the
commit subjects are good. Read step 6 before deciding it needs more.

## Steps

**1. Start from a clean, current tree.**

```
git status --porcelain            # must be empty
git pull --ff-only
```

**2. Choose the version.** Patch for fixes, minor for new capability, major for
a break in the CLI, the API, or the on-disk format. Everything derived from a
scan is rebuildable, so a schema change is not a break by itself; a change to
the shape of an exported bundle is, because bundles outlive the version that
wrote them.

```
just version                      # what it is now
just bump 0.2.0                   # sets pyproject.toml, refreshes uv.lock
```

**3. Check it.** This is what CI checks, run locally first because it is faster
to find out here.

```
just check                        # pytest, tsc, wheel built and smoke-tested
just test-corpus                  # optional; only meaningful on a real machine
```

**4. Commit the bump.**

```
git add -A && git commit -m "Release 0.2.0"
git push
```

**5. Tag and push.**

```
just tag
```

`just tag` re-runs `just check`, refuses a dirty tree or an existing tag, then
pushes `v0.2.0`, which starts the release workflow. Watch it:

```
gh run watch
```

If the workflow fails after the tag is pushed, fix the cause, delete the tag
locally and remotely (`git tag -d v0.2.0 && git push --delete origin v0.2.0`),
and start again from step 3. Re-running the workflow against an existing tag is
also possible: `gh workflow run release.yml -f tag=v0.2.0`.

**6. Read the published page and decide whether it needs a summary.**

```
gh release view v0.2.0 --web
```

The generated notes say what changed, commit by commit. They cannot say what
the release is *for*. Add a short paragraph at the top when the answer is not
obvious from the list -- a new client supported, a correction to how something
was costed, a breaking change and what to do about it. Leave it alone when the
list already reads clearly.

```
gh release edit v0.2.0 --notes-file notes.md      # after editing a local copy
```

Preview the generated notes at any point, before or after tagging, with
`just notes`.

## What the workflow will refuse

- A tag whose version does not match `pyproject.toml`. `just bump` then a
  commit is the fix; the tag has to be re-cut.
- A wheel that does not carry the built dashboard, a default rate table, or
  cannot scan and serve from a bare install
  ([`packaging/smoke-test.sh`](packaging/smoke-test.sh)).
- A test failure on any of Python 3.12, 3.13, 3.14.

A tag containing a hyphen -- `v0.3.0-rc1` -- is published as a prerelease and
does not become the latest release.

## Checklist

```
[ ] clean tree, pulled
[ ] just bump X.Y.Z
[ ] just check
[ ] commit + push
[ ] just tag
[ ] gh run watch
[ ] read the release page; add a summary if the commit list does not explain itself
```
