# Tool-schema context cost

What an MCP server charges you on every turn, before it does anything.

```sh
venv/bin/python bench/toolcost/run.py                    # bettermemory alone
venv/bin/python bench/toolcost/run.py --spec spec.json   # head-to-head
```

## Why this is the first comparative number published

It is the axis bettermemory **loses** on. Publishing a loss first is the
only way the wins that follow get read at all, and this is the cheapest
honest comparison available: no corpus, no labels, no judge, no API key.
Two processes, one JSON-RPC round trip each, a byte count.

## Re-measure — 2026-07-31 (bettermemory only)

Phase 6 of the 2026-07-30 upgrade program cut the resident surface. Our
arm was re-run at 3.31.1; claude-mem was not, so **the 4.84x ratio below
stays a 2026-07-26 figure** and is not comparable to this row.

| server | tools | full bytes | names+descriptions | input schemas |
| --- | --- | --- | --- | --- |
| **bettermemory 3.31.1** | 18 | **33,714** | 26,846 | 5,252 |

Artifact: [`results/bettermemory-2026-07-31.json`](results/bettermemory-2026-07-31.json).
Two changes account for it: pydantic's auto-generated `title` annotations
are stripped from every served schema, and roughly 1.9k chars of
duplicated policy prose was cut from the descriptions. Re-pairing the
head-to-head means re-running claude-mem's arm under the same conditions;
until that happens, quote this row for our own cost and the section below
for the comparison.

## Result — 2026-07-26

bettermemory 3.29.0 vs claude-mem 13.12.4, both at their shipped
defaults, both probed through the same code path.

| server | tools | full bytes | per tool |
| --- | --- | --- | --- |
| **bettermemory** | 18 | **38,009** | 2,112 |
| claude-mem | 14 | **7,845** | 560 |

**bettermemory costs 4.84x more context per turn than claude-mem** — and
3.77x more *per tool*, so the gap is not simply that it ships more tools.

Broken down:

| component | bettermemory | claude-mem |
| --- | --- | --- |
| names + descriptions | 28,604 B | 2,827 B |
| input schemas | 7,096 B | 4,823 B |

The descriptions are where it goes: **10.1x** claude-mem's. That is a
deliberate design choice — bettermemory pushes retrieval discipline,
write-gate policy and the four `record_use` outcomes into per-tool
descriptions so the model reads policy at the point of call, and CI caps
the total. It is still a real cost a user pays on every turn whether or
not a memory tool is ever invoked, and calling it a design choice does
not make it free.

### It also corrects this project's own published figure

bettermemory's docs have quoted **~27k chars** for the default surface.
That is the *name + description subset* (measured here at 28,604 bytes),
not what a client actually pastes. The full serialized `tools/list` — the
honest unit, because `inputSchema` goes into context too — is **38,009
bytes**. The project has been understating its own context cost by
roughly 25%.

## Method, and the fairness rules that make it quotable

- **Same code path for every server.** Hand-rolled JSON-RPC over stdio:
  `initialize` → `notifications/initialized` → `tools/list`. An SDK
  client would put its own re-serialisation between us and the number.
- **Identical serialization**: `json.dumps(tools, sort_keys=True,
  separators=(",", ":"))`. No formatting difference can move the result.
- **Shipped defaults only.** Neither server's features were disabled to
  improve or worsen its score.
- **HOME is redirected to a throwaway directory for every probe**, so each
  server starts at its shipped default rather than at whatever the person
  running the benchmark has configured. This is not a detail: the first
  run of this harness reported **27 tools** for bettermemory instead of
  18, because it silently read the author's own
  `full_tool_surface = true`. A benchmark that reads the operator's
  config is measuring the operator.
- **claude-mem is run unmodified from its published npm tarball**
  (`npm pack claude-mem`, v13.12.4), via
  `plugin/scripts/mcp-server.cjs` with `CLAUDE_MEM_DATA_DIR` pointed at a
  temp directory. Nothing was installed globally and no hooks were
  registered.

## What this does not say

- **Nothing about quality.** Fewer bytes is not better memory. This
  measures price, not value, and a reader who takes it as a verdict on
  either system has been misled by us, not by the number.
- **Nothing about per-call cost.** claude-mem uses a three-layer
  progressive-disclosure retrieval pattern whose per-result token cost is
  a separate measurement not made here.
- **Nothing about totals in practice.** Real context cost depends on how
  often each system's tools are actually invoked.

## Reproducing

```sh
npm pack claude-mem && tar xzf claude-mem-*.tgz
```

Then point a spec entry at `package/plugin/scripts/mcp-server.cjs` with
`node`. The full spec used for the published run is recorded in
`results/`.
