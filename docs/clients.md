# Client setup

bettermemory speaks the standard MCP wire protocol over stdio, so any
MCP-aware client should be able to host it. This document collects the
config snippet and config-file location for each commonly-used MCP host.

The fastest path is `bettermemory init --client X`, which writes the
right snippet into the right file (idempotently — re-running is safe,
a stale binary path is updated, an exact-match entry is a no-op).
Where `init` doesn't auto-patch, the show-and-tell mode
(`bettermemory init` with no flags) prints the snippet plus all known
config locations with `[✓]` markers showing which exist on your
machine.

After any setup change, restart the client process so it picks up the
new MCP server entry.

## Claude Code (CLI)

```sh
bettermemory init --client claude-code
```

Patches `~/.claude.json` (user scope). For project scope, point at the
repo's `.mcp.json` instead:

```sh
bettermemory init --client claude-code --config-path .mcp.json
```

Project-scope wins when both are present, and that's usually what you
want for a project-scoped store (`./.claude-memory/`).

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

Restart Claude Desktop after the patch — it loads the config at startup
and doesn't watch the file.

## Cursor

```sh
bettermemory init --client cursor
```

Patches `~/.cursor/mcp.json` (user scope). For project scope, point at
`<repo>/.cursor/mcp.json`:

```sh
bettermemory init --client cursor --config-path .cursor/mcp.json
```

Cursor's MCP support is configured per-window; a window opened before
the patch won't see the new server until reloaded (Cmd-Shift-P → "Reload
Window").

## Continue (VS Code / JetBrains extension)

```sh
bettermemory init --client continue
```

Patches `~/.continue/config.json`. Continue auto-reloads when the config
file changes, so you usually don't need to restart the editor.

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

The default assumes you're on standard VS Code. If you're on
Code-Insiders, Codium, or VSCodium, the `Code` directory is renamed to
`Code - Insiders`, `VSCodium`, etc. — pass the right path explicitly:

```sh
bettermemory init --client cline --config-path \
  "$HOME/Library/Application Support/Code - Insiders/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"
```

Reload the VS Code window after the patch so Cline picks up the new
server.

## Other MCP clients

For anything not listed above, run:

```sh
bettermemory init
```

This prints the canonical JSON snippet plus the known config locations
for each supported client. Copy the snippet into your client's MCP
config file by hand. If the client doesn't have a place for raw JSON
configuration, check whether the `mcp` SDK can be embedded directly —
the [programmatic example](../examples/programmatic_client.py) shows
the wire-protocol shape your client needs to speak.

## Verifying setup

Regardless of which client you used, confirm the install with:

```sh
bettermemory doctor
```

The `mcp_client_configs` check scans every known client's config and
cross-checks the registered binary path against what `find_binary()`
resolves to right now. A mismatch (typically: you reinstalled
bettermemory into a different venv and the registered path is stale)
is flagged with a one-line fix hint.

In the host itself, ask the model:

> What memory tools do you have?

You should see a list including `memory_search`, `memory_show`,
`memory_write`, etc. If not, the server failed to start —
`bettermemory doctor` will tell you why.

## Verification status of each client snippet

The MCP wire protocol is the same across hosts; the only thing that
varies between clients is the *config file shape and location*. The
table below records what each client's config has been verified
against:

| Client          | Snippet shape | Path | Init auto-patch |
|-----------------|---------------|------|-----------------|
| Claude Code     | `mcpServers` map | user `~/.claude.json` or project `.mcp.json` | yes |
| Claude Desktop  | `mcpServers` map | platform-standard `claude_desktop_config.json` | yes |
| Cursor          | `mcpServers` map | `~/.cursor/mcp.json` or `<repo>/.cursor/mcp.json` | yes |
| Continue        | `mcpServers` map | `~/.continue/config.json` | yes |
| Cline           | `mcpServers` map | VS Code `globalStorage/saoudrizwan.claude-dev/...` | yes (default VS Code only) |

The "snippet shape" is the same `{"mcpServers": {"memory": {"command":
"...", "args": []}}}` for every supported client because that's the
shape the MCP spec standardizes. Differences are entirely in *where*
the file lives. If you find a client whose snippet shape *isn't* this
— file an issue; we'll add support.
