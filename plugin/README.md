# bettermemory: Claude Code plugin

The Claude Code plugin wrapper for [bettermemory](https://github.com/0Mattias/bettermemory) — persistent memory between sessions, retrieved on demand. Memories live on disk as markdown + YAML.

The plugin bundles two things:

1. **MCP server registration** ([`.mcp.json`](.mcp.json)) — spawns `uvx bettermemory` as a stdio MCP server. All 18 memory tools become available on plugin enable.
2. **Memory-discipline skill** ([`skills/bettermemory/SKILL.md`](skills/bettermemory/SKILL.md)) — lands the opt-in retrieval policy, transparency requirement, and writing discipline at the system-prompt level. The MCP server's own `instructions` block carries a short summary; the skill is the long-form companion (Claude Code truncates the `instructions` block at ~1.8 KB).

## Install

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

Requires `uv` ([Astral](https://docs.astral.sh/uv/)) on `$PATH`. `uvx` fetches bettermemory from PyPI on first run.

If you prefer a pre-installed `bettermemory` binary, edit `.mcp.json` after install to use `"command": "bettermemory"` instead of `uvx`, and `uv tool install bettermemory` (or `pipx install bettermemory`) first.

## Verify

```text
What memory tools do you have?
```

You should see the 18 tools listed with the `mcp__bettermemory__` prefix. Then:

```text
Remember that I prefer hands-on tutorials with runnable code, not screenshots.
```

Claude should call `memory_write` with `category="user-inference"`, ask for confirmation, and commit. Look in `~/.claude-memory/` for the markdown file.

In a fresh session, ask *"Walk me through pandas from zero to hero"* — Claude should call `memory_search`, surface the preference, and say *"Using your stored preference for code-driven tutorials…"* before answering.

## Troubleshooting

```sh
uvx bettermemory doctor
```

Checks binary on PATH, config loadable, storage writable, memories parse cleanly, event log writable, and any client config referencing a stale path. Each failed check has a one-line fix hint.

## Uninstall

```text
/plugin uninstall bettermemory@bettermemory
```

Memories on disk (`~/.claude-memory/`) are not touched. Uninstall removes the server registration and skill, not your data.

## Other clients

For Claude Desktop, Cursor, Continue, Cline, or anything else, see the main [installation docs](../docs/installation.md) and [per-client setup](../docs/clients.md). The plugin is just the Claude Code-specific wrapper around the same MCP server.

## License

MIT. See [LICENSE](../LICENSE).
