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
failed to start: run `bettermemory doctor`, then
`bettermemory up --foreground`, which logs the store it opens and
prints any start error.

```sh
bettermemory doctor        # the install, config, store, key, daemon, client wiring and disk
bettermemory config path   # which config, store, keys and state directories are in use
```

`doctor` is read-only and grades each check ok, warn or fail with a
one-line fix; it exits 0 when every check is ok, 1 on any failure and 2
on warnings only. Its one write is `--clear-orphan-keys --yes`, which
moves the key rings whose store is shown gone to the keys directory's
`quarantine/` and deletes nothing (without `--yes`, the plan). `config path` prints the config file, the
organisation's managed file, the store directory with the rule that
chose it, and the keys and state directories.

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

The settings under which the promise holds are delivered as managed
settings on macOS and Linux (the sandbox does not run on native
Windows). Generate them on the host, or on one with the same layout:

```sh
bettermemory security sandbox --format managed > managed-settings.json
```

The block goes to stdout and the notes to stderr; nothing is written.
The notes name the file to install: `/Library/Application
Support/ClaudeCode/managed-settings.json` on macOS and
`/etc/claude-code/managed-settings.json` on Linux, or a file in the
`managed-settings.d/` directory beside it. Placing it needs
administrator rights, and nothing in a user's or a project's settings
overrides it. Without `--format managed` the same block is printed for
`~/.claude/settings.json`, which the user, and an agent with a shell,
can still edit. On Linux, with the default locations, the block is:

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
      "Edit(~/.local/state/bettermemory/**)",
      "Read(.claude-memory/**)",
      "Edit(.claude-memory/**)"
    ]
  }
}
```

The three directories are the store directory (`bettermemory config
path` prints it; `~/.claude-memory` unless `BETTERMEMORY_DIR` or
`[storage] directory` names another), the user config directory that
holds the keys (or `BETTERMEMORY_KEYS_DIR`), and the user state
directory that holds the daemon's state file with its token (or
`BETTERMEMORY_STATE_DIR`). On Linux the last two are
`~/.config/bettermemory` and `~/.local/state/bettermemory`, as above;
on macOS both are `~/Library/Application Support/bettermemory`, and the
macOS block names that one directory in place of the two. The command
applies any `BETTERMEMORY_*` override and spells a path under the home
directory with `~/`, so one block serves every account that keeps the
default locations; on a fleet where those variables or `[storage]
directory` differ between accounts, generate it per layout. The store
directory is in the list so that memories reach the agent only through
the tools, under the caller's admission. The two `.claude-memory/**`
rules keep Claude Code's Read and Edit tools off a project's own store;
the sandbox has no entry for a path relative to the session's working
directory, so a sandboxed command can still read and write a
`./.claude-memory` there. Set `[storage] cwd_store = false` in the
managed config (below) to keep a repository from choosing the store at
all. The `denyRead` entries
hold for every process a sandboxed command starts; the
`permissions.deny` rules cover Claude Code's own Read, Edit and Write
tools, which run outside the sandbox. Beside them: no entry in
`sandbox.network.allowedDomains` that reaches the loopback (`localhost`,
`*.localhost`, any address in `127.0.0.0/8` in any spelling, `::1`,
`0.0.0.0`, `*`) without a port, or with the daemon's, since an entry
with no port opens every port on that host and the daemon listens on
`127.0.0.1`; on macOS, `sandbox.network.allowLocalBinding` left off (it
has no effect on Linux); and no `sandbox.excludedCommands`, since an
excluded command runs as the user.

Under these settings a `bettermemory` command the agent runs fails: it
can read neither the state file nor the store. The hooks and the shim
run outside the sandbox and are unaffected. Every change the agent makes
goes through the tools, and the only prompts the daemon holds as the
user's words are the ones the prompt hook posted.

What the sandbox does not cover: native Windows; a command the user
types at the `!` shell-mode prompt, which runs as the user; and MCP
clients other than Claude Code, which have no hooks, so there the words
are taken as given and the log row says so.

`bettermemory security audit` compares the settings Claude Code reads
on the host with this block and reports the gaps, beside the modes of
the store, the keys and the daemon's files. It reads the file-based
settings only (the managed file, its `managed-settings.d/` drop-ins and
the user's settings), not settings an MDM profile or a server delivers.

### The managed config

bettermemory has a managed config of its own, which pins settings the
user's `config.toml` cannot loosen, written by an administrator: the
file `/Library/Application Support/bettermemory/managed.toml` on macOS
and `/etc/bettermemory/managed.toml` on Linux, and on Windows the
registry value `ManagedConfig` under
`HKEY_LOCAL_MACHINE\SOFTWARE\Policies\bettermemory` (a REG_SZ, or a
REG_MULTI_SZ with one line per string), which Group Policy or Intune can
deploy. No environment variable moves it, and nothing under
`%ProgramData%` is read; Cygwin's and MSYS2's Python read the Linux
path and refuse it under a folder another account can write
([operations.md](operations.md#the-managed-config)). It takes `config.toml`'s sections and keys, and
every key it sets is pinned. `examples/managed.toml` is the starting
point, with the `reg add` command for Windows:

```toml
[storage]
cwd_store = false

[security]
strict_provenance = true

[policy]
forbid_acknowledge = ["credential"]
```

A managed config that cannot be read or does not load stops the config
from loading, so the daemon does not start: check it with `bettermemory
config check`, which grades the user's file and the managed config and
exits 0 clean, 1 on an error and 2 on warnings only, and read what is in
force with `bettermemory config show`, which labels each setting
`default`, `user` or `managed`. A daemon reads the file when it starts:
run `bettermemory down` after a change. [operations.md](operations.md#the-managed-config)
has the rollout, and SECURITY.md what the file does and does not bind.

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

## 7. Optional: run the daemon as a service

The daemon starts on first use and needs no service. To have it
supervised, started at login and again after a crash:

```sh
bettermemory service install     # a LaunchAgent on macOS, a systemd user unit on Linux
bettermemory service status      # installed, current, loaded, and the daemon's state
bettermemory service uninstall
```

`service print` shows the unit without writing it. Run `install` as the
user, never under `sudo`, from the install the clients run: it refuses
an interpreter in a package manager's cache, which rules out the
plugin's `uvx` launcher, so install the tool first (section 1). Other
platforms get the command to supervise yourself and exit 2.
[operations.md](operations.md#running-it-as-a-service) has what the unit
pins and the caveats.

## 8. Optional: shell completion

`bettermemory completion bash|zsh|fish` prints a completion script
generated from the installed version's own parser; regenerate it after
an upgrade.

```sh
eval "$(bettermemory completion bash)"     # in ~/.bashrc
eval "$(bettermemory completion zsh)"      # in ~/.zshrc, after compinit
bettermemory completion fish > ~/.config/fish/completions/bettermemory.fish
```

## Troubleshooting

Start with `bettermemory doctor`: each check it fails or warns on names
its fix.

- **`bettermemory` not found** when the client starts the server: use
  the absolute path (`bettermemory init --client X` does this).
- **Memories not found by `memory_search`**: `bettermemory config path`
  prints the store directory and the rule that chose it
  (`BETTERMEMORY_DIR`, `[storage] directory`, a project's
  `./.claude-memory/`, or the default `~/.claude-memory/`). A project-scoped
  `./.claude-memory/` overrides the global one unless `[storage]
  cwd_store = false`.
- **The daemon does not start after a config change**: the config does
  not load. `bettermemory config check` names the file and the line.
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
  pointers cannot be checked here (under `[security] strict_provenance
  = true` they read `unaccounted` instead), and `bettermemory log
  verify` reports the store `unverifiable`. If the old key turns up,
  `bettermemory key import FILE` files it back as a retired key, on the
  record (FILE is a `bettermemory key export` or the key file itself).
- **The daemon does not start, and its log says the key file "is there
  and cannot be read"**: the key file, or a symbolic link to it or to
  the keys directory, cannot be read (a permission, a volume not
  mounted). Nothing is rekeyed over it; every writing open fails until
  the file can be read again, owner-only (`0600`).
- **A store "opens for reading only" because "the key ring ... belongs
  to the store at" another path**: two store files carry one store id (a
  copy of the store in another directory, say), and the key ring is
  bound to the other one, whose key, head and anchor it keeps. Move or
  remove one of the two, or run `bettermemory key adopt --yes` with
  `BETTERMEMORY_DIR` naming the directory of the file to keep, which
  binds the key ring to it on the record; the other then opens for
  reading only. [operations.md](operations.md#files-and-modes) has the
  binding.
- **A store "opens for reading only" because the key ring "is bound to
  no store file yet and holds a history this file does not carry"**: a
  key ring from before 9.0.0rc13 is bound only to the file that carries
  its history. Open that store first, which binds the ring to it; if
  this file is that store and its history was replaced on purpose,
  `bettermemory log accept-history --yes` accepts it on the record.
- **`bettermemory log verify` reports `tampered`**: a log row fails its
  MAC, the chain is broken, or a table holds a row no log row produced.
  `--json` names the rows. Nothing repairs a tampered store in place;
  the rows it names are the ones to look at.
- **`log verify` reports `foreign_segment`**: rows signed under a key
  this machine does not hold come after its own history. Either
  something wrote into the store file without the key, or the store was
  moved away and back, or opened once under another
  `BETTERMEMORY_KEYS_DIR`; in those two cases, `bettermemory key import`
  of the other key (a `key export` of the keys directory that holds it)
  files it as a retired key and restores `ok`
  ([operations.md](operations.md#monitoring)).
- **`log verify` reports `origin_mismatch`**: the row where this
  machine's history of the store begins is gone or changed. A store file
  copied over the existing one from another machine reads this too; the
  recovery is in [operations.md](operations.md#monitoring).
- **Model over-calling `memory_search`**: verify the client surfaces
  the server's `instructions` block; if not, paste
  [system_prompt.md](system_prompt.md) into `CLAUDE.md`.
