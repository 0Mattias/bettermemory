# bettermemory

[![CI](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg)](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A local, file-backed memory MCP server for Claude — built around the principle that **memory is a tool the model retrieves on demand, not context auto-injected into every prompt.**

## Why

Cloud-style memory features tend to over-apply. They dump stored "facts" into every system prompt, and the model then drags irrelevant context into unrelated conversations. Asking for a Python tutorial pulls in your home-lab notes; a generic question gets coloured by some preference you stated months ago.

This project takes the opposite tack:

- The model **only sees memories when it actively calls `memory_search`.**
- The default is to *not* retrieve. False negatives (forgot something, ask once) beat false positives (irrelevant context, cascades for the whole conversation).
- Memories are **plain markdown files with YAML frontmatter** in a directory you can `grep`, `git log`, and edit by hand.
- Removal is a **tombstone**, not a delete. Audit trail.
- Per-session **scope disable** lets the user say "this conversation is unrelated to project X" and shut off a slice of memory until the server restarts.

## Install

```sh
# recommended — isolated install via uv tool
uv tool install bettermemory

# or pipx
pipx install bettermemory

# or pip into a venv
pip install bettermemory
```

Python ≥ 3.11. From a clone (development): `uv pip install -e .` or `uv tool install .`.

## Quick start

After installing, run:

```sh
bettermemory init --client claude-code      # or: claude-desktop, cursor, continue, cline
```

That idempotently writes the MCP server entry into the right config file for your client. Restart the client and ask:

> What memory tools do you have?

Per-client setup details (config paths, restart requirements, gotchas for Code-Insiders / Codium variants of Cline, project-scoped vs user-scoped patching) live in [`docs/clients.md`](docs/clients.md). If your client isn't in the supported list (or you'd rather copy by hand), run `bettermemory init` with no flags — it prints the canonical JSON snippet plus the common config locations, with `[✓]` markers showing which already exist on your machine.

That's it — defaults are sane. The opt-in policy, transparency requirement, and verification obligation now live in the server's MCP `instructions` block (which every client surfaces at the system-prompt level) and in each tool's description, so a fresh install behaves correctly without further configuration.

**Optional tightening.** [`docs/system_prompt.md`](docs/system_prompt.md) is the longer-form addendum — paste it into your project's `CLAUDE.md` for additional discipline around scope hygiene, the record-use loop, and confirmation-tier policy. It's no longer load-bearing for correctness; treat it as the advanced tuning document, not a required setup step. (Pass `--with-addendum` to `bettermemory init` to print the block.)

See [`docs/installation.md`](docs/installation.md) for more detail.

## Tools

The full surface contract — signatures, defaults, return shapes, audit notes — lives in [`docs/api.md`](docs/api.md). The table below is the at-a-glance summary.

| Tool | What it does |
|---|---|
| `memory_search(query, scopes?, max_results?, expand_top?, auto_scope?)` | Rank and return memory hits with snippets. Each hit carries `relevance: "high" \| "medium" \| "low"` and `match_terms` (the query words that actually hit) — branch on `relevance`, not the raw `score`. Hits also include `created`, `updated`, `last_verified_at`, and cheap `path_drift_checked`/`path_drift_missing` integers so stale hits are obvious without a `memory_show` round-trip. Pass `expand_top=true` to inline the full body of the top hit when its relevance is `"high"` (collapses search→show into one call on confident hits, and surfaces the full `path_drift` report on the expanded hit). |
| `memory_show(id)` | Full body of one memory, plus the full `path_drift` report. |
| `memory_write(content, scopes, confidence?, source?, force?, acknowledge_transient?)` | Create a new memory. Runs the structural durability check, then dedup against active memories (`status="duplicate"`), then dedup against tombstones (`status="previously_removed"`, carrying the original `removed_reason`). `force=true` overrides both gates. |
| `memory_update(id, content?, scopes?, confidence?)` | Refine an existing memory in place. Preserves `id`, `created`, and `source`; bumps `updated`. Use this instead of `memory_remove` + `memory_write` when correcting or extending a stored fact — that round-trip would lose the original timestamp and litter the tombstone log with non-deletes. Replace semantics for `scopes` (provide the full new list). |
| `memory_verify(id, note?)` | Bump `last_verified_at` after spot-checking that the body's claims still match reality. Orthogonal to `memory_update`: a typo fix bumps `updated` but not `last_verified_at`; a verify call bumps `last_verified_at` but not `updated`. |
| `memory_list(scopes?, with_bodies?)` | List active memories — IDs and one-line summaries by default. Pass `with_bodies=true` for a single-call corpus dump; useful for small stores where N round trips of `list → show → show` would be wasteful. Race-safe against concurrent tombstoning (a file disappearing mid-iteration is skipped, not crashed). |
| `memory_remove(id, reason)` | Tombstone a memory. The originating session id is captured into the tombstone frontmatter so the link to the removal session survives event-log rotation. |
| `memory_restore(id)` | Bring a tombstoned memory back to the active set. Strips the removal frontmatter, preserves `created` / `updated` / `last_verified_at` (the body didn't change while it was gone). Errors loudly if the id is active or unknown. |
| `memory_list_tombstones(scopes?)` | List removed memories with their removal metadata. The curation surface for "what did I clear out?" and the investigation surface for "I think I had a memory about X — what happened?" |
| `memory_rename_scope(old_scope, new_scope, include_tombstones?)` | Replace `old_scope` with `new_scope` across active memories (and tombstones, by default). The cheap fix for typo'd or deprecated scopes surfaced via `memory_health.rare_scopes`. Bumps `updated`; preserves `last_verified_at`. |
| `memory_record_use(memory_ids, outcome, note?)` | Record how a retrieved memory landed: `"applied"`, `"ignored"`, or `"contradicted"`. Feeds the memory_health view; lets you spot dead-weight, stale, or hallucinated memories. |
| `memory_health(window_days?, heavily_used_top_k?, min_applied?)` | Aggregate health view: dead-weight memories, heavily-used memories, unresolved contradictions, transient-marker fire/override rates, scope distribution, per-scope `scope_health` rollup, singleton `rare_scopes` likely-typos, and `orphan_use_events` (a fabricated-id smoke test). Same data as the `bettermemory health` CLI. |
| `memory_scope_overview(auto_scope?)` | Cheap session-start hint: counts of memories per scope. `total=0` means `memory_search` is unlikely to be fruitful. |
| `memory_scope_disable(scope)` | Mute a scope for the rest of this session. |
| `memory_scope_enable(scope)` | Re-enable a previously muted scope. |
| `memory_write_confirm(pending_id)` | Commit a pending write (when confirmation is required). |
| `memory_write_cancel(pending_id)` | Drop a pending write without committing. |

### Pending-write flow

When `behavior.require_write_confirmation = true` in config, `memory_write` does not commit immediately. It returns:

```json
{
  "status": "pending",
  "pending_id": "pending_abc123",
  "preview": { ... },
  "hint": "Confirm with memory_write_confirm(pending_id) ..."
}
```

The consumer (or the model itself, after asking the user) then calls `memory_write_confirm(pending_id)` to commit, or `memory_write_cancel(pending_id)` to drop. Pending entries expire after one hour to keep the in-memory queue tidy.

The default for solo single-user setups is `false` — writes commit immediately.

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

Tombstones move to `.tombstones/` with `removed:` and `removed_reason:` added — the body is preserved.

**Schema version.** `schema_version: 1` is emitted by every new write. Memories without the field load implicitly as version 1 (the format predates the constant). A reader that encounters a memory with a *higher* version refuses it (`load_all` skips with a logged warning, `bettermemory doctor` surfaces the count gap) — graceful degradation rather than risk misinterpreting fields whose semantics changed under a downgrade. Within a major version, bumps are additive-only: new optional fields, never renamed, never removed, never re-defined. A major bump (1 → 2) is reserved for breaking changes and would ship with a `bettermemory migrate` subcommand.

## Performance characteristics

`Store.load_all` walks every file every time `memory_search` is called — there's no in-memory index, no incremental refresh. That's deliberate (simpler invariants, no cache-coherence story), but it sets a practical ceiling on corpus size.

Numbers from `bench/storage.py` on an Apple Silicon laptop (your hardware will differ; the *shape* of the curve is what to plan around):

| n      | disk MB | load_all median | search median | search p95 |
|--------|---------|-----------------|---------------|------------|
|  1,000 |   0.5   |    276 ms       |    16 ms      |    17 ms   |
| 10,000 |   4.8   |    2.8 s        |   168 ms      |   189 ms   |
| 50,000 |  23.8   |    23 s         |   956 ms      |  1.08 s    |

Read this as roughly linear in N. Practical guidance:

- **Up to ~5,000 memories**: comfortable. `memory_search` returns in well under 100 ms; you'll never feel the latency.
- **5,000–10,000**: still fine. ~150–200 ms per `memory_search`; perceptible but not annoying.
- **10,000–50,000**: usable but starting to drag. ~0.5–1 s per `memory_search`; one second is the rough threshold where the model's tool-call latency starts being noticeable in conversation.
- **Beyond 50,000**: the architecture would need an index. We're not there, and your store probably won't be either — the project encourages curation (`memory_health`, dead-weight pruning, scope hygiene, tombstone-aware dedup) precisely so the corpus stays small and useful rather than ever growing into the tens of thousands.

Re-run the bench yourself with `venv/bin/python bench/storage.py --sizes 1000,10000,50000` if you want numbers for your own hardware.

## Where memories live

Resolution order:

1. `$BETTERMEMORY_DIR` env var, if set.
2. `./.claude-memory/` if it exists in the working directory (project-scoped).
3. `~/.claude-memory/` (global).

Crossing projects is *not* default behavior. A memory written while working on Project A only appears when working on Project B if you stored it globally.

In addition to the directory-based separation above, every memory carries an `origin` block recording the cwd, git remote URL, and branch at write time:

```yaml
origin:
  cwd: /Users/me/projects/foo
  repo: git@github.com:me/foo.git
  branch: main
```

`memory_search` defaults to `auto_scope=true`, which filters results to memories whose `origin.repo` matches the caller's current repository. Legacy memories without an `origin` field, and writes from outside any git repo, are treated as global and surface from anywhere. Pass `auto_scope=false` for cross-project queries.

## Durability check

Memory is for facts that will still be true in a week if nobody updates
them. The tool enforces this structurally: `memory_write` scans the body
for transient-state markers — `currently`, `today I`, `we just`, `the
new`, commit-SHA-like hex tokens, and friends — and returns

```json
{
  "status": "transient_warning",
  "markers": [
    {"marker": "currently", "snippet": "...currently using GitHub Actions..."}
  ],
  "hint": "..."
}
```

instead of writing. Either rephrase the body to extract the level-up
durable form (the architectural decision, the why, the what-was-built —
discard the timestamp/state) or pass `acknowledge_transient=true` to
override. The override is recorded in the event log so the false-positive
rate per marker is observable; high-override markers are candidates for
trimming.

The full marker list is in `src/bettermemory/durability.py`. Adding to it
costs one false-positive slot — a phrase that's transient in some contexts
and durable in others will trip writes that shouldn't be tripped, and the
caller will learn to rubber-stamp `acknowledge_transient`. That's worse
than not having the marker. Watch override rates before extending.

## Event log

Every tool call appends one JSON line to `<storage>/.events.jsonl`:

```jsonl
{"ts":"2026-05-07T19:00:00Z","session":"sess_a1b2","kind":"search","query":"home lab","scopes_filter":null,"max_results":5,"returned":["01H..","01H.."],"relevance":["high","low"],"expand_top":false,"expanded_id":null}
{"ts":"2026-05-07T19:00:01Z","session":"sess_a1b2","kind":"write","status":"committed","id":"01H..","scopes":["projects:foo"],"forced":false,"related":[]}
{"ts":"2026-05-07T19:00:02Z","session":"sess_a1b2","kind":"show","id":"01H.."}
```

The log is the substrate the `memory_health` view, the use-recording feedback signal, and the durability marker tuner all read from. It rotates to `.events-<timestamp>.jsonl.gz` once the active file crosses `[telemetry] max_bytes` (default 10 MB). Archives are kept indefinitely — prune by hand if disk pressure matters.

Search queries are recorded verbatim. The log lives in the same directory as the memories themselves, so it shares the same trust boundary — but if you don't want this behavior set `[telemetry] enabled = false` in `config.toml`.

## Config

The config file is created on first run at the platform-standard config dir
(via `platformdirs`):

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

Avoid the catch-all `general` scope — it defeats the whole point.

## CLI

The `bettermemory` script is the MCP server entry point by default — running it with no arguments launches over stdio, which is what your client expects. It also exposes offline tooling:

```sh
bettermemory init                                # show-and-tell: print snippet + locations
bettermemory init --client claude-code           # auto-patch a known client (idempotent)
bettermemory init --client claude-desktop
bettermemory init --client cursor
bettermemory init --client continue
bettermemory init --client cline
bettermemory init --client cursor --print-only   # print snippet without writing
bettermemory init --json                         # structured output for tooling
bettermemory init --with-addendum                # also print the optional advanced addendum

bettermemory doctor                  # diagnose install state (binary, config, storage,
                                     #   memory parse, event log, MCP client configs)
bettermemory doctor --json           # ...as JSON. Exit code: 0=ok, 1=warn, 2=fail.

bettermemory health                  # aggregate report (text)
bettermemory health --json           # ...as JSON
bettermemory health --days 60 --top-k 20

bettermemory migrate origin --dry-run            # preview the backfill
bettermemory migrate origin                      # apply (project-scoped dir)
bettermemory migrate origin --repo <url>         # force-tag (global dir)
bettermemory migrate origin \
  --scope-repo projects:foo=git@github.com:me/foo.git \
  --scope-repo projects:bar=git@github.com:me/bar.git
                                                 # route by scope (preferred for global dirs)

bettermemory tombstones list                     # all removed memories
bettermemory tombstones list --json --scope tools
bettermemory tombstones prune --older-than 365   # hard-delete year-old removals
bettermemory tombstones prune --older-than 365 --dry-run
```

`health` returns the same data as the `memory_health` MCP tool — drive curation passes outside any conversation: prune dead-weight memories, refresh contradicted ones, trim transient markers whose override rate is high.

`migrate origin` is a one-shot backfill for memories written before the auto-scope feature shipped (no `origin:` block in their frontmatter). For project-scoped directories (`./.claude-memory/` next to a git repo) the inference is automatic. For global directories (`~/.claude-memory/`) the migration deliberately does nothing without an explicit routing flag — the memories there came from many projects and stamping them with one repo URL would be misinformation.

For a global directory whose memories already use `projects:<name>` scopes, `--scope-repo SCOPE=URL` (repeatable) routes by tag. The first matching scope wins; memories that match no entry in the map fall through to `--repo` (if given) or are left untagged. `cwd` is left null on these paths since we don't know per-memory cwd retroactively — only the auto-inferred path (project-scoped dir) sets cwd.

The migration is idempotent (re-running is safe), atomic per file (`.tmp` + rename), and skips tombstones.

`tombstones list` enumerates removed memories with their removal metadata (`removed`, `removed_reason`, `removed_session`). The same data is available to the model via the `memory_list_tombstones` MCP tool. `tombstones prune --older-than DAYS` is a hard delete — pruned tombstones are gone from disk with no further audit trail beyond what the event log captured. `behavior.tombstone_retention_days` in `config.toml` sets a default cutoff; with the default of `0`, the flag is required explicitly.

### Tombstone lifecycle

Tombstones are first-class records, not deletions. The lifecycle:

1. **`memory_remove(id, reason)`** moves the file to `.tombstones/`, stamps `removed`, `removed_reason`, and the originating `removed_session` into the frontmatter.
2. **`memory_write` checks tombstones at dedup time.** If a new body has high overlap with a tombstone, the write returns `status="previously_removed"` carrying the original `removed_reason` — the lesson encoded in the removal isn't lost. `force=true` overrides; `memory_restore(id)` brings the original record back if the rejection no longer applies.
3. **`memory_list_tombstones`** is the curation surface. The same data on the CLI is `bettermemory tombstones list`.
4. **`memory_restore(id)`** strips the removal frontmatter and moves the file back. `created`, `updated`, and `last_verified_at` are preserved — the body didn't change while the record was tombstoned, so a freshly-restored ten-year-old memory ranks like a ten-year-old memory in the recency boost.
5. **`bettermemory tombstones prune --older-than DAYS`** is the only path that hard-deletes. Active memories are unaffected.

### Auto-scope is a UX filter, not access control

`memory_search(auto_scope=True)` and `memory_scope_overview(auto_scope=True)` filter their *defaults* by the caller's current repo so the first-look surface stays focused. They do not gate `memory_show(id)`, which serves any active id verbatim. The threat model is "don't accidentally surface irrelevant memories", not "prevent information flow across project boundaries". For real isolation, use separate stores via the project-scoped resolution rule (`./.claude-memory/`) or `BETTERMEMORY_DIR`.

## Development

```sh
# direnv users: just `cd` in — `.envrc` exports UV_PROJECT_ENVIRONMENT=venv.
# Otherwise:
export UV_PROJECT_ENVIRONMENT=venv

uv sync --extra dev
source venv/bin/activate
pytest -q

# With coverage (spec asks for >80% on store.py and search.py)
pytest --cov=bettermemory.store --cov=bettermemory.search --cov-report=term-missing
```

`tests/conftest.py` puts `src/` on `sys.path` directly, so the suite passes even if the editable install is in a weird state. `pytest -q` is a sanity check that doesn't depend on `uv pip install -e .` succeeding.

### macOS gotcha: the env is `venv/`, not `.venv/`

macOS Sequoia auto-applies `UF_HIDDEN` to anything literally named `.venv` inside iCloud-synced folders (`~/Documents/`, `~/Desktop/`). Python 3.12+ then silently skips hidden `.pth` files, so `import bettermemory` after an editable install fails with `ModuleNotFoundError`. A one-shot `chflags -R nohidden .venv` works for ~5 seconds before iCloud re-applies the flag — there is no good cure.

Two clean ways to avoid it:

1. **Name the venv anything else** — `venv`, `.env-mcp`, `env`. Only the literal `.venv` triggers the iCloud heuristic. This repo defaults to `venv/` via `.envrc` + `UV_PROJECT_ENVIRONMENT`.
2. **Or keep the project outside `~/Documents/` / `~/Desktop/`** — the auto-hide doesn't fire elsewhere.

This is not a uv bug. `uv venv .venv` in `/tmp/` or `~/projects/` stays clean. It's macOS being opinionated about virtualenvs in iCloud-synced trees.

### YAML + frontmatter

The on-disk format is YAML frontmatter inside a markdown file. We use a tiny vendored parser (`src/bettermemory/_frontmatter.py`) instead of `python-frontmatter` for two reasons:

1. **Python 3.14 compatibility.** `python-frontmatter` 1.1.0 (the current release) calls `codecs.open()`, which 3.14 emits a `DeprecationWarning` for. The library is effectively unmaintained.
2. **Forced pure-Python YAML.** `yaml.CSafeDumper` has a state-machine bug under submodule coverage instrumentation (`--cov=bettermemory.store`). The vendored parser pins `yaml.SafeLoader` / `yaml.SafeDumper` directly. Memory frontmatter is dozens of bytes per write, so the libyaml C speedup is irrelevant.

Files written by the previous `python-frontmatter`-based code keep loading byte-for-byte; cross-tested against the upstream library before the swap.

## Optional: semantic dedup

By default, `memory_write` dedup uses Jaccard on stopword-stripped, kebab-expanded token sets — fast, deterministic, no extra deps. It catches lexical overlap well but misses paraphrases (`"the database"` vs `"Postgres"`, `"shipped"` vs `"released"`).

To catch paraphrases too, install the `embeddings` extra and flip the toggle:

```sh
uv pip install -e ".[embeddings]"
```

```toml
# config.toml
[behavior]
semantic_dedup = true
semantic_model_name = "all-MiniLM-L6-v2"     # default; smaller models start faster
semantic_high_threshold = 0.85
semantic_medium_threshold = 0.65
```

Behavior unchanged when the toggle is off, so existing setups are untouched. If you flip the toggle without installing the extra, the server logs one WARNING and falls back to Jaccard — no errors, no surprises.

Embeddings are cached per-process keyed by `(memory_id, updated)`, so an updated memory busts its own cache entry. The first dedup call after server start pays the model load (~1-2s for `all-MiniLM-L6-v2`); subsequent calls are fast.

## Limitations

1. **Multi-process access on Unix is exercised.** The fcntl-based per-file locking in `store.py` and the parallel lock on the event log in `events.py` are stress-tested under contention by `tests/test_concurrency.py` (four worker processes, mixed write/update/remove/restore on a shared root, post-condition asserts no torn writes, no orphan tombstones, no malformed JSONL). Windows uses a no-op fallback (no `fcntl`); on Windows the recommendation is single-process.
2. **No conflict resolution.** If you edit a memory file by hand while the server is running, the next read will pick up your change but there's no merge story.
3. **No encryption.** Memories are plaintext on disk. Don't store secrets — use OS-level disk encryption if you need it.
4. **memory_search is keyword-only.** Synonyms and paraphrases are not handled by `memory_search`. (`memory_write` dedup can use semantic similarity — see "Optional: semantic dedup" above.) A short stopword list is stripped from the *query* (so "how to bake sourdough" doesn't match every memory on shared filler tokens), but bodies stay unfiltered. Hits are returned with a `relevance` label calibrated on coverage — distinguish "1 of 4 query words matched" (low) from "all 3 matched" (high) without inventing a score threshold. The recency boost reads `max(created, updated)`, so editing a fact via `memory_update` ranks it as fresh.
5. **Disabled scopes don't survive restart.** Intentional — start each session fresh.

## What's out of scope

- Cloud sync. Memories are local. If you want sync, that's `git`'s job.
- Cross-user sharing. Single-user tool.
- Automatic memory extraction from transcripts. The whole point of this project is that auto-extraction is the failure mode it exists to fix.

## Origins

I started building this because the existing memory feature in Claude Code at the time auto-injected every stored "fact" into every system prompt. The more I taught the model about my preferences, the more it dragged irrelevant context into unrelated conversations — asking for a Python tutorial would pull in my home-lab notes; a generic question would get coloured by some preference I'd stated months ago. I wanted memory the model retrieved on demand, like any other tool. That's the design you see throughout.

The project was originally called `bettermemory`. Mid-build the auto-injecting memory feature kept overriding my stated preference and renaming the package `memory-mcp` in conversation. The irony was sufficient motivation to finish.

Built by Mattias Rask.

## License

MIT — see [LICENSE](LICENSE).
