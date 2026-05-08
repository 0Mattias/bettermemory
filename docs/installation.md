# Installation

## 1. Install the package

```sh
# From a local clone
uv pip install -e .

# Or as an isolated tool (recommended)
uv tool install .
```

This puts a `bettermemory` script on your `$PATH`.

```sh
which bettermemory
# → /Users/<you>/.local/bin/bettermemory  (or similar)
```

## 2. Register with Claude Code

Add the server to your Claude Code MCP config.

The config location depends on your platform — see the [Claude Code docs on MCP servers](https://docs.claude.com/en/docs/claude-code/mcp). Typical locations include:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json` (Claude Desktop)
- A project-level `.mcp.json` in the repo root (Claude Code)
- Or via `claude mcp add memory bettermemory` if your version of the CLI supports it.

Whichever config file you edit, add:

```json
{
  "mcpServers": {
    "memory": {
      "command": "bettermemory",
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
      "command": "/Users/you/.local/bin/bettermemory",
      "args": []
    }
  }
}
```

## 3. Verify

Start a Claude Code session and ask:

> What memory tools do you have?

You should see a list including `memory_search`, `memory_show`, `memory_write`, `memory_list`, `memory_remove`, `memory_scope_disable`, and `memory_scope_enable`.

Then try:

> Remember that I prefer hands-on tutorials with runnable code, not screenshots.

Claude should ask for confirmation, call `memory_write` with the scopes `learning-style` (or similar), and confirm. Look in `~/.claude-memory/` to see the markdown file that was created.

In a *new* session, ask:

> Walk me through pandas from zero to hero.

Claude should call `memory_search`, surface the stored preference, and tell you ("using your stored preference for code-driven tutorials…") before answering — that last sentence is the transparency requirement, baked into the server's MCP `instructions` block.

## 4. Optional: tighten with the system-prompt addendum

The server's MCP `instructions` block already carries the load-bearing policy: opt-in retrieval, the when-to-search rules, the transparency requirement, and the verification obligation. A fresh install behaves correctly without further configuration.

For an additional layer of discipline (more elaborate scope hygiene reminders, the confirmation-tier policy for preferences vs. facts, expanded record-use guidance), open [`../docs/system_prompt.md`](system_prompt.md), copy the fenced block, and paste it into your project's `CLAUDE.md` or your global system prompt. The addendum complements the server `instructions`; it does not replace them.

## Troubleshooting

- **`bettermemory` not found** when Claude tries to start the server: use the absolute path in the config.
- **Memories aren't being found by `memory_search`**: check `BETTERMEMORY_DIR` env var, then look at the directory the server logs at startup ("memory directory: …"). Project-scoped `./.claude-memory/` overrides global `~/.claude-memory/`.
- **The server isn't picking up changes after I edit a memory file**: it should — there's no in-memory cache. If you see staleness, restart Claude Code.
- **Claude is over-calling `memory_search`**: check that your client is surfacing the server's `instructions` block in the system prompt. Most MCP clients do this automatically; if yours doesn't, paste `docs/system_prompt.md` into your `CLAUDE.md` as a fallback.
