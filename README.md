<p align="center">
  <img src="https://raw.githubusercontent.com/0Mattias/bettermemory/main/docs/assets/banner.svg" alt="bettermemory: memory that is checked before it is believed" width="100%">
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

Anything else that speaks MCP:

```sh
uv tool install bettermemory
bettermemory init --client claude-desktop   # or cursor / cline / claude-code
```

Restart the client. `bettermemory doctor` exits 0 when it's wired
correctly and prints a one-line fix for anything that isn't.

Try it without installing: `uvx bettermemory try` writes a memory
citing a file, deletes the file, and shows the next search flagging it.
Offline, throwaway store.

## What it does

- Checks memory before believing it. Every hit carries a staleness
  verdict: calendar age, whether the paths it cites still exist, and
  the commits landed since it was last confirmed. Declared claims
  (`path`, `path::symbol`, `path::NAME=literal`) are re-checked
  against the working tree; a claim that stops being true blocks the
  stamp instead of riding along.
- Retrieval is deliberate. Memory is a tool call, not an
  injection. Write gates bounce transient state, secret-shaped tokens
  and near-duplicates; claims about *you* stage for confirmation.
- The code is the model. Search is deterministic lexical ranking —
  keyword + BM25, fused — over your own vocabulary. No embedding
  models, no downloads, nothing to warm up, same answer every time.
- Plain files. One markdown file per memory. Greppable,
  git-syncable, no cloud, no account. The SQLite index beside the
  files is a derived cache you can delete; `bettermemory reindex`
  rebuilds it.
- Rot gets acted on, not accumulated. Episodes journal per-session
  run-state without polluting durable search; health telemetry and
  curation tools surface what drifted, what went cold, and what
  contradicts what.
- Receipts, not adjectives. The claims above are measured by
  preregistered benchmarks with published artifacts — misses included
  — in [bench/][bench], and `bettermemory eval` scores whether memory
  actually helped against your own log ([ours is
  published][eval-results]).

## For agents

You were likely handed this link with "set this up." On Claude Code,
the two plugin commands above are the whole install — skip to step 3.
Everywhere else, steps are idempotent and safe to re-run:

1. `uv tool install bettermemory` — or `pipx install bettermemory`,
   or `pip install bettermemory` into a venv. Python 3.11–3.14.
2. `bettermemory init --client claude-code` — likewise
   `claude-desktop`, `cursor`, `cline`. For any other MCP host, plain
   `bettermemory init` prints the canonical `mcpServers` JSON snippet
   plus known config locations. (Continue needs a hand-written YAML
   entry — see [docs/clients.md][clients-continue].)
3. `bettermemory doctor` — exit 0 means correctly wired; every failed
   check prints a one-line fix.
4. Have the user restart the client so the server loads, then confirm
   by asking the model *"what memory tools do you have?"*

Your operating contract — tool signatures, retrieval discipline,
write gates — is [docs/api.md][api]; the server's
`instructions` block delivers the core policy automatically. For the
long-form policy in your system prompt: `bettermemory init
--with-addendum`. Migrating from Claude Code's built-in auto-memory:
`bettermemory ingest` imports those files once.

## Everything else

The rest is written down: mechanics, storage format, the full tool
surface, CLI, configuration and limitations in
[internals][internals]; per-client setup in [clients][clients] and
[installation][installation]; eval methodology in [eval][eval];
postmortems in [incidents][incidents]; release history in
[CHANGELOG][changelog]; dev setup and the compatibility contract in
[CONTRIBUTING][contributing].

MIT licensed — see [LICENSE][license].

[api]: https://github.com/0Mattias/bettermemory/blob/main/docs/api.md
[bench]: https://github.com/0Mattias/bettermemory/tree/main/bench
[changelog]: https://github.com/0Mattias/bettermemory/blob/main/CHANGELOG.md
[clients]: https://github.com/0Mattias/bettermemory/blob/main/docs/clients.md
[clients-continue]: https://github.com/0Mattias/bettermemory/blob/main/docs/clients.md#continue-legacy-shape--see-caveat
[contributing]: https://github.com/0Mattias/bettermemory/blob/main/CONTRIBUTING.md
[eval]: https://github.com/0Mattias/bettermemory/blob/main/docs/eval.md
[eval-results]: https://github.com/0Mattias/bettermemory/blob/main/docs/eval-results.md
[incidents]: https://github.com/0Mattias/bettermemory/blob/main/docs/incidents/
[installation]: https://github.com/0Mattias/bettermemory/blob/main/docs/installation.md
[internals]: https://github.com/0Mattias/bettermemory/blob/main/docs/internals.md
[license]: https://github.com/0Mattias/bettermemory/blob/main/LICENSE
