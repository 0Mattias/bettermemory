# Installation

## 1. Install the package

```sh
uv tool install bettermemory       # recommended: isolated tool install
pipx install bettermemory          # or pipx
pip install bettermemory           # or plain pip into a venv
```

Optional extras:

```sh
uv tool install 'bettermemory[embeddings]'        # sentence-transformers (PyTorch, ~500 MB)
uv tool install 'bettermemory[embeddings-fast]'   # fastembed (ONNX, ~50 MB)
uv tool install 'bettermemory[ui]'                # FastAPI + uvicorn for `bettermemory ui`
```

The two embeddings extras expose the same retrieval surface;
`embeddings-fast` is the right pick on CI runners, small VMs, and
air-gapped boxes. Installing either one is sufficient: the default
`search_mode = "hybrid"` picks the model up on its own and fuses it as a
third leg beside the two lexical ones. No config flag is required — in
particular `semantic_dedup` is not one, and setting it to "activate"
semantic search only flips write-time dedup from Jaccard to cosine.

When both are installed, sentence-transformers wins unless
`[behavior] semantic_provider = "fastembed"` is set — except that
auto-detect skips a provider whose import is broken, so a damaged
sentence-transformers falls through to a working fastembed rather than
losing the semantic leg. `bettermemory doctor` names an extra that is
installed but failing to import.

Python 3.11–3.14. From a development clone: `uv tool install .` (or
`uv pip install -e .` for editable). Either path puts a `bettermemory`
script on `$PATH`.

## 2. Register with your MCP client

```sh
bettermemory init --client claude-code      # or claude-desktop / cursor / continue / cline
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

If `bettermemory` isn't on the spawned client's `$PATH` (common for GUI
clients launched from Finder), `init` substitutes the absolute path.
`--print-only` previews the patch without writing.

The server key under `mcpServers` becomes the tool-name prefix
(`mcp__bettermemory__memory_search`). `init` detects and removes legacy
`memory` entries (the pre-1.1 default key) pointing at the same binary.

Per-client paths and quirks: [clients.md](clients.md).

## 3. Verify

```sh
bettermemory try     # offline demo in a temp store, no client needed
```

Then, in a session, ask the model *"what memory tools do you have?"* —
you should see `memory_search`, `memory_write`, etc. If not, the server
failed to start; `bettermemory doctor` will say why.

## 4. Optional: long-form policy

The server's `instructions` block carries the core contract and lands
at the system-prompt level on every compliant client; fresh installs
behave correctly out of the box. For the full writing-discipline and
verification policy:

- **Claude Code**: install the [plugin](../plugin/README.md) — its
  skill ships the policy without Claude Code's ~1.8 KB `instructions`
  truncation.
- **Other clients**: paste the fenced block from
  [system_prompt.md](system_prompt.md) into your `CLAUDE.md` or
  equivalent.

## Troubleshooting

```sh
bettermemory doctor
```

Runs the full diagnostic suite — install wiring (binary path, config,
client configs), store integrity (parse, index, storage), and
sync-repo leak surfaces (tracked-despite-gitignore sidecars, parent
repos tracking store files) — each failed check has a one-line fix
hint. `--json` for machine-readable; exit codes 0/1/2 for
ok/warn/fail. `--fix` applies the safe subset of the remediations —
store/event-log permissions, search-index rebuild, stale-lockfile
removal, sync `.gitignore` refresh — re-runs the affected checks,
and exits on the post-fix state; destructive remediations stay
hints. Plain `doctor` remains the dry run.

Common failures:

- **`bettermemory` not found** when the client starts the server: use
  the absolute path (`bettermemory init --client X` does this).
- **Memories not found by `memory_search`**: check `BETTERMEMORY_DIR`
  and the startup-log "memory directory" line. Project-scoped
  `./.claude-memory/` overrides global `~/.claude-memory/`.
- **Slow first start after upgrading**: releases that bump the index
  schema rebuild the derived search index automatically on first start
  (one-time; INFO log on success). If that rebuild fails, search falls
  back to full scans — correct, just slower — until a manual
  `bettermemory reindex` repairs it.
- **Model over-calling `memory_search`**: verify the client surfaces
  the server's `instructions` block; if not, paste
  [system_prompt.md](system_prompt.md) into `CLAUDE.md`.
