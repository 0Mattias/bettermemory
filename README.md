<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/0Mattias/bettermemory/main/docs/assets/banner.svg">
    <img src="https://raw.githubusercontent.com/0Mattias/bettermemory/main/docs/assets/banner-night.svg" alt="bettermemory" width="100%">
  </picture>
</p>

<p align="center">
  <a href="https://pypi.org/project/bettermemory/"><img src="https://img.shields.io/pypi/v/bettermemory.svg" alt="PyPI"></a>
  <a href="https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml"><img src="https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%20to%203.14-blue.svg" alt="Python 3.11 to 3.14"></a>
  <a href="https://github.com/0Mattias/bettermemory/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT"></a>
  <a href="https://glama.ai/mcp/servers/0Mattias/bettermemory"><img src="https://glama.ai/mcp/servers/0Mattias/bettermemory/badges/score.svg" alt="bettermemory MCP server"></a>
</p>

<!-- mcp-name: io.github.0Mattias/bettermemory -->

An MCP memory server for coding agents. Memories are checked against your
files and git history on every read, and stale ones come back flagged.

## Install

Claude Code, with [uv](https://docs.astral.sh/uv/) installed:

```
/plugin marketplace add 0Mattias/bettermemory
/plugin install bettermemory@bettermemory
```

The plugin adds the server, the memory policy as a skill, and three
hooks.

Anything else that speaks MCP:

```sh
uv tool install bettermemory
bettermemory init --client claude-desktop   # or cursor / cline / claude-code / hermes
```

Restart the client and ask the model what memory tools it has; nine
tools, `memory_search` among them, mean the server is wired.

Coming from 8.x: run `bettermemory migrate v8` once (`uvx bettermemory
migrate v8` if you use the plugin). Until then the server refuses to
start beside the old files and says why.

## What it does

- Every search hit carries a staleness verdict: its age, whether the
  paths it cites still exist, and the commits since it was last
  checked. Declared claims (`path`, `path::symbol`,
  `path::NAME=literal`, `!path`) are re-checked against the working
  tree, and `memory_verify` will not stamp a memory fresh while one of
  its claims no longer holds.
- The store is one SQLite file built from a hash-chained log, each row
  signed with a key kept outside the store. A record planted or forged
  outside the tool reads `unaccounted` on the next search, short of the
  cases [SECURITY][security] lists under "When a planted row is
  detected"; `bettermemory log verify` audits the whole chain.
- Memory reaches the model through tool calls. On Claude Code the hooks
  add pointers, never bodies: when a session opens, the project's
  standing memories in force and the memories the conversation so far
  bears on, eight of each at most, and up to three matching memories
  from the project on each prompt.
- Writes pass gates for transient state, secret-shaped tokens and
  near-duplicates, then a review: the model sees the stored statements
  the write resembles and answers four questions before anything is
  written. Claims about you are stored labelled as inferences. No
  second model is involved.
- Session capture is off by default. With
  `[capture] stop_checkpoint = true` in `config.toml`, the Stop hook
  asks the model in the session to save what each 12,000 tokens of
  conversation established. `bettermemory capture review` lists the
  batches and `capture undo` takes one back.
- Search ranks by keyword and BM25, fused, with no embedding model and
  nothing to download; the same query gets the same ranking.
- `episode` keeps per-session run-state out of durable search.
  `bettermemory health` reports what drifted, what went cold and what
  contradicts what.
- One local daemon per store, on 127.0.0.1, serves every session and
  hook; `bettermemory up`, `down` and `status` manage it.
  `bettermemory export` writes the store as JSON or as the 8.x markdown
  directory.
- The benchmarks and their result files, misses included, are in
  [bench/][bench].

## For agents

You were likely handed this link with "Install bettermemory". On Claude
Code, ask the user to type the two plugin commands above, then go to
step 3. Everywhere else, every step is idempotent:

1. `uv tool install bettermemory`, or `pipx install bettermemory`, or
   `pip install bettermemory` into a venv. Python 3.11 to 3.14.
2. `bettermemory init --client claude-code`, or `claude-desktop`,
   `cursor`, `cline`, `hermes`. For any other MCP host, plain
   `bettermemory init` prints the canonical `mcpServers` JSON snippet
   plus known config locations. (Continue needs a hand-written YAML
   entry; see [docs/clients.md][clients-continue].)
3. Have the user restart the client so the server loads, then confirm
   by asking the model "what memory tools do you have?": nine tools,
   `memory_search` among them (under the `mcp__bettermemory__` prefix
   on Claude Code).

With an 8.x store, run `bettermemory migrate v8` (`uvx bettermemory
migrate v8` with the plugin) before step 3; until then the server
refuses to start.

Your operating contract (tool signatures, retrieval discipline, write
gates, the write review) is [docs/api.md][api]; the server's
`instructions` block delivers the core policy automatically. For the
long-form policy in your system prompt: `bettermemory init
--with-addendum`.

## Everything else

Mechanics, storage format, the full tool surface, CLI, configuration
and limitations are in [internals][internals]; per-client setup in
[clients][clients] and [installation][installation]; eval methodology
in [eval][eval]; running it for a team (service, monitoring, backups,
the audit export, the managed config) in [operations][operations]; the
threat model in [SECURITY][security]; postmortems
in [incidents][incidents]; release history in [CHANGELOG][changelog];
dev setup and the compatibility contract in
[CONTRIBUTING][contributing].

MIT licensed. See [LICENSE][license].

[api]: https://github.com/0Mattias/bettermemory/blob/v9/docs/api.md
[bench]: https://github.com/0Mattias/bettermemory/tree/v9/bench
[changelog]: https://github.com/0Mattias/bettermemory/blob/v9/CHANGELOG.md
[clients]: https://github.com/0Mattias/bettermemory/blob/v9/docs/clients.md
[clients-continue]: https://github.com/0Mattias/bettermemory/blob/v9/docs/clients.md#continue-legacy-shape-see-caveat
[contributing]: https://github.com/0Mattias/bettermemory/blob/v9/CONTRIBUTING.md
[eval]: https://github.com/0Mattias/bettermemory/blob/v9/docs/eval.md
[incidents]: https://github.com/0Mattias/bettermemory/blob/v9/docs/incidents/
[installation]: https://github.com/0Mattias/bettermemory/blob/v9/docs/installation.md
[internals]: https://github.com/0Mattias/bettermemory/blob/v9/docs/internals.md
[license]: https://github.com/0Mattias/bettermemory/blob/v9/LICENSE
[operations]: https://github.com/0Mattias/bettermemory/blob/v9/docs/operations.md
[security]: https://github.com/0Mattias/bettermemory/blob/v9/SECURITY.md
