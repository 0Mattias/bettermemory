# Installation

## 1. Install the package

```sh
# From a local clone
uv pip install -e .

# Or as an isolated tool (recommended)
uv tool install .
```

This puts a `memory-mcp` script on your `$PATH`.

```sh
which memory-mcp
# → /Users/<you>/.local/bin/memory-mcp  (or similar)
```

## 2. Register with Claude Code

Add the server to your Claude Code MCP config.

The config location depends on your platform — see the [Claude Code docs on MCP servers](https://docs.claude.com/en/docs/claude-code/mcp). Typical locations include:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json` (Claude Desktop)
- A project-level `.mcp.json` in the repo root (Claude Code)
- Or via `claude mcp add memory memory-mcp` if your version of the CLI supports it.

Whichever config file you edit, add:

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

If the binary isn't on `$PATH` for the Claude process, give the absolute path:

```json
{
  "mcpServers": {
    "memory": {
      "command": "/Users/you/.local/bin/memory-mcp",
      "args": []
    }
  }
}
```

## 3. Add the system-prompt addendum

This is the step that flips memory from "auto-applied" to "opt-in." Without it, the model will call `memory_search` too often and apply stale context.

Open `docs/system_prompt.md`, copy the block, and paste it into your project's `CLAUDE.md` (or your global system prompt). Both work — the project file takes precedence inside that project.

## 4. Verify

Start a Claude Code session and ask:

> What memory tools do you have?

You should see a list including `memory_search`, `memory_show`, `memory_write`, `memory_list`, `memory_remove`, `memory_scope_disable`, and `memory_scope_enable`.

Then try:

> Remember that I prefer hands-on tutorials with runnable code, not screenshots.

Claude should ask for confirmation, call `memory_write` with the scopes `learning-style` (or similar), and confirm. Look in `~/.claude-memory/` to see the markdown file that was created.

In a *new* session, ask:

> Walk me through pandas from zero to hero.

Claude should call `memory_search`, surface the stored preference, and tell you ("using your stored preference for code-driven tutorials…") before answering. That last sentence is the transparency requirement from the addendum — keep an eye on it; if Claude stops doing that, the addendum probably isn't being included.

## Troubleshooting

- **`memory-mcp` not found** when Claude tries to start the server: use the absolute path in the config.
- **Memories aren't being found by `memory_search`**: check `MEMORY_MCP_DIR` env var, then look at the directory the server logs at startup ("memory directory: …"). Project-scoped `./.claude-memory/` overrides global `~/.claude-memory/`.
- **The server isn't picking up changes after I edit a memory file**: it should — there's no in-memory cache. If you see staleness, restart Claude Code.
- **Claude is calling `memory_search` for everything**: the system-prompt addendum isn't being included. That's the most common cause.
