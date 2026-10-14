# Client setup

bettermemory speaks the standard MCP wire protocol over stdio, so any MCP-aware client can host it. This document collects the snippet shape and config-file location for each commonly-used host.

The fastest path is `bettermemory init --client X`, which writes the right snippet into the right file (idempotently). Show-and-tell mode (`bettermemory init` with no flags) prints the snippet plus all known config locations with `[✓]` markers for files that exist.

After any setup change, restart the client so it picks up the new MCP server entry.

## Claude Code (plugin)

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

The easiest path for Claude Code 2.x. Bundles the MCP server registration AND a system-prompt-level skill carrying the long-form policy. Uses `uvx bettermemory`, so the only prerequisite is [`uv`](https://docs.astral.sh/uv/). See [`../plugin/README.md`](../plugin/README.md).

## Claude Code (manual)

```sh
bettermemory init --client claude-code
```

Patches `~/.claude.json` (user scope). For project scope, point at the repo's `.mcp.json`:

```sh
bettermemory init --client claude-code --config-path .mcp.json
```

Claude Code uses the project entry when both are present. Which store the server opens is decided separately: `$BETTERMEMORY_DIR`, then `[storage] directory`, then `./.claude-memory/` in the working directory, then `~/.claude-memory/`.

The manual path doesn't include the system-prompt skill the plugin ships. For the long-form policy with a manual install, paste [`system_prompt.md`](system_prompt.md) into your `CLAUDE.md`, or `bettermemory init --with-addendum` to print it.

The manual path also skips the plugin's **hooks**, which carry real behavior on Claude Code. Each hook is one HTTP call to the local daemon.

- The Stop hook (`bettermemory hook stop`) settles retrievals at turn end and runs the silent-miss audit. With `[capture] stop_checkpoint = true` it also asks the chat model to save what the conversation established since its last capture.
- The UserPromptSubmit hook (`bettermemory hook prompt`) prints up to three pointers to this project's memories that match the prompt.
- The SessionStart hook (`bettermemory hook session-start`) prints the session brief: up to eight pointers to the repository's newest fresh memories.

Without the Stop hook, the health report suppresses its dead-weight and cold-endorsement verdicts rather than misread the silence. To wire the hooks on a manual install, copy the shapes in the plugin's `hooks/hooks.json`. Other hosts have no hook surface; the in-process settlement and the suppressed verdicts are the designed degradation.

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

Restart Claude Desktop after the patch; it loads config at startup and does not watch the file.

## Cursor

```sh
bettermemory init --client cursor
```

Patches `~/.cursor/mcp.json` (user scope). For project scope:

```sh
bettermemory init --client cursor --config-path .cursor/mcp.json
```

Cursor's MCP support is per-window. A window opened before the patch won't see the new server until reloaded (Cmd-Shift-P → "Reload Window").

## Continue (legacy shape, see caveat)

```sh
bettermemory init --client continue
```

Patches `~/.continue/config.json` with an **object-shaped** `mcpServers`.

**Caveat: current Continue does not read this shape.** As of 2026-07, Continue's released schema reads MCP servers as a **YAML list** in `~/.continue/config.yaml` (or as individual files under `~/.continue/mcpServers/`); the object-in-`config.json` form is a deprecated format current Continue ignores. `init --client continue` therefore prints a warning and writes the entry for backward compatibility with legacy Continue only. On a current install, add the server to `~/.continue/config.yaml` by hand instead:

```yaml
mcpServers:
  - name: bettermemory
    command: bettermemory   # or the absolute path init prints
    args: []
```

MCP tools in Continue are only available in **agent mode**.

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

Default assumes standard VS Code. On Code-Insiders, Codium, or VSCodium, the `Code` directory becomes `Code - Insiders`, `VSCodium`, and so on; pass the right path:

```sh
bettermemory init --client cline --config-path \
  "$HOME/Library/Application Support/Code - Insiders/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"
```

Reload the VS Code window after the patch.

## Hermes Agent

```sh
bettermemory init --client hermes
```

Patches `~/.hermes/config.yaml`. Hermes reads MCP servers from a YAML map under a top-level `mcp_servers` key, so this target writes the entry as YAML with the stdio keys Hermes documents (`command`, `args`, `env`) and no `type`:

```yaml
mcp_servers:
  bettermemory:
    command: /absolute/path/to/bettermemory
    args: []
    env:
      BETTERMEMORY_CLIENT: hermes
```

The patch is a splice, not a rewrite. Only the `bettermemory` entry is inserted or replaced; every other line of the file (your key order, your comments, the commented-out sections Hermes's installer leaves for you) stays exactly as it was. Keys you add to the entry (`idle_timeout_seconds`, `enabled`, a `tools` filter) survive a re-run, and a `BETTERMEMORY_CLIENT` you set yourself is kept. `bettermemory init --client hermes --print-only` prints the YAML entry without writing anything.

Two things follow from Hermes's shape. One gateway process serves every chat platform from one directory and passes no `cwd` to a stdio server. A memory written from Hermes therefore records the gateway's own directory, which the stdio shim forwards as the workspace (`origin.source: header`), not a project. If your Hermes work is one project, add `BETTERMEMORY_WORKSPACE: /path/to/project` to the `env` block and writes record that project, its git remote and branch instead. And Hermes recycles idle stdio servers, so per-process session state (a scope disabled for the session, an unsettled use token) does not outlive a quiet gap between tool calls; the store itself is unaffected.

A running interactive Hermes session reloads its MCP connections when the file changes; a gateway started before the patch reads the block at its next start.

Once two clients write to one store (Claude Code declaring itself through `clientInfo`, Hermes through the `env` block above) the search can be narrowed to one of them: `memory_search(query=…, client="claude-code")`. The match is exact and case-sensitive on whatever the client declared; `memory_show` renders the `client` / `model` a record was written with, and the health report's `actor_slices` lists every spelling in the store, so they are there to read rather than guess. Records written before 7.10.0 carry no actor and match no value: a filtered search shows what is labelled, not a census of who wrote what. And since the value is declared by the client itself, the filter is for attribution and cleanup, never a boundary between clients: any client can call itself anything.

## Other clients

For anything not listed, run `bettermemory init` (no flags) to print the canonical snippet plus known config locations. Copy by hand into your client's MCP config file. If your client doesn't accept raw JSON config, embed the `mcp` SDK directly; see [`../examples/programmatic_client.py`](../examples/programmatic_client.py) for the wire-protocol shape.

## Verifying setup

```sh
bettermemory try
```

The offline demo writes a memory citing a file into a throwaway store, deletes the file, and shows the next search flagging it; it needs no client and touches no real store.

In the host itself, ask the model *"what memory tools do you have?"*. If the nine tools are not listed, the server failed to start. `bettermemory status` names the store, and `bettermemory up --foreground` logs the store it opens and any config problem.

After a few sessions, the health report (`bettermemory health`, or `memory_admin(action="health")`) says whether the Stop hook is wired: `telemetry_coverage.covered` is true when Stop-hook settlement telemetry is landing, and the dead-weight and cold-endorsement verdicts are suppressed, with the reason, when it is not.

## Snippet shape

The MCP wire protocol is the same across hosts; the config shape and location vary. Every JSON client takes this entry (Hermes takes the YAML entry above):

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

The `bettermemory` server key is the default; override it with `--name` only if you have a strong reason (Claude Code prefixes its tool names with this key). When you run `bettermemory init`, the `command` is written as the *absolute* path that `find_binary()` resolves on your PATH, not the bare `bettermemory` shown here, so a later reinstall into a different venv is visible in the config file. The bare-name form above also works if the binary stays on PATH; the plugin's `.mcp.json` instead uses `"command": "uvx"` with `"args": ["bettermemory"]`. The empty `env` is what plain `init` prints; `init --client` declares the client there ([below](#declaring-who-is-calling)).

If you find a JSON client that does not take this shape, please file an issue.

| Client          | Path | Init auto-patch |
|-----------------|------|-----------------|
| Claude Code (plugin) | `/plugin install bettermemory@bettermemory` | n/a (plugin-managed) |
| Claude Code (manual) | user `~/.claude.json` or project `.mcp.json` | yes |
| Claude Desktop  | platform-standard `claude_desktop_config.json` | yes |
| Cursor          | `~/.cursor/mcp.json` or `<repo>/.cursor/mcp.json` | yes |
| Continue        | `~/.continue/config.json` (legacy shape; current Continue wants a YAML list in `config.yaml`, see the caveat above) | writes + warns |
| Cline           | VS Code `globalStorage/saoudrizwan.claude-dev/...` | yes (default VS Code only) |
| Hermes Agent    | `~/.hermes/config.yaml` (a YAML `mcp_servers` map, spliced in place) | yes |

## Declaring who is calling

Since 7.10.0 a memory carries an `actor` block whenever the caller
declared anything. Through the stdio shim every memory carries one,
holding at least the shim's session; `actor.client` stays null when the
client named itself in no channel. A memory always records which
channel named the directory it was written from (`origin.source`), and
events carry the same actor block. A stdio client has one out-of-band
channel for declaring itself: the `env` block of its config entry,
which the shim forwards to the daemon as request headers.

- `BETTERMEMORY_CLIENT`: the client's name. `bettermemory init
  --client cursor` writes `"env": {"BETTERMEMORY_CLIENT": "cursor"}`
  into the entry it patches; a value you set yourself is kept.
- `BETTERMEMORY_CLIENT_VERSION`, `BETTERMEMORY_MODEL`: optional. The
  handshake's `clientInfo` fills the client name and version when the
  client sends one (Claude Code does), so the model is the one you
  usually have to declare.
- `BETTERMEMORY_WORKSPACE`: the project directory, for a host that
  starts the server somewhere else. One long-lived gateway serving many
  chat surfaces from `$HOME` (Hermes Agent's shape: stdio servers take
  `command`, `args` and `env`, and no `cwd`) would otherwise anchor
  every write to the gateway's own directory. With the variable set
  the write records the declared project, its git remote and branch
  (`source: header` under the daemon, `source: env` under
  `bettermemory serve`).

Over HTTP the same declarations travel as request headers
(`x-bettermemory-client`, `x-bettermemory-client-version`,
`x-bettermemory-model`, `x-bettermemory-workspace`) and take precedence
over the environment. Under `bettermemory serve`, a client that offers
`roots` is asked once per connection where it works, ranking below
both; through the shim, the client's working directory is forwarded as
the workspace header instead. An OAuth token verifier on the transport
fills `actor.principal`, the only attested field.
Everything declared is client input and is stored as such; `SECURITY.md`
says what that does and does not protect. `docs/api.md` (under
Identity) has the full precedence table and the on-disk shape.
