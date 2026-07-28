# Client candidates for Agent Cache Monitor

Inventory of coding agents we could scan for token / cache / cost stats.

**Currently implemented** (see `ccm/sources/` and README): Codex, Claude Code, Pi, OpenCode, Grok, Kimi Code, Kimi CLI, Hermes, Copilot CLI, Gemini CLI.

This file is about **what else exists on this machine (or should)**, split by whether we already have a local history corpus to reverse-engineer against.

Priorities below bias toward clients that already store **per-request usage** (input / output / cache read / cache write / model), not only chat text.

---

## 1. Local samples present (not yet supported)

These have install/state dirs on this host and look scannable (or almost). Rough readiness is “how close is the stored data to a `requests` row”.

| Client | Local path(s) | Format | Usage data? | Notes / readiness |
|---|---|---|---|---|
| **Kimi Code CLI** | `~/.kimi-code/sessions/…/agents/*/wire.jsonl`, `session_index.jsonl` | JSONL wire protocol | **Yes — excellent.** `usage.record` + `context.append_loop_event` / `step.end` with `inputOther`, `output`, `inputCacheRead`, `inputCacheCreation`, model alias | ~476 MB corpus, 90+ indexed sessions. Best next client. Distinct from Kimi CLI. |
| **Kimi CLI** | `~/.kimi/sessions/<project>/<id>/wire.jsonl` | JSONL (`StatusUpdate`, `TurnBegin`, …) | **Yes.** `token_usage`: `input_other`, `output`, `input_cache_read`, `input_cache_creation` + context size | ~183 MB, ~350 session roots, ~1k wire files. Same product family as Kimi Code but different event schema. |
| **Hermes** (Nous) | `~/.hermes/state.db` (`sessions`, `session_model_usage`, `messages`) | SQLite | **Yes — excellent.** Per-session and per-model: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, cost fields | ~11 MB DB, 18 sessions / 35 model-usage rows. Near drop-in source. Also has `~/.hermes/sessions/` request dumps. |
| **GitHub Copilot CLI** | `~/.copilot/session-store.db` (`assistant_usage_events`), per-session `session-state/*/events.jsonl` | SQLite + JSONL | **Yes — excellent.** `assistant_usage_events`: model, input/output/cache read/write/reasoning tokens, duration, endpoint | ~1.7 GB total; 196 usage events in sample (~10 M input tokens). Events stream is conversational; usage table is the gold path. |
| **Cursor** (IDE agent + CLI) | `~/.cursor/projects/*/agent-transcripts/**/*.jsonl`, `~/.cursor/chats`, `~/.cursor/ai-tracking/ai-code-tracking.db`, `~/.config/Cursor/User/globalStorage` | JSONL transcripts, SQLite tracking, Electron storage | **Partial.** Transcripts are role/message content (often no billable usage). `ai-code-tracking.db` is **code attribution** (hashes, AI % of commits), not tokens. Real usage may live in Composer / globalStorage (harder) | Transcripts: ~600 JSONL files. `cursor-agent` under `~/.local/share/cursor-agent` is **binaries only** — history is under `~/.cursor`. Worth a dedicated spike on where Composer stores token counts. |
| **Claude Desktop / Cowork** | `~/.config/Claude/` (13 GB), especially `local-agent-mode-sessions/`, `IndexedDB/`, logs (`cowork_*.log`) | Electron: IndexedDB/LevelDB + JSON session metadata | **Partial / opaque.** Local agent sessions store metadata (`cliSessionId`, model, cwd, title) and may **delegate to Claude Code** (shared transcript path). Cloud chats sit in IndexedDB | Cowork/local-agent mode is the interesting path: if it reuses Claude Code transcripts, existing `claude` source may already cover host-loop work; pure Desktop chat needs IndexedDB reverse-engineering. |
| **Gemini CLI** | `~/.gemini/tmp/*/chats/session-*.jsonl` | JSONL | **Yes — per-request.** Each `type=gemini` line carries `tokens: {input, output, cached, thoughts, tool, total}` + `model` | ✅ implemented (`ccm/sources/gemini.py`). 3 session files on this host (~35 unique requests). |
| **Goose** | `~/.local/share/goose/sessions/sessions.db` | SQLite | **Schema yes, data thin.** Columns: `total_tokens`, `input_tokens`, `output_tokens`, accumulated_*, provider/model config | Only 1 session / 0 messages in sample — easy source once someone uses it more. |
| **Zed Agent** | `~/.local/share/zed/threads/threads.db` | SQLite blob threads | **Unclear.** 6 threads; usage likely embedded in `data` blob if at all | Low volume; needs schema peek inside `data`. |
| **OpenHands** | `~/.openhands/` | Mostly skills/cache | **No real session corpus** | Install present; not a usage source yet. |
| **Crush** | `~/.config/crush/` | Config + skills only | No session history found | Not a corpus. |
| **Claude Code multi-home** | `~/.claude` → `~/.claude-w` → `~/.claude-shared/projects` (~2.6 GB, ~2250 JSONL) | Already supported format | Already covered by `claude` source | Document multi-root / profile layouts if scan only follows the primary symlink. |

### Highest-ROI next sources (local)

1. **Kimi Code** — ✅ implemented (`ccm/sources/kimi_code.py`)
2. **Hermes** — ✅ implemented (`ccm/sources/hermes.py`)
3. **Copilot CLI** — ✅ implemented (`ccm/sources/copilot.py`)
4. **Kimi CLI** — ✅ implemented (`ccm/sources/kimi_cli.py`)
5. **Cursor** — only after confirming a token-bearing store (not just transcripts / AI-code %). A 2026-07-28 spike confirmed `ai-code-tracking.db` has **no token columns** (only code-attribution hashes and AI-commit-%). Chat `store.db` blobs are serialized conversation messages (role/content JSON) with no structured token metadata. The remaining candidate is the opaque 516 MB Electron `state.vscdb` (LevelDB blob), which needs deep reverse-engineering with uncertain payoff. **Verdict: negative — no per-request token store found.**
6. **Claude Desktop / Cowork** — 2026-07-28 spike checked `~/.config/Claude/`: LevelDB localStorage has no token fields; cowork logs (`cowork_vm_node.log`, `claude.ai-web.log`) carry no usage data; no SQLite databases present. Cowork sessions may already appear under Claude Code projects via `cliSessionId`, in which case the existing `claude` source covers them. **Verdict: negative — no independent per-request token store.**
7. **Gemini CLI** — ✅ implemented (`ccm/sources/gemini.py`). Session JSONL under `~/.gemini/tmp/*/chats/` carries per-request `tokens` blocks with input/output/cached/thoughts splits and model attribution.

---

## 2. No local samples (candidates to support later)

Popular agents that fit CCM’s model but have **no useful history on this machine** right now. Grouped by how likely they are to keep local, parseable usage.

### Likely file/DB-backed local history

| Client | Why it matters | Expected / typical storage (to verify) |
|---|---|---|
| **Aider** | Heavy CLI coding use; often logs per-chat | `~/.aider` / repo `.aider*` chat history; may include token counts in analytics |
| **Continue.dev** | IDE extension with local session history | `~/.continue` / IDE globalStorage |
| **Cline** | VS Code agent with long autonomous runs | VS Code `globalStorage` for the Cline extension |
| **Roo Code** | Cline fork; same pattern | Same class as Cline |
| **Windsurf (Cascade)** | Major IDE agent | App data under `~/.codeium` / Windsurf config dirs |
| **Amazon Q Developer** | CLI + IDE | `~/.aws/amazonq` or similar |
| **Warp Agent** | Terminal-native agent | Warp app data |
| **Factory / Droid** | Autonomous coding agent | Product-specific local state |
| **Amp** (Sourcegraph) | Agent product | Local cache if any |
| **ChatGPT / Codex desktop apps** | Distinct from Codex CLI rollouts | Electron app storage (may not expose list prices usage cleanly) |
| **VS Code Copilot Chat** | Different from Copilot **CLI** | Extension globalStorage; may not persist full usage |
| **JetBrains AI Assistant** | IDE-native | IDE system dirs |
| **Tabnine / Codeium standalone** | Completions-first; sometimes chat | Product caches; often weak on request-level tokens |

### Cloud-first / hard to scrape locally

| Client | Why hard |
|---|---|
| **Devin** | Session history primarily cloud |
| **Cursor cloud agents / Background Agents** | May not leave full local usage |
| **Claude.ai web** | Same IndexedDB problem as Desktop without local agent mode |
| **GitHub.com Copilot Workspace / cloud** | Server-side |

### Adjacent / lower priority

| Client | Notes |
|---|---|
| **OpenHands (full)** | Local install is thin here; when used heavily, likely docker volumes / session stores |
| **SWE-agent / Mini-SWE-agent** | Research harnesses; run logs often ad hoc |
| **LangChain/LangGraph studio, AutoGen, CrewAI** | Frameworks, not a single history layout |
| **Custom OpenAI-compatible proxies** | Better handled via proxy logs if needed, not CCM sources |

---

## How to add a client (reminder)

From README / `ccm/sources/`:

1. Write `ccm/sources/<name>.py` implementing `Source.plan` / `Source.ingest`  
2. Register in `ccm/sources/__init__.py` `_BUILDERS` and `ccm/config.py` `ALL_SOURCES` + `Settings` path  
3. Prefer clients with **request-level** usage + model id; fold cache read/write into the same columns as Claude/Codex  
4. Add corpus tests under `tests/test_corpus_clients.py` when a real history exists  

A client with no history on a machine is skipped automatically once registered (`Source.available()`).

---

## Snapshot of this machine (2026-07-28)

| Corpus | Approx size |
|---|---|
| OpenCode DB tree | 20 G |
| Codex sessions | 11 G |
| Claude Code projects (shared) | 2.6 G |
| Copilot | 1.7 G |
| Cursor chats | 1.3 G |
| Pi sessions | 634 M |
| Kimi Code | 476 M |
| Gemini | 213 M |
| Kimi CLI sessions | 183 M |
| Cursor projects / transcripts | 43 M |
| Cursor ai-tracking | 65 M |
| Claude Desktop local-agent sessions | 67 M |
| Hermes `state.db` | 11 M |
| Goose | 144 K |

Supported clients with empty/tiny primary paths on this host may still work when pointed at alternate profile dirs (`CCM_*_DIR` env overrides).
