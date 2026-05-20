# Installation

## 1. Install the package

```sh
uv tool install bettermemory       # recommended: isolated tool install
pipx install bettermemory          # or pipx
pip install bettermemory           # or plain pip into a venv
```

Optional extras:

```sh
uv tool install 'bettermemory[embeddings]'   # sentence-transformers for semantic / hybrid search
uv tool install 'bettermemory[ui]'           # FastAPI + uvicorn for `bettermemory ui`
uv tool install 'bettermemory[embeddings,ui]'
```

Python 3.11–3.14. From a development clone: `uv tool install .` (or `uv pip install -e .` for editable).

Either path puts a `bettermemory` script on `$PATH`.

## 2. Register with your MCP client

```sh
bettermemory init --client claude-code      # or claude-desktop / cursor / continue / cline
```

Idempotently merges the bettermemory entry into the right config file (creating the file if needed). Re-running is safe; an unchanged entry is a no-op; a stale binary path is updated.

If `init` doesn't know your client, run it with no flags:

```sh
bettermemory init
```

Prints the canonical JSON snippet plus the known config locations, with `[✓]` markers for files that exist. The snippet itself:

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

`type: "stdio"` and `env: {}` are optional in the MCP spec but match what `claude mcp add` writes by default.

If `bettermemory` isn't on the spawned client's `$PATH` (common for GUI clients launched from Finder or Launchpad on macOS), `init` substitutes the absolute path. `bettermemory init --print-only --client <name>` previews the patch without writing.

The server key under `mcpServers` becomes the tool-name prefix (`mcp__bettermemory__memory_search`). The 1.0 default was the shorter `memory`; 1.1+ defaults to `bettermemory` because the shorter name collided. `bettermemory init` detects and removes legacy `memory` entries pointing at the same binary.

## 3. Verify

In a Claude Code session:

> What memory tools do you have?

You should see `memory_search`, `memory_show`, `memory_write`, etc. Then:

> Remember that I prefer hands-on tutorials with runnable code, not screenshots.

Claude calls `memory_write` with `category="user-inference"`, asks for confirmation, and commits. Look in `~/.claude-memory/` for the markdown file.

In a *new* session: *"Walk me through pandas from zero to hero."* Claude calls `memory_search`, surfaces the preference, and says *"Using your stored preference for code-driven tutorials…"* before answering.

## 4. Optional: long-form policy

The server's `instructions` block carries the core contract and lands at the system-prompt level on every compliant client. Fresh installs behave correctly out of the box for most workflows.

Two cases where you want more:

- **Claude Code and you want the long-form policy.** Claude Code truncates the `instructions` block at ~1.8 KB, which fits the core rules but not the full writing-discipline / scope-hygiene / confirmation-tier surface. Install the [plugin](../plugin/README.md) — its `SKILL.md` ships without the cap.
- **Any other client (or you don't want the plugin).** Paste the fenced block from [`system_prompt.md`](system_prompt.md) into your project's `CLAUDE.md` or global system prompt.

## Troubleshooting

```sh
bettermemory doctor
```

Checks binary on PATH, config loadable, storage writable, memories parse cleanly, event log writable, semantic-dedup extras present (when enabled), and any MCP client config referencing a stale binary path. Each failed check has a one-line fix hint. `--json` for machine-readable; exit codes `0` ok, `1` warn, `2` fail.

Common failures it catches:

- **`bettermemory` not found** when Claude tries to start the server. Use the absolute path (`bettermemory init --client X` does this automatically).
- **Memories not found by `memory_search`.** Check `BETTERMEMORY_DIR` and the startup-log "memory directory" line. Project-scoped `./.claude-memory/` overrides global `~/.claude-memory/`.
- **Server not picking up file edits.** There is no in-memory cache; restart Claude Code if you see staleness.
- **Claude over-calling `memory_search`.** Verify the client is surfacing the server's `instructions` block. Most do automatically; if yours doesn't, paste [`system_prompt.md`](system_prompt.md) into `CLAUDE.md` as a fallback.
