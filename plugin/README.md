# bettermemory: Claude Code plugin

This is the Claude Code plugin wrapper for [bettermemory](https://github.com/0Mattias/bettermemory), a local file-backed memory MCP server with retrieval-on-demand.

The plugin bundles two things:

1. **The MCP server registration** ([`.mcp.json`](.mcp.json)). Installs bettermemory as an MCP server, so all 17 memory tools become available to Claude immediately on plugin enable.
2. **The memory-discipline skill** ([`skills/bettermemory/SKILL.md`](skills/bettermemory/SKILL.md)). Lands the opt-in retrieval policy, transparency requirement, verification obligation, and writing discipline at the system-prompt level. The MCP server's own `instructions` block carries a short summary, but Claude Code truncates that block at roughly 1.8KB; the SKILL is the long-form companion.

## Install

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

That is it. Claude Code starts the MCP server, loads the skill, and on the next turn the model has the full memory toolset and the policy.

### What the install does

- Adds `0Mattias/bettermemory` as a plugin marketplace pointing at the GitHub repo.
- Installs the `bettermemory` plugin from that marketplace, which:
  - Spawns `uvx bettermemory` as a stdio MCP server (`uvx` will fetch bettermemory from PyPI on first run if it is not already cached).
  - Loads the `bettermemory` skill so the model sees the memory policy in its system prompt.

## Requirements

- **Claude Code** with plugin support (`/plugin` command).
- **`uv`** ([Astral](https://docs.astral.sh/uv/)) on your PATH. bettermemory is a Python tool, and the plugin's `.mcp.json` uses `uvx` so users do not have to manually `pip install` first. If you prefer to use a pre-installed `bettermemory` binary, edit `.mcp.json` after install:

  ```json
  {
    "mcpServers": {
      "bettermemory": {
        "type": "stdio",
        "command": "bettermemory",
        "args": [],
        "env": {}
      }
    }
  }
  ```

  And run `pip install bettermemory` or `uv tool install bettermemory` first.

## What you get

After install, Claude has access to:

- `memory_search`, `memory_show`, `memory_list`, `memory_scope_overview` for retrieval.
- `memory_write`, `memory_update`, `memory_write_confirm`, `memory_write_cancel` for writing.
- `memory_remove`, `memory_restore`, `memory_list_tombstones` for lifecycle.
- `memory_verify`, `memory_record_use`, `memory_health`, `memory_rename_scope` for verification and curation.
- `memory_scope_disable`, `memory_scope_enable` for session-local muting.

Memories live in `~/.claude-memory/` as plain markdown plus YAML frontmatter. They are `grep`-able, `git`-versionable, and hand-editable. Override the location with the `$BETTERMEMORY_DIR` environment variable, or drop a `./.claude-memory/` directory in any project for project-scoped memory.

## Verify

After install, ask Claude:

> What memory tools do you have?

You should see the 17 tools listed with their `mcp__bettermemory__` prefix. Then:

> Remember that I prefer hands-on tutorials with runnable code, not screenshots.

Claude should call `memory_write` with the `learning-style` (or similar) scope, ask for confirmation if `category="user-inference"` (default for inferences about you), and confirm. Look in `~/.claude-memory/` to see the markdown file.

In a *new* session, ask:

> Walk me through pandas from zero to hero.

Claude should call `memory_search`, surface the stored preference, and tell you (*"Using your stored preference for code-driven tutorials…"*) before answering.

## Troubleshooting

Run:

```sh
uvx bettermemory doctor
```

That checks binary on PATH, config loadable, storage directory writable, memories parse cleanly, event log writable, and any MCP client config that references a stale path. Each failed check carries a one-line fix hint.

## Uninstall

```text
/plugin uninstall bettermemory@bettermemory
```

Memories on disk (`~/.claude-memory/`) are not touched. Uninstall removes the MCP server registration and the skill, not your data.

## Differences from manual install

The plugin path is the easiest install for Claude Code users. Equivalent setups exist for users who prefer to wire things up by hand or who use other MCP clients (Claude Desktop, Cursor, Continue, Cline). See the main [installation docs](../docs/installation.md) and [per-client setup](../docs/clients.md). The plugin packages the same thing those docs describe; nothing new.

## License

MIT, same as bettermemory itself. See [LICENSE](../LICENSE).
