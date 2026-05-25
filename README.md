# bettermemory

[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97757)](plugin/README.md)
[![PyPI](https://img.shields.io/pypi/v/bettermemory.svg)](https://pypi.org/project/bettermemory/)
[![CI](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg)](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Memory you can verify.**

MCP-native memory for AI coding agents. Every retrieved fact carries a **staleness verdict** — calendar age + filesystem path drift + git commit drift — so the model knows when a stored memory has rotted before it relies on it. Retrievals are logged with three attribution tiers — model-explicit (with claim-level excerpts), Stop-hook substring match, or an auto-fallback if neither fires — so a reply months later can be traced back to the load-bearing stored claim wherever the model or hook recorded one. Retrieval is opt-in; writes about *you* always stage for confirmation. Stored as plain markdown on disk; MIT-licensed; works with Claude Code, Cursor, Continue, Cline, and any MCP client.

> **Why this exists.** Every other memory layer — Mem0, Zep, Letta, claude-mem, Anthropic's reference server, the dozen SQLite-FTS5 MCP clones — stores facts. None of them tell the model *when a stored fact has rotted*, *which sentence in the reply a memory shaped*, or *which retrievals the model never deliberately reaches for*. bettermemory does all three. See [Where it fits](#where-it-fits) for the comparison.

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

**Month three:** *"Where does the auth middleware live again?"* Claude retrieves a memory that cites `src/auth/middleware.py`. The hit comes back with `staleness_verdict="spot_check_recommended"` and `path_drift={missing: ["src/auth/middleware.py"]}` — the file was renamed weeks ago. Claude says so, calls `memory_update` to point at the new path, and answers from the corrected memory. The rot was caught at retrieval, not buried in a wrong answer.

**Month six:** *"What's the difference between `find` and `fd`?"* Generic question. Claude doesn't search. The reply isn't tinted by months of accumulated personal context. That's the design point.

## Features

### Verification surface (the differentiated lane)

- **Per-hit staleness verdict.** Every retrieval carries `staleness_verdict ∈ {fresh, spot_check_recommended, spot_check_required}`, derived from three orthogonal drift signals: calendar verification age, filesystem path drift (paths cited in the body that no longer exist), and commit drift against the memory's origin repo. The model can decide *whether to trust the memory before relying on it*. Hits also carry an inline `path_drift = {checked, missing, verified}` list when drift is detected, so the model can `memory_update` the rotted path or `memory_verify` the rest without a `memory_show` round-trip.
- **Claim-level audit trail.** `memory_record_use(claim_excerpts=[…])` logs the load-bearing sentence each retrieved memory shaped — when the model deliberately calls it. The Stop hook (`bettermemory audit-turn`) catches retrievals the model forgot to log by running a precision-tuned substring match against the assistant reply and emitting a `record_use(attribution="hook")` event with the matched phrase. Retrievals that neither path covers within ~2 turns fall back to `attribution="auto"` with no excerpt — present in the count of the dead-letter detector (`endorsement_rate`) but not the load-bearing numerator. Three tiers, one event per retrieval, no double-counting. Explicit `ignored` / `contradicted` / `corrected` overrides record nuance.
- **Endorsement-debt visibility.** `memory_health` surfaces memories the ranker keeps surfacing but the model never *deliberately* reaches for — the search-result equivalent of a dead-letter queue. No other memory system exposes this.
- **Silent-miss probe.** `memory_audit_turn` re-runs the model's ranker over the just-completed turn and flags high-relevance hits the model *didn't* retrieve. Closes the loop on retrieval-contract slippage that is otherwise structurally invisible.
- **Confirmation tier for claims about you.** `category="user-inference"` *always* stages pending regardless of config — misattribution of preferences sticks for months, so the user always has the veto on claims about themselves.
- **Write-time groundedness gate.** Opt-in `memory_write(groundedness_check=True, source_transcript=…)` flags sentences in the proposed memory that don't anchor to the conversation that produced them. Catches LLM-extraction hallucinations at write time.
- **Negative-results suppression.** A hit that was `ignored` or `contradicted` recently and not since `applied` carries `recent_negative_outcomes`. The model doesn't re-suggest junk the user already rejected.

### The rest

- **Opt-in retrieval.** `memory_search` is a tool the model calls deliberately. The default per turn is not to call it.
- **Proactive writing with structural gates.** Aggressive writing is safe because a durability check, content/tombstone dedup, scope-mismatch check, and the pending tier guard the writes.
- **Hybrid retrieval.** Four selectable rankers: `hybrid` (default since 2.6.8 — RRF over keyword + BM25, plus semantic when the embeddings extra is installed; degrades gracefully without it), `bm25` (Okapi BM25), `keyword` (legacy TF + coverage; no IDF), or `semantic` (sentence-transformers, requires extra).
- **Typed inter-memory links.** `supersedes` / `contradicts` / `extends` / `depends_on`. Surfaced bidirectionally on `memory_show`.
- **Tombstones, not deletes.** Removed memories keep their `removed_reason`. Tombstone-aware dedup catches paraphrases six months later. Reversible via `memory_restore`.
- **Auto-scoped by repo and worktree.** Memories written from a git checkout carry the repo URL and worktree root; `memory_search` filters by both. Sibling worktrees of the same repo are isolated.
- **Cross-machine sync, no cloud.** `bettermemory sync` is a thin git wrapper — your laptop and workstation share the same memory via your own git remote, no SaaS account required.
- **Plain-text storage.** No database, no opaque blob. Files are `grep`-able, `git`-versionable, hand-editable.

## Where it fits

bettermemory occupies the file-backed, retrieval-on-demand corner of the memory-system design space. Most other projects optimize for *more memory, faster retrieval*; bettermemory optimizes for *memory the model can decide whether to trust*. The table sketches what each system does in its own terms — pick the one whose defaults match what you need.

| | bettermemory | mem0 | Letta (MemGPT) | Zep / Graphiti | Anthropic native (Claude Code + Dreaming) | claude-mem |
|---|---|---|---|---|---|---|
| Storage | Markdown + YAML on disk | Vector DB (+ optional graph) | Tiered core/recall/archival | Temporal knowledge graph | Filesystem + auto-managed | SQLite + ChromaDB |
| Retrieval | MCP tool-call, opt-in per turn | Explicit `search()` API | Tool-routed across tiers | `search()` over temporal graph | Auto-injected + on-demand | KG + vector |
| **Per-hit staleness verdict** | **Calendar + path + commit drift, exposed per result** | Temporal `created`/`updated` | — | Bi-temporal (`t_valid` + `t_invalid`) | Dreaming refreshes asynchronously, no per-hit signal | — |
| **Claim-level audit trail** | **`memory_record_use(claim_excerpts=…)`** | — | — | — | — | — |
| **User-inference confirmation tier** | **Claims about the user always stage for veto** | Auto-extraction (no staging) | Background memory manager | Auto-ingest | Auto-write | Auto-compress |
| **Endorsement-debt visibility** | **`memory_health` surfaces never-deliberately-used hits** | — | Letta Evals (offline) | — | — | — |
| Inter-memory links | Typed (supersedes / contradicts / extends / depends_on) | Graph edges (optional Neo4j) | Implicit via tiers | Graph edges (Graphiti) | — | KG |
| Cross-host sync | Built-in git wrapper, BYO remote | Self-host or managed cloud | Self-host or managed cloud | Self-host or managed cloud | Provider-managed | Per-machine |
| License | MIT | Apache-2.0 | Apache-2.0 | Apache-2.0 (Graphiti) | Closed | MIT |

The four bolded rows are the lane bettermemory deliberately runs in. The dashes elsewhere aren't gaps in those projects — they're choices, and most of those projects optimize for objectives bettermemory doesn't (multi-tenant cloud, graph reasoning over evolving facts, transparent in-context memory). Pick what fits.

## Coexistence with Claude Code's built-in memory

Claude Code 2.x ships its own filesystem-backed memory that auto-injects into the system prompt. Installing the plugin lands the *"persistent memory between sessions lives in this server's MCP tools, do not fragment it across ad-hoc files alongside"* anchor in the system prompt, which keeps the model from drifting back to the built-in directory mid-conversation. Manual installs can paste [`docs/system_prompt.md`](docs/system_prompt.md) into `CLAUDE.md` for the same effect.

## On-disk format

One file per memory:

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

A memory carrying the optional fields — captured under a project repo, verified against a couple of paths, and superseding an earlier record — looks like:

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
  - docs/eval.md
links:
  - {type: supersedes, target_id: 01HXYZ111AAABCDEFGHJKMNPQR}
---
The `compute_health` rollup honors the latest `silent_miss_cutoff`
event in the log and drops earlier `turn_audited` / `search_miss`
rows from both numerator and denominator.
```

Tombstones move to `.tombstones/`. Optional fields are written only when populated: `origin`, `last_verified_at`, `category`, `verified_paths` / `verified_commits` / `verified_versions`, and `links`.

Storage resolution: `$BETTERMEMORY_DIR` if set, else `./.claude-memory/` if it exists, else `~/.claude-memory/`. Project-scoped overrides global; cross-project queries are explicit (`auto_scope=false`).

## Tools

18 MCP tools, grouped:

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
bettermemory consolidate --llm              # +LLM pass: merges, contradictions, date rewrites, demotions
bettermemory consolidate --llm --from-transcript PATH  # +propose new memories from a Claude Code session JSONL / plain transcript
bettermemory consolidate --llm --apply      # interactive accept; or --apply --yes for batch
bettermemory consolidate --acknowledge-debt                        # one-shot clear of the endorsement_debt curation bucket
bettermemory consolidate --acknowledge-misses-before <ISO_TS>      # invalidate stale silent-miss events after a fix lands
bettermemory eval                           # memory_helped_rate / endorsement_rate / silent_miss_rate
bettermemory eval --since 7d --scope tools  # narrow to a window or a scope
bettermemory eval --tool-usage              # per-MCP-tool call-count rollup ("which tools is the model reaching for?")
bettermemory eval --threshold-sweep         # counterfactual replay of logged search_miss events under alternative rules
bettermemory ingest                         # import Claude Code's auto-memory directory into the store
bettermemory reindex                        # rebuild FTS5 index from on-disk files
bettermemory reindex --embeddings           # also re-embed bodies into the active provider's cache
bettermemory sync init --remote URL         # git-based cross-host sync
bettermemory sync push | pull | auto | status
bettermemory ui                             # local FastAPI curation UI (needs [ui] extra)
bettermemory tombstones list | prune
bettermemory export                         # backup
```

## Performance

Below ~500 memories, search uses `load_all` (byte-stable to 1.x). Above the threshold (`BETTERMEMORY_INDEX_THRESHOLD`), an SQLite FTS5 inverted index pre-filters candidates, capping per-search work regardless of corpus size. Files stay canonical; the index is a derived cache at `<store>/.index.sqlite`, kept live by Store hooks. Recovery from hand-edits: `bettermemory reindex`.

### Embeddings for semantic / hybrid retrieval

Hybrid retrieval (RRF over keyword + BM25) is the default and ships with zero extra deps — the hybrid mode gracefully degrades to keyword+BM25 fusion when no embedding extra is installed. To add the semantic third leg (paraphrase matching via sentence-transformers cosine), install one of two optional extras:

```sh
uv pip install -e ".[embeddings]"       # sentence-transformers + PyTorch (~500MB; the well-trodden path)
uv pip install -e ".[embeddings-fast]"  # fastembed + ONNX Runtime (~50MB total; same retrieval surface)
```

`[behavior] semantic_provider = "auto"` (the default) picks torch when `[embeddings]` is installed, otherwise fastembed, otherwise falls back to Jaccard for the dedup path and keyword for retrieval. Set `"torch"` or `"fastembed"` to pick explicitly. The persistent embedding cache is provider-namespaced (`.embeddings.<model>.npz` for torch, `.embeddings.fastembed.<model>.npz` for fastembed) so flipping providers gives a fresh file rather than mixing incompatible vectors. After flipping providers, run `bettermemory reindex --embeddings` to populate the new cache before the next dedup-heavy operation.

## Config

`config.toml` is created on first run under `platformdirs`:

- macOS: `~/Library/Application Support/bettermemory/config.toml`
- Linux: `~/.config/bettermemory/config.toml`
- Windows: `%LOCALAPPDATA%\bettermemory\config.toml`

Defaults are sensible — most users never edit it. Knobs that matter: `behavior.search_mode` (`hybrid` default since 2.6.8, plus `keyword` / `bm25` / `semantic`), `behavior.semantic_provider` (`auto` / `torch` / `fastembed`), `behavior.require_write_confirmation` (per-write veto; off by default for solo setups, but `category="user-inference"` always goes pending regardless), `behavior.verification_stale_days` (default 30), `telemetry.enabled` (flip to `false` to disable the event log), `telemetry.log_queries_verbatim` (default `false`; off-by-default opt-in for storing raw `memory_search` query text — see Privacy below).

## Limitations

- **No encryption.** Memories are plaintext on disk. Don't store secrets; use OS-level disk encryption if you need it.
- **No automatic conflict resolution for sync.** `bettermemory sync` delegates to git. True content conflicts surface as normal merge conflicts.
- **Web UI is read-mostly.** Curation and one-click `memory_verify` only. Writes happen in-conversation.
- **Disabled scopes don't survive restart.** Intentional; each session starts fresh.
- **Multi-process locking falls back to no-op on Windows.** Single-process recommended there.

## Out of scope

- **Cloud sync as a service.** Sync is git-based; bring your own remote (GitHub, Forgejo, bare repo over SSH).
- **Cross-user sharing.** Single-user tool. Team scopes are deferred.
- **Silent / autonomous memory extraction from transcripts.** Writes that happen behind the user's back defeat the opt-in retrieval contract. `bettermemory consolidate --llm --from-transcript PATH` is the audited alternative: explicit command, dry-run by default, every proposed memory rendered as a diff with a `source_excerpt` provenance line, `--apply` refuses without `--yes` (batch) or interactive per-proposal y/N.

## Design notes

The motivating observation is that stored facts rot. A memory written last quarter about "the auth middleware in `src/auth/middleware.py`" doesn't know the file moved to `src/auth/jwt.py`. A preference captured from a conversation last March may have been provisional. A configuration fact may have been superseded by a commit two days ago. Most memory systems treat retrieval as a black box that produces text; once the text comes back, the model is on its own to decide whether to trust it.

bettermemory's response is to surface the *provenance and freshness of every memory at retrieval time* — calendar age, filesystem path drift, commit drift against the originating repo, the chain of supersession links, and the user's prior outcomes (`applied` / `ignored` / `contradicted`). The model gets a signal to spot-check before relying. The opt-in retrieval contract, the user-inference confirmation tier, the typed links, the groundedness gate, the silent-miss probe, and the endorsement-debt rollup all exist to make that loop trustworthy enough to actually depend on.

## Further reading

- [`docs/eval.md`](docs/eval.md) — the three metrics bettermemory wants the field to adopt: `memory_helped_rate`, `endorsement_rate`, `silent_miss_rate`. Defined for any system that exposes the right telemetry, not just this one.
- [`docs/incidents/`](docs/incidents/) — public postmortems for memory-rot bugs the verification trifecta should have caught. The contract puts the verdict in every retrieval response; we owe a public accounting when the verdict was wrong.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — what's planned (comparative-publication run of `bettermemory eval` against Mem0 / claude-mem / agentmemory; operational polish) and what's deliberately out of scope (managed cloud, multi-user RBAC, graph backend). The fastembed extra, `bettermemory eval` CLI, `consolidate --llm` Dreaming-defense pass, and `consolidate --llm --from-transcript` writing-reflex closure all shipped between 2.5.0 and 2.6.0. 2.7.0 added the calibration trio (`eval --tool-usage` for "which tools is the model actually reaching for?", `eval --threshold-sweep` for counterfactual silent-miss replay), the `memory_scope_overview` delta field (`curation_pending_new_since_last_session`), and the `bettermemory ingest` bridge from Claude Code's auto-memory directory. 2.7.3 closed a silent-miss false-positive class (same-repo cwd suppression) and added `consolidate --acknowledge-debt` for retroactively clearing the endorsement-debt curation bucket; the unreleased follow-up adds `consolidate --acknowledge-misses-before <ISO_TS>` so the historical miss batch invalidated by the cwd-suppression fix can be dropped from the rollup without rewriting the events log.

Built by Mattias Rask. MIT licensed — see [LICENSE](LICENSE).
