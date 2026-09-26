# Installation

## 1. Install the package

```sh
uv tool install bettermemory       # recommended: isolated tool install
pipx install bettermemory          # or pipx
pip install bettermemory           # or plain pip into a venv
```

There are no runtime extras and no models to download: every ranker is
deterministic lexical code. Python 3.11 to 3.14. From a development
clone: `uv tool install .` (or `uv pip install -e .` for editable).
Either path puts a `bettermemory` script on `$PATH`.

## 2. Register with your MCP client

```sh
bettermemory init --client claude-code      # or claude-desktop / cursor / continue / cline / hermes
```

Idempotently merges the bettermemory entry into the right config file,
creating it if needed. Re-running is safe; a stale binary path is
updated. With no flags, `init` prints the canonical JSON snippet plus
known config locations:

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

If `bettermemory` is not on the spawned client's `$PATH` (common for
GUI clients launched from Finder), `init` substitutes the absolute path.
`--print-only` previews the patch without writing.

The server key under `mcpServers` becomes the tool-name prefix
(`mcp__bettermemory__memory_search`). `init` detects and removes legacy
`memory` entries (the pre-1.1 default key) pointing at the same binary.

Per-client paths and quirks: [clients.md](clients.md).

## 3. Verify

```sh
bettermemory try     # offline demo in a temp store, no client needed
```

Then, in a session, ask the model *"what memory tools do you have?"*.
You should see the nine tools: `memory_search`, `memory_show`,
`memory_write`, `memory_update`, `memory_remove`, `memory_verify`,
`memory_record_use`, `episode` and `memory_admin`. If not, the server
failed to start; run `bettermemory` by hand from a terminal and read
its startup log.

The `bettermemory` command your client spawns is a stdio shim in front
of one local daemon per store, started on first use and shared by every
session and hook on the machine. `bettermemory status` says whether it
is running, `bettermemory up` and `down` start and stop it, and a
client that speaks streamable HTTP can point at
`http://127.0.0.1:<port>/mcp` directly with the bearer token from the
state file `status` names. When no daemon can be started the shim
serves the store in-process and says so on stderr, so memory keeps
working either way.

## 4. Optional: long-form policy

The server's `instructions` block carries the core contract and lands
at the system-prompt level on every compliant client; fresh installs
behave correctly out of the box. For the full writing-discipline and
verification policy:

- **Claude Code**: install the [plugin](../plugin/README.md). Its skill
  ships the policy without Claude Code's `instructions` truncation, and
  its hooks settle retrievals at turn end.
- **Other clients**: paste the fenced block from
  [system_prompt.md](system_prompt.md) into your `CLAUDE.md` or
  equivalent, or print it with `bettermemory init --with-addendum`.

## 5. Coming from 8.x

The 9.0 store is one SQLite file, `memory.sqlite`, in the store
directory, and it is not created from an 8.x directory automatically.
Run the migration once:

```sh
bettermemory migrate v8 --dry-run   # the report, nothing written
bettermemory migrate v8             # the store beside the 8.x files
```

The 8.x files are never written; `bettermemory export --mirror DIR`
writes the store back out as an 8.x directory whenever you want one.
[api.md](api.md#the-store) has the details.

## Troubleshooting

- **`bettermemory` not found** when the client starts the server: use
  the absolute path (`bettermemory init --client X` does this).
- **Memories not found by `memory_search`**: check `BETTERMEMORY_DIR`
  and the startup-log "memory directory" line. Project-scoped
  `./.claude-memory/` overrides global `~/.claude-memory/`. An 8.x
  directory that has not been migrated has no `memory.sqlite`, so the
  server starts with an empty store there.
- **Every hit reads `trust_unavailable`**: this machine does not hold
  the store's key, which lives under the user config directory
  (`keys/<store_id>.key`, or under `BETTERMEMORY_KEYS_DIR` when that is
  set), never in the store. A store copied from
  another machine without its key reads but cannot vouch for its rows.
  `bettermemory log verify` reports the same condition as
  `unverifiable`.
- **`bettermemory log verify` reports `tampered`**: a log row fails its
  MAC, the chain is broken, or a table holds a row no log row produced.
  `--json` names the rows. Nothing repairs a tampered store in place;
  the rows it names are the ones to look at.
- **Model over-calling `memory_search`**: verify the client surfaces
  the server's `instructions` block; if not, paste
  [system_prompt.md](system_prompt.md) into `CLAUDE.md`.
