# bettermemory

[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97757)](plugin/README.md)
[![PyPI](https://img.shields.io/pypi/v/bettermemory.svg)](https://pypi.org/project/bettermemory/)
[![CI](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg)](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Memory you can verify** — local-first, MCP-native memory for AI coding agents where every fact comes back with a verdict on whether it's *still true*.

Most memory tools store a fact and hand it back months later as if nothing changed — but files move, preferences change, and a commit two days ago can quietly invalidate a config note. bettermemory attaches a **staleness verdict** to every retrieval (calendar age **+** moved file paths **+** commits since the fact was last confirmed), so your agent spot-checks *before* it trusts. Plain markdown on disk. No cloud, no database, no account. MIT.

## Quick start

**Claude Code** — one marketplace, one install:

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

**Any other MCP client** — Claude Desktop, Cursor, Continue, Cline:

```sh
uv tool install bettermemory        # or: pipx install / pip install
bettermemory init --client claude-desktop
```

That's it — the server registers itself in the right config file and runs over stdio. No database to provision, no API key, no account. Per-client setup → [`docs/clients.md`](docs/clients.md); deeper install notes → [`docs/installation.md`](docs/installation.md).

## Why it's different

```jsonc
// The model asks: "where does the auth middleware live again?"
// memory_search returns the fact — and the fact's health alongside it:
{
  "snippet": "Auth middleware lives in src/auth/middleware.py, wraps every /api route…",
  "relevance": "high",
  "staleness_verdict": "spot_check_recommended",          // ← don't trust this blindly
  "path_drift": { "missing": ["src/auth/middleware.py"] }, // ← that file moved weeks ago
  "commit_drift_count": 12                                 // ← 12 commits since last confirmed
}
// So the model says so, calls memory_update to repoint the path, and answers from the
// corrected memory. The rot is caught at retrieval — not buried inside a confident wrong answer.
```

**Three things bettermemory does that no other memory layer does:**

1. **Tells the model when a stored fact has rotted** — a per-hit `staleness_verdict` folding calendar age, filesystem path drift, and git commit drift into one signal.
2. **Records which sentence of the reply a memory shaped** — a claim-level audit trail, so a model's answer months later traces back to the load-bearing stored claim.
3. **Surfaces the retrievals the model never reaches for** — the search-result equivalent of a dead-letter queue, so over-surfaced or stale memories become visible instead of silently dragging on every turn.

Retrieval is opt-in; writes about *you* always stage for your confirmation. Works with Claude Code, Claude Desktop, Cursor, Continue, Cline, and any MCP client.

## What it looks like in practice

**Day one.** You say: *"When I ask for a tutorial, I want runnable code, not screenshots of an IDE."* Claude calls `memory_write(category="user-inference")`. Because it's a claim about *you*, the write stages pending and Claude asks: *"Want me to remember that?"* You confirm. A markdown file lands at `~/.claude-memory/`.

**Week two**, fresh session: *"Walk me through pandas from zero to hero."* The phrase is ambiguous in a way your stored preference resolves, so Claude calls `memory_search`, finds it, and tells you up front: *"Using your stored preference for code-driven tutorials…"* before answering.

**Month three:** *"Where does the auth middleware live again?"* — the scenario above. The hit comes back `spot_check_recommended` with `path_drift.missing`, the file having been renamed weeks ago. Claude says so, repoints the path, and answers from the corrected memory.

**Month six:** *"What's the difference between `find` and `fd`?"* Generic question. Claude doesn't search. The reply isn't tinted by months of accumulated personal context. **That's the design point** — opt-in retrieval means memory helps when it's relevant and stays out of the way when it isn't.

## Features

### The verification surface — the lane bettermemory runs in

- **Per-hit staleness verdict.** Every retrieval carries `staleness_verdict ∈ {fresh, spot_check_recommended, spot_check_required}`, derived from three orthogonal drift signals: calendar verification age, filesystem path drift (cited paths that no longer exist), and commit drift against the memory's origin repo. Hits also carry an inline `path_drift = {checked, missing, verified}` so the model can `memory_update` the rotted path or `memory_verify` the rest without a round-trip.
- **Claim-level audit trail.** `memory_record_use(claim_excerpts=[…])` logs the load-bearing sentence each retrieved memory shaped. A Stop hook catches retrievals the model forgot to log via a precision-tuned substring match; anything neither path covers falls back to an `auto` attribution. Three tiers, one event per retrieval, no double-counting.
- **Cold-endorsement visibility.** `memory_health` surfaces memories the ranker keeps serving but the model never *deliberately* uses — a dead-letter queue for retrieval. No other memory system exposes this.
- **Silent-miss probe.** `memory_audit_turn` re-runs the ranker over the just-finished turn and flags high-relevance hits the model *should* have retrieved but didn't — closing the loop on contract slippage that is otherwise invisible.
- **Confirmation tier for claims about you.** `category="user-inference"` *always* stages pending, regardless of config. Misattributed preferences stick for months, so you always hold the veto on claims about yourself.
- **Write-time groundedness gate.** Opt-in `memory_write(groundedness_check=True, …)` flags sentences in a proposed memory that don't anchor to the conversation that produced them — catching extraction hallucinations at write time.
- **Negative-results suppression.** A hit recently `ignored` or `contradicted` and not since `applied` carries `recent_negative_outcomes`, so the model doesn't re-suggest junk you already rejected.

### The fundamentals — done right

- **Opt-in retrieval.** `memory_search` is a deliberate tool call. The default per turn is *not* to search — false positives cost more than false negatives.
- **Proactive writing, structurally safe.** A durability check, content/tombstone dedup, scope-mismatch check, and the pending tier let the model write aggressively without polluting the store.
- **Hybrid retrieval.** Four selectable rankers: `hybrid` (default — RRF over keyword + BM25, plus semantic when the embeddings extra is installed), `bm25`, `keyword`, or `semantic`. Degrades gracefully with zero extra deps.
- **Typed inter-memory links.** `supersedes` / `contradicts` / `extends` / `depends_on`, surfaced bidirectionally and auto-resolved on retrieval.
- **Tombstones, not deletes.** Removed memories keep their `removed_reason`; tombstone-aware dedup catches paraphrases months later. Reversible via `memory_restore`.
- **Auto-scoped by repo and worktree.** Memories carry the repo URL and worktree root; search filters by both. Sibling worktrees stay isolated; cross-project queries are explicit.
- **Cross-machine sync, no cloud.** `bettermemory sync` is a thin git wrapper — your laptop and workstation share one store over your own remote, no SaaS account.
- **Plain-text storage.** No database, no opaque blob. Files are `grep`-able, `git`-versionable, hand-editable.

## How it compares

bettermemory occupies the file-backed, retrieval-on-demand corner of the design space. Most other projects optimize for *more memory, faster retrieval*; bettermemory optimizes for *memory the model can decide whether to trust*. Every other memory layer — Mem0, Zep, Letta, claude-mem, Anthropic's reference server, the SQLite-FTS5 clones — stores facts well. None of them tell the model *when a fact has rotted*, *which sentence a memory shaped*, or *which retrievals the model never reaches for*.

| | bettermemory | mem0 | Letta (MemGPT) | Zep / Graphiti | Anthropic native | claude-mem |
|---|---|---|---|---|---|---|
| Storage | Markdown + YAML on disk | Vector DB (+ optional graph) | Tiered core/recall/archival | Temporal knowledge graph | Filesystem + auto-managed | SQLite + ChromaDB |
| Retrieval | MCP tool-call, opt-in per turn | Explicit `search()` API | Tool-routed across tiers | `search()` over temporal graph | Auto-injected + on-demand | KG + vector |
| **Per-hit staleness verdict** | **Calendar + path + commit drift, per result** | Temporal `created`/`updated` | — | Bi-temporal (`t_valid`+`t_invalid`) | Async refresh, no per-hit signal | — |
| **Claim-level audit trail** | **`memory_record_use(claim_excerpts=…)`** | — | — | — | — | — |
| **User-inference confirmation tier** | **Claims about you always stage for veto** | Auto-extraction | Background manager | Auto-ingest | Auto-write | Auto-compress |
| **Cold-endorsement visibility** | **`memory_health` surfaces never-used hits** | — | Letta Evals (offline) | — | — | — |
| **Run-state journal tier** | **`episode_*` family, TTL-pruned, promote-gated** | Working memory (auto-decayed) | Core/recall/archival tiers | Time-versioned in one graph | Auto-injected mix | Per-session blob |
| Inter-memory links | Typed (4 kinds) | Graph edges (optional Neo4j) | Implicit via tiers | Graph edges (Graphiti) | — | KG |
| Cross-host sync | Built-in git wrapper, BYO remote | Self-host or managed cloud | Self-host or managed cloud | Self-host or managed cloud | Provider-managed | Per-machine |
| License | MIT | Apache-2.0 | Apache-2.0 | Apache-2.0 (Graphiti) | Closed | MIT |

The bolded rows are the lane bettermemory deliberately runs in. The dashes elsewhere aren't gaps — they're choices; most of those projects optimize for objectives bettermemory doesn't (multi-tenant cloud, graph reasoning, transparent in-context memory). Pick what fits.

## On-disk format

One file per memory — `grep`-able, `git`-versionable, hand-editable:

```
~/.claude-memory/2025-03-14-jupyter-tutorial-style.md
```

```markdown
---
schema_version: 1
id: 01HXYZ123ABCDEFGHJKMNPQRST
created: 2025-03-14T10:23:00+00:00
updated: 2025-03-14T10:23:00+00:00
scopes: [tools, learning-style]
confidence: high
source: explicit-statement
---
When I ask for a "zero to hero" tutorial, I want a hands-on
walkthrough with code I can run, not a tour of the IDE.
```

A richer memory — captured under a repo, verified against a couple of paths, superseding an earlier record — adds optional frontmatter only when populated:

```markdown
---
schema_version: 1
id: 01HXYZ456DEFGHJKMNPQRSTVWX
created: 2025-04-02T09:11:00+00:00
updated: 2025-05-10T14:02:00+00:00
scopes: [projects:bettermemory, infrastructure]
confidence: high
source: explicit-statement
category: fact
origin:
  cwd: /Users/m/code/bettermemory
  repo: https://github.com/0Mattias/bettermemory
  branch: main
  worktree_root: /Users/m/code/bettermemory
last_verified_at: 2025-05-10T14:02:00+00:00
verified_paths:
  - src/bettermemory/health.py
links:
  - {type: supersedes, target_id: 01HXYZ111AAABCDEFGHJKMNPQR}
---
The compute_health rollup honors the latest silent_miss_cutoff
event and drops earlier turn_audited / search_miss rows.
```

Tombstones move to `.tombstones/` keeping their `removed_reason`. **Episodes** — the sibling tier for journal-shaped run-state (loop-iteration takeaways, "what we tried") — live under `episodes/<session_id>/<ulid>.md`, auto-pruned at 30 days, and stay invisible to `memory_search` so durable retrieval never gets polluted by run-state.

Storage resolves to `$BETTERMEMORY_DIR` if set, else `./.claude-memory/` if it exists, else `~/.claude-memory/`. Project-scoped overrides global; cross-project queries are explicit (`auto_scope=false`).

## Tools

**24 MCP tools — 18 registered by default.** Six curation / power-user tools (`memory_health`, `memory_acknowledge_miss`, `memory_rename_scope`, `memory_restore`, `memory_list_tombstones`, `memory_proposals`) register only under `[behavior] full_tool_surface = true` — except `memory_proposals`, which also surfaces when `[proposals]` is enabled. Five have a direct `bettermemory` CLI counterpart on the lean default — `health`, `tombstones list` / `tombstones restore`, `rename-scope`, and `proposals` — so trimming them keeps the per-turn tool-description context lean without giving up the capability. `memory_acknowledge_miss` is the exception: its per-event ack stays MCP-only, with the CLI offering the blunter bulk `consolidate --acknowledge-misses-before` cutoff instead.

- **Retrieval** — `memory_search` (incl. `since_prior_session`), `memory_show`, `memory_list`, `memory_scope_overview`
- **Writing** — `memory_write` (+ `memory_write_confirm` / `memory_write_cancel` for the staged flow), `memory_update`
- **Lifecycle** — `memory_remove`, `memory_restore`, `memory_list_tombstones`
- **Verification** — `memory_verify`
- **Curation** — `memory_record_use`, `memory_health`, `memory_audit_turn`, `memory_acknowledge_miss`, `memory_rename_scope`, `memory_proposals`
- **Session-local** — `memory_scope_disable`, `memory_scope_enable`
- **Episodes** (journal / run-state tier) — `episode_write`, `episode_handoff`, `episode_search`, `episode_promote`

Full signatures, defaults, and return shapes in [`docs/api.md`](docs/api.md).

## CLI

The `bettermemory` script *is* the MCP server with no arguments (stdio). It also exposes offline tooling:

```sh
bettermemory init --client claude-code   # register with a client (idempotent)
bettermemory doctor                      # diagnose install state
bettermemory health                      # curation rollup (text or --json)
bettermemory tombstones list|restore ID|prune    # inspect / restore / hard-delete removed memories
bettermemory rename-scope OLD NEW        # bulk-rename a scope tag across the store
bettermemory proposals list|accept ID --scope S|dismiss ID   # review the write-reflex queue
bettermemory consolidate                 # dedup + demote + cold-scope + typo passes (dry-run)
bettermemory consolidate --llm           # + an LLM pass: merges, contradictions, date rewrites, demotions
bettermemory consolidate --llm --from-transcript PATH   # propose new memories from a transcript
bettermemory eval                        # memory_helped_rate / endorsement_rate / silent_miss_rate
bettermemory ingest                      # import Claude Code's auto-memory directory
bettermemory reindex [--embeddings]      # rebuild the FTS5 index (and re-embed)
bettermemory sync push | pull | auto     # git-based cross-host sync
bettermemory ui                          # local FastAPI curation UI ([ui] extra)
bettermemory episodes list | prune       # inspect the journal tier
bettermemory export                      # backup
```

Run `bettermemory <command> --help` for the flags on each; the [API docs](docs/api.md) cover the MCP tool surface.

## Coexistence with Claude Code's built-in memory

Claude Code 2.x ships its own filesystem-backed memory that auto-injects into the system prompt. Installing the plugin lands an anchor in the system prompt — *"persistent memory lives in this server's MCP tools, don't fragment it across ad-hoc files"* — that keeps the model from drifting back to the built-in directory mid-conversation. Manual installs can paste [`docs/system_prompt.md`](docs/system_prompt.md) into `CLAUDE.md` for the same effect. Already have auto-memory files? `bettermemory ingest` imports them once.

## Performance

Below ~500 memories, search loads everything (byte-stable to 1.x). Above the threshold (`BETTERMEMORY_INDEX_THRESHOLD`), an SQLite FTS5 inverted index pre-filters candidates, capping per-search work regardless of corpus size. Files stay canonical; the index is a derived cache at `<store>/.index.sqlite`, kept live by every write/update/remove/restore/rename. Out-of-band edits (hand-editing a file, `sync pull`, restore-from-backup) bypass the hook — the server warns at startup when it detects the divergence, and `bettermemory reindex` rebuilds from disk.

**Semantic retrieval** is optional. Hybrid (RRF over keyword + BM25) is the zero-dep default; add the semantic third leg with one extra:

```sh
uv pip install -e ".[embeddings]"       # sentence-transformers + PyTorch (~500MB; well-trodden)
uv pip install -e ".[embeddings-fast]"  # fastembed + ONNX Runtime (~50MB; same retrieval surface)
```

`[behavior] semantic_provider = "auto"` picks torch when installed, else fastembed, else falls back to keyword. Run `bettermemory reindex --embeddings` after switching providers.

## Config

`config.toml` is created on first run under `platformdirs` (`~/Library/Application Support/bettermemory/` on macOS, `~/.config/bettermemory/` on Linux, `%LOCALAPPDATA%\bettermemory\` on Windows). Defaults are sensible — most users never edit it. The knobs that matter: `behavior.search_mode` (`hybrid` default), `behavior.semantic_provider` (`auto`/`torch`/`fastembed`), `behavior.require_write_confirmation` (off by default for solo setups; `user-inference` always stages regardless), `behavior.verification_stale_days` (30), and `telemetry.enabled` (flip to `false` to disable the event log).

## Limitations

- **No encryption.** Memories are plaintext on disk. Don't store secrets; use OS-level disk encryption if you need it.
- **No automatic sync conflict resolution.** `bettermemory sync` delegates to git; content conflicts surface as normal merge conflicts.
- **Web UI is read-mostly.** Curation and one-click `memory_verify`; writes happen in-conversation.
- **Disabled scopes don't survive restart.** Intentional — each session starts fresh.
- **Multi-process locking is a no-op on Windows.** Single-process recommended there.

## Out of scope

- **Cloud sync as a service.** Sync is git-based; bring your own remote.
- **Cross-user sharing / RBAC.** Single-user tool. Team scopes are deferred.
- **Silent transcript extraction.** Writes behind your back defeat the opt-in contract. `bettermemory consolidate --llm --from-transcript` is the audited alternative — explicit, dry-run by default, every proposal a reviewable diff with provenance.

## Design notes

Stored facts rot. A memory written last quarter about "the auth middleware in `src/auth/middleware.py`" doesn't know the file moved to `src/auth/jwt.py`. A preference captured in March may have been provisional. A config fact may have been superseded by a commit two days ago. Most memory systems treat retrieval as a black box that returns text — once the text comes back, the model is on its own to judge it.

bettermemory's answer is to surface the *provenance and freshness of every memory at retrieval time*: calendar age, path drift, commit drift, the supersession chain, and your prior outcomes. The opt-in retrieval contract, the confirmation tier, the typed links, the groundedness gate, the silent-miss probe, and the cold-endorsement rollup all exist to make that loop trustworthy enough to actually depend on.

## Learn more

- [`docs/api.md`](docs/api.md) — every tool's signature, defaults, and return shape.
- [`docs/clients.md`](docs/clients.md) · [`docs/installation.md`](docs/installation.md) — per-client setup and install paths.
- [`docs/eval.md`](docs/eval.md) — the three metrics bettermemory wants the field to adopt: `memory_helped_rate`, `endorsement_rate`, `silent_miss_rate`. Defined for any system with the right telemetry, not just this one.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — what's planned and what's deliberately out of scope.
- [`docs/incidents/`](docs/incidents/) — public postmortems for memory-rot bugs the verification surface should have caught. The contract puts a verdict in every retrieval; we owe an accounting when the verdict was wrong.
- [`CHANGELOG.md`](CHANGELOG.md) — the source of truth for what shipped, release by release.

Built by Mattias Rask. MIT licensed — see [LICENSE](LICENSE).
