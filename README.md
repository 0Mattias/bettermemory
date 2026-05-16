# bettermemory

[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97757)](plugin/README.md)
[![PyPI](https://img.shields.io/pypi/v/bettermemory.svg)](https://pypi.org/project/bettermemory/)
[![CI](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg)](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Verification-grade persistent memory for Claude Code. Retrieved on demand, not force-fed into every prompt.**

bettermemory is a [Claude Code plugin](plugin/README.md) (and a standalone MCP server for any other client) that fixes the structural failure mode shared by every other LLM memory tool in the May-2026 landscape. Those systems auto-inject stored facts into every conversation, have no sense of which facts are stale or relevant, hallucinate "memories" at write time, and provide no audit trail when a stored claim shaped a reply. Ask for a Python tutorial, get an answer tinted by your home-lab notes. Ask a generic shell question, get advice coloured by a preference you stated months ago. Stale facts get dispensed confidently with no way to spot-check the source.

bettermemory inverts the contract on every axis. The model calls `memory_search` only when context is actually needed. Every retrieval ships with three structured staleness signals (`verification`, `path_drift`, `commit_drift`) so the model can spot-check before relying on what it pulled up. Every retrieved hit carries `recent_negative_outcomes` if it was rejected before and not since validated, so the model doesn't keep re-suggesting the same junk. The optional groundedness gate on `memory_write` flags sentences that don't anchor to the conversation that produced them — the HaluMem benchmark, operationalised inline. Memories live as plain markdown plus YAML on disk, so you can `grep` them, `git log` them, hand-edit them, and sync them across hosts via the built-in `bettermemory sync` (a thin git wrapper). A separate `memory_health` view, the `bettermemory consolidate` offline curation CLI, and a local web UI tell *you* what is dead weight, what has drifted, and what to retire — instead of letting the store grow into a haunted closet of half-true notes.

## Install in Claude Code

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

That is it. Claude Code starts the MCP server, loads a system-prompt-level [skill](plugin/skills/bettermemory/SKILL.md) carrying the opt-in retrieval and proactive writing policies, and on the next turn the model has all 17 memory tools and the discipline to use them correctly. For other clients (Claude Desktop, Cursor, Continue, Cline) and manual setup, see [§ Other MCP clients](#other-mcp-clients) below.

## How it compares

bettermemory vs. the rest of the May-2026 memory-MCP landscape:

| Capability | bettermemory | mem0 | Letta (MemGPT) | Zep / Graphiti | Cognee | Anthropic Memory Tool |
|---|---|---|---|---|---|---|
| **Retrieval contract** | Opt-in (model calls `memory_search`) | Auto-injected | Tiered tool-routed | Auto-injected | Auto-injected | List+read, no search |
| **Hybrid retrieval** | BM25 + Jaccard + semantic via RRF | Vector only | Tool-routed | Graph hybrid | Hybrid + ontology | None |
| **Claim-level provenance** | Yes (`claim_excerpts`) | No | No | No | No | No |
| **Verification with attestation** | Yes (`verified_paths` / `commits` / `versions`) | No | No | Partial (recency) | No | No |
| **Write-time hallucination gate** | Yes (`groundedness_check`) | No | No | No | No | No |
| **Three-axis staleness signals** | Yes (`verification`, `path_drift`, `commit_drift`) + rollup `staleness_verdict` | No | No | Bi-temporal only | No | No |
| **Negative-results suppression** | Yes (`recent_negative_outcomes` on hits) | No | No | No | No | No |
| **Offline consolidation** | `bettermemory consolidate` (no 2nd agent) | No | Sleep-time agent | Partial | Partial | No |
| **Typed inter-memory links** | Yes (supersedes / contradicts / extends / depends_on) | No | No | Graph edges | Graph edges | No |
| **FTS5 inverted index** | Yes (files canonical, index derived) | Vector-only | Per-tier | Graph | Per-store | None |
| **Cross-host sync** | Yes (`bettermemory sync`, git-based) | Cloud-only | Cloud-only | Cloud-only | Cloud-only | None |
| **Local web UI for curation** | Yes (`bettermemory ui`) | No | No | Partial | Partial | No |
| **Plain-text storage** | Yes (grep, git, hand-edit) | No | No | No | No | Yes (host-implemented) |
| **Confirmation tier for claims about you** | Yes (`category="user-inference"`) | No | No | No | No | No |
| **Open source** | MIT | Apache-2.0 | Apache-2.0 | Apache-2.0 (Graphiti) | Apache-2.0 | Closed |
| **Production junk-rate report** | n/a | **97.8%** ([#4573](https://github.com/mem0ai/mem0/issues/4573)) | n/a | n/a | n/a | n/a |

Bold cells in the bettermemory column mark capabilities **no other system in the field has**.

## What it looks like in practice

Day one. You tell Claude something:

> *"When I ask for a tutorial, I want runnable code, not screenshots of an IDE."*

Claude calls `memory_write(category="user-inference", scopes=["learning-style"], …)`. Because the memory captures a claim about *you*, the write goes pending. Claude asks: *"Want me to remember that you prefer hands-on tutorials with runnable code?"* You confirm. The fact lands at `~/.claude-memory/` as a markdown file you can read, edit, or delete.

Week two, in a fresh session. You ask:

> *"Walk me through pandas from zero to hero."*

The phrase *"zero to hero tutorial"* is the kind of ambiguity stored preferences could resolve, so Claude calls `memory_search`, surfaces the stored learning-style memory, and tells you up front: *"Using your stored preference for code-driven tutorials…"* before answering. Compare with auto-injection memory, which would have done the same thing silently, even on *"what is the capital of France?"*

Month three. You ask about an unrelated tool:

> *"What is the difference between `find` and `fd`?"*

This question is generic. Claude does not call `memory_search`. The reply is pristine generic-shell prose, untainted by months of accumulated personal context. That is the whole design.

## What you get

- **Opt-in retrieval.** `memory_search` is a tool the model calls when context is needed. The default is not to call it. Generic questions stay generic.
- **Proactive writing with structural gates.** `memory_write` is a routine reflex — the model captures whenever something durable enters the conversation. The guardrails (durability check, dedup against active + tombstones, scope-mismatch check, optional write-time groundedness gate, user-inference pending tier) make aggressive writing safe.
- **Hybrid retrieval (new in 2.0).** Four selectable rankers via `memory_search(mode=...)`: `keyword` (default, the original TF + scope + coverage + recency scorer), `bm25` (Okapi BM25 with the same scope-bonus + recency), `semantic` (sentence-transformers cosine; requires the `[embeddings]` extra), and `hybrid` (Reciprocal Rank Fusion over all of the above). Per-call override beats the per-store config default.
- **Claim-level provenance (new in 2.0).** Optional `claim_excerpts` parameter on `memory_record_use` records the load-bearing claim the model applied / ignored / contradicted / corrected from each memory. Audits trace any response back to the specific claim, not just the memory id.
- **Write-time groundedness gate (new in 2.0).** Optional `memory_write(groundedness_check=True, source_transcript=...)` walks the proposed body sentence-by-sentence against the conversation that produced it. Sentences that don't anchor to the transcript come back as `status: "ungrounded"` so the caller can rephrase. The HaluMem benchmark made operational; no other memory system runs a write-time gate.
- **Three staleness signals on every retrieval, plus a rollup verdict.** Calendar age (`verification`: never, stale, or fresh), filesystem path drift (`path_drift_checked` / `path_drift_missing`), and repo commit drift (`commit_drift_count` when the caller is in the matching repo). All three fold into a `staleness_verdict` ∈ {`fresh`, `spot_check_recommended`, `spot_check_required`}.
- **Negative-results suppression (new in 2.0).** When a hit's memory was ignored or contradicted in the last 30 days AND not since applied, the hit carries `recent_negative_outcomes`. The model sees "rejected on date X, claim was Y" and rephrases or skips rather than re-suggesting the same junk.
- **Typed inter-memory links (new in 2.0).** Memories carry `links` of type `supersedes`, `contradicts`, `extends`, or `depends_on`. Surface bidirectionally in `memory_show` (forward `links` on source, `reverse_links` on target) so retrieval consumers see relationships from either side.
- **Hand-editable storage.** Memories are markdown + YAML in `~/.claude-memory/` (or `./.claude-memory/` for project-scoped, or `$BETTERMEMORY_DIR`). No database. No opaque blob.
- **SQLite FTS5 inverted index (new in 2.0).** Files stay canonical; the index is a derived cache at `<store>/.index.sqlite`. Removes the load_all linear-scan ceiling that bites at ~5-10K memories. Kept live by Store hooks; rebuild via `bettermemory reindex`.
- **Offline consolidation CLI (new in 2.0).** `bettermemory consolidate` runs four passes against the store: near-duplicate dedup, demote-never-applied to ambient, cold-scope suggestions, scope-typo pairs. Dry-run by default; `--apply` to commit. Closes the Letta sleep-time gap without the dual-agent topology.
- **Cross-host sync via git (new in 2.0).** `bettermemory sync init/status/push/pull/auto`. Thin git wrapper with sensible `.gitignore` (excludes the derived caches), post-pull index rebuild, no commit when nothing changed. Memories follow you across machines.
- **Local web UI (new in 2.0).** `bettermemory ui` runs a small FastAPI app on `127.0.0.1:8765` surfacing the curation surfaces (memory_health rollups, dead-weight, contradictions, never-verified) plus a memory browser, detail view, and one-click verify. Gated behind the optional `[ui]` extra.
- **A curation surface as a tool *and* a CLI *and* a web page.** `memory_health` is available as an MCP tool the model calls mid-conversation, as `bettermemory health` for batch curation, and at `/health` in the web UI.
- **Tombstones, not deletes.** Removed memories keep their `removed_reason`. Tombstone-aware dedup catches a paraphrase six months later that tries to sneak the same wrong fact back in. Removals are reversible via `memory_restore`.
- **Auto-scoped by project and worktree.** Memories written from inside a git checkout carry the repo URL *and* the worktree root. `memory_search` defaults to filtering by both — sibling `git worktree add` checkouts of the same repo are isolated. Cross-project queries are explicit (`auto_scope=false`).
- **A confirmation tier for claims about you.** `memory_write(category="user-inference")` always goes pending regardless of global config. The user always gets the veto on misattribution.
- **A feedback loop.** `memory_record_use(ids, outcome)` after each response logs `applied` / `ignored` / `contradicted` / `corrected`. Auto-commits as `applied` ~2 turns after retrieval if no explicit call. Feeds `memory_health` so dead weight surfaces automatically.

## Other MCP clients

The plugin install above is the easy path for Claude Code. Equivalent setups exist for every other MCP client.

```sh
# Pick one:
uv tool install bettermemory       # recommended: isolated tool install via uv
pipx install bettermemory          # or pipx
pip install bettermemory           # or plain pip into a venv
```

Optional extras:

```sh
uv pip install 'bettermemory[embeddings]'   # sentence-transformers for semantic mode
uv pip install 'bettermemory[ui]'           # FastAPI + uvicorn for `bettermemory ui`
```

Python 3.11 through 3.14 is supported. From a clone (development): `uv pip install -e .` or `uv tool install .`.

Then register with your client in one command:

```sh
bettermemory init --client claude-code      # or: claude-desktop, cursor, continue, cline
```

That command idempotently merges the MCP server entry into the right config file. Re-running is safe. Unchanged entries are no-ops, and stale binary paths are repaired. Restart the client and ask: *"What memory tools do you have?"*

If your client is not in the supported list, run `bettermemory init` with no flags. It prints the canonical JSON snippet plus the common config locations with `[✓]` markers showing which already exist on your machine. Per-client gotchas (config paths, restart behavior, Code-Insiders, Codium, Cline variants, and project-scoped vs user-scoped patching) live in [`docs/clients.md`](docs/clients.md). The long-form install reference is in [`docs/installation.md`](docs/installation.md).

### How the policy lands at the system-prompt level

Every compliant MCP client surfaces the server's `instructions` block in its system prompt. This is verified empirically on Claude Code 2.1.x, where it appears under "MCP Server Instructions". The block carries the core policy on both axes: opt-in retrieval (when to call `memory_search`, when not to, plus the transparency and verification obligations) and proactive writing (the four triggers and the load-bearing "your job is to capture" summary), together with the confirmation-tier policy for claims about the user. Claude Code truncates that block at roughly 1.8 KB, so the body is sized to fit comfortably under the cap with detail pushed into per-tool descriptions.

The Claude Code plugin path bypasses the truncation entirely. Its [`SKILL.md`](plugin/skills/bettermemory/SKILL.md) carries the long-form policy as a system-prompt-level skill with no cap. For other clients that want the long form, [`docs/system_prompt.md`](docs/system_prompt.md) is the canonical copy-pasteable addendum (also exported as `bettermemory.SYSTEM_PROMPT_ADDENDUM` for programmatic access).

## Coexistence with Claude Code's built-in memory

Claude Code 2.x ships its own filesystem-backed memory that auto-injects stored facts into the system prompt. That is the exact failure mode bettermemory exists to fix. The two can sit on disk together, but they fragment recall: a fact stored in one is invisible to the other's tools. If you adopt bettermemory, **install the plugin**, which lands the *"persistent memory between sessions lives in this server's MCP tools, do not fragment it across ad-hoc files alongside"* anchor in the system prompt. Or paste the [addendum](docs/system_prompt.md) into your `CLAUDE.md`. That one sentence is what keeps the model from drifting back to the built-in memory directory mid-conversation.

## Tools

The full surface contract (signatures, defaults, return shapes, audit notes) lives in [`docs/api.md`](docs/api.md). The table below is the at-a-glance summary; new-in-2.0 parameters are flagged inline.

| Tool | What it does |
|---|---|
| `memory_search(query, scopes?, max_results?, expand_top?, auto_scope?, mode?)` | Rank and return memory hits. Each hit carries `relevance`, `match_terms`, `staleness_verdict`, drift counters, and (when applicable) `recent_negative_outcomes` (new in 2.0: rejection history with `claim_excerpt` per outcome type, only when not since superseded by an `applied` event). New `mode` parameter (2.0) picks the ranker: `keyword` (default, byte-stable to 1.x), `bm25`, `semantic`, or `hybrid` (RRF fusion). |
| `memory_show(id)` | Full body, full `verification` block, `path_drift` report, `commit_drift` block, plus (new in 2.0) `links` and `reverse_links` for typed inter-memory edges. |
| `memory_write(content, scopes, confidence?, source?, category?, force?, acknowledge_transient?, acknowledge_scope_mismatch?, groundedness_check?, source_transcript?, acknowledge_ungrounded?)` | Create a new memory. Runs the durability check, dedup against active and tombstones, scope-mismatch check, and (new in 2.0) the optional write-time groundedness gate when `groundedness_check=True` plus `source_transcript=...` are passed. `category="user-inference"` routes the write through the structural confirmation tier. |
| `memory_update(id, content?, scopes?, confidence?, category?, links?)` | Refine in place. Preserves `id`, `created`, `source`; bumps `updated`. New (2.0) `links` parameter sets the typed inter-memory edge list — REPLACE semantics, pass the full new list, pass `[]` to clear. |
| `memory_verify(id, note?, verified_paths?, verified_commits?, verified_versions?)` | Bump `last_verified_at` after spot-checking. Pass the actual claims you spot-checked — the server uses these to short-circuit later drift signals. |
| `memory_list(scopes?, with_bodies?)` | List active memories. IDs and one-line summaries by default; `with_bodies=true` for a single-call corpus dump. Race-safe against concurrent tombstoning. |
| `memory_remove(id, reason)` | Tombstone a memory. Captures originating session id into the tombstone frontmatter. |
| `memory_restore(id)` | Bring a tombstoned memory back. Preserves `created`, `updated`, `last_verified_at`. |
| `memory_list_tombstones(scopes?)` | List removed memories with their removal metadata. |
| `memory_rename_scope(old_scope, new_scope, include_tombstones?)` | Replace `old_scope` with `new_scope` across active memories (and tombstones, by default). The cheap fix for typo'd scopes surfaced by `memory_health.rare_scopes`. |
| `memory_record_use(memory_ids, outcome, note?, claim_excerpts?)` | Record how a retrieved memory landed. New (2.0) `claim_excerpts` parallel to `memory_ids` carries the load-bearing claim — the audit log captures *which* claim shaped the response. |
| `memory_health(window_days?, heavily_used_top_k?, min_applied?)` | Aggregate health view: dead weight, heavily-used, contradictions with `resolution_timeline`, transient marker stats, scope distribution, `rare_scopes`, `verification_debt`, `commit_drift_debt`. Same data as `bettermemory health`. |
| `memory_scope_overview(auto_scope?)` | Cheap session-start hint: per-scope counts plus a `curation_pending` rollup (`{stale, never_verified, drifted, cold, dead}`). |
| `memory_scope_disable(scope)` / `memory_scope_enable(scope)` | Mute / unmute a scope for the rest of this session. |
| `memory_write_confirm(pending_id)` / `memory_write_cancel(pending_id)` | Commit or drop a pending write (returned for `category="user-inference"`). |

### Pending-write flow

When `behavior.require_write_confirmation = true` in config (or whenever `category="user-inference"`), `memory_write` does not commit immediately. It returns:

```json
{
  "status": "pending",
  "pending_id": "pending_abc123",
  "preview": { ... },
  "hint": "Confirm with memory_write_confirm(pending_id) ..."
}
```

The consumer (or the model itself, after asking the user) then calls `memory_write_confirm(pending_id)` to commit, or `memory_write_cancel(pending_id)` to drop. Pending entries expire after one hour.

The default for solo single-user setups is `false`, so `category="fact"` writes commit immediately.

## On-disk format

Each memory is one file:

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
walkthrough with code I can run, not a tour of the IDE
or interface chrome.
```

Tombstones move to `.tombstones/` with `removed:` and `removed_reason:` added; the body is preserved.

**Optional frontmatter fields** are written only when populated, so files stay visually clean: `origin` (cwd + repo + branch + worktree_root captured at write time), `last_verified_at`, `category` (`fact` / `user-inference` / `ambient`), `verified_paths` / `verified_commits` / `verified_versions` (from `memory_verify`), and `links` (typed edges; new in 2.0).

**Schema version.** `schema_version: 1` is emitted by every new write. Memories without the field load implicitly as version 1. A reader that encounters a memory with a *higher* version refuses it: `load_all` skips with a logged warning, and `bettermemory doctor` surfaces the count gap. Within a major version, bumps are additive only — new optional fields, never renamed, never removed, never re-defined. The 2.0 release stays at `schema_version: 1` because every new field (links, claim_excerpts in the event log, etc.) is purely additive; legacy memories load unchanged.

## Performance characteristics

Before 2.0, `Store.load_all` walked every file every time `memory_search` was called. That bit hard at ~5-10K memories. **2.0 ships a SQLite FTS5 inverted index** that's kept live by Store hooks on every write / update / tombstone, used as a candidate pre-filter when the store crosses `BETTERMEMORY_INDEX_THRESHOLD` memories (default 500). Below the threshold the search still uses `load_all` for byte-stable result quality. Above the threshold, the FTS5 candidate set caps the per-search work regardless of corpus size.

Recovery path for the rare drift case (memories hand-edited outside the runtime, restored from backup, etc.): `bettermemory reindex` rebuilds the index from the on-disk files in one transaction.

If you want hard numbers for your hardware, the old `load_all` benchmark is in `bench/storage.py`; the FTS5 path will give you roughly constant-time search above 500 memories regardless of corpus size.

## Cross-host sync

```sh
bettermemory sync init --remote git@github.com:you/your-memory-repo.git
bettermemory sync push           # commit + push (no-op when nothing changed)
bettermemory sync pull           # rebase-pull + rebuild the FTS5 index
bettermemory sync auto           # pull-then-push: the cron / shell-alias one-shot
bettermemory sync status         # branch, modified files, ahead/behind
```

It's a thin git wrapper — git handles history, distributed copies, and three-way merge for the cases that are interesting. The wrapper buys you a sensible `.gitignore` (excludes `.index.sqlite`, `.events.jsonl`, embedding caches, lock files), a post-pull `reindex` so the FTS view matches the new file contents, and "no commit when nothing changed" semantics so the audit log isn't littered with empty syncs. Conflict resolution stays in git's domain — true content conflicts fall through to `git rebase --continue` like any other merge.

## Local web UI

```sh
pip install 'bettermemory[ui]'
bettermemory ui                  # binds 127.0.0.1:8765 by default
```

A small FastAPI app surfacing the curation surfaces — memory_health rollups, a searchable memory list with scope filter, per-memory detail with verify form (one-click `memory_verify`), and a tombstone browser. Local-only by default (binding non-loopback logs a warning since the UI exposes curation data). No editing surface — writes happen in-conversation via `memory_write`; the UI is read-mostly with verify as the one mutation, since "I just spot-checked this" is a natural human action.

## Where memories live

Resolution order:

1. `$BETTERMEMORY_DIR` env var, if set.
2. `./.claude-memory/` if it exists in the working directory (project-scoped).
3. `~/.claude-memory/` (global).

Crossing projects is *not* default behavior. A memory written while working on Project A only appears when working on Project B if you stored it globally.

In addition to the directory-based separation above, every memory carries an `origin` block recording the cwd, git remote URL, branch, and worktree root at write time:

```yaml
origin:
  cwd: /Users/me/projects/foo
  repo: git@github.com:me/foo.git
  branch: main
  worktree_root: /Users/me/projects/foo
```

`memory_search` defaults to `auto_scope=true`, which filters results to memories whose `origin.repo` AND `origin.worktree_root` match the caller's current checkout. Sibling `git worktree add` checkouts of the same repo are isolated from each other. Legacy memories without an `origin` field are treated as global and surface from anywhere. Pass `auto_scope=false` for cross-project queries.

## Durability check

Memory is for facts that will still be true in a week if nobody updates them. The tool enforces this structurally: `memory_write` scans the body for transient-state markers like `currently`, `today I`, `we just`, `the new`, commit-SHA-like hex tokens, and similar phrases. It returns

```json
{
  "status": "transient_warning",
  "markers": [
    {"marker": "currently", "snippet": "...currently using GitHub Actions..."}
  ],
  "hint": "..."
}
```

instead of writing. Either rephrase the body to extract the level-up durable form (the architectural decision, the why, the what-was-built; discard the timestamp or state) or pass `acknowledge_transient=true` to override. The override is recorded in the event log so the false-positive rate per marker is observable.

## Optional: write-time groundedness gate (new in 2.0)

```python
memory_write(
    content="The user prefers terse code-driven explanations.",
    scopes=["learning-style"],
    groundedness_check=True,
    source_transcript="user: I want terse code-driven explanations, no prose.",
)
```

The server walks the proposed body sentence-by-sentence and flags any sentence whose stopword-stripped content tokens overlap the transcript by less than 30%. Returns `{status: "ungrounded", claims: [...]}` instead of committing. The override is `acknowledge_ungrounded=True`, used when the caller has other grounding sources (a file read, a tool result) not represented in the transcript.

Off by default — back-compat for every existing caller. Opt in when you want a paper trail proving the memory came from the conversation, not from training-data confabulation. Closes the failure mode mem0's 97.8% junk audit traces back to. The HaluMem benchmark, made operational inline.

## Event log

Every tool call appends one JSON line to `<storage>/.events.jsonl`:

```jsonl
{"ts":"2026-05-07T19:00:00Z","session":"sess_a1b2","kind":"search","query":"home lab","scopes_filter":null,"max_results":5,"returned":["01H..","01H.."],"relevance":["high","low"],"expand_top":false,"expanded_id":null}
{"ts":"2026-05-07T19:00:01Z","session":"sess_a1b2","kind":"write","status":"committed","id":"01H..","scopes":["projects:foo"],"forced":false,"related":[]}
{"ts":"2026-05-07T19:00:02Z","session":"sess_a1b2","kind":"use","ids":["01H.."],"outcome":"applied","claim_excerpts":["the user prefers terse output"]}
```

The log is the substrate the `memory_health` view, the use-recording feedback signal, the negative-outcomes annotation on search hits, and the durability marker tuner all read from. `claim_excerpts` (new in 2.0) carries the load-bearing claim per applied memory so an audit can trace any response back to the specific sentence. It rotates to `.events-<timestamp>.jsonl.gz` once the active file crosses `[telemetry] max_bytes` (default 10 MB).

Search queries are recorded verbatim. The log lives in the same directory as the memories themselves, so it shares the same trust boundary. If you do not want this behavior, set `[telemetry] enabled = false` in `config.toml`.

## Config

The config file is created on first run at the platform-standard config dir (via `platformdirs`):

- macOS: `~/Library/Application Support/bettermemory/config.toml`
- Linux: `~/.config/bettermemory/config.toml`
- Windows: `%LOCALAPPDATA%\bettermemory\config.toml`

Defaults:

```toml
[storage]
# directory = "~/.claude-memory"   # default: resolution rule above

[behavior]
require_write_confirmation = false
default_max_results = 5
recency_boost_half_life_days = 30
search_mode = "keyword"               # new in 2.0; one of keyword/bm25/semantic/hybrid
semantic_dedup = false                # optional, requires [embeddings] extra
semantic_model_name = "all-MiniLM-L6-v2"
verification_stale_days = 30

[scopes]
allowed = []   # if non-empty, writes with unknown scopes fail

[telemetry]
enabled = true                # see "Event log" below; flip to false to opt out
max_bytes = 10000000          # rotate the active log at this size
```

## Scopes

Scopes are lowercase, alphanumeric, with hyphens or colons (for nesting). Examples:

- `tools`, `learning-style`, `infrastructure`, `personal-context`
- `projects:foo`, `projects:bar:subsystem`

Avoid the catch-all `general` scope. It defeats the whole point.

## CLI

The `bettermemory` script is the MCP server entry point by default. Running it with no arguments launches over stdio, which is what your client expects. It also exposes offline tooling:

```sh
bettermemory --version

# Onboarding
bettermemory init                                # show-and-tell: print snippet + locations
bettermemory init --client claude-code           # auto-patch a known client (idempotent)
bettermemory init --client claude-desktop        # (or cursor, continue, cline)
bettermemory init --client cursor --print-only   # print snippet without writing
bettermemory init --json                         # structured output for tooling
bettermemory init --with-addendum                # also print the long-form policy addendum

# Diagnostics
bettermemory doctor                              # diagnose install state
bettermemory doctor --json                       # exit code: 0=ok, 1=warn, 2=fail

# Curation
bettermemory health                              # aggregate report (text)
bettermemory health --json                       # ...as JSON
bettermemory health --days 60 --top-k 20

# Consolidation (new in 2.0)
bettermemory consolidate                         # dry-run: dedup/demote/cold-scope/typo suggestions
bettermemory consolidate --apply                 # commit dedup tombstones + category demotions
bettermemory consolidate --json
bettermemory consolidate --window-days 30 --cold-scope-days 180 --semantic-threshold 0.85 --typo-distance 2

# Index management (new in 2.0)
bettermemory reindex                             # drop + rebuild the FTS5 index from on-disk files
bettermemory reindex --json

# Cross-host sync (new in 2.0)
bettermemory sync init --remote git@host:repo.git
bettermemory sync status                         # branch, ahead/behind, modified
bettermemory sync push                           # commit + push
bettermemory sync pull                           # rebase-pull + rebuild index
bettermemory sync auto                           # pull then push (cron/alias one-shot)

# Web UI (new in 2.0, requires [ui] extra)
bettermemory ui --host 127.0.0.1 --port 8765

# One-shot data migrations
bettermemory migrate origin --dry-run            # preview the backfill
bettermemory migrate origin                      # apply (project-scoped dir)
bettermemory migrate origin --repo <url>         # force-tag (global dir)
bettermemory migrate origin \
  --scope-repo projects:foo=git@github.com:me/foo.git \
  --scope-repo projects:bar=git@github.com:me/bar.git

# Tombstone management
bettermemory tombstones list                     # all removed memories
bettermemory tombstones list --json --scope tools
bettermemory tombstones prune --older-than 365   # hard-delete year-old removals
bettermemory tombstones prune --older-than 365 --dry-run

# Backup / migration
bettermemory export                              # dump active + tombstones to stdout
bettermemory export -o backup.json
bettermemory export --no-tombstones              # active set only
bettermemory export --scope projects:demo        # filter by scope (repeatable)
```

`health` returns the same data as the `memory_health` MCP tool. Use it to drive curation passes outside any conversation.

`consolidate` is the offline batch curation. Four passes: near-duplicate dedup (semantic when the embeddings extra is installed, Jaccard otherwise), demote-never-applied to ambient, cold-scope suggestions, scope-typo pairs. Dry-run by default — `--apply` commits dedup tombstones and category demotions; cold-scope and scope-typo passes are suggest-only regardless.

`reindex` is the recovery path for "I edited memory files outside the runtime" — the Store hooks keep the index live during normal operation. Safe to run anytime; the rebuild is transactional.

`sync` is the cross-host replication. The wrapper sits over `git`. See [§ Cross-host sync](#cross-host-sync) above.

`ui` runs the local web UI. See [§ Local web UI](#local-web-ui) above.

`migrate origin` is a one-shot backfill for memories written before the auto-scope feature shipped. For project-scoped directories the inference is automatic; for global directories the migration does nothing without an explicit routing flag, because the memories there came from many projects and stamping them with one repo URL would be misinformation.

`tombstones list` enumerates removed memories. `tombstones prune --older-than DAYS` is a hard delete with no further audit trail beyond what the event log captured.

### Tombstone lifecycle

Tombstones are first-class records, not deletions. The lifecycle:

1. **`memory_remove(id, reason)`** moves the file to `.tombstones/`, stamps `removed`, `removed_reason`, `removed_session`.
2. **`memory_write` checks tombstones at dedup time.** If a new body has high overlap with a tombstone, the write returns `status="previously_removed"` carrying the original `removed_reason`. The lesson encoded in the removal is not lost. `memory_restore(id)` brings the original record back if the rejection no longer applies.
3. **`memory_list_tombstones`** is the curation surface. Same data on the CLI is `bettermemory tombstones list`; same data in the web UI is `/tombstones`.
4. **`memory_restore(id)`** strips the removal frontmatter. `created`, `updated`, `last_verified_at` are preserved.
5. **`bettermemory tombstones prune --older-than DAYS`** is the only hard-delete path.

### Auto-scope is a UX filter, not access control

`memory_search(auto_scope=True)` and `memory_scope_overview(auto_scope=True)` filter their *defaults* by the caller's current repo + worktree so the first-look surface stays focused. They do not gate `memory_show(id)`, which serves any active id verbatim. The threat model is "do not accidentally surface irrelevant memories", not "prevent information flow across project boundaries". For real isolation, use separate stores via the project-scoped resolution rule (`./.claude-memory/`) or `BETTERMEMORY_DIR`.

## Development

```sh
# direnv users: just `cd` in. `.envrc` exports UV_PROJECT_ENVIRONMENT=venv.
# Otherwise:
export UV_PROJECT_ENVIRONMENT=venv

uv sync --extra dev
source venv/bin/activate
pytest -q

# With coverage:
pytest --cov=bettermemory --cov-report=term-missing
```

`tests/conftest.py` puts `src/` on `sys.path` directly, so the suite passes even if the editable install is in a weird state.

### macOS gotcha: the env is `venv/`, not `.venv/`

macOS Sequoia auto-applies `UF_HIDDEN` to anything literally named `.venv` inside iCloud-synced folders (`~/Documents/`, `~/Desktop/`). Python 3.12+ then silently skips hidden `.pth` files, so `import bettermemory` after an editable install fails with `ModuleNotFoundError`. Two clean workarounds:

1. **Name the venv anything else**: `venv`, `.env-mcp`, or `env`. Only the literal `.venv` triggers the iCloud heuristic. This repo defaults to `venv/` via `.envrc` and `UV_PROJECT_ENVIRONMENT`.
2. **Keep the project outside `~/Documents/` and `~/Desktop/`.** The auto-hide doesn't fire elsewhere.

Not a uv bug; it's macOS being opinionated about virtualenvs in iCloud-synced trees.

## Optional extras

```sh
uv pip install -e ".[embeddings]"   # sentence-transformers for semantic dedup + semantic search mode
uv pip install -e ".[ui]"           # FastAPI + uvicorn + httpx for the local web UI
```

With `[embeddings]` installed, you can flip `[behavior] semantic_dedup = true` in `config.toml` (catches paraphrase duplicates at write time) and use `memory_search(mode="semantic")` or `mode="hybrid"` for paraphrase-aware retrieval. Without the extra, `mode="semantic"` raises with an install hint; `mode="hybrid"` falls back to keyword + BM25 fusion.

## Limitations

1. **Multi-process access on Unix is exercised.** The fcntl-based per-file locking in `store.py` and the parallel lock on the event log in `events.py` are stress-tested under contention by `tests/test_concurrency.py` (four worker processes with mixed write, update, remove, and restore on a shared root). Windows uses a no-op fallback (no `fcntl`); on Windows the recommendation is single-process.
2. **No automatic conflict resolution for memory edits via sync.** `bettermemory sync` delegates to git's three-way merge for non-overlapping edits. True content conflicts surface as normal merge conflicts the user resolves by hand (`git rebase --continue`). Auto-resolving conflicting memory edits is unsolved across the field; we don't pretend otherwise.
3. **No encryption.** Memories are plaintext on disk. Do not store secrets. Use OS-level disk encryption if you need it.
4. **The web UI is read-mostly.** It surfaces curation and the verify action, but writing happens in-conversation via the MCP tools. Editing arbitrary memory bodies from a browser would invite a class of mistakes that `memory_update`'s in-conversation discipline avoids.
5. **Disabled scopes do not survive restart.** Intentional: start each session fresh.

## What is out of scope

- **Cloud sync as a service.** Memories are local; sync is git-based and self-hosted. Run your own remote (GitHub, Forgejo, a bare repo over SSH) — bettermemory is the wrapper, not the host.
- **Cross-user sharing.** This is a single-user tool. Team / multi-user scopes are deferred (see `docs/v1.6-plan.md` T4.2).
- **Automatic memory extraction from transcripts.** The whole point of this project is that auto-extraction is the failure mode it exists to fix — see mem0's 97.8% junk audit. The optional `groundedness_check` flag goes the other way: gate proposed writes against the transcript, don't generate them from it.

## Origins

I started building this because the existing memory feature in Claude Code at the time auto-injected every stored "fact" into every system prompt. The more I taught the model about my preferences, the more it dragged irrelevant context into unrelated conversations. Asking for a Python tutorial would pull in my home-lab notes; a generic question would get coloured by some preference I had stated months ago. I wanted memory the model retrieved on demand, like any other tool. That is the design you see throughout.

The project was originally called `bettermemory`. Mid-build, the auto-injecting memory feature kept overriding my stated preference and renaming the package `memory-mcp` in conversation. The irony was sufficient motivation to finish.

Built by Mattias Rask.

## License

MIT. See [LICENSE](LICENSE).
