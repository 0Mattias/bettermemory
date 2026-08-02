# Eval results

What `bettermemory eval` reports on the author's live store. Two
sources, kept separate on purpose: **production telemetry** (real
usage, one user, the full trio) and the **comparative harness** (a
synthetic corpus, several systems, recall plus a capability matrix).
Metric definitions live in [eval.md](eval.md).

## Production telemetry

`bettermemory eval` over the author's live store — 134 active
memories, 4,967 logged events, 422 distinct sessions across just over
two months of daily agent use (measured 2026-07-16, `v1_top1_high`
rule, Wilson 95% CIs). Rates only; the raw event log is personal and
stays local.

The numbers in this section are generated with `bettermemory eval
--report`, which emits exactly this rates-and-counts shape with the
leak-free property enforced by a tested contract.

Read `memory_helped_rate` as a deliberate floor, not as an estimate of
usefulness: the numerator counts only *explicit, claim-excerpt-backed*
endorsements, while the denominator counts every retrieval occurrence.
A retrieval that genuinely helped but left no attestation still counts
against it. Roughly one in fourteen retrievals in the last month left a
verifiable "this memory shaped this sentence" record.

| rate | last 30 days | all time |
|---|---|---|
| `memory_helped_rate` | 91/1,282 = **0.07** [0.06, 0.09] | 99/2,768 = 0.04 [0.03, 0.04] |
| `endorsement_rate` | 99/808 = **0.12** [0.10, 0.15] | 149/1,652 = 0.09 [0.08, 0.10] |
| `silent_miss_rate` | 2/244 = **0.01** [0.00, 0.03] | 2/403 = 0.00 [0.00, 0.02] |

Scan detail — last 30d: 1,282 retrieval occurrences · 808 applied-use
events · 244 turns audited (32 no-signal excluded, 59 repeat audits
deduped). All time: 2,768 · 1,652 · 403 (38 no-signal, 59 deduped).

Reading the table:

- The 30-day rates beat the all-time rates — `memory_helped` by
  roughly two to one — because the attestation tooling matured
  mid-history: early events couldn't carry signals that now exist.
  Read the trend, not either column alone.
- **The `silent_miss_rate` figures are a floor, and the low value is
  substantially an artifact of message length.** The v1 verdict fires on
  a coverage fraction whose denominator grows with the user's message,
  so on the same 195-turn sample the label's `high` rate runs 45% → 32%
  → 0% → 3% across increasing message length: a long turn is close to
  unflaggable, and long turns are the ones most likely to have needed
  memory. Read a rise as signal; do not read 0.01 as evidence the store
  is being retrieved well. The full measurement, and why the shadow
  `relevance_v2` label makes it worse rather than better, are in
  [eval.md](eval.md#silent_miss_rate).
- The log has now recorded its **first silent misses**: 2 all-time,
  both inside the 30-day window — one each on `claude-sonnet-5` and
  `claude-opus-4-8`; see the per-model table. A third probe flag was
  reviewed and acknowledged as a false positive
  (`memory_acknowledge_miss`, reason persisted in the log), which the
  retraction contract excludes from these rates. The 30-day rate is
  a real non-zero 0.01 now, and these are exactly the calibration
  data the threshold rule wants. A counterfactual sweep
  (`bettermemory eval --threshold-sweep`) replays the 15 v1-flagged
  misses against the stricter v2/v3/v4 rules, which flag none of them
  — so v1 isn't over-firing. (Strictly *looser* rules are the other
  question, and they get their own lane: `bettermemory eval
  --widening-preview` replays them over the `turn_audited` stream,
  which has carried a compact `top_hits` payload on every miss-capable
  event since 3.14.0. Three labeling passes have used it; the most
  recent is
  [`eval/widening-labeling-2026-07-29.md`](eval/widening-labeling-2026-07-29.md).)
- n=1. This measures one user's store, workload, and retrieval
  discipline. Run `bettermemory eval` on your own log — anomalies are
  exactly the calibration data the threshold rule needs.

### Per-model audit telemetry (all time)

| model | audited | no-signal | misses |
|---|---|---|---|
| `claude-fable-5` | 40 | 7 | 0 |
| `claude-opus-4-8` | 70 | 10 | 1 |
| `claude-sonnet-5` | 36 | 13 | 1 |

### Threshold sweep (counterfactual, all time)

| rule | would flag | Δ v1 | % of v1 |
|---|---|---|---|
| `v1_top1_high` | 15 | — | 100.0% |
| `v2_top1_high_score_50` | 0 | -15 | 0.0% |
| `v3_top1_high_dominant` | 0 | -15 | 0.0% |
| `v4_top1_high_strict_combined` | 0 | -15 | 0.0% |

Stricter rules replay over misses v1 already flagged, so this answers
"is v1 over-firing?" — not "what does v1 miss?".

### Tool usage (top 10, all time)

| tool | calls | share |
|---|---|---|
| `memory_audit_turn` | 1,185 | 24.2% |
| `memory_show` | 758 | 15.5% |
| `memory_record_use` | 713 | 14.6% |
| `memory_verify` | 547 | 11.2% |
| `memory_update` | 489 | 10.0% |
| `memory_search` | 277 | 5.7% |
| `memory_scope_overview` | 270 | 5.5% |
| `memory_write` | 244 | 5.0% |
| `episode_write` | 243 | 5.0% |
| `episode_handoff` | 50 | 1.0% |

4,891 tool calls across 25 known tools as of the 2026-07-16 snapshot
(the registry has grown since; a re-run at HEAD enumerates 27) —
retrieval (`memory_search`, 5.7%) is dwarfed by upkeep (audit, verify,
update, record_use).

### Corrections (2026-07-30)

Two hand edits made after publication. Nothing above was regenerated:
the tables are still the 2026-07-16 `--report` snapshot, number for
number, and the bullets still read that same run. A re-run moves every
count *and* the measured date, which would desynchronize the
hand-authored narrative — including the "first silent misses" story —
from the tables it reads.

- **Looser rules are measurable after all.** The threshold-sweep
  bullet claimed `turn_audited` doesn't carry `top_hits`, so looser
  rules couldn't be evaluated from the log. That was already wrong when
  this page was written — the payload has shipped on every miss-capable
  `turn_audited` event since 3.14.0 (2026-07-03) and
  `--widening-preview` exists to replay looser rules over it.
  Corrected in place.
- **"25 known tools" is a snapshot value, not a current one** —
  annotated rather than bumped to 27, because it and the 4,891 came out
  of the same `--report` run. The current registry size is pinned by
  `_EXPECTED_TOOL_COUNT` in `tests/test_eval.py`, which one assertion
  holds equal to the runtime-registered set.

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
