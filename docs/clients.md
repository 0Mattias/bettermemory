# Client setup

bettermemory speaks the standard MCP wire protocol over stdio, so any MCP-aware client should be able to host it. This document collects the config snippet and config-file location for each commonly-used MCP host.

The fastest path is `bettermemory init --client X`, which writes the right snippet into the right file. It is idempotent: re-running is safe, a stale binary path is updated, and an exact-match entry is a no-op. Where `init` does not auto-patch, the show-and-tell mode (`bettermemory init` with no flags) prints the snippet plus all known config locations with `[✓]` markers showing which exist on your machine.

After any setup change, restart the client process so it picks up the new MCP server entry.

## Claude Code (CLI), plugin path

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

This is the easiest path for Claude Code 2.x. The plugin bundles the MCP server registration AND a system-prompt-level skill carrying the opt-in retrieval policy. The plugin uses `uvx bettermemory`, so the only prerequisite on your machine is [`uv`](https://docs.astral.sh/uv/). uvx fetches bettermemory from PyPI on first run and caches it for subsequent invocations. See [`../plugin/README.md`](../plugin/README.md) for the full plugin documentation.

## Claude Code (CLI), manual path

```sh
bettermemory init --client claude-code
```

Patches `~/.claude.json` (user scope), the same file `claude mcp add` writes to. For project scope, point at the repo's `.mcp.json` instead:

```sh
bettermemory init --client claude-code --config-path .mcp.json
```

Project-scope wins when both are present, and that is usually what you want for a project-scoped store (`./.claude-memory/`).

The manual path does not include the system-prompt-level skill the plugin ships. If you want the long-form policy (writing discipline, scope hygiene, confirmation-tier guidance) with the manual install, paste [`system_prompt.md`](system_prompt.md) into your project's `CLAUDE.md`, or pass `--with-addendum` to `bettermemory init` to print it.

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

Restart Claude Desktop after the patch. It loads the config at startup and does not watch the file.

## Cursor

```sh
bettermemory init --client cursor
```

Patches `~/.cursor/mcp.json` (user scope). For project scope, point at `<repo>/.cursor/mcp.json`:

```sh
bettermemory init --client cursor --config-path .cursor/mcp.json
```

Cursor's MCP support is configured per-window. A window opened before the patch will not see the new server until reloaded (Cmd-Shift-P, then "Reload Window").

## Continue (VS Code or JetBrains extension)

```sh
bettermemory init --client continue
```

Patches `~/.continue/config.json`. Continue auto-reloads when the config file changes, so you usually do not need to restart the editor.

## Cline (VS Code extension)

```sh
bettermemory init --client cline
```

Patches Cline's MCP settings inside VS Code's `globalStorage`:

| OS      | Path |
|---------|------|
| macOS   | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Linux   | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Windows | `%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |

The default assumes you are on standard VS Code. If you are on Code-Insiders, Codium, or VSCodium, the `Code` directory is renamed to `Code - Insiders`, `VSCodium`, etc. Pass the right path explicitly:

```sh
bettermemory init --client cline --config-path \
  "$HOME/Library/Application Support/Code - Insiders/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"
```

Reload the VS Code window after the patch so Cline picks up the new server.

## Other MCP clients

For anything not listed above, run:

```sh
bettermemory init
```

This prints the canonical JSON snippet plus the known config locations for each supported client. Copy the snippet into your client's MCP config file by hand. If the client does not have a place for raw JSON configuration, check whether the `mcp` SDK can be embedded directly. The [programmatic example](../examples/programmatic_client.py) shows the wire-protocol shape your client needs to speak.

## Verifying setup

Regardless of which client you used, confirm the install with:

```sh
bettermemory doctor
```

The `mcp_client_configs` check scans every known client's config and cross-checks the registered binary path against what `find_binary()` resolves to right now. A mismatch (typically: you reinstalled bettermemory into a different venv and the registered path is stale) is flagged with a one-line fix hint.

In the host itself, ask the model:

> What memory tools do you have?

You should see a list including `memory_search`, `memory_show`, `memory_write`, etc. If not, the server failed to start. `bettermemory doctor` will tell you why.

## Verification status of each client snippet

The MCP wire protocol is the same across hosts. The only thing that varies between clients is the *config file shape and location*. The table below records what each client's config has been verified against:

| Client          | Snippet shape | Path | Init auto-patch |
|-----------------|---------------|------|-----------------|
| Claude Code (plugin) | bundled `.mcp.json` plus `SKILL.md` | `/plugin install bettermemory@bettermemory` | n/a (managed by the plugin system) |
| Claude Code (manual) | `mcpServers` map | user `~/.claude.json` or project `.mcp.json` | yes |
| Claude Desktop  | `mcpServers` map | platform-standard `claude_desktop_config.json` | yes |
| Cursor          | `mcpServers` map | `~/.cursor/mcp.json` or `<repo>/.cursor/mcp.json` | yes |
| Continue        | `mcpServers` map | `~/.continue/config.json` | yes |
| Cline           | `mcpServers` map | VS Code `globalStorage/saoudrizwan.claude-dev/...` | yes (default VS Code only) |

The "snippet shape" is the same `{"mcpServers": {"bettermemory": {"type": "stdio", "command": "...", "args": [], "env": {}}}}` for every supported client because that is the shape the MCP spec standardizes (`type` and `env` are optional but match what `claude mcp add` writes by default). Differences are entirely in *where* the file lives. If you find a client whose snippet shape is *not* this, please file an issue. We will add support.

The 1.0 default for the server key was the shorter `memory`. 1.1 defaults to `bettermemory` because the shorter name collided with other MCP servers and Claude Code's evolving built-in memory features. `bettermemory init` detects a legacy `memory` entry pointing at the same binary and removes it as part of the patch, so you do not end up with the server registered twice. If you have a hand-written `memory` entry pointing at a *different* binary (some other memory MCP server), `init` leaves it alone.
