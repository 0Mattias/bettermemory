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

`init` writes the absolute path of the `bettermemory` it finds on your
`$PATH`, so GUI clients launched from Finder find it too. It falls back
to the bare name shown above only when it finds none. `--print-only`
previews the patch without writing.

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
failed to start: run `bettermemory status`, then
`bettermemory up --foreground`, which logs the store it opens and
prints any start error.

The `bettermemory` command your client spawns is a stdio shim in front
of one local daemon per store, started on first use and shared by every
session and hook on the machine. `bettermemory status` says whether it
is running and prints its port, and `bettermemory up` and `down` start
and stop it. The daemon keeps its state file, `daemon-<hash>.json`,
and its log, `daemon-<hash>.log`, under the user state directory (or
under `BETTERMEMORY_STATE_DIR` when that is set). A client that speaks
streamable HTTP can point at `http://127.0.0.1:<port>/mcp` directly,
with the bearer token from the state file. When no daemon can be
started the shim serves the store in-process and says so on stderr,
so memory keeps working either way.

## 4. Optional: long-form policy

The server's `instructions` block carries the core contract and lands
at the system-prompt level on every compliant client, so a fresh install
needs no prompt changes. For the full writing-discipline and
verification policy:

- **Claude Code**: install the [plugin](../plugin/README.md). Its skill
  ships the policy without Claude Code's `instructions` truncation, and
  its three hooks carry the recall surface: the session brief at
  session start, up to three pointers per submitted prompt, and the
  turn-end settlement of what was shown and retrieved. Session capture,
  where the Stop hook asks the chat model to save what a stretch of
  conversation established, rides the same hook and is off until
  `[capture] stop_checkpoint = true`.
- **Other clients**: paste the fenced block from
  [system_prompt.md](system_prompt.md) into your `CLAUDE.md` or
  equivalent, or print it with `bettermemory init --with-addendum`.

## 5. Optional: managed deployment

The standing guard holds an agent that works through the tools: a
standing memory changes only with the user's words, checked against the
prompts the prompt hook posted (SECURITY.md, "An agent that changes the
user's rules"). An agent that runs as the user with a shell is outside
that: it can open the store file, read the key, and post a prompt of its
own to the daemon's hook endpoint with the token every hook and shim
holds. The line between the user's words and the agent's shell is the
harness's to draw, and Claude Code's sandbox draws it: command hooks and
local MCP servers run outside the sandbox with the user's full access,
while the commands the agent runs are confined. bettermemory offers no
daemon under a separate OS account. That would keep the file, the key
and the log out of the agent's reach, but the agent's account would
still hold the token the hook and the shim need, and with it the words
channel.

The settings under which the promise holds, delivered as managed
settings on macOS and Linux (the sandbox does not run on native
Windows):

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "allowUnsandboxedCommands": false,
    "filesystem": {
      "denyRead": [
        "~/.claude-memory",
        "~/.config/bettermemory",
        "~/.local/state/bettermemory"
      ]
    }
  },
  "permissions": {
    "deny": [
      "Read(~/.claude-memory/**)",
      "Edit(~/.claude-memory/**)",
      "Read(~/.config/bettermemory/**)",
      "Edit(~/.config/bettermemory/**)",
      "Read(~/.local/state/bettermemory/**)",
      "Edit(~/.local/state/bettermemory/**)"
    ]
  }
}
```

The three directories are the store directory (`bettermemory status`
prints it; `~/.claude-memory` unless `BETTERMEMORY_DIR` or `[storage]
directory` names another), the user config directory that holds the
keys (or `BETTERMEMORY_KEYS_DIR`), and the user state directory that
holds the daemon's state file with its token (or
`BETTERMEMORY_STATE_DIR`). On Linux the last two are
`~/.config/bettermemory` and `~/.local/state/bettermemory`, as above;
on macOS both are `~/Library/Application Support/bettermemory`. The
store directory is in the list so that memories reach the agent only
through the tools, under the caller's admission. The `denyRead` entries
hold for every process a sandboxed command starts; the
`permissions.deny` rules cover Claude Code's own Read, Edit and Write
tools, which run outside the sandbox. Beside them: no `localhost`,
`*.localhost` or `127.0.0.1` entry without a port in
`sandbox.network.allowedDomains`, since an entry with no port opens
every port on that host and the daemon's with it;
`sandbox.network.allowLocalBinding` left off; and no
`sandbox.excludedCommands`, since an excluded command runs as the user.

Under these settings a `bettermemory` command the agent runs fails: it
can read neither the state file nor the store. The hooks and the shim
run outside the sandbox and are unaffected. Every change the agent makes
goes through the tools, and the only prompts the daemon holds as the
user's words are the ones the prompt hook posted.

What the sandbox does not cover: native Windows; a command the user
types at the `!` shell-mode prompt, which runs as the user; and MCP
clients other than Claude Code, which have no hooks, so there the words
are taken as given and the log row says so.

## 6. Coming from 8.x

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

Until the migration has run, a server pointed at the 8.x directory
refuses to start rather than open an empty store beside your memories;
its error names the directory and the command above. Client
registrations do not change: the server key, the binary and the
`instructions` block are served as before, and the plugin's hooks find
the migrated store on their own.

## Troubleshooting

- **`bettermemory` not found** when the client starts the server: use
  the absolute path (`bettermemory init --client X` does this).
- **Memories not found by `memory_search`**: check `BETTERMEMORY_DIR`,
  `[storage] directory` in `config.toml`, and the store path
  `bettermemory status` prints. A project-scoped `./.claude-memory/`
  overrides the global `~/.claude-memory/`.
- **The server refuses to start, naming `bettermemory migrate v8`**: the
  store directory holds an 8.x store (memory files, event shards or a
  tombstone directory) and no `memory.sqlite`. Run the migration once
  (section 6); nothing in the directory is written.
- **The daemon's log says the store was opened without its key**: this
  machine did not hold the store's key, which lives under the user
  config directory (`keys/<store_id>.key`, or under
  `BETTERMEMORY_KEYS_DIR` when that is set), never in the store. The
  server wrote a new key and appended a `rekey` row. Rows signed under
  the old key keep the labels they arrived with, because their
  pointers cannot be checked here, and `bettermemory log verify`
  reports the store `unverifiable`.
- **`bettermemory log verify` reports `tampered`**: a log row fails its
  MAC, the chain is broken, or a table holds a row no log row produced.
  `--json` names the rows. Nothing repairs a tampered store in place;
  the rows it names are the ones to look at.
- **Model over-calling `memory_search`**: verify the client surfaces
  the server's `instructions` block; if not, paste
  [system_prompt.md](system_prompt.md) into `CLAUDE.md`.
