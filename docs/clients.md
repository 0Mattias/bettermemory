# Client setup

bettermemory speaks the standard MCP wire protocol over stdio, so any MCP-aware client can host it. This document collects the snippet shape and config-file location for each commonly-used host.

The fastest path is `bettermemory init --client X`, which writes the right snippet into the right file (idempotently). Show-and-tell mode (`bettermemory init` with no flags) prints the snippet plus all known config locations with `[✓]` markers for files that exist.

After any setup change, restart the client so it picks up the new MCP server entry.

## Claude Code — plugin path

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

The easiest path for Claude Code 2.x. Bundles the MCP server registration AND a system-prompt-level skill carrying the long-form policy. Uses `uvx bettermemory`, so the only prerequisite is [`uv`](https://docs.astral.sh/uv/). See [`../plugin/README.md`](../plugin/README.md).

## Claude Code — manual path

```sh
bettermemory init --client claude-code
```

Patches `~/.claude.json` (user scope). For project scope, point at the repo's `.mcp.json`:

```sh
bettermemory init --client claude-code --config-path .mcp.json
```

Project-scope wins when both are present — usually what you want for a project-scoped store (`./.claude-memory/`).

The manual path doesn't include the system-prompt skill the plugin ships. For the long-form policy with a manual install, paste [`system_prompt.md`](system_prompt.md) into your `CLAUDE.md`, or `bettermemory init --with-addendum` to print it.

## Claude Desktop

```sh
bettermemory init --client claude-desktop
```

Patches the platform-standard `claude_desktop_config.json`:

| OS      | Path |
|---------|------|
| macOS   | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux   | `~/.config/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%/Claude/claude_desktop_config.json` |

Restart Claude Desktop after the patch — it loads config at startup and doesn't watch the file.

## Cursor

```sh
bettermemory init --client cursor
```

Patches `~/.cursor/mcp.json` (user scope). For project scope:

```sh
bettermemory init --client cursor --config-path .cursor/mcp.json
```

Cursor's MCP support is per-window. A window opened before the patch won't see the new server until reloaded (Cmd-Shift-P → "Reload Window").

## Continue (legacy shape — see caveat)

```sh
bettermemory init --client continue
```

Patches `~/.continue/config.json` with an **object-shaped** `mcpServers`.

**Caveat — this shape is not read by current Continue.** As of 2026-07, Continue's released schema reads MCP servers as a **YAML list** in `~/.continue/config.yaml` (or as individual files under `~/.continue/mcpServers/`); the object-in-`config.json` form is a deprecated format current Continue ignores. `init --client continue` therefore prints a warning and writes the entry for backward compatibility with legacy Continue only. On a current install, add the server to `~/.continue/config.yaml` by hand instead:

```yaml
mcpServers:
  - name: bettermemory
    command: bettermemory   # or the absolute path init prints
    args: []
```

(Substitute the absolute binary path `bettermemory init` reports for `command` so a later reinstall is detectable — same rationale as the JSON snippet below.) MCP tools in Continue are only available in **agent mode**.

## Cline

```sh
bettermemory init --client cline
```

Patches Cline's MCP settings inside VS Code's `globalStorage`:

| OS      | Path |
|---------|------|
| macOS   | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Linux   | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Windows | `%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |

Default assumes standard VS Code. On Code-Insiders, Codium, or VSCodium, the `Code` directory becomes `Code - Insiders`, `VSCodium`, etc. — pass the right path:

```sh
bettermemory init --client cline --config-path \
  "$HOME/Library/Application Support/Code - Insiders/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"
```

Reload the VS Code window after the patch.

## Other clients

For anything not listed, run `bettermemory init` (no flags) to print the canonical snippet plus known config locations. Copy by hand into your client's MCP config file. If your client doesn't accept raw JSON config, embed the `mcp` SDK directly — see [`../examples/programmatic_client.py`](../examples/programmatic_client.py) for the wire-protocol shape.

## Verifying setup

```sh
bettermemory doctor
```

The `mcp_client_configs` check scans every known client's config and cross-checks the registered binary path against what `find_binary()` resolves to now. A mismatch (typically: reinstalled bettermemory into a different venv) is flagged with a one-line fix hint.

In the host itself, ask the model *"what memory tools do you have?"*. If the tools aren't listed, the server failed to start — `bettermemory doctor` will tell you why.

## Snippet shape

The MCP wire protocol is the same across hosts. Only the config file shape and location vary. Every supported client uses the same snippet:

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

The `bettermemory` server key is the default; override it with `--name` only if you have a strong reason (Claude Code prefixes its tool names with this key). When you run `bettermemory init`, the `command` is written as the *absolute* path that `find_binary()` resolves on your PATH — not the bare `bettermemory` shown here — so a later reinstall into a different venv is detectable (that's what `bettermemory doctor`'s `mcp_client_configs` check compares against). The bare-name form above also works if the binary stays on PATH; the plugin's `.mcp.json` instead uses `"command": "uvx"` with `"args": ["bettermemory"]`.

If you find a client whose snippet shape isn't this, please file an issue.

| Client          | Path | Init auto-patch |
|-----------------|------|-----------------|
| Claude Code (plugin) | `/plugin install bettermemory@bettermemory` | n/a (plugin-managed) |
| Claude Code (manual) | user `~/.claude.json` or project `.mcp.json` | yes |
| Claude Desktop  | platform-standard `claude_desktop_config.json` | yes |
| Cursor          | `~/.cursor/mcp.json` or `<repo>/.cursor/mcp.json` | yes |
| Continue        | `~/.continue/config.json` (legacy shape; current Continue wants a YAML list in `config.yaml` — see caveat above) | writes + warns |
| Cline           | VS Code `globalStorage/saoudrizwan.claude-dev/...` | yes (default VS Code only) |
