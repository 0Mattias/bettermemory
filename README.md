# bettermemory

[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97757)](plugin/README.md)
[![PyPI](https://img.shields.io/pypi/v/bettermemory.svg)](https://pypi.org/project/bettermemory/)
[![CI](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg)](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Persistent memory for Claude Code, retrieved on demand — not force-fed into every prompt.**

Other LLM memory tools auto-inject stored facts into every conversation. Ask for a Python tutorial, get an answer tinted by your home-lab notes. Ask a generic shell question, get advice coloured by a preference you stated months ago. bettermemory inverts the contract: the model calls `memory_search` only when context is needed, and tells you when stored memory shaped a reply. Memories live as plain markdown on disk — `grep`-able, `git`-versionable, hand-editable.

## Install

For Claude Code:

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

For any other MCP client (Claude Desktop, Cursor, Continue, Cline), see [`docs/clients.md`](docs/clients.md). The short form:

```sh
uv tool install bettermemory           # or: pipx install / pip install
bettermemory init --client claude-desktop
```

## What it looks like

**Day one.** You say: *"When I ask for a tutorial, I want runnable code, not screenshots of an IDE."* Claude calls `memory_write(category="user-inference")`. Because it's a claim about you, the write goes pending. Claude asks: *"Want me to remember that?"* You confirm. A markdown file lands at `~/.claude-memory/`.

**Week two**, fresh session: *"Walk me through pandas from zero to hero."* The phrase is ambiguous in a way stored preferences could resolve, so Claude calls `memory_search`, surfaces the preference, and tells you up front: *"Using your stored preference for code-driven tutorials…"* before answering.

**Month three:** *"What's the difference between `find` and `fd`?"* Generic question. Claude doesn't search. The reply is untainted by months of accumulated personal context. That's the whole design.

## Features

- **Opt-in retrieval.** `memory_search` is a tool the model calls when context is needed. The default is not to call it.
- **Proactive writing with structural gates.** Aggressive writing is safe because a durability check, content/tombstone dedup, scope-mismatch check, and a `user-inference` pending tier guard the writes.
- **Hybrid retrieval.** Four selectable rankers: `keyword` (default), `bm25`, `semantic` (sentence-transformers), or `hybrid` (Reciprocal Rank Fusion). Per-call or via config.
- **Three staleness signals on every hit**, folded into a `staleness_verdict` ∈ {`fresh`, `spot_check_recommended`, `spot_check_required`}: calendar verification age, filesystem path drift, and commit drift against the memory's origin repo.
- **Claim-level provenance.** `memory_record_use(claim_excerpts=[…])` logs the load-bearing claim each memory contributed. Audits trace a response back to a specific sentence.
- **Write-time groundedness gate.** Opt-in `memory_write(groundedness_check=True, source_transcript=…)` flags sentences that don't anchor to the conversation that produced them. The HaluMem benchmark, made operational inline.
- **Negative-results suppression.** When a hit was `ignored` or `contradicted` recently and not since `applied`, it carries `recent_negative_outcomes` so the model doesn't keep re-suggesting the same junk.
- **Typed inter-memory links.** `supersedes` / `contradicts` / `extends` / `depends_on`. Surfaced bidirectionally on `memory_show`.
- **Tombstones, not deletes.** Removed memories keep their `removed_reason`. Tombstone-aware dedup catches paraphrases six months later. Reversible via `memory_restore`.
- **Confirmation tier for claims about you.** `category="user-inference"` always goes pending regardless of config — misattribution sticks, so the user always gets the veto.
- **Auto-scoped by repo and worktree.** Memories written from a git checkout carry the repo URL and worktree root. `memory_search` filters by both. Sibling worktrees of the same repo are isolated.
- **Plain-text storage.** No database, no opaque blob.

## How it compares

| | bettermemory | mem0 | Letta (MemGPT) | Zep / Graphiti | Cognee | Anthropic Memory Tool |
|---|---|---|---|---|---|---|
| Retrieval contract | **Opt-in** | Auto-inject | Tool-routed | Auto-inject | Auto-inject | List+read, no search |
| Claim-level provenance | **Yes** | No | No | No | No | No |
| Write-time groundedness gate | **Yes** | No | No | No | No | No |
| Staleness signals (calendar + path + commit) | **Yes** | No | No | Bi-temporal | No | No |
| Negative-results suppression | **Yes** | No | No | No | No | No |
| Typed inter-memory links | **Yes** | No | No | Graph edges | Graph edges | No |
| Cross-host sync | git-based | Cloud-only | Cloud-only | Cloud-only | Cloud-only | No |
| Plain-text storage | **Yes** | No | No | No | No | Yes |
| Production junk-rate report | n/a | **97.8%** ([#4573](https://github.com/mem0ai/mem0/issues/4573)) | n/a | n/a | n/a | n/a |
| License | MIT | Apache-2.0 | Apache-2.0 | Apache-2.0 (Graphiti) | Apache-2.0 | Closed |

Bold cells in the bettermemory column mark capabilities no other system in the field has.

## Coexistence with Claude Code's built-in memory

Claude Code 2.x ships its own filesystem-backed memory that auto-injects into the system prompt. That's the exact failure mode bettermemory exists to fix. Installing the plugin lands the *"persistent memory between sessions lives in this server's MCP tools, do not fragment it across ad-hoc files alongside"* anchor in the system prompt, which keeps the model from drifting back to the built-in directory mid-conversation. Manual installs can paste [`docs/system_prompt.md`](docs/system_prompt.md) into `CLAUDE.md` for the same effect.

## On-disk format

One file per memory:

```
~/.claude-memory/2025-03-14-jupyter-tutorial-style.md
```

```markdown
---
schema_version: 1
id: 01HXYZ123ABC
created: 2025-03-14T10:23:00+00:00
updated: 2025-03-14T10:23:00+00:00
scopes: [tools, learning-style]
confidence: high
source: explicit-statement
---
When I ask for a "zero to hero" tutorial, I want a hands-on
walkthrough with code I can run, not a tour of the IDE.
```

Tombstones move to `.tombstones/`. Optional fields are written only when populated: `origin` (cwd + repo + branch + worktree captured at write time), `last_verified_at`, `category`, `verified_paths` / `verified_commits` / `verified_versions`, and `links`.

Storage resolution: `$BETTERMEMORY_DIR` if set, else `./.claude-memory/` if it exists, else `~/.claude-memory/`. Project-scoped overrides global; cross-project queries are explicit (`auto_scope=false`).

## Tools

17 MCP tools, grouped:

- **Retrieval** — `memory_search`, `memory_show`, `memory_list`, `memory_scope_overview`
- **Writing** — `memory_write` (plus `memory_write_confirm` / `memory_write_cancel` for the staged-write flow), `memory_update`
- **Lifecycle** — `memory_remove`, `memory_restore`, `memory_list_tombstones`
- **Verification** — `memory_verify`
- **Curation** — `memory_record_use`, `memory_health`, `memory_audit_turn`, `memory_rename_scope`
- **Session-local** — `memory_scope_disable`, `memory_scope_enable`

Full signatures, defaults, and return shapes in [`docs/api.md`](docs/api.md).

## CLI

The `bettermemory` script is the MCP server entry point by default — no args, runs over stdio. It also exposes offline tooling:

```sh
bettermemory init --client claude-code      # register with a client (idempotent)
bettermemory doctor                         # diagnose install state
bettermemory health                         # curation rollup (text or --json)
bettermemory consolidate                    # dedup + demote + cold-scope + typo passes
bettermemory consolidate --apply            # commit dedup + demotions
bettermemory reindex                        # rebuild FTS5 index from on-disk files
bettermemory sync init --remote URL         # git-based cross-host sync
bettermemory sync push | pull | auto | status
bettermemory ui                             # local FastAPI curation UI (needs [ui] extra)
bettermemory tombstones list | prune
bettermemory export                         # backup
```

## Performance

Below ~500 memories, search uses `load_all` (byte-stable to 1.x). Above the threshold (`BETTERMEMORY_INDEX_THRESHOLD`), an SQLite FTS5 inverted index pre-filters candidates, capping per-search work regardless of corpus size. Files stay canonical; the index is a derived cache at `<store>/.index.sqlite`, kept live by Store hooks. Recovery from hand-edits: `bettermemory reindex`.

## Config

`config.toml` is created on first run under `platformdirs`:

- macOS: `~/Library/Application Support/bettermemory/config.toml`
- Linux: `~/.config/bettermemory/config.toml`
- Windows: `%LOCALAPPDATA%\bettermemory\config.toml`

Defaults are sensible — most users never edit it. Knobs that matter: `behavior.search_mode` (`keyword` / `bm25` / `semantic` / `hybrid`), `behavior.require_write_confirmation` (per-write veto; off by default for solo setups, but `category="user-inference"` always goes pending regardless), `behavior.verification_stale_days` (default 30), `telemetry.enabled` (flip to `false` to disable the event log).

## Limitations

- **No encryption.** Memories are plaintext on disk. Don't store secrets; use OS-level disk encryption if you need it.
- **No automatic conflict resolution for sync.** `bettermemory sync` delegates to git. True content conflicts surface as normal merge conflicts.
- **Web UI is read-mostly.** Curation and one-click `memory_verify` only. Writes happen in-conversation.
- **Disabled scopes don't survive restart.** Intentional; each session starts fresh.
- **Multi-process locking falls back to no-op on Windows.** Single-process recommended there.

## Out of scope

- **Cloud sync as a service.** Sync is git-based; bring your own remote (GitHub, Forgejo, bare repo over SSH).
- **Cross-user sharing.** Single-user tool. Team scopes are deferred.
- **Automatic memory extraction from transcripts.** That's mem0's pitch and the source of its 97.8% junk problem. The opt-in retrieval contract loses meaning if you bolt on auto-write.

## Origins

I started building this because Claude Code's built-in memory at the time auto-injected every stored "fact" into every system prompt. The more I taught the model about my preferences, the more it dragged irrelevant context into unrelated conversations. I wanted memory the model retrieved on demand, like any other tool. That's the design.

The project was originally called `bettermemory`. Mid-build, the auto-injecting memory feature kept overriding my stated preference and renaming the package `memory-mcp` in conversation. The irony was sufficient motivation to finish.

Built by Mattias Rask. MIT licensed — see [LICENSE](LICENSE).
