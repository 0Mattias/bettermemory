# bench

What each program here measures. A program's `results/` holds the
receipts for the numbers the docs and the CHANGELOG cite, kept as
written: a result is never rewritten, a new reading is a new file.

A result file names the code it measured in its `commit` field, the
checkout's HEAD at the run, and that tree's declared `version`. Read a
result by its commit: the version is the tree's at the time, not a
release (the pre-rc7 tree declares 9.0.0rc6, so the rc7 measurements
under `daemon/results/latency-2026-10-24-*`, `recall/results/rsf7-*` and
`decisions/results/rsf7-*` are stamped rc6 and belong to the commits
they name).

Unit RS-F7's receipts (the 9.0.0rc7 CHANGELOG entry's figures) are
aggregates written from each stage's reading, the post-hoc parts
labelled as such, with the prompt, case and reply text left in the
owner's receipts folders:
`recall/results/rsf7-stage-a-2026-10-22.json` (100 live prompts, the
top ten graded), `rsf7-stage-b-2026-10-22.json` (60 prompts under six
recall conditions, judged blind), `rsf7-stage-c-2026-10-24.json` (41
session starts and compactions drawn, 40 judged, under five brief
conditions), `decisions/results/rsf7-standing-tau-2026-10-23.json` (the
standing threshold's weighted kappa against the judges) and
`rsf7-update-relation-2026-10-23.json` (the update answer read as a
replacement on 60 long records).

## Programs

| Program | Measures |
|---|---|
| `capture/` | Session capture at the Stop hook: the gates, the one-call review, the attended switch, undo and the served surface. |
| `daemon/` | Daemon latency: the hook round trip and `memory_search` through the stdio shim, on a scratch copy of a real store. |
| `decisions/` | The decision models behind the write and recall gates, graded blind. |
| `integrity/` | The integrity benchmark: planted and forged memories against five systems under one protocol. |
| `parity/` | The v8 migration round trip, and rank parity of the search step alone. |
| `recall/` | The session brief and prompt recall on a sealed copy of a real store. |
| `retrieval/` | recall@k on a committed, blind-authored gold set. |
| `rot/` | Memory rot: claims checked against real repositories at pinned commits, pooled. |
| `toolcost/` | The tool-schema context cost an MCP server charges on every turn. |
| `llm.py`, `storage.py`, `swarm.py`, `interval.py`, `claims.py` | Shared: the model client, the storage latency bench, the fleet-concurrency bench, confidence intervals, claim parsing. |

## Closed programs

The programs closed on 2026-10-14 left the tree on 2026-10-23. Each
one stands, with its receipts and its tests, at commit `8426799e`,
which the tag `archive/bench-closed-2026-10-23` marks; its harness
runs there as it did, and a number cited from one of them is read
there:

- `longmemeval/`: LongMemEval-S under the official protocol,
  bettermemory and rival arms. Frozen: no further S runs.
- `l/`: the L1 receipts, LongMemEval-S, all 500 questions.
- `aml/`: BEAM, CLBench, PersonaMem and LoCoMo through one harness,
  and `parity/aml_search.py`, the search-parity check of the 9.0 port
  on its stores.
- `w/` and `instruments/`: the encoder program (lanes W and F), its
  trainers, the BEIR datasets pinned for its reads, and the F1
  receipts.
- `heldout/`: the held-out retrieval instrument and its scoring
  harness, which the retrieval receipts cite.
- `dedup/`: one study of what a lower `related` floor in
  `find_similar` buys.
- `judge/`: the judge prompts and parse rules those harnesses graded
  with, verbatim from their sources.
- `*_census.py`, `embed_*.py`, `leg_*.py`: the August 2026 retrieval
  census scripts.

Datasets and built stores live under each program's `data/` and
`.stores/`, gitignored, and are fetched by the program's own script.
