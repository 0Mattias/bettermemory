# Installation

## 1. Install the package

```sh
# recommended — isolated install via uv tool
uv tool install bettermemory

# or pipx
pipx install bettermemory

# or pip into a venv
pip install bettermemory
```

From a development clone, swap the first line for `uv tool install .` (or `uv pip install -e .` if you want an editable install).

Either path puts a `bettermemory` script on your `$PATH`.

```sh
which bettermemory
# → /Users/<you>/.local/bin/bettermemory  (or similar)
```

## 2. Register with your MCP client

Run:

```sh
bettermemory init --client claude-code      # or: claude-desktop, cursor, continue
```

This idempotently merges the bettermemory entry into the right config file for your client (creating the file if needed). Re-running is safe — an unchanged entry is a no-op, a stale binary path is updated.

If `init` doesn't know your client, run it with no flags for show-and-tell mode:

```sh
bettermemory init
```

This prints the canonical JSON snippet plus a list of common per-client config-file locations, with `[✓]` markers showing which exist on your machine. Copy the snippet into the right file by hand.

For the curious, the snippet itself is:

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

If `bettermemory` isn't on the spawned client process's `$PATH` (a common failure mode for GUI clients launched from Finder/Launchpad on macOS), `init` substitutes the absolute path — that's the same path you'd write by hand. `bettermemory init --print-only --client <name>` shows you exactly what would be written.

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

Before reading the list below, run:

```sh
bettermemory doctor
```

That checks the most common breakage points — binary on PATH, config loadable, storage directory writable, memories parse cleanly, event log writable, semantic-dedup extras present (when enabled), and any MCP client config that references a stale or non-existent binary path. Each check that fails or warns includes a one-line fix hint. Use `--json` for machine-readable output; exit codes are `0` (ok), `1` (warn), `2` (fail) so the command is scriptable.

Common failures it catches:

- **`bettermemory` not found** when Claude tries to start the server: use the absolute path in the config (`bettermemory init --client X` does this automatically; `bettermemory doctor` flags it).
- **Memories aren't being found by `memory_search`**: check `BETTERMEMORY_DIR` env var, then look at the directory the server logs at startup ("memory directory: …"). Project-scoped `./.claude-memory/` overrides global `~/.claude-memory/`. Doctor's `storage_directory` check shows the resolved path explicitly.
- **The server isn't picking up changes after I edit a memory file**: it should — there's no in-memory cache. If you see staleness, restart Claude Code.
- **Claude is over-calling `memory_search`**: check that your client is surfacing the server's `instructions` block in the system prompt. Most MCP clients do this automatically; if yours doesn't, paste `docs/system_prompt.md` into your `CLAUDE.md` as a fallback.
