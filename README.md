<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/0Mattias/bettermemory/main/docs/assets/banner.svg">
    <img src="https://raw.githubusercontent.com/0Mattias/bettermemory/main/docs/assets/banner-night.svg" alt="bettermemory" width="100%">
  </picture>
</p>

<p align="center">
  <a href="https://pypi.org/project/bettermemory/"><img src="https://img.shields.io/pypi/v/bettermemory.svg" alt="PyPI"></a>
  <a href="https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml"><img src="https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg" alt="Python 3.11-3.14"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT"></a>
  <a href="https://glama.ai/mcp/servers/0Mattias/bettermemory"><img src="https://glama.ai/mcp/servers/0Mattias/bettermemory/badges/score.svg" alt="bettermemory MCP server"></a>
</p>

<!-- mcp-name: io.github.0Mattias/bettermemory -->

An MCP memory server for coding agents. Stored facts get checked against
the filesystem and git *before* the model relies on them, so a memory
that has rotted is flagged instead of quoted back at you.

## Install

Claude Code — two slash commands, zero config:

```
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

The plugin registers the server, lands the memory policy as a skill
and wires three hooks: a brief of the repository's newest checked
memories when a session opens, up to three pointers to matching
memories per prompt, and the turn-end settlement of what was shown and
used.

Anything else that speaks MCP:

```sh
uv tool install bettermemory
bettermemory init --client claude-desktop   # or cursor / cline / claude-code / hermes
```

Restart the client and ask the model what memory tools it has: the
nine `mcp__bettermemory__` tools mean the server is wired.

Try it without installing: `uvx bettermemory try` writes a memory
citing a file, deletes the file, and shows the next search flagging it.
Offline, throwaway store.

Coming from 8.x: `bettermemory migrate v8` imports the directory once
and never writes it. Until it has run, the server refuses to start
beside the old files and says so, rather than opening an empty store
next to your memories.

## What it does

- Checks memory before believing it. Every hit carries a staleness
  verdict: calendar age, whether the paths it cites still exist, the
  commits landed since it was last confirmed, and how the record
  entered the store (written here, migrated, or placed by hand).
  Declared claims (`path`, `path::symbol`, `path::NAME=literal`) are
  re-checked against the working tree; a claim that stops being true
  blocks the stamp instead of riding along.
- Retrieval is deliberate. Memory is a tool call, not an injection. On
  Claude Code the hooks add pointers, never bodies: when a session
  opens, up to eight of the repository's newest memories whose verdict
  is fresh; on each prompt, up to three of this project's memories
  that match it. Each pointer is an id and a summary under a header
  that says they are pointers, not instructions, and the model opens
  one with `memory_show` when it bears on the request.
- Writes are gated, then reviewed. The gates bounce transient state,
  secret-shaped tokens and near-duplicates, and claims about *you* are
  labelled as inferences, so they stay distinguishable and
  correctable. A write that passes writes nothing yet: it comes back
  with the stored statements it resembles and three questions, how it
  relates to each of them, and whether following it would be unsafe or
  the body holds a secret. The model answers and re-sends. A
  restatement is refused, an update marks the old statement
  superseded, a conflict is filed for arbitration. No second model is
  involved; the questions go to the caller.
- Session capture, when you switch it on. At a turn end, once the
  conversation since the last capture holds 12,000 tokens, the Stop
  hook asks the model in the session to save what that stretch
  established, under ten rules: durable, one claim, a plain subject,
  specific, dated, with the why. No API key and no other model. Every
  capture is a batch: `bettermemory capture review` lists them,
  `capture undo` takes one back, and an undone body is not captured
  again. Off by default.
- The code is the model. Search is deterministic lexical ranking —
  keyword + BM25, fused — over your own vocabulary. No embedding
  models, no downloads, nothing to warm up, same answer every time.
- One file, and it keeps receipts. The store is a single SQLite file
  whose tables are the fold of a hash-chained log: every write is a
  signed log row first, every record points at the row that made it,
  and a row planted around the tool, or a log row forged without the
  key, reads `unaccounted` on the next search. `bettermemory log
  verify` audits the whole chain; `bettermemory export` writes the
  store out as JSON or as the 8.x markdown directory. No cloud, no
  account.
- One warm process per store. `bettermemory` with no arguments is a
  stdio shim in front of a local daemon on 127.0.0.1 that owns the
  store and answers every session and hook on the machine. The shim
  starts the daemon on first use and serves the store in-process when
  it cannot; `bettermemory up`, `down` and `status` manage it.
- Rot gets acted on, not accumulated. Episodes journal per-session
  run-state without polluting durable search; the health report
  surfaces what drifted, what went cold, and what contradicts what.
- Receipts, not adjectives. The claims above are measured by
  preregistered benchmarks whose frozen result artifacts — misses
  included — live in [bench/][bench] (`results/*.json`, each carrying
  the declared terms it was scored under), and `bettermemory eval`
  scores whether memory actually helped against your own log: the
  three effectiveness rates, what the hooks showed and what came of
  it, and your own grades of reviewed writes, deliveries and captures.

## For agents

You were likely handed this link with "set this up." On Claude Code,
the two plugin commands above are the whole install — skip to step 3.
Everywhere else, steps are idempotent and safe to re-run:

1. `uv tool install bettermemory` — or `pipx install bettermemory`,
   or `pip install bettermemory` into a venv. Python 3.11–3.14.
2. `bettermemory init --client claude-code` — likewise
   `claude-desktop`, `cursor`, `cline`, `hermes`. For any other MCP host, plain
   `bettermemory init` prints the canonical `mcpServers` JSON snippet
   plus known config locations. (Continue needs a hand-written YAML
   entry — see [docs/clients.md][clients-continue].)
3. Have the user restart the client so the server loads, then confirm
   by asking the model *"what memory tools do you have?"*: nine tools
   under the `mcp__bettermemory__` prefix.

Your operating contract — tool signatures, retrieval discipline,
write gates, the write review — is [docs/api.md][api]; the server's
`instructions` block delivers the core policy automatically. For the
long-form policy in your system prompt: `bettermemory init
--with-addendum`. Coming from an 8.x store: `bettermemory migrate v8`
imports the directory once; the server names that command and refuses
to start until it has run.

## Everything else

The rest is written down: mechanics, storage format, the full tool
surface, CLI, configuration and limitations in
[internals][internals]; per-client setup in [clients][clients] and
[installation][installation]; eval methodology in [eval][eval]; the
threat model in [SECURITY][security]; postmortems in
[incidents][incidents]; release history in [CHANGELOG][changelog]; dev
setup and the compatibility contract in [CONTRIBUTING][contributing].

MIT licensed — see [LICENSE][license].

[api]: https://github.com/0Mattias/bettermemory/blob/v9/docs/api.md
[bench]: https://github.com/0Mattias/bettermemory/tree/v9/bench
[changelog]: https://github.com/0Mattias/bettermemory/blob/v9/CHANGELOG.md
[clients]: https://github.com/0Mattias/bettermemory/blob/v9/docs/clients.md
[clients-continue]: https://github.com/0Mattias/bettermemory/blob/v9/docs/clients.md#continue-legacy-shape--see-caveat
[contributing]: https://github.com/0Mattias/bettermemory/blob/v9/CONTRIBUTING.md
[eval]: https://github.com/0Mattias/bettermemory/blob/v9/docs/eval.md
[incidents]: https://github.com/0Mattias/bettermemory/blob/v9/docs/incidents/
[installation]: https://github.com/0Mattias/bettermemory/blob/v9/docs/installation.md
[internals]: https://github.com/0Mattias/bettermemory/blob/v9/docs/internals.md
[license]: https://github.com/0Mattias/bettermemory/blob/v9/LICENSE
[security]: https://github.com/0Mattias/bettermemory/blob/v9/SECURITY.md
