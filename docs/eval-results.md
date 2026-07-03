# Eval results

The numbers behind the README's "numbers, not vibes" claim. Two
sources, kept separate on purpose: **production telemetry** (real
usage, one user, the full trio) and the **comparative harness** (a
synthetic corpus, several systems, recall plus a capability matrix).
Metric definitions live in [eval.md](eval.md).

## Production telemetry

`bettermemory eval` over the author's live store — 58 active
memories, 3,492 logged events, 288 distinct sessions across roughly
two months of daily agent use (measured 2026-07-03, `v1_top1_high`
rule, Wilson 95% CIs). Rates only; the raw event log is personal and
stays local.

| rate | last 30 days | all time |
|---|---|---|
| `memory_helped_rate` | 45/644 = **0.07** [0.05, 0.09] | 49/1,774 = 0.03 [0.02, 0.04] |
| `endorsement_rate` | 57/425 = **0.13** [0.10, 0.17] | 97/1,043 = 0.09 [0.08, 0.11] |
| `silent_miss_rate` | 0/167 = **0.00** [0.00, 0.02] | 0/237 = 0.00 [0.00, 0.02] |

Reading it honestly:

- `memory_helped_rate` is a deliberate floor: the numerator counts
  only *explicit, claim-excerpt-backed* endorsements, while the
  denominator counts every retrieval occurrence. One in fourteen
  retrievals in the last month left a verifiable "this memory shaped
  this sentence" record.
- The 30-day rates beat the all-time rates roughly two-to-one because
  the attestation tooling matured mid-history — early events couldn't
  carry signals that now exist. The trend is the point.
- Zero silent misses across 237 audited turns is a claim about the
  *loosest evaluable rule*: a counterfactual sweep
  (`bettermemory eval --threshold-sweep`) replays the 4 historical
  v1-flagged misses against the stricter v2/v3/v4 rules, which flag
  none of them — and strictly looser rules can't be evaluated from
  the log at all (`turn_audited` doesn't carry `top_hits`).
- n=1. This measures one user's store, workload, and retrieval
  discipline. Run `bettermemory eval` on your own log — anomalies are
  exactly the calibration data the threshold rule needs.

## Comparative harness

`tests/eval/comparative.py --live`, run 2026-07-03 over the fixed
10-fact / 10-probe workload at k=5. Committed artifact:
[`eval/comparative-live-2026-07-03.json`](eval/comparative-live-2026-07-03.json).

The structural finding is the capability matrix — which systems can
even *measure* the trio:

| system | version | per-hit retrieval | endorsement tagging | miss audit | recall@5 |
|---|---|---|---|---|---|
| bettermemory | 3.13.0 | yes | **yes** | **yes** | 7/7 |
| mem0 | 2.0.11 | yes | no | no | 7/7 |
| server-memory | 0.6.3 | yes | no | no | 7/7 † |
| agentmemory | — | yes | no | no | not run |
| claude-mem | — | no | no | no | not run |

Every system that ran scored a perfect recall@5 — expected, and worth
stating plainly: the corpus uses deliberately distinctive per-fact
vocabulary, so recall saturates by construction. The recall row shows
"handles verbatim facts without falling over," not ranking headroom.
The comparison that doesn't saturate is the matrix: only a system
that logs per-hit retrieval, load-bearing endorsement, *and* a
post-turn miss audit can compute `memory_helped_rate`,
`endorsement_rate`, and `silent_miss_rate` at all. On this workload
bettermemory's audit lane also reproduces the 5 constructed silent
misses (5/7 = 0.71 [0.36, 0.92]) — the workload plants exactly five.

Fairness accommodations, stated up front:

- **mem0** ran fully local and keyless: MiniLM embeddings, embedded
  qdrant, `add(..., infer=False)`. That deliberately bypasses its LLM
  extraction pipeline — the row measures mem0's retrieval stack over
  verbatim facts, not its extraction quality.
- **† server-memory**'s native `search_nodes` is a whole-query,
  case-insensitive substring match; no probe query appears verbatim in
  any fact (a pinned test documents this), so the raw server scores
  0/7 by construction. The harness donates a tokenized-OR ranker on
  top and reports that. Both numbers are true; the donated one is the
  generous one.
- **agentmemory** (PyPI) last released in Oct 2023 and pins a pre-1.x
  chromadb; the trending 2026 "agentmemory" is an unrelated TypeScript
  service. **claude-mem** ingests through an AI-compressing session
  pipeline with no direct fact-insertion path — seeding this workload
  would mean simulating whole sessions through a nondeterministic,
  key-dependent compressor. Forcing either into the harness would
  produce a rigged number in one direction or the other, so both stay
  capability-row-only.
- Single run, defaults, competitors untuned. `system_version` in the
  artifact pins what executed.

## Reproduce

```sh
# production trio over your own store
bettermemory eval --since 30d
bettermemory eval --threshold-sweep

# comparative matrix (stubs — no competitor installs)
python -m tests.eval.comparative

# live competitor lane (maintainer machine; throwaway venv, ~2 GB
# model download on first run, Node 20+ for the server-memory row)
tests/eval/run_live.sh
```
