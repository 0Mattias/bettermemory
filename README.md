# bettermemory

[![PyPI](https://img.shields.io/pypi/v/bettermemory.svg)](https://pypi.org/project/bettermemory/)
[![CI](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg)](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Persistent memory for AI coding agents, served over MCP. Memories are
plain markdown files on disk, and every retrieval carries a verdict on
whether the stored fact is still true.

Stored facts rot: files move, preferences change, a commit two days ago
can invalidate a config note. bettermemory checks each hit against
calendar age, the file paths it cites, and the commits landed since it
was last confirmed, and returns the rollup as a per-hit
`staleness_verdict` — so the model spot-checks before it trusts:

```jsonc
{
  "snippet": "Auth middleware lives in src/auth/middleware.py …",
  "relevance": "high",
  "staleness_verdict": "spot_check_recommended",
  "path_drift": { "missing": ["src/auth/middleware.py"] },  // file moved
  "commit_drift_count": 12   // commits since the fact was last verified
}
```

The model repoints the path with `memory_update`, attests the rest with
`memory_verify`, and answers from the corrected memory. No database, no
cloud, no account.

## Install

Claude Code:

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

Any other MCP client (Claude Desktop, Cursor, Continue, Cline):

```sh
uv tool install bettermemory        # or pipx / pip
bettermemory init --client claude-desktop
```

`bettermemory try` runs a 60-second offline demo in a throwaway store:
it writes a memory citing a file, deletes the file, and shows the next
search flag the memory stale. Already using Claude Code's built-in
auto-memory? `bettermemory ingest` imports those files once.

Per-client setup: [docs/clients.md](docs/clients.md). Install details
and troubleshooting: [docs/installation.md](docs/installation.md).

## Features

- Staleness verdict on every retrieval: calendar age + cited-path
  drift + git commit drift, with the missing paths listed inline.
- Retrieval is opt-in. `memory_search` is a deliberate tool call;
  nothing is auto-injected into context.
- Claims about the user always stage for confirmation before commit.
- Write gates instead of trust: durability check (rejects transient
  state), credential check (rejects secret-shaped tokens), duplicate
  and tombstone dedup, scope-mismatch check, optional groundedness
  check against the source transcript.
- Usage telemetry: `memory_record_use` logs which sentence of a reply
  each memory shaped; a turn-end probe flags retrievals the model
  should have made but didn't; `memory_health` and `memory_curate`
  report and act on the resulting rot.
- Hybrid search (keyword + BM25). An optional semantic leg needs the
  `embeddings` extra plus a config opt-in:
  `[behavior] search_mode = "semantic"` or `semantic_dedup = true`.
- Typed inter-memory links (`supersedes`, `contradicts`, `extends`,
  `depends_on`), surfaced as trust signals at retrieval.
- Auto-scoping by repo and worktree; explicit cross-project queries.
- Episodes: a sibling journal tier for run-state that never pollutes
  durable search, with promotion when a takeaway hardens into a fact.
- Tombstones instead of deletes; everything is restorable.
- Scales past ~500 memories via a derived SQLite FTS5 index. The
  markdown files stay canonical; `bettermemory reindex` rebuilds.
- Cross-machine sync over your own git remote, a local web UI, and an
  eval CLI (`memory_helped_rate` / `endorsement_rate` /
  `silent_miss_rate`, see [docs/eval.md](docs/eval.md)).

## Storage

One file per memory, grep-able and hand-editable:

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

Verification attestations, origin (repo/branch/worktree), and typed
links are optional frontmatter, added only when populated. Removed
memories move to `.tombstones/` with their `removed_reason`; episodes
live under `episodes/<session_id>/` with a 30-day TTL.

The store resolves to `$BETTERMEMORY_DIR` if set, else `./.claude-memory/`
if it exists, else `~/.claude-memory/`.

## Tools

25 MCP tools; 18 register by default. Seven curation/power-user tools
sit behind `[behavior] full_tool_surface = true`, and most of those
have a CLI counterpart, so the default per-turn tool context stays
small. Grouped: retrieval, writing (with a staged-confirm flow),
lifecycle, verification, curation, session-local scope toggles, and
episodes. Signatures, defaults, and return shapes: [docs/api.md](docs/api.md).

## CLI

`bettermemory` with no arguments is the MCP server (stdio). It also
provides:

```text
bettermemory try              # offline staleness demo
bettermemory init --client X  # register with a client (idempotent)
bettermemory doctor           # diagnose install state
bettermemory health           # curation rollup
bettermemory consolidate      # dedup/demote pass (dry-run; --llm for more)
bettermemory eval             # the three metrics, with CIs
bettermemory sync push|pull   # git-based cross-host sync
bettermemory ui               # local curation UI ([ui] extra)
```

`bettermemory <command> --help` for flags; `reindex`, `ingest`,
`tombstones`, `proposals`, `rename-scope`, `episodes`, and `export`
also exist.

## Configuration

`config.toml` lives under platformdirs (`~/Library/Application
Support/bettermemory/` on macOS, `~/.config/bettermemory/` on Linux).
Defaults are sensible; most installs never edit it. See
[docs/api.md](docs/api.md) and the file's own comments for the knobs.

## Limitations

- No encryption at rest. Don't store secrets (a write-time check
  refuses secret-shaped tokens); use disk encryption if you need it.
- Sync conflicts are git merge conflicts; there is no auto-resolution.
- The web UI is read-mostly; writes happen in-conversation.
- Multi-process file locking is a no-op on Windows.

## Docs

- [docs/api.md](docs/api.md) — the tool contract: signatures,
  defaults, return shapes.
- [docs/clients.md](docs/clients.md) / [docs/installation.md](docs/installation.md)
  — setup.
- [docs/eval.md](docs/eval.md) — metric definitions and the eval CLI.
- [docs/ROADMAP.md](docs/ROADMAP.md) — planned and not-planned work.
- [docs/incidents/](docs/incidents/) — postmortems for memory-rot bugs
  the verification surface should have caught.
- [CHANGELOG.md](CHANGELOG.md) — what shipped, release by release.
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup and the 3.x
  compatibility contract.

MIT licensed — see [LICENSE](LICENSE).
