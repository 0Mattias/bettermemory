# bettermemory: Claude Code plugin

The Claude Code plugin wrapper for
[bettermemory](https://github.com/0Mattias/bettermemory) — a trust
layer between an agent and its own past: every retrieved fact carries
a staleness verdict, every use an attribution, and whether it helped
is measured rather than assumed.

The plugin bundles four things:

1. **MCP server registration** ([`.mcp.json`](.mcp.json)) — spawns
   `uvx bettermemory` as a stdio MCP server. 18 of the 27 tools
   register by default; the curation/power-user tools sit behind
   `[behavior] full_tool_surface = true` (see
   [docs/api.md](../docs/api.md)).
2. **Memory-discipline skill**
   ([`skills/bettermemory/SKILL.md`](skills/bettermemory/SKILL.md)) —
   the long-form retrieval/writing policy at the system-prompt level.
   The server's own `instructions` block carries a short summary;
   Claude Code truncates that block at ~1.8 KB, the skill has no cap.
3. **Stop hook** ([`hooks/hooks.json`](hooks/hooks.json)) — runs
   `uvx bettermemory audit-turn --quiet` at each turn end to log
   silent retrieval misses. Always exits 0, so a transient failure
   never surfaces as a hook-error banner.
4. **SessionStart hook** (same file) — runs
   `uvx bettermemory session-start` when a conversation opens and
   prints the per-scope memory counts for the current repository.
   Claude Code injects a SessionStart hook's stdout into the model's
   context, so the session begins knowing what is stored without
   spending a `memory_scope_overview` call on it. Reads the search
   index only (never memory bodies), records nothing, prints nothing
   when the store is empty, and always exits 0.

## Install

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

Requires [`uv`](https://docs.astral.sh/uv/) on `$PATH`; `uvx` fetches
bettermemory from PyPI on first run. To use a pre-installed binary
instead, `uv tool install bettermemory` and edit `.mcp.json` to
`"command": "bettermemory"`.

## Verify

Ask the model *"what memory tools do you have?"* — you should see tools
with the `mcp__bettermemory__` prefix. Then try *"remember that I
prefer hands-on tutorials with runnable code"*: the model should call
`memory_write` with `category="user-inference"`, ask for confirmation,
and a markdown file lands in `~/.claude-memory/`.

## Troubleshooting

```sh
uvx bettermemory doctor
```

Checks the install end to end (binary, config, storage, event log,
hook cadence, stale client paths), one fix hint per failed check.

## Uninstall

```text
/plugin uninstall bettermemory@bettermemory
```

Removes the server registration and skill. Memories on disk
(`~/.claude-memory/`) are not touched.

## Other clients

The plugin is the Claude Code-specific wrapper around the same MCP
server. For Claude Desktop, Cursor, Continue, Cline, or anything else,
see [docs/installation.md](../docs/installation.md) and
[docs/clients.md](../docs/clients.md).

## License

MIT. See [LICENSE](../LICENSE).
