# bench

What each program here measures, and whether it still runs. A program's
`results/` holds the receipts for the numbers the docs and the CHANGELOG
cite, kept as written. A program marked closed is not maintained: its
receipts stand, and its harness runs at the commit that produced them.

## Live in 9.0

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
| `judge/` | The judge prompts and parse rules the QA harnesses grade with, verbatim from their sources. |
| `llm.py`, `storage.py`, `swarm.py`, `interval.py`, `claims.py` | Shared: the model client, the storage latency bench, the fleet-concurrency bench, confidence intervals, claim parsing. |

## Receipts only

Closed or frozen on 2026-10-14. Nothing new is built on these.

| Program | What it was |
|---|---|
| `longmemeval/` | LongMemEval-S under the official protocol, bettermemory and rival arms. Frozen: no further S runs. |
| `l/` | The L1 receipts: LongMemEval-S, all 500 questions. |
| `aml/` | BEAM, CLBench, PersonaMem and LoCoMo through one harness. |
| `w/` | The encoder program (lanes W and F): the trainers, their instruments and the F1 receipts. Closed. |
| `instruments/` | BEIR datasets pinned by sha256 for the encoder reads. Closed with the encoder program. |
| `heldout/` | The held-out retrieval instrument and its scoring harness, which the retrieval receipts cite. |
| `dedup/` | One study of what a lower `related` floor in `find_similar` buys. |
| `*_census.py`, `embed_*.py`, `leg_*.py` | The August 2026 retrieval census scripts a test or a source comment still cites. |

Datasets and built stores live under each program's `data/` and
`.stores/`, gitignored, and are fetched by the program's own script.
