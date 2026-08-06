<p align="center">
  <img src="https://raw.githubusercontent.com/0Mattias/bettermemory/main/docs/assets/banner.svg" alt="bettermemory: memory that is checked before it is believed" width="100%">
</p>

<p align="center">
  <a href="https://pypi.org/project/bettermemory/"><img src="https://img.shields.io/pypi/v/bettermemory.svg" alt="PyPI"></a>
  <a href="https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml"><img src="https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg" alt="Python 3.11-3.14"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT"></a>
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

- Every hit carries a staleness verdict — calendar age, the paths it
  cites, commits landed since it was last confirmed.
- That verdict is measured, not asserted. A preregistered benchmark
  ([bench/rot][rot]) grades the shipped staleness code against git
  ground truth on 30 repositories it did not choose — 37,635 claims
  (the artifact's `pooled_claims`), every prediction filed before the
  run and the misses published as retractions rather than
  renegotiated. The claim-level tier reads 94% precision at 1.1
  alerts per catch against the file-level signal's 3.4
  ([artifact][rot-artifact]); declared claims on `memory_write` /
  `memory_verify` are that detector shipped (3.40.0).
- Retrieval is a deliberate tool call, with one score-gated exception:
  on the rare prompts where the silent-miss probe says memory was
  needed (the measured rate lives in [docs/eval-results.md][eval-results]),
  the recall hook injects the top hit's id + snippet — a
  pointer, never a body, so verification still runs through
  `memory_show` (`[behavior] prompt_recall`; off = purely opt-in). A
  second exception ships default-off: `[behavior] standing_tier`
  delivers the repository's fresh-verified `ambient` memories whole at
  session start (~1 KB, truncated only at memory boundaries), because
  opt-in retrieval cannot serve knowledge whose trigger condition is
  not knowing you need it — and verification is the admission ticket,
  so a stale ambient memory is never delivered, only named in an
  aggregate verify-to-restore line. The
  18 default tools still charge schema every turn: a serialized
  `tools/list` of 33,960 bytes, 27,092 of it names and descriptions,
  measured 2026-07-31 at 3.32.0 ([bench/toolcost][toolcost]). CI caps
  the descriptions.
- Write gates: transient state, secret-shaped tokens and near-duplicates
  bounce; claims about *you* stage for confirmation.
- One markdown file per memory. Greppable, git-syncable. Markdown is
  canonical; the SQLite index next to it is a derived cache you can
  delete. No cloud, no account.
- `bettermemory eval` measures whether memory helped, against your own
  log. [Ours is published][eval-results] — one store, one user, misses
  included.

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
[rot]: https://github.com/0Mattias/bettermemory/blob/main/bench/rot/README.md
[rot-artifact]: https://github.com/0Mattias/bettermemory/blob/main/bench/rot/results/multirepo-anchored-2026-07-30.json
[toolcost]: https://github.com/0Mattias/bettermemory/blob/main/bench/toolcost/README.md
