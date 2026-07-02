# bettermemory

[![PyPI](https://img.shields.io/pypi/v/bettermemory.svg)](https://pypi.org/project/bettermemory/)
[![CI](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg)](https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Claude forgets you. Every session starts from zero: the stack you
explained yesterday, the deploy quirk you debugged last week, the "we
use uv, not pip" you have now said eleven times.

bettermemory gives Claude Code — or any MCP client — memory that
survives the session. And it handles the part every memory layer
skips: memories go stale, and a confidently remembered stale fact is
worse than no memory at all. So every recall here comes with a
verdict on whether the fact still holds, and the model checks before
it trusts.

## What that feels like

Three weeks ago, mid-session:

```text
> for the record: staging deploys go through Fly now, the Render
  setup is dead

⏺ Stored: "Staging deploys via fly deploy --config fly.staging.toml;
  the old Render service is decommissioned." (projects:acme)
```

Today, brand-new session, no context given:

```text
> staging is 404ing after every deploy. ideas?

⏺ You have a stored note that staging deploys through Fly and Render
  is gone — last verified 3 weeks and 41 commits ago, so checking it
  before relying on it… fly.staging.toml still exists, but its
  internal_port changed on Monday. That's the 404: the app listens
  on 8080, the config still says 3000.

> fix it

⏺ Fixed. Re-verified the memory too, so the next session inherits it
  fresh instead of re-checking.
```

(Illustrative transcript.) Two things happened that a raw session
cannot do: it remembered across a three-week gap, and it distrusted
itself exactly enough to catch that the world had moved since.

Want to see the distrust moment for real before wiring anything up?

```sh
uv tool install bettermemory
bettermemory try     # 60-second offline demo in a throwaway store
```

It writes a memory citing a file, deletes the file, and shows the
next search flag the memory as stale.

## Why this one

- **It knows when it might be wrong.** Facts rot — files move,
  decisions get reversed, a commit from Tuesday invalidates a config
  note. Every hit carries a staleness verdict built from calendar
  age, the file paths the memory cites, and the commits landed since
  it was last confirmed. Trust is checked, not assumed.
- **It stays out of your context.** Nothing is auto-injected.
  Retrieval is a deliberate tool call, and the model is instructed to
  say when a stored memory shaped its answer — no silent steering
  from last month's context.
- **It refuses to hoard.** Writes pass gates: transient state bounces
  ("we just merged…" stops being true next week), secret-shaped
  tokens bounce, near-duplicates bounce, and claims about *you* stage
  for your confirmation before they commit.
- **It's your data, on your disk.** One markdown file per memory —
  greppable, hand-editable, git-syncable across machines. No
  database, no cloud, no account. MIT.
- **It proves it's helping.** A built-in eval reports how often
  retrieved memory actually shaped a reply, and how often the model
  should have retrieved but didn't. Numbers, not vibes.

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

Already using Claude Code's built-in auto-memory? `bettermemory
ingest` imports those files once. Per-client setup:
[docs/clients.md](docs/clients.md); troubleshooting:
[docs/installation.md](docs/installation.md).

---

Everything below is mechanics — useful when you want to know *how*,
unnecessary for using it.

## Under the hood

The staleness verdict the model acts on, as it appears on a search
hit:

```jsonc
{
  "snippet": "Auth middleware lives in src/auth/middleware.py …",
  "relevance": "high",
  "staleness_verdict": "spot_check_recommended",
  "path_drift": { "missing": ["src/auth/middleware.py"] },  // file moved
  "commit_drift_count": 12   // commits since the fact was last verified
}
```

The model repoints the path with `memory_update`, attests the rest
with `memory_verify`, and answers from the corrected memory.

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
- Hybrid search (keyword + BM25), with plural-folding and CJK-capable
  tokenization. An optional semantic leg needs the `embeddings` extra
  plus a config opt-in: `[behavior] search_mode = "semantic"` or
  `semantic_dedup = true`.
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
scopes: [projects:acme, infrastructure]
confidence: high
source: explicit-statement
---
Staging deploys via `fly deploy --config fly.staging.toml`; the old
Render service is decommissioned.
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
