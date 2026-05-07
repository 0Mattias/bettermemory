# memory-mcp

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
# from a clone
uv pip install -e .

# or as an isolated tool (recommended)
uv tool install .
```

Python ≥ 3.11.

## Quick start with Claude Code

1. Install (above).
2. Register the MCP server. Add to Claude Code's MCP config:
   ```json
   {
     "mcpServers": {
       "memory": {
         "command": "memory-mcp",
         "args": []
       }
     }
   }
   ```
3. Add the system-prompt addendum (in [`docs/system_prompt.md`](docs/system_prompt.md)) to your project's `CLAUDE.md` or system prompt. **Without this, the model will overuse memory** — the addendum is what flips it into opt-in mode.
4. Sanity-check: ask Claude *"what memory tools do you have?"*

See [`docs/installation.md`](docs/installation.md) for more detail.

## Tools

| Tool | What it does |
|---|---|
| `memory_search(query, scopes?, max_results?)` | Rank and return memory hits with snippets. Each hit carries `relevance: "high" \| "medium" \| "low"` and `match_terms` (the query words that actually hit) — branch on `relevance`, not the raw `score`. |
| `memory_show(id)` | Full body of one memory. |
| `memory_write(content, scopes, confidence?, source?)` | Create a new memory. |
| `memory_list(scopes?)` | List active memories — IDs and one-line summaries, no body. |
| `memory_remove(id, reason)` | Tombstone a memory. |
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

## Where memories live

Resolution order:

1. `$MEMORY_MCP_DIR` env var, if set.
2. `./.claude-memory/` if it exists in the working directory (project-scoped).
3. `~/.claude-memory/` (global).

Crossing projects is *not* default behavior. A memory written while working on Project A only appears when working on Project B if you stored it globally.

## Config

The config file is created on first run at the platform-standard config dir
(via `platformdirs`):

- macOS: `~/Library/Application Support/memory-mcp/config.toml`
- Linux: `~/.config/memory-mcp/config.toml`
- Windows: `%LOCALAPPDATA%\memory-mcp\config.toml`

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
```

## Scopes

Scopes are lowercase, alphanumeric, with hyphens or colons (for nesting). Examples:

- `tools`, `learning-style`, `infrastructure`, `personal-context`
- `projects:foo`, `projects:bar:subsystem`

Avoid the catch-all `general` scope — it defeats the whole point.

## Development

```sh
# direnv users: just `cd` in — `.envrc` exports UV_PROJECT_ENVIRONMENT=venv.
# Otherwise:
export UV_PROJECT_ENVIRONMENT=venv

uv sync --extra dev
source venv/bin/activate
pytest -q

# With coverage (spec asks for >80% on store.py and search.py)
pytest --cov=memory_mcp.store --cov=memory_mcp.search --cov-report=term-missing
```

`tests/conftest.py` puts `src/` on `sys.path` directly, so the suite passes even if the editable install is in a weird state. `pytest -q` is a sanity check that doesn't depend on `uv pip install -e .` succeeding.

### macOS gotcha: the env is `venv/`, not `.venv/`

macOS Sequoia auto-applies `UF_HIDDEN` to anything literally named `.venv` inside iCloud-synced folders (`~/Documents/`, `~/Desktop/`). Python 3.12+ then silently skips hidden `.pth` files, so `import memory_mcp` after an editable install fails with `ModuleNotFoundError`. A one-shot `chflags -R nohidden .venv` works for ~5 seconds before iCloud re-applies the flag — there is no good cure.

Two clean ways to avoid it:

1. **Name the venv anything else** — `venv`, `.env-mcp`, `env`. Only the literal `.venv` triggers the iCloud heuristic. This repo defaults to `venv/` via `.envrc` + `UV_PROJECT_ENVIRONMENT`.
2. **Or keep the project outside `~/Documents/` / `~/Desktop/`** — the auto-hide doesn't fire elsewhere.

This is not a uv bug. `uv venv .venv` in `/tmp/` or `~/projects/` stays clean. It's macOS being opinionated about virtualenvs in iCloud-synced trees.

### YAML + frontmatter

The on-disk format is YAML frontmatter inside a markdown file. We use a tiny vendored parser (`src/memory_mcp/_frontmatter.py`) instead of `python-frontmatter` for two reasons:

1. **Python 3.14 compatibility.** `python-frontmatter` 1.1.0 (the current release) calls `codecs.open()`, which 3.14 emits a `DeprecationWarning` for. The library is effectively unmaintained.
2. **Forced pure-Python YAML.** `yaml.CSafeDumper` has a state-machine bug under submodule coverage instrumentation (`--cov=memory_mcp.store`). The vendored parser pins `yaml.SafeLoader` / `yaml.SafeDumper` directly. Memory frontmatter is dozens of bytes per write, so the libyaml C speedup is irrelevant.

Files written by the previous `python-frontmatter`-based code keep loading byte-for-byte; cross-tested against the upstream library before the swap.

## Limitations

1. **Single-process access.** Concurrent writes from two MCP servers pointed at the same directory may corrupt files. A file-lock guard is in place; multi-process is still untested.
2. **No conflict resolution.** If you edit a memory file by hand while the server is running, the next read will pick up your change but there's no merge story.
3. **No encryption.** Memories are plaintext on disk. Don't store secrets — use OS-level disk encryption if you need it.
4. **Search is keyword-only.** Synonyms, paraphrases, semantic similarity — not handled. Embeddings are a Phase 2 feature. A short stopword list is stripped from the *query* (so "how to bake sourdough" doesn't match every memory on shared filler tokens), but bodies stay unfiltered. Hits are returned with a `relevance` label calibrated on coverage — distinguish "1 of 4 query words matched" (low) from "all 3 matched" (high) without inventing a score threshold.
5. **Disabled scopes don't survive restart.** Intentional — start each session fresh.

## What's out of scope

- Cloud sync. Memories are local. If you want sync, that's `git`'s job.
- Cross-user sharing. Single-user tool.
- Automatic memory extraction from transcripts. The whole point of this project is that auto-extraction is the failure mode it exists to fix.
