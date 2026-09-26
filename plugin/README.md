# bettermemory: Claude Code plugin

The Claude Code plugin wrapper for
[bettermemory](https://github.com/0Mattias/bettermemory) — a trust
layer between an agent and its own past: every retrieved fact carries
a staleness verdict, every use an attribution, and whether it helped
is measured rather than assumed.

The plugin bundles five things:

1. **MCP server registration** ([`.mcp.json`](.mcp.json)): spawns
   `uvx bettermemory` as a stdio MCP server, which registers the nine
   tools (see [docs/api.md](../docs/api.md)). It is a shim in front of
   the local daemon (`bettermemory up`), started on first use, so every
   session and every hook on the machine shares one warm process.
2. **Memory-discipline skill**
   ([`skills/bettermemory/SKILL.md`](skills/bettermemory/SKILL.md)):
   the long-form retrieval and writing policy at the system-prompt
   level. The server's own `instructions` block carries a short
   summary; Claude Code truncates that block, the skill has no cap.
3. **Stop hook** ([`hooks/hooks.json`](hooks/hooks.json)): runs
   `uvx bettermemory hook stop --quiet` at each turn end to settle the
   turn's retrievals and log silent retrieval misses. Always exits 0,
   so a transient failure never surfaces as a hook-error banner.
4. **SessionStart hook** (same file): runs
   `uvx bettermemory hook session-start` when a conversation opens and
   prints the per-scope memory counts for the current repository.
   Claude Code injects a SessionStart hook's stdout into the model's
   context, so the session begins knowing what is stored without
   spending a tool call on it. Reads the store's scope counts only
   (never memory bodies), records nothing, prints nothing when the
   store is empty, and always exits 0.
5. **UserPromptSubmit hook** (same file): runs
   `uvx bettermemory hook prompt` on each prompt submission. Probes
   the prompt with the same silent-miss predicate the Stop hook
   audits with; on the few prompts that clear it, prints a one-hit
   pointer block (memory id, scopes and snippet, never a body) that
   Claude Code injects into context, and records a `prompt_recall`
   event the audit counts as retrieval. `[behavior]
   prompt_recall = false` disables it. Always exits 0.

## Install

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

Requires [`uv`](https://docs.astral.sh/uv/) on `$PATH`; `uvx` fetches
bettermemory from PyPI on first run. To use a pre-installed binary
instead, `uv tool install bettermemory` and edit `.mcp.json` to
`"command": "bettermemory"`.

## Verify

Ask the model *"what memory tools do you have?"* — you should see tools
with the `mcp__bettermemory__` prefix. Then try *"remember that I
prefer hands-on tutorials with runnable code"*: the model should call
`memory_write` with `category="user-inference"`, and the record lands
in `~/.claude-memory/memory.sqlite` without a confirmation round trip.

## Troubleshooting

```sh
uvx bettermemory try
```

The offline demo proves the package runs; if the tools still do not
appear in the session, run `uvx bettermemory` by hand and read its
startup log, which names the store directory and any config problem.
`uvx bettermemory health` says whether the Stop hook's settlement
telemetry is landing (`telemetry_coverage`).

## Uninstall

```text
/plugin uninstall bettermemory@bettermemory
```

Removes the server registration and skill. Memories on disk
(`~/.claude-memory/`) are not touched.

## Other clients

The plugin is the Claude Code-specific wrapper around the same MCP
server. For Claude Desktop, Cursor, Continue, Cline, or anything else,
see [docs/installation.md](../docs/installation.md) and
[docs/clients.md](../docs/clients.md).

## License

MIT. See [LICENSE](../LICENSE).
