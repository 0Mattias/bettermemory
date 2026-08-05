# Eval results

What `bettermemory eval` reports on the author's live store. Two
sources, kept separate on purpose: **production telemetry** (real
usage, one user, the full trio) and the **comparative harness** (a
synthetic corpus, several systems, recall plus a capability matrix).
Metric definitions live in [eval.md](eval.md).

## Production telemetry

`bettermemory eval` over the author's live store — 243 active
memories, 7,037 logged events, 579 distinct sessions across just
under three months of daily agent use (measured 2026-08-04,
`v1_top1_high` rule, Wilson 95% CIs). Rates only; the raw event log
is personal and stays local.

The numbers in this section are generated with `bettermemory eval
--report`, which emits exactly this rates-and-counts shape with the
leak-free property enforced by a tested contract. This snapshot ran
the published 3.38.0 binary.

Read `memory_helped_rate` as a deliberate floor, not as an estimate of
usefulness: the numerator counts only *explicit, claim-excerpt-backed*
endorsements, while the denominator counts every retrieval occurrence.
A retrieval that genuinely helped but left no attestation still counts
against it. Roughly one in seventeen retrievals in the last month left
a verifiable "this memory shaped this sentence" record.

| rate | last 30 days | all time |
|---|---|---|
| `memory_helped_rate` | 136/2,341 = **0.06** [0.05, 0.07] | 196/4,371 = 0.04 [0.04, 0.05] |
| `endorsement_rate` | 182/1,508 = **0.12** [0.11, 0.14] | 290/2,705 = 0.11 [0.10, 0.12] |
| `silent_miss_rate` | 3/128 = **0.02** [0.01, 0.07] | 3/128 = 0.02 [0.01, 0.07] |

Scan detail — last 30d: 2,341 retrieval occurrences · 1,508
applied-use events · 128 turns audited (20 no-signal excluded, 102
repeat audits deduped). All time: 4,371 · 2,705 · 128 (20 no-signal,
102 deduped).

**The audited denominators restarted on 2026-07-22.** A
`consolidate --acknowledge-misses-before` cutoff was written that day
(as part of the second widening-labeling pass's probe triage), and
under the retraction contract it drops every earlier `turn_audited` /
`search_miss` event from all miss surfaces — eval, `memory_health`,
and the curation rollup agree over the same stream. That is why the
30-day and all-time miss columns are identical, why 128 is *smaller*
than the July snapshot's 403, and why the two snapshots' audited
counts must not be read as one series. Retrieval and use telemetry
carry no such cutoff and accumulate across the whole log.

Reading the table:

- The 30-day rates still beat the all-time rates, but the gap is
  closing (`endorsement_rate` 0.12 vs 0.11 now, against 0.12 vs 0.09
  in July): the attestation tooling matured mid-history, and the mature
  era now dominates the log, so the columns converge as the early
  signal-poor months shrink as a share of all time. Read the trend,
  not either column alone.
- **The `silent_miss_rate` figures are a floor, and the low value is
  substantially an artifact of message length.** The v1 verdict fires on
  a coverage fraction whose denominator grows with the user's message,
  so on the same 195-turn sample the label's `high` rate runs 45% → 32%
  → 0% → 3% across increasing message length: a long turn is close to
  unflaggable, and long turns are the ones most likely to have needed
  memory. Read a rise as signal; do not read a low rate as evidence
  the store is being retrieved well. The full measurement, and why the shadow
  `relevance_v2` label makes it worse rather than better, are in
  [eval.md](eval.md#silent_miss_rate).
- Three silent misses have accrued since the 2026-07-22 cutoff — 1 on
  `claude-fable-5`, 2 on `claude-opus-5` — and all three are pending
  triage as of this snapshot. They are *new* events, distinct from the
  two published in July (those fell before the cutoff and are
  retracted from every rate above; the July rows survive in this
  file's git history). A counterfactual sweep (`bettermemory eval
  --threshold-sweep`) replays the 8 post-cutoff v1-flagged misses
  against the stricter v2/v3/v4 rules, which flag none of them — so
  v1 isn't over-firing. (Strictly *looser* rules were the other
  question, and their lane is now closed: three
  `--widening-preview` labeling passes —
  [2026-07-08](eval/widening-labeling-2026-07-08.md) ·
  [2026-07-22](eval/widening-labeling-2026-07-22.md) ·
  [2026-07-29](eval/widening-labeling-2026-07-29.md) — ended with the
  w2 candidate dropped per the pre-registered precision band and its
  refined successor declined on the confidence interval;
  see ROADMAP's "Not planned" for the reopening bar.)
- n=1. This measures one user's store, workload, and retrieval
  discipline. Run `bettermemory eval` on your own log — anomalies are
  exactly the calibration data the threshold rule needs.

### Per-model audit telemetry (all time)

| model | audited | no-signal | misses |
|---|---|---|---|
| `claude-fable-5` | 34 | 4 | 1 |
| `claude-opus-5` | 94 | 16 | 2 |

"All time" here starts at the 2026-07-22 cutoff, which is also why
the `claude-sonnet-5` / `claude-opus-4-8` rows from the July snapshot
are gone: their audits predate it, and neither model has produced
post-cutoff traffic on this machine.

### Threshold sweep (counterfactual, all time)

| rule | would flag | Δ v1 | % of v1 |
|---|---|---|---|
| `v1_top1_high` | 8 | — | 100.0% |
| `v2_top1_high_score_50` | 0 | -8 | 0.0% |
| `v3_top1_high_dominant` | 0 | -8 | 0.0% |
| `v4_top1_high_strict_combined` | 0 | -8 | 0.0% |

Stricter rules replay over misses v1 already flagged, so this answers
"is v1 over-firing?" — not "what does v1 miss?".

### Tool usage (top 10, all time)

| tool | calls | share |
|---|---|---|
| `memory_audit_turn` | 1,650 | 23.8% |
| `memory_show` | 1,153 | 16.6% |
| `memory_record_use` | 916 | 13.2% |
| `memory_verify` | 806 | 11.6% |
| `memory_update` | 694 | 10.0% |
| `memory_write` | 418 | 6.0% |
| `memory_search` | 414 | 6.0% |
| `memory_scope_overview` | 347 | 5.0% |
| `episode_write` | 324 | 4.7% |
| `episode_handoff` | 68 | 1.0% |

6,934 tool calls across 27 known tools as of the 2026-08-04 snapshot —
retrieval (`memory_search`, 6.0%) is dwarfed by upkeep (audit, verify,
update, record_use), the same shape as both earlier snapshots.

### Snapshot history

Three snapshots of the same live store so far — the first predates
`--report`; both later ones ran the then-published binary. This file
is rewritten in place per snapshot, so the earlier columns live in
git history (2026-07-16 as e4e19cd).

| measured | binary | memories | events | sessions | helped (30d) | endorsement (30d) | miss denom (30d/all) |
|---|---|---|---|---|---|---|---|
| 2026-07-03 | — | 58 | 3,492 | 288 | 0.07 | 0.13 | 167 / 237 |
| 2026-07-16 | 3.23.0 | 134 | 4,967 | 422 | 0.07 | 0.12 | 244 / 403 |
| 2026-08-04 | 3.38.0 | 243 | 7,037 | 579 | 0.06 | 0.12 | 128 / 128 † |

† audited denominators restart at the 2026-07-22 acknowledgment
cutoff; the miss columns are not one series across that line.

The helped/endorsement rates are stable across a store that quadrupled
its memory count and doubled its event log — the floor is holding, not
rising. Two hand corrections were applied to the 2026-07-16
publication on 2026-07-30 (a wrong claim that looser rules were
unmeasurable, and a stale tool count annotated as snapshot-valued);
both are embodied in the current prose, and the correction text
itself is in git history.

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
