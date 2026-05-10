# bettermemory

[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97757)](plugin/README.md)
[![PyPI](https://img.shields.io/pypi/v/bettermemory.svg)](https://pypi.org/project/bettermemory/)
[![CI](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg)](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Persistent memory for Claude Code, retrieved on demand instead of force-fed into every prompt.**

bettermemory is a [Claude Code plugin](plugin/README.md) (and a standalone MCP server for any other client) that fixes a problem common to most LLM memory features. Those tools auto-inject every stored fact into every conversation. They have no sense of which facts are stale, which are relevant, or which you would rather forget. The longer you use them, the more polluted unrelated conversations become. Ask for a Python tutorial, get answers tinted by your home-lab notes. Ask a generic shell question, get advice coloured by a preference you stated months ago. Stale facts get dispensed confidently.

bettermemory inverts the contract. The model calls `memory_search` only when it actually needs context. Every retrieval ships with three structured staleness signals (`verification`, `path_drift`, `commit_drift`) so the model can spot-check before relying on what it pulled up. Memories live as plain markdown and YAML on disk, so you can `grep` them, `git log` them, and hand-edit them. A separate health surface tells *you* what is dead weight and what has drifted, instead of letting the store grow into a haunted closet of half-true notes.

## Install in Claude Code

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

That is it. Claude Code starts the MCP server, loads a system-prompt-level [skill](plugin/skills/bettermemory/SKILL.md) carrying the opt-in retrieval policy, and on the next turn the model has all 17 memory tools and the discipline to use them correctly. For other clients (Claude Desktop, Cursor, Continue, Cline) and manual setup, see [§ Other MCP clients](#other-mcp-clients) below.

## How it compares

|                              | Most memory features | bettermemory |
|------------------------------|----------------------|--------------|
| **Retrieval**                | Auto-injected into every prompt | Model calls `memory_search` only when needed |
| **Staleness awareness**      | None: facts are surfaced as if current | Three structured signals (`verification`, `path_drift`, `commit_drift`) on every retrieval |
| **Storage**                  | Opaque database | Plain markdown plus YAML on disk; works with `grep`, `git`, and any text editor |
| **Curation tools**           | None: memory just grows | `memory_health` surfaces dead weight, contradictions, typo scopes, and verification debt |
| **Deletes**                  | Gone forever | Tombstones with reason, tombstone-aware dedup, reversible via `memory_restore` |
| **Project scoping**          | Everything mixed together | Auto-scoped by git repo; cross-project queries are explicit |
| **Inferences about you**     | Saved silently | Structural confirmation tier: the model asks before saving |
| **Feedback loop**            | None | `memory_record_use` outcomes feed `memory_health` so dead weight surfaces automatically |

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

- **Opt-in retrieval.** `memory_search` is a tool the model calls when it needs context. The default is not to call it. Generic questions stay generic.
- **Three staleness signals on every retrieval.** Calendar age (`verification`: never, stale, or fresh), filesystem path drift (`path_drift`: are cited paths still on disk?), and repo commit drift (`commit_drift`: how many commits since the last `memory_verify` in the matching repo). When a signal fires, the model spot-checks before relying. It then either confirms via `memory_verify` or fixes the body via `memory_update` followed by verify. Most memory systems do not have *any* staleness story.
- **Hand-editable storage.** Memories are markdown and YAML files in `~/.claude-memory/` (or `./.claude-memory/` for project-scoped, or `$BETTERMEMORY_DIR`). No database. No opaque blob. The on-disk format is your data.
- **A curation surface.** `memory_health` reports dead weight (memories retrieved often but never marked `applied`), heavily-used items, unresolved contradictions, scope typos (singletons within Levenshtein distance 2 of an existing scope), and verification debt (counts of never-verified, stale, and fresh memories). Available as both an MCP tool the model calls and a `bettermemory health` CLI you run by hand.
- **Tombstones, not deletes.** Removed memories keep their `removed_reason`. Tombstone-aware dedup catches a paraphrase six months later that tries to sneak the same wrong fact back in. Removals are reversible via `memory_restore`.
- **Auto-scoped by project.** Memories written from inside a git checkout carry the repo URL. `memory_search` defaults to filtering by the caller's current repo. Cross-project queries are explicit (`auto_scope=false`), not the default.
- **A confirmation tier for claims about you.** `memory_write(category="user-inference")` always goes pending and requires confirmation before commit, regardless of global config. The user always gets the veto on misattribution. Project, infrastructure, and tooling facts (the default `category="fact"`) commit immediately.
- **A feedback loop.** `memory_record_use(ids, outcome)` after each response logs whether retrieved memories were `applied`, `ignored`, `contradicted`, or `corrected`. `memory_health` reads the event log so dead weight surfaces automatically. The system tells you what to prune, instead of the other way around.

## Other MCP clients

The plugin install above is the easy path for Claude Code. Equivalent setups exist for every other MCP client.

```sh
# Pick one:
uv tool install bettermemory       # recommended: isolated tool install via uv
pipx install bettermemory          # or pipx
pip install bettermemory           # or plain pip into a venv
```

Python 3.11 through 3.14 is supported. From a clone (development): `uv pip install -e .` or `uv tool install .`.

Then register with your client in one command:

```sh
bettermemory init --client claude-code      # or: claude-desktop, cursor, continue, cline
```

That command idempotently merges the MCP server entry into the right config file. Re-running is safe. Unchanged entries are no-ops, and stale binary paths are repaired. Restart the client and ask: *"What memory tools do you have?"*

If your client is not in the supported list, run `bettermemory init` with no flags. It prints the canonical JSON snippet plus the common config locations with `[✓]` markers showing which already exist on your machine. Per-client gotchas (config paths, restart behavior, Code-Insiders, Codium, Cline variants, and project-scoped vs user-scoped patching) live in [`docs/clients.md`](docs/clients.md). The long-form install reference is in [`docs/installation.md`](docs/installation.md).

### How the policy lands at the system-prompt level

Every compliant MCP client surfaces the server's `instructions` block in its system prompt. This is verified empirically on Claude Code 2.1.x, where it appears under "MCP Server Instructions". The block carries the core opt-in retrieval contract: when to call `memory_search`, when not to, the transparency requirement, the verification obligation, and the confirmation-tier policy. Claude Code truncates that block at roughly 1.8 KB, so the body is sized to fit comfortably under the cap with detail pushed into per-tool descriptions.

The Claude Code plugin path bypasses the truncation entirely. Its [`SKILL.md`](plugin/skills/bettermemory/SKILL.md) carries the long-form policy as a system-prompt-level skill with no cap. For other clients that want the long form, [`docs/system_prompt.md`](docs/system_prompt.md) is the canonical copy-pasteable addendum (also exported as `bettermemory.SYSTEM_PROMPT_ADDENDUM` for programmatic access).

## Coexistence with Claude Code's built-in memory

Claude Code 2.x ships its own filesystem-backed memory that auto-injects stored facts into the system prompt. That is the exact failure mode bettermemory exists to fix. The two can sit on disk together, but they fragment recall: a fact stored in one is invisible to the other's tools. If you adopt bettermemory, **install the plugin**, which lands the *"persistent memory between sessions lives in this server's MCP tools, do not fragment it across ad-hoc files alongside"* anchor in the system prompt. Or paste the [addendum](docs/system_prompt.md) into your `CLAUDE.md`. That one sentence is what keeps the model from drifting back to the built-in memory directory mid-conversation.

## Tools

The full surface contract (signatures, defaults, return shapes, audit notes) lives in [`docs/api.md`](docs/api.md). The table below is the at-a-glance summary.

| Tool | What it does |
|---|---|
| `memory_search(query, scopes?, max_results?, expand_top?, auto_scope?)` | Rank and return memory hits with snippets. Each hit carries `relevance` (`"high"`, `"medium"`, or `"low"`) and `match_terms` (the query words that actually hit). Branch on `relevance`, not the raw `score`. Hits also include `created`, `updated`, `last_verified_at`, cheap `path_drift_checked` and `path_drift_missing` integers, and (when applicable) a `commit_drift_count` integer so stale hits are obvious without a `memory_show` round-trip. Pass `expand_top=true` to inline the full body of the top hit when its relevance is `"high"`. That collapses search and show into one call and surfaces the full `path_drift` and `commit_drift` blocks on the expanded hit. |
| `memory_show(id)` | Full body of one memory, plus the full `verification` block, `path_drift` report, and `commit_drift` block (when the caller is in the matching repo). |
| `memory_write(content, scopes, confidence?, source?, category?, force?, acknowledge_transient?)` | Create a new memory. Runs the structural durability check, then dedup against active memories (`status="duplicate"`), then dedup against tombstones (`status="previously_removed"`, carrying the original `removed_reason`). `category="user-inference"` (versus the default `"fact"`) routes the write through a structural confirmation tier. It returns `status="pending"` regardless of the global confirmation config so the user always vetoes claims about themselves. `force=true` overrides both dedup gates. |
| `memory_update(id, content?, scopes?, confidence?, category?)` | Refine an existing memory in place. Preserves `id`, `created`, and `source`; bumps `updated`. Use this instead of `memory_remove` plus `memory_write` when correcting or extending a stored fact. The round-trip would lose the original timestamp and litter the tombstone log with non-deletes. `scopes` has replace semantics (provide the full new list). `category` accepts `"fact"` or `"ambient"`; `"user-inference"` is rejected because that category gates the pending-confirm WRITE flow and there is no equivalent gate on update. |
| `memory_verify(id, note?, verified_paths?, verified_commits?, verified_versions?)` | Bump `last_verified_at` after spot-checking that the body's claims still match reality. Orthogonal to `memory_update`: a typo fix bumps `updated` but not `last_verified_at`, and a verify call bumps `last_verified_at` but not `updated`. Pass the actual paths, commits, or versions you spot-checked via the `verified_paths`, `verified_commits`, and `verified_versions` parameters. The server uses these to short-circuit later drift signals. |
| `memory_list(scopes?, with_bodies?)` | List active memories: IDs and one-line summaries by default. Pass `with_bodies=true` for a single-call corpus dump. Useful for small stores where N round trips of `list` then `show` then `show` would be wasteful. Race-safe against concurrent tombstoning (a file disappearing mid-iteration is skipped, not crashed). |
| `memory_remove(id, reason)` | Tombstone a memory. The originating session id is captured into the tombstone frontmatter so the link to the removal session survives event-log rotation. |
| `memory_restore(id)` | Bring a tombstoned memory back to the active set. Strips the removal frontmatter, preserves `created`, `updated`, and `last_verified_at` (the body did not change while it was gone). Errors loudly if the id is active or unknown. |
| `memory_list_tombstones(scopes?)` | List removed memories with their removal metadata. The curation surface for "what did I clear out?" and the investigation surface for "I think I had a memory about X. What happened?" |
| `memory_rename_scope(old_scope, new_scope, include_tombstones?)` | Replace `old_scope` with `new_scope` across active memories (and tombstones, by default). The cheap fix for typo'd or deprecated scopes surfaced via `memory_health.rare_scopes`. Bumps `updated`; preserves `last_verified_at`. |
| `memory_record_use(memory_ids, outcome, note?)` | Record how a retrieved memory landed: `"applied"`, `"ignored"`, `"contradicted"`, or `"corrected"`. `"corrected"` is the audit-only sibling of `"contradicted"` for the noticed-and-fixed-inline workflow where the caller already ran `memory_update` or `memory_verify` in the same turn. Feeds the `memory_health` view; lets you spot dead weight, stale memories, and stuck contradictions. |
| `memory_health(window_days?, heavily_used_top_k?, min_applied?)` | Aggregate health view: dead-weight memories, heavily-used memories, unresolved contradictions (each row carries a `resolution_timeline` so a stuck flag can be self-diagnosed; cleared by either `memory_update` or `memory_verify` after the contradiction event), transient-marker fire and override rates, scope distribution, per-scope `scope_health` rollup, `rare_scopes` (singletons within Levenshtein distance 2 of another scope, which are usually typos), `orphan_use_events` (a fabricated-id smoke test), `verification_debt` (the never_verified, stale, and fresh partition against the configured threshold), and `commit_drift_debt` (rows whose verification anchor sits behind HEAD when the server is in a repo whose memories live in this store). Same data as the `bettermemory health` CLI. |
| `memory_scope_overview(auto_scope?)` | Cheap session-start hint: counts of memories per scope. `total=0` means `memory_search` is unlikely to be fruitful. Also returns a `curation_pending` rollup of integer counts (`stale`, `never_verified`, `drifted`, `cold`, `dead`) so the model can spot pending curation without paying the full `memory_health` cost. |
| `memory_scope_disable(scope)` | Mute a scope for the rest of this session. |
| `memory_scope_enable(scope)` | Re-enable a previously muted scope. |
| `memory_write_confirm(pending_id)` | Commit a pending write (returned when `category="user-inference"` was passed, or when `behavior.require_write_confirmation = true` in config). |
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

The default for solo single-user setups is `false`, so writes commit immediately.

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

**Schema version.** `schema_version: 1` is emitted by every new write. Memories without the field load implicitly as version 1 (the format predates the constant). A reader that encounters a memory with a *higher* version refuses it: `load_all` skips with a logged warning, and `bettermemory doctor` surfaces the count gap. That is graceful degradation rather than risk misinterpreting fields whose semantics changed under a downgrade. Within a major version, bumps are additive only: new optional fields, never renamed, never removed, never re-defined. A major bump (1 to 2) is reserved for breaking changes and would ship with a `bettermemory migrate` subcommand.

## Performance characteristics

`Store.load_all` walks every file every time `memory_search` is called. There is no in-memory index and no incremental refresh. That is deliberate (simpler invariants, no cache-coherence story), but it sets a practical ceiling on corpus size.

Numbers from `bench/storage.py` on an Apple Silicon laptop. Your hardware will differ; the *shape* of the curve is what to plan around.

| n      | disk MB | load_all median | search median | search p95 |
|--------|---------|-----------------|---------------|------------|
|  1,000 |   0.5   |    276 ms       |    16 ms      |    17 ms   |
| 10,000 |   4.8   |    2.8 s        |   168 ms      |   189 ms   |
| 50,000 |  23.8   |    23 s         |   956 ms      |  1.08 s    |

Read this as roughly linear in N. Practical guidance:

- **Up to 5,000 memories**: comfortable. `memory_search` returns in well under 100 ms and you will never feel the latency.
- **5,000 to 10,000**: still fine. Around 150 to 200 ms per `memory_search`. Perceptible but not annoying.
- **10,000 to 50,000**: usable but starting to drag. Roughly 0.5 to 1 second per `memory_search`. One second is the rough threshold where the model's tool-call latency starts being noticeable in conversation.
- **Beyond 50,000**: the architecture would need an index. We are not there, and your store probably will not be either. The project encourages curation (`memory_health`, dead-weight pruning, scope hygiene, tombstone-aware dedup) precisely so the corpus stays small and useful rather than growing into the tens of thousands.

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

instead of writing. Either rephrase the body to extract the level-up durable form (the architectural decision, the why, the what-was-built; discard the timestamp or state) or pass `acknowledge_transient=true` to override. The override is recorded in the event log so the false-positive rate per marker is observable. High-override markers are candidates for trimming.

The full marker list is in `src/bettermemory/durability.py`. Adding to it costs one false-positive slot. A phrase that is transient in some contexts and durable in others will trip writes that should not be tripped, and the caller will learn to rubber-stamp `acknowledge_transient`. That is worse than not having the marker. Watch override rates before extending.

## Event log

Every tool call appends one JSON line to `<storage>/.events.jsonl`:

```jsonl
{"ts":"2026-05-07T19:00:00Z","session":"sess_a1b2","kind":"search","query":"home lab","scopes_filter":null,"max_results":5,"returned":["01H..","01H.."],"relevance":["high","low"],"expand_top":false,"expanded_id":null}
{"ts":"2026-05-07T19:00:01Z","session":"sess_a1b2","kind":"write","status":"committed","id":"01H..","scopes":["projects:foo"],"forced":false,"related":[]}
{"ts":"2026-05-07T19:00:02Z","session":"sess_a1b2","kind":"show","id":"01H.."}
```

The log is the substrate the `memory_health` view, the use-recording feedback signal, and the durability marker tuner all read from. It rotates to `.events-<timestamp>.jsonl.gz` once the active file crosses `[telemetry] max_bytes` (default 10 MB). Archives are kept indefinitely. Prune by hand if disk pressure matters.

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
bettermemory --version                           # print version and exit

bettermemory init                                # show-and-tell: print snippet + locations
bettermemory init --client claude-code           # auto-patch a known client (idempotent)
bettermemory init --client claude-desktop
bettermemory init --client cursor
bettermemory init --client continue
bettermemory init --client cline
bettermemory init --client cursor --print-only   # print snippet without writing
bettermemory init --json                         # structured output for tooling
bettermemory init --with-addendum                # also print the long-form policy addendum

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

bettermemory export                              # dump active + tombstones to stdout
bettermemory export -o backup.json               # ...or to a file (status on stderr)
bettermemory export --no-tombstones              # active set only
bettermemory export --scope projects:demo        # filter by scope (repeatable)
```

`health` returns the same data as the `memory_health` MCP tool. Use it to drive curation passes outside any conversation: prune dead-weight memories, refresh contradicted ones, and trim transient markers whose override rate is high.

`migrate origin` is a one-shot backfill for memories written before the auto-scope feature shipped (no `origin:` block in their frontmatter). For project-scoped directories (`./.claude-memory/` next to a git repo) the inference is automatic. For global directories (`~/.claude-memory/`) the migration deliberately does nothing without an explicit routing flag, because the memories there came from many projects and stamping them with one repo URL would be misinformation.

For a global directory whose memories already use `projects:<name>` scopes, `--scope-repo SCOPE=URL` (repeatable) routes by tag. The first matching scope wins. Memories that match no entry in the map fall through to `--repo` (if given) or are left untagged. `cwd` is left null on these paths since we do not know per-memory cwd retroactively. Only the auto-inferred path (project-scoped dir) sets cwd.

The migration is idempotent (re-running is safe), atomic per file (`.tmp` plus rename), and skips tombstones.

`tombstones list` enumerates removed memories with their removal metadata (`removed`, `removed_reason`, `removed_session`). The same data is available to the model via the `memory_list_tombstones` MCP tool. `tombstones prune --older-than DAYS` is a hard delete: pruned tombstones are gone from disk with no further audit trail beyond what the event log captured. `behavior.tombstone_retention_days` in `config.toml` sets a default cutoff. With the default of `0`, the flag is required explicitly.

### Tombstone lifecycle

Tombstones are first-class records, not deletions. The lifecycle:

1. **`memory_remove(id, reason)`** moves the file to `.tombstones/`, stamps `removed`, `removed_reason`, and the originating `removed_session` into the frontmatter.
2. **`memory_write` checks tombstones at dedup time.** If a new body has high overlap with a tombstone, the write returns `status="previously_removed"` carrying the original `removed_reason`. The lesson encoded in the removal is not lost. `force=true` overrides; `memory_restore(id)` brings the original record back if the rejection no longer applies.
3. **`memory_list_tombstones`** is the curation surface. The same data on the CLI is `bettermemory tombstones list`.
4. **`memory_restore(id)`** strips the removal frontmatter and moves the file back. `created`, `updated`, and `last_verified_at` are preserved. The body did not change while the record was tombstoned, so a freshly-restored ten-year-old memory ranks like a ten-year-old memory in the recency boost.
5. **`bettermemory tombstones prune --older-than DAYS`** is the only path that hard-deletes. Active memories are unaffected.

### Auto-scope is a UX filter, not access control

`memory_search(auto_scope=True)` and `memory_scope_overview(auto_scope=True)` filter their *defaults* by the caller's current repo so the first-look surface stays focused. They do not gate `memory_show(id)`, which serves any active id verbatim. The threat model is "do not accidentally surface irrelevant memories", not "prevent information flow across project boundaries". For real isolation, use separate stores via the project-scoped resolution rule (`./.claude-memory/`) or `BETTERMEMORY_DIR`.

## Development

```sh
# direnv users: just `cd` in. `.envrc` exports UV_PROJECT_ENVIRONMENT=venv.
# Otherwise:
export UV_PROJECT_ENVIRONMENT=venv

uv sync --extra dev
source venv/bin/activate
pytest -q

# With coverage (the spec asks for >80% on store.py and search.py):
pytest --cov=bettermemory.store --cov=bettermemory.search --cov-report=term-missing
```

`tests/conftest.py` puts `src/` on `sys.path` directly, so the suite passes even if the editable install is in a weird state. `pytest -q` is a sanity check that does not depend on `uv pip install -e .` succeeding.

### macOS gotcha: the env is `venv/`, not `.venv/`

macOS Sequoia auto-applies `UF_HIDDEN` to anything literally named `.venv` inside iCloud-synced folders (`~/Documents/`, `~/Desktop/`). Python 3.12+ then silently skips hidden `.pth` files, so `import bettermemory` after an editable install fails with `ModuleNotFoundError`. A one-shot `chflags -R nohidden .venv` works for about 5 seconds before iCloud re-applies the flag. There is no good cure.

Two clean ways to avoid it:

1. **Name the venv anything else**: `venv`, `.env-mcp`, or `env`. Only the literal `.venv` triggers the iCloud heuristic. This repo defaults to `venv/` via `.envrc` and `UV_PROJECT_ENVIRONMENT`.
2. **Or keep the project outside `~/Documents/` and `~/Desktop/`**. The auto-hide does not fire elsewhere.

This is not a uv bug. `uv venv .venv` in `/tmp/` or `~/projects/` stays clean. It is macOS being opinionated about virtualenvs in iCloud-synced trees.

### YAML and frontmatter

The on-disk format is YAML frontmatter inside a markdown file. We use a tiny vendored parser (`src/bettermemory/_frontmatter.py`) instead of `python-frontmatter` for two reasons:

1. **Python 3.14 compatibility.** `python-frontmatter` 1.1.0 (the current release) calls `codecs.open()`, which 3.14 emits a `DeprecationWarning` for. The library is effectively unmaintained.
2. **Forced pure-Python YAML.** `yaml.CSafeDumper` has a state-machine bug under submodule coverage instrumentation (`--cov=bettermemory.store`). The vendored parser pins `yaml.SafeLoader` and `yaml.SafeDumper` directly. Memory frontmatter is dozens of bytes per write, so the libyaml C speedup is irrelevant.

Files written by the previous `python-frontmatter`-based code keep loading byte-for-byte, cross-tested against the upstream library before the swap.

## Optional: semantic dedup

By default, `memory_write` dedup uses Jaccard on stopword-stripped, kebab-expanded token sets. It is fast, deterministic, and has no extra deps. It catches lexical overlap well but misses paraphrases (`"the database"` vs `"Postgres"`, `"shipped"` vs `"released"`).

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

Behavior is unchanged when the toggle is off, so existing setups are untouched. If you flip the toggle without installing the extra, the server logs one WARNING and falls back to Jaccard. No errors, no surprises.

Embeddings are cached per-process keyed by `(memory_id, updated)`, so an updated memory busts its own cache entry. The first dedup call after server start pays the model load (around 1 to 2 seconds for `all-MiniLM-L6-v2`); subsequent calls are fast.

## Limitations

1. **Multi-process access on Unix is exercised.** The fcntl-based per-file locking in `store.py` and the parallel lock on the event log in `events.py` are stress-tested under contention by `tests/test_concurrency.py` (four worker processes with mixed write, update, remove, and restore on a shared root). Post-condition asserts confirm no torn writes, no orphan tombstones, and no malformed JSONL. Windows uses a no-op fallback (no `fcntl`); on Windows the recommendation is single-process.
2. **No conflict resolution.** If you edit a memory file by hand while the server is running, the next read will pick up your change but there is no merge story.
3. **No encryption.** Memories are plaintext on disk. Do not store secrets. Use OS-level disk encryption if you need it.
4. **memory_search is keyword-only.** Synonyms and paraphrases are not handled by `memory_search`. (`memory_write` dedup *can* use semantic similarity. See "Optional: semantic dedup" above.) A short stopword list is stripped from the *query* (so "how to bake sourdough" does not match every memory on shared filler tokens), but bodies stay unfiltered. Hits are returned with a `relevance` label calibrated on coverage. That distinguishes "1 of 4 query words matched" (low) from "all 3 matched" (high) without inventing a score threshold. The recency boost reads `max(created, updated)`, so editing a fact via `memory_update` ranks it as fresh.
5. **Disabled scopes do not survive restart.** Intentional: start each session fresh.

## What is out of scope

- Cloud sync. Memories are local. If you want sync, that is `git`'s job.
- Cross-user sharing. This is a single-user tool.
- Automatic memory extraction from transcripts. The whole point of this project is that auto-extraction is the failure mode it exists to fix.

## Origins

I started building this because the existing memory feature in Claude Code at the time auto-injected every stored "fact" into every system prompt. The more I taught the model about my preferences, the more it dragged irrelevant context into unrelated conversations. Asking for a Python tutorial would pull in my home-lab notes; a generic question would get coloured by some preference I had stated months ago. I wanted memory the model retrieved on demand, like any other tool. That is the design you see throughout.

The project was originally called `bettermemory`. Mid-build, the auto-injecting memory feature kept overriding my stated preference and renaming the package `memory-mcp` in conversation. The irony was sufficient motivation to finish.

Built by Mattias Rask.

## License

MIT. See [LICENSE](LICENSE).
