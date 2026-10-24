# bettermemory: Claude Code plugin

Memory for Claude Code, checked against your files and git history on
every read, with a session brief and recall on each prompt. This is the
plugin for [bettermemory](https://github.com/0Mattias/bettermemory).

It adds the MCP server ([`.mcp.json`](.mcp.json)), the memory policy as
a skill ([`skills/bettermemory/SKILL.md`](skills/bettermemory/SKILL.md)),
and three hooks ([`hooks/hooks.json`](hooks/hooks.json)). The
SessionStart and UserPromptSubmit hooks print pointers, never bodies;
all three always exit 0.

- SessionStart: the session brief, the project's standing memories in
  force and the memories the conversation so far bears on, eight pointers
  of each at most, the standing ones marked in force. `[behavior]
  session_brief = false` turns it off.
- UserPromptSubmit: up to three pointers to this project's memories
  that match the prompt. `[behavior] prompt_recall` takes `project`
  (the default), `everywhere` or `off`.
- Stop: settles what the turn showed and used. With
  `[capture] stop_checkpoint = true` in `config.toml`, it also asks the
  model to save what each 12,000 tokens of conversation established;
  `bettermemory capture review` and `capture undo` manage the batches.

## Install

```text
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

Requires [`uv`](https://docs.astral.sh/uv/) on `$PATH`; `uvx` fetches
bettermemory from PyPI on first run. To use an installed binary
instead, run `uv tool install bettermemory` and set
`"command": "bettermemory"` and `"args": []` in `.mcp.json`. The hooks
in `hooks/hooks.json` still call `uvx`.

Coming from 8.x: run `uvx bettermemory migrate v8` once. Until then the
server refuses to start beside the old files and says why.

## Verify

Ask the model "what memory tools do you have?"; you should see tools
with the `mcp__bettermemory__` prefix. Then try "remember that I prefer
hands-on tutorials with runnable code". The model calls `memory_write`
with `category="user-inference"`, answers the write review that comes
back, and re-sends. The record then lands in
`~/.claude-memory/memory.sqlite`; you are not asked to confirm.

## Troubleshooting

```sh
uvx bettermemory --version
uvx bettermemory status
```

`--version` shows the package runs; `status` names the store and says
whether its daemon is up. If the tools still do not appear, run
`uvx bettermemory` by hand and read what it prints.
`uvx bettermemory health` says whether the Stop hook's settlement
telemetry is landing (`telemetry_coverage`).

## Uninstall

Run `uvx bettermemory down` first to stop the daemon, then:

```text
/plugin uninstall bettermemory@bettermemory
```

This removes the server registration, the skill and the hooks.
Memories on disk (`~/.claude-memory/`) are not touched.

## Other clients

The plugin is the Claude Code wrapper around the same MCP server. For
Claude Desktop, Cursor, Cline, Hermes Agent, Continue or any other MCP
client, see [docs/installation.md](../docs/installation.md) and
[docs/clients.md](../docs/clients.md).

## License

MIT. See [LICENSE](../LICENSE).
