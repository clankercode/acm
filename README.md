# Agent Cache Monitor

Reads every coding agent's session history on this machine and serves a live
dashboard of token usage, prompt-cache performance and notional cost.

| Client | Where it keeps history | Format |
|---|---|---|
| Codex | `~/.codex-shared/sessions` | rollout JSONL |
| Claude Code | `~/.claude/projects` | transcript JSONL |
| Pi | `~/.pi/agent/sessions` | session JSONL |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite |
| Grok | `~/.grok/sessions` | session update JSONL |

A client with no history on this machine is skipped, so the same build runs
whether one is installed or all of them.

The metric it leads with is **effective rate**: total cost divided by input
tokens processed, in dollars per million. It folds cache hit rate, context tier,
cache-write overhead and model mix into one number where lower is always better,
regardless of how much work was done — `$4,445 / 2.38 B tokens` ($1.87/M) is
worse than `$20,004 / 31.6 B` ($0.63/M). Costing every client at published list
prices is what makes that number comparable across them.

```
just setup                     # uv sync, pnpm install
just serve                     # scan, watch, serve
```

Or install it, dashboard and rate table baked into the wheel, and run it in the
background from boot:

```
just enable                    # build, install, systemd --user, start now
just logs
```

The dashboard binds `0.0.0.0:8808`, so it is reachable from the LAN as well as
loopback; the startup banner prints both URLs. Every corpus is opened
**read-only** and never written to.

`just` with no arguments lists everything; see [`just` recipes](#just-recipes)
below for the full set, and [RELEASE.md](RELEASE.md) for the release procedure.

## Commands

```
ccm scan            # scan every client, print a summary, exit
ccm serve           # scan, watch for changes, serve the dashboard
ccm models          # per-model table on the terminal
ccm machines        # local and imported data, side by side
ccm export          # write a portable bundle (--label, --origins, --out)
ccm import FILE     # load another machine's bundle (--label)
ccm reset           # delete derived state (safe; rescanning rebuilds it)
```

Overrides: `--sessions`, `--db`, `--pricing`, or the environment variables
`CCM_SESSIONS_DIR`, `CCM_CLAUDE_DIR`, `CCM_PI_DIR`, `CCM_OPENCODE_DB`,
`CCM_GROK_DIR`,
`CCM_SOURCES`, `CCM_DB`, `CCM_PRICING`, `CCM_REFERENCE`, `CCM_HOST`, `CCM_PORT`,
`CCM_POLL`. `CCM_SOURCES=codex,claude` restricts the scan to named clients.
For the service, they go in `~/.config/ccm/env` — see
[`packaging/ccm.env.example`](packaging/ccm.env.example).

## `just` recipes

`just` with no arguments lists all of them with a one-line description. The
ones worth knowing by name:

**Working on it from a checkout**

```
just setup                     # uv sync --extra dev; pnpm install
just serve [args]              # uv run ccm serve
just scan [args]                # uv run ccm scan
just ccm <subcommand> [args]   # any ccm subcommand, e.g. just ccm export -o out.json
just dev                       # API + Vite dev server together, hot reload, Ctrl-C stops both
just test [args]               # the fixture suite (~5s)
just test-corpus [args]        # the suite that reads the real corpora on this machine (~45s)
just typecheck                 # tsc -b --force on the dashboard
just check                     # test + typecheck + a wheel that must actually serve; what CI runs
just clean                     # remove dist/, ccm/_web, web/dist, caches
```

**Building and installing**

```
just build-web                 # pnpm build, then stage web/dist into ccm/_web
just build                     # build-web, then uv build -> dist/*.whl, dist/*.tar.gz
just verify-wheel              # build, then install the wheel into a throwaway venv+HOME and prove it scans and serves
just install                   # build, then uv tool install the wheel -> `ccm` on PATH
just uninstall                 # uv tool uninstall (state and pricing are left alone)
```

**Running it as a service** (systemd `--user`; see next section)

```
just enable                    # install + install-service + start now + start at boot
just disable                   # stop it and leave it stopped across boots
just linger                    # keep it running when nobody is logged in (once per machine)
just start / stop / restart
just status                    # systemctl --user status ccm.service
just logs [args]               # journalctl --user -u ccm.service -f, e.g. just logs -n 200
just uninstall-service         # remove the unit file (leaves the installed tool alone)
```

**Updating and releasing**

```
just update                    # git pull --ff-only; setup; build; install; restart the service if it's running
just reset                     # delete the database (rescanning rebuilds it)
just version                   # print the version in pyproject.toml
just bump X.Y.Z                # set the version and refresh uv.lock
just notes                     # preview the release notes the workflow would publish
just tag                       # release-check, then tag and push -- starts the release workflow
```

[RELEASE.md](RELEASE.md) walks through a release end to end.

## Running it as a service

```
just enable
```

builds the wheel with the dashboard and rate table baked in, installs it with
`uv tool install`, installs
[`packaging/ccm.service`](packaging/ccm.service) as a systemd **user** unit
(user-scoped because every corpus it reads lives under `$HOME` and needs no
privilege beyond the login user's), and starts it now and at every future
login. To have it come up even when nobody is logged in:

```
just linger
```

Override defaults (host, port, which clients to scan, custom corpus paths) by
copying [`packaging/ccm.env.example`](packaging/ccm.env.example) to
`~/.config/ccm/env` and uncommenting what you need — systemd does not expand
`~` or `$HOME` in that file, so paths have to be written out in full.

```
just logs                      # follow it
just restart                   # after changing ~/.config/ccm/env
just update                     # pull, rebuild, reinstall, and restart it in one step
```

## Where it keeps things

Run from a checkout, everything writable stays in the checkout: `data/ccm.sqlite`
and the `pricing.toml` beside it. Installed, there is no checkout to write to,
so it uses the XDG directories instead.

| | checkout | installed |
|---|---|---|
| database, safe to delete | `data/ccm.sqlite` | `~/.local/state/ccm/ccm.sqlite` |
| rate table, editable | `pricing.toml` | `~/.config/ccm/pricing.toml` |
| models.dev cache | `data/models-dev.json` | `~/.cache/ccm/models-dev.json` |

Which one applies is decided by whether a `pyproject.toml` sits above the
package, so an editable install still behaves like a checkout. The installed
copy carries the default rate table inside the wheel and writes it out the first
time it runs, and carries the built dashboard too — `uv tool install` of the
released wheel needs no Node and no build step.

The systemd unit asks systemd for those three directories by name
(`StateDirectory=`, `ConfigurationDirectory=`, `CacheDirectory=`), which is what
keeps them writable under `ProtectSystem=strict`. Pointing `CCM_DB` somewhere
else means adding a matching `ReadWritePaths=` in a drop-in.

## Several machines, one view

Stats are portable. Export from one PC, import on another, and both appear in
every chart, KPI and table with a `MACHINE` selector beside the `CLIENT` one.

```
ccm export --label max-desktop -o max-desktop.json     # on the first machine
ccm import max-desktop.json                            # on the second
ccm export --label team -o team.json                   # re-export the pool
```

A bundle carries the hourly rollup plus per-session stats — 1,117 bucket rows
and 1,019 session rows for 110k requests, about 85 KB gzipped. What it
deliberately does not carry:

- **Costs.** Only token counts travel; the receiving machine prices them with
  its own rate table. That is the only way figures from several machines are
  comparable, and it means a rate correction applies retroactively to imported
  data too.
- **Paths and working directories.** Meaningless elsewhere and the most likely
  thing to leak. The normalised project label does travel, so cross-machine
  repo comparison still works.

Session rows are split by `(model, context tier)`, the same grouping the local
query uses, so an imported session re-costs *exactly* when prices change rather
than being frozen at whatever the exporter charged. They carry no per-request
timeline, which the UI states rather than faking.

Imported data lives in the same rollup under an `origin`, so `ccm serve`
treats another machine as one more dimension. Two consequences worth knowing:
the rollup rebuild is scoped to the local origin so it cannot delete imported
rows, and a full rescan keeps imports — this machine cannot re-read someone
else's corpus, so dropping it would simply lose it.

## What the clients disagree about

Each format records the same underlying event — one API request and its token
counts — and almost nothing else lines up. Getting any of these backwards
produces figures that look entirely plausible and are wrong, so each is pinned
down by a test.

**"Input tokens" means different things.** Codex and Grok report the whole
prompt with cache hits included. Claude Code, Pi and OpenCode report only the
uncached remainder, with cache traffic in separate counters. Left as reported, a
98%-cached Claude request looks like a 400-token prompt and its cache rate is
meaningless. Every reader normalises to the whole prompt, with cached reads and
cache writes as subsets of it.

**Reasoning tokens are sometimes inside output and sometimes beside it.** They
are a subset of `output_tokens` on Codex, Pi and Grok, and a sibling field on
OpenCode — where they still bill as output. Readers store billable output, and
report reasoning separately for information only.

**One client counts turns, not requests.** Grok writes usage once per completed
turn, covering the `modelCalls` API calls the turn needed, and for a subagent
once for the whole run. Nothing finer is recorded — per-call figures exist only
in a rolling debug log with no model attribution — so a Grok row is a turn, and
the calls folded into it are counted as an anomaly rather than left to make the
request count quietly mean something different for one client.

**Cache writes are free on some providers and expensive on others.** OpenAI, xAI
and MiniMax populate their caches for nothing and never report the tokens.
Anthropic charges 1.25x base input for a 5-minute entry and 2x for a 1-hour one,
against 0.1x to read one back. Claude Code writes long-TTL entries by default,
and although they are only ~1.3% of its prompt tokens they are a material share
of its bill — the tests assert both that the share is non-zero and that it stays
in a plausible band.

**Every client repeats itself, differently.** See below.

## Deduplication

**Codex replays.** It rewrites a thread's entire prior history into a new
rollout file whenever a session resumes or a subagent forks, stamping every
replayed line with the moment of the rewrite. One file in the reference corpus
holds 41,619 token-count events covering six minutes of wall clock. Summing the
corpus naively overstates it by about **3.9x**, and by up to 40x within a single
lineage.

**Claude Code streams.** A response is written out once per content block — text,
thinking, each tool call — and every one of those lines repeats the whole usage
object, with `output_tokens` growing as it goes. 4,942 lines describe 2,130 real
requests, a **2.3x** overstatement.

**OpenCode mutates.** Rows are edited in place as a response completes, so the
same row is legitimately read more than once.

**Grok does neither.** Its update log is append-only with no replay, and every
line carries an event id, so it deduplicates on that alone.

One rule covers them all. A request is identified by `(source, dk)`, where each
reader mints `dk` from whatever its format guarantees: cumulative token counters
for Codex, the API message id for Claude Code, the response id for Pi, the row
id for OpenCode, the event id for Grok. A `rank` column then decides which
sighting of a duplicate wins — highest rank, with the earliest timestamp
breaking ties.

That single comparison expresses both policies. Codex leaves rank at 0, which
reduces it to "earliest wins" — correct, because a replay carries the replay's
clock and the minimum recovers the true time, and with it the right hour bucket.
Claude Code sets rank to the output-token count, giving last-complete-wins even
if a resume copies a truncated early line into a transcript that gets read
first. OpenCode ranks by `time_updated`.

Ingest is therefore idempotent and order-independent: re-reading bytes already
consumed changes nothing, so an interrupted scan is always safe to resume.

## Incremental reads

The newest file in each corpus is appended to while the tool runs and can be
tens of megabytes. File-backed clients are read from a stored byte offset, never
re-parsed whole, and the cursor advances only over complete lines — a record
still being written is re-read intact next pass. If a file shrinks or its inode
changes it is re-ingested from scratch. OpenCode keeps a high-water mark on
`message.time_updated` instead, and its database is opened read-only, which
still reads the write-ahead log so a session in progress is visible.

Throughput is roughly **190 MB/s** across all of them: the 3.9 GB corpus scans cold
in ~22 s. Most of that comes from rejecting lines on a byte-substring test before
paying for a JSON parse, which skips ~80% of the Codex corpus with zero misses.

## Cost model

Rates live in [`pricing.toml`](pricing.toml), are hot-reloaded, and are editable
from the dashboard. **Costs are never stored** — only token counts are — so a
rate change updates every figure instantly with no rescan.

```
cost     = fresh * input_rate + cached * cached_rate
         + written_5m * cache_write_rate + written_1h * cache_write_1h_rate
         + output * output_rate
uncached = input * input_rate + output * output_rate
saved    = uncached - cost
eff_rate = cost / (input / 1e6)
```

where `input` is the whole prompt and `fresh` is what remains of it after cached
reads and cache writes. The counterfactual deliberately prices the entire prompt
at the base input rate: with no cache there would be nothing to read back and
nothing worth storing.

Two tiers apply. Requests whose prompt exceeds `long_context_threshold` bill at
long-context rates — on the GPT-5.6 family, double the input rate and 1.5x the
output rate above 200k; on MiniMax-M3, double everything above 512k. This is not
a rounding error: over the Codex corpus 25% of requests but **42% of input
tokens** land in the long tier, and ignoring it understates spend by 42%. Claude
4.6 and later carry their full 1M window at standard pricing and so have no long
tier at all.

Because the tier is a property of the individual request, it is part of the
rollup key. Buckets are homogeneous in model *and* tier, so costing a bucket's
summed tokens at its tier's rate is exactly equal to summing the requests inside
it — an invariant asserted in the tests against the real corpora.

Every costed figure therefore also carries what the same tokens would have cost
at standard rates, and the gap is reported as the **long-context surcharge**.
It is worth separating because no invoice itemises it — it arrives as a higher
rate per token, not a line — and because it is the one part of the bill that
shrinks by changing behaviour rather than by changing model. On this corpus it
is **$5,470 of $18,966, or 28.8% of spend**.

Provider prefixes (`pooler/`, `llmp/`, `minimax/`) resolve to the underlying
model's rates but are kept as a separate dimension, which is what makes
routed-versus-direct comparable. Most clients record the prefix themselves.
Grok does not: it names a routed model twice — `[model.llmp-glm-5-2]` in the
session, `model = "glm-5.2"` in the usage block — and only `~/.grok/config.toml`
knows they are the same thing, so the reader joins through it. Without that,
gateway traffic would sit in the "direct" bucket and the comparison would be
quietly wrong.

Costs are pay-as-you-go equivalents. On a subscription plan there is no literal
invoice; the rate-limit data in the Codex sessions is charted separately as
actual quota burn.

## Architecture

```
ccm/config.py            paths, enabled clients, settings
ccm/pricing.py           rate table, tier resolution, cost arithmetic
ccm/store.py             SQLite schema, idempotent upserts, dirty-hour triggers
ccm/sources/base.py      the Source contract and the shared JSONL tailing
ccm/sources/codex.py     rollout JSONL, replay collapse
ccm/sources/claude.py    transcript JSONL, streaming rewrites, cache-write TTLs
ccm/sources/pi.py        session JSONL, client-reported costs
ccm/sources/opencode.py  SQLite, mutable rows
ccm/sources/grok.py      session update JSONL, turn-level usage, gateway names
ccm/scanner.py           the driver: plan, ingest, report progress
ccm/aggregate.py         rollup maintenance and every query the UI makes
ccm/portable.py          export/import bundles, machine labels
ccm/modelsdev.py         cached reference prices, for cross-checking the table
ccm/engine.py            background scan loop and SSE fan-out
ccm/watcher.py           inotify with a polling fallback
ccm/server.py            REST + server-sent events
web/                     Vite + React + TypeScript + uPlot
justfile                 setup, build, install, service, release
packaging/               systemd unit, wheel smoke test, release notes
.github/workflows/       tests on three Pythons; tagged builds become releases
```

Adding a client means writing one file under `ccm/sources/` — `plan()` lists the
units with work outstanding, `ingest()` consumes one — and registering it. The
scanner treats them identically and reports progress per client.

All state lives in one SQLite file. Deleting it
and rescanning reproduces it exactly; a schema version bump does that
automatically rather than migrating, since nothing here is a source of truth.

The hourly rollup is a materialised view keyed by `(hour, origin, source, model,
provider, repo, is_subagent, long_ctx)`. Stale hours are tracked by trigger —
including both the old and new hour when a late timestamp correction moves a
request — and rebuilt delete-then-insert so the rollup cannot drift from
`requests`.

Two views deliberately ignore the selected time range. The **calendar** paginates
months itself, so applying a 7-day window on top of it would leave every other
month blank; every other filter still applies, because those say what is being
counted rather than when. Cells are shaded from zero rather than from the
quietest day — except for cache *rate*, which is a rate and would otherwise be
one flat colour across a corpus that sits in the high nineties all month.

`repo` is a normalised project label rather than a git remote. Only Codex
records a remote, and it records two different ones for the same working tree,
so remotes cannot join across clients. Every client records a cwd, so the label
is the nearest ancestor holding a `.git` — which also folds requests made from a
subdirectory into the project they belong to.

Live updates use two event kinds. `scan` carries progress only and lands often,
driving the progress bar. `data` carries a generation counter and the running
totals, and fires only when stored rows actually changed; that counter is what
tells the charts to refetch. Series data is pulled over HTTP rather than pushed,
since pushing every chart's points on every tick would be megabytes a second for
numbers nobody can read that fast.

Progress is reported sparingly, at both ends. A pass that plans nothing emits no
`scan` event at all, and discovery only announces itself once it has run long
enough to look like a hang, so the ordinary quiet poll never reaches the browser.
The header then draws a bar only for a pass worth waiting on — ten files or eight
megabytes, sustained for a moment — because the corpora are live and a pass over
a few appended lines happens every couple of seconds forever. What it does draw
goes in the gap between the title and the controls, so a scan starting or ending
never moves anything that was already on screen.

If inotify is unavailable — the per-user instance limit is easy to hit, and each
client needs its own watcher — the watcher logs it once and falls back to
polling. A slow poll runs regardless, because watches only cover directories
that existed at start-up and each new day creates one.

## Tests

```
just test                     # 154 unit and integration tests, ~5s
just test-corpus              # 28 tests against the real corpora, ~44s
just check                    # what CI runs: tests, tsc, and a wheel that must serve
```

The corpus suites are the real gate. Because the corpora are live — files grow
while the tests run — nothing asserts a hard-coded total. Instead each reader is
compared against an independent reference implementation over *exactly the bytes
it consumed*, which the `files` table records as a per-file offset. Same bytes
in, same deduped requests out, or the test fails.

Pi gets a stronger check still: it records what it believes each request cost, so
over the 466 requests it priced itself our figure must match to within 1e-6 —
which exercises the rate table, the prompt-total normalisation and the cache
accounting in one shot. It currently matches exactly.

Around those sit the invariants: buckets equal per-request costs across a mixed
corpus, the rollup conserves tokens, cached reads plus cache writes never exceed
the prompt, cumulative counters stay monotonic, per-client totals sum to the
whole, every model in every corpus is priced, and each client's duplicate ratio
stays above its known floor.

The export path is tested as a round trip — export, import into a fresh store,
and every headline figure, breakdown and session row must come out identical —
plus the two regressions the design exists to prevent: a dirty-hour rollup
rebuild must leave imported rows untouched, and pooling must not count one
session twice (buckets from two machines sum; a repeated rollout id is the same
session round-tripped).

## Reference figures

From every corpus at time of writing (927 files, 4.1 GB, 2026-07-05 onward):

| | Codex | Claude Code | Pi | OpenCode | Grok | All |
|---|---|---|---|---|---|---|
| Requests | 107,264 | 2,595 | 507 | 106 | 2 | 110,474 |
| Input tokens | 15.59 B | 0.68 B | 51.5 M | 8.6 M | 64.3 k | 16.33 B |
| Cache rate | 92.9% | 98.8% | 91.6% | 93.7% | 36.3% | 93.1% |
| Cost | $18,471 | $479 | $5.59 | $0.00 | $0.09 | $18,957 |
| Effective rate | $1.185/M | $0.702/M | $0.108/M | $0.000/M | $1.445/M | **$1.161/M** |
| Duplicate ratio | 3.89x | 2.20x | 1.00x | 1.00x | 1.00x | 3.83x |

OpenCode's zero is real, not missing: `big-pickle` and `deepseek-v4-flash-free`
are free during the OpenCode Zen beta, and are priced at an explicit $0 rather
than left unpriced. Grok's two rows are two turns, not two API calls — it had
only just been installed, and one of those turns covered two calls.

Two things the dashboard is built to surface:

- **Routing through the pooler destroys the cache.** `pooler/gpt-5.6-sol` caches
  at 70.4% against 97.5% for the same model direct — $2.97/Mtok versus
  $0.85/Mtok, 3.5x as much per token processed.
- **It regressed sharply in late July.** The daily effective rate went from
  $0.77/Mtok on 18 July to $2.52/Mtok on 27 July, tracking cache rate falling
  from 97% to 74%.

## Pricing sources

Rates were taken from each vendor's published pricing page and cross-checked;
every entry in `pricing.toml` carries its `source`, and a test asserts that none
is missing. Anything inferred rather than published is marked `estimated` and
flagged in the Data Quality panel — currently only the cached rate for
`grok-composer-2.5-fast`, which affects a single request.

`pricing.toml` stays the source of truth, but it has a standing second opinion:
the **Reference prices** panel fetches [models.dev](https://models.dev) on
demand, caches it to `data/models-dev.json`, and holds every configured rate
against it. At the time of writing all 18 models agree exactly on input, cached
and output; one (`grok-composer-2.5-fast`) is not listed there.

The panel distinguishes a difference that could change a figure from one that
cannot. models.dev quotes a cache-write rate for the OpenAI models — its house
1.25x convention — which OpenAI does not charge and Codex never reports. With
no tokens in that category it cannot move a number, so it is shown as **inert**
rather than as a discrepancy. Flagging it would train the reader to ignore the
table, which is the only failure mode that matters for a check like this.

Fetching is manual and offline-safe: an unreachable network leaves the cache
intact and the comparison merely goes stale.
