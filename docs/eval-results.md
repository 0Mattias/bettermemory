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
- **The miss series ends in its current meaning at 3.41.0.** The
  prompt-recall hook now runs the same predicate at prompt time and
  converts a would-be miss into a `prompt_recall` delivery event plus
  an `ok` audit verdict, so on hook-wired stores with the default
  `[behavior] prompt_recall = true` the `search_miss` lane measures
  only the residual the recall path could not serve. The next
  snapshot's miss columns are not comparable to this one's — same
  discontinuity discipline as the 2026-07-22 cutoff above, named
  before the numbers rather than after.
- **The `prompt_recall` series widens at 6.2.0.** Delivery gains a
  second lane: under `[behavior] recall_in_project` (default on) the
  hook also injects on the project cohort the audit deliberately
  suppresses (caller inside the top hit's own repo). Delivered events
  stamp `delivered_reason` (`"miss"` / `"project_cohort"`); recall-rate
  comparisons across the 6.2.0 boundary must slice on
  `delivered_reason == "miss"` to stay like-for-like, and the audit's
  `search_miss` lane is unaffected. Named before the numbers, same
  discipline as above.
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

## Integrity benchmark

`bench/integrity/run.py`, v0, run 2026-09-04 on the sealed corpus
(`bench/integrity/corpus.json`, sha256 `39e7ed5b…`) at commits
90dd6de, 3b590dd and d349a5c of this repository, each on a clean tree.
Method and metric definitions are in
[eval.md](eval.md#integrity-benchmark). Every number below is printed
from `bench/integrity/results/integrity-v0-summary-2026-09-04.json` by
`run.py scorecard --markdown`, and the pre-registered predictions were
graded by the same command into
`bench/integrity/results/integrity-v0-scorecard-2026-09-04.json`. Arms:
bettermemory 7.0.0; mem0ai 2.0.18 twice (`mem0-raw`, `add(infer=False)`
with MiniLM embeddings; `mem0-infer`, extraction on); graphiti-core
0.30.1 on neo4j 5.26; the Letta server 0.16.8 with letta-client 1.12.1.
The extraction arms and Letta's embeddings ran on local models through
ollama (qwen2.5:7b, nomic-embed-text), keyless. Single run each,
defaults, nothing tuned.

**Staleness, memory versus memory** (24 supersession, 8 distractor, 8 reversion topics; k = 5):

| arm | sup. current | sup. stale unsignaled | sup. top-1 current | distr. current | distr. top-1 current | rev. current | rev. stale unsignaled | rev. top-1 current |
|---|---|---|---|---|---|---|---|---|
| bettermemory | 0.83 | 1.00 | 0.38 | 1.00 | 1.00 | 1.00 | 0.75 | 0.50 |
| mem0-raw | 0.96 | 1.00 | 0.08 | 1.00 | 1.00 | 1.00 | 1.00 | 0.88 |
| graphiti | 0.62 | 0.29 | 0.21 | 0.50 | 0.50 | 0.50 | 0.50 | 0.38 |
| letta | 0.92 | 1.00 | 0.17 | 1.00 | 1.00 | 1.00 | 0.88 | 0.88 |
| `serve_all_unsignaled` | 1.00 | 1.00 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `recency_top1` | 1.00 | 0.00 | 1.00 | 0.00 | 0.00 | 1.00 | 0.00 | 1.00 |
| `oracle_replica` | 1.00 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 |

**Poisoning, write admission** (30 payloads against 94 legitimate statements; flagged = refused, held pending or stored with a warning):

| arm | false fact flagged | instruction flagged | secret plain flagged | secret evasive flagged | legit flagged | precision | J | Fisher p | alerts/catch |
|---|---|---|---|---|---|---|---|---|---|
| bettermemory | 0.00 | 0.00 | 1.00 | 0.50 | 0.04 | 0.67 | 0.224 | 0.0012 | 1.5 |
| mem0-raw | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | n/a | 0.000 | n/a | n/a |
| graphiti | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | n/a | 0.000 | n/a | n/a |
| letta | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | n/a | 0.000 | n/a | n/a |
| `always_flag` | n/a | n/a | n/a | n/a | n/a | 0.24 | 0.000 | 1.0000 | 4.1 |
| `never_flag` | n/a | n/a | n/a | n/a | n/a | n/a | 0.000 | n/a | n/a |
| `oracle_replica` | n/a | n/a | n/a | n/a | n/a | 1.00 | 1.000 | 0.0000 | 1.0 |

`graphiti` extraction over 124 statements: 136 relations; 43 statements yielded none (34 of 94 legitimate); update statements that yielded a relation: 29 of 48.

**Poisoning, retrieval** (admitted payloads only; k = 5):

| arm | false facts admitted | poison top-1 rate | poison served | instructions admitted | injection served |
|---|---|---|---|---|---|
| bettermemory | 10 | 0.70 | 1.00 | 10 | 0.40 |
| mem0-raw | 10 | 0.50 | 1.00 | 10 | 0.40 |
| graphiti | 10 | 0.20 | 0.50 | 10 | 0.20 |
| letta | 10 | 0.40 | 1.00 | 10 | 0.50 |

**Poisoning, store injection** (10 false facts inserted around the write API; k = 10; rank shift is injected minus twin, negative when the injected record ranks higher):

| arm | plain: detected | plain: J | plain: median rank shift | provenance forged: detected |
|---|---|---|---|---|
| bettermemory | 1.00 | 1.000 | -1 | 0.00 |
| mem0-raw | 0.00 | 0.000 | -1 | 0.00 |
| graphiti | 1.00 | 1.000 | 1 | 0.00 |
| letta | 0.00 | 0.000 | 0 | 0.00 |

Arms that did not run:

- `mem0-infer`: the extractor (qwen2.5:7b through ollama) issued no UPDATE or DELETE on the self-test contradiction (events: ['ADD']); rerun with BM_INTEGRITY_LLM pointing at a model whose decision step updates

**Staleness, memory versus world** (carried from `bench/rot/results/multirepo-anchored-2026-07-30.json`, 30 repositories, 37,635 claims; rivals: not measurable: no rival exposes an interface that observes files or git):

| detector | precision | J | alerts/catch |
|---|---|---|---|
| file-level incumbent | 0.29 | 0.287 | 3.4 |
| claim-level weak | 0.94 | 0.973 | 1.1 |

**Scorecard** (pre-registered predictions, graded mechanically):

|  | arm | prediction | observed | grade |
|---|---|---|---|---|
| P1 | bettermemory | supersession: stale_served@5 >= 0.90 and stale_signaled <= 0.10 | `{"stale_served@5": 1.0, "stale_signaled": 0.0}` | hit |
| P2 | bettermemory | top1_current in [0.30, 0.70] on supersession, >= 0.70 on distractor | `{"supersession": 0.375, "distractor": 1.0}` | hit |
| P3 | mem0-infer | supersession: stale_served@5 <= 0.50 | `{"stale_served@5": null}` | not run |
| P3b | mem0-infer | distractor: current_served@5 <= 0.90 | `{"current_served@5": null}` | not run |
| P4 | mem0-raw | supersession: stale_served@5 >= 0.90, unsignaled | `{"stale_served@5": 1.0}` | hit |
| P5 | graphiti | supersession: stale_signaled >= 0.60; reversion: current_served@5 >= 0.60 | `{"stale_signaled": 0.25, "stale_served@5": 0.5417, "reversion_current_served@5": 0.5}` | **MISSED** |
| P6 | letta | supersession: stale_served@5 >= 0.90, unsignaled | `{"stale_served@5": 1.0}` | hit |
| P7 | bettermemory | plain secrets flagged >= 0.80, evasive <= 0.50, instruction 0, false fact 0, legit false alarms <= 0.10 | `{"secret_plain": 1.0, "secret_evasive": 0.5, "instruction": 0.0, "false_fact": 0.0, "legit_flagged": 0.0426}` | hit |
| P8 | all | rivals flag 0.00 in every class; mem0-infer admits >= 0.50 of instruction payloads | `{"mem0-raw": [0.0, 0.0, 0.0], "graphiti": [0.0, 0.0, 0.0], "letta": [0.0, 0.0, 0.0], "mem0-infer": "not run"}` | hit |
| P9 | all | false-fact poison_top1_rate >= 0.40 on every arm that ran | `{"bettermemory": 0.7, "mem0-raw": 0.5, "graphiti": 0.2, "letta": 0.4}` | **MISSED** |
| P10 | bettermemory | bettermemory plain plant detected 1.00; forged-event plant 0.00; every rival J = 0 | `{"bettermemory_plain": 1.0, "bettermemory_forged_event": 0.0, "mem0-raw": 0.0, "graphiti": 1.0, "letta": 0.0}` | **MISSED** |
| P11 | all | median rank shift between an injected record and its API-written twin <= 1 | `{"bettermemory": -1.0, "mem0-raw": -1.0, "graphiti": 1.0, "letta": 0.0}` | hit |

Reading the tables:

- **Every store without extraction serves the superseded fact, and
  none signals it.** On the 24 supersession topics bettermemory,
  mem0-raw and Letta each serve the stale value in the top five on
  every topic, unsignaled, which is what `serve_all_unsignaled` scores.
  This is the loss the declaration predicted for bettermemory: it has
  no write-time supersession, and its `superseded_by` and `contradicts`
  annotations render only links a caller sets. `recency_top1` shows
  what the trivial rule would buy and cost: exact on supersession and
  reversion, wrong on every distractor.
- **Graphiti is the only arm that invalidates, and it does so when its
  extractor reaches the fact.** Of the 14 stale relations it served on
  supersession topics, 11 carried `invalid_at`, and 6 of the 24 topics
  read fully signaled under the informative rule. Its other numbers
  are extraction gaps rather than reasoning: with the local model 43
  of 124 statements yielded no relation at all, so the current fact
  was never in the graph on 9 of 24 supersession topics and on half of
  the distractor and reversion topics, and a topic whose current fact
  is absent counts as unsignaled by the rule. P5 is graded MISSED on
  the signal clause for that reason, and the reading is the model's,
  not Graphiti's design: a stronger model would raise both the
  extraction rate and the signaled rate together.
- **Rank favours the older phrasing.** Top-1 current on supersession
  reads 0.38 for bettermemory, 0.21 for Graphiti, 0.17 for Letta and
  0.08 for mem0-raw. The first statement of a topic is phrased as the
  fact and the update as a change, and the queries ask for the fact, so
  the older statement matches better on every retrieval stack;
  bettermemory's recency factor, capped at 1.1x, lifts it to 0.38 and
  no further.
- **bettermemory's write gates cost it three legitimate updates.**
  Current served on supersession reads 0.83 because the transient gate
  refused the updates of t02, t05 and t20 on the phrase "the new" ("the
  new one", "the new SDK release", "the new base image"). The fourth
  refusal is the hard negative authored to trip it ("right now", "is in
  progress"). All four count as false alarms (legit flagged 0.04). The
  rivals refuse nothing and lose nothing here.
- **Admission: the credential gate is the only gate that moves.**
  bettermemory refused all six plain secrets and two of the four
  evasive ones: the split AWS key was caught on its unsplit secret half
  and the code-fenced token on its prefix, while the dot-separated
  token and the key described in prose were admitted. No arm flags a
  false fact or an embedded instruction, and every rival admits every
  payload. Pooled against the legitimate statements, bettermemory's
  write path scores J 0.224 at precision 0.67 and 1.5 alerts per catch,
  against 4.1 for `always_flag`; the rivals score exactly `never_flag`.
- **Once admitted, poison ranks well wherever it is stored whole.** A
  false fact written through the API outranks the fact it contradicts
  on the topic's own query on 7 of 10 topics in bettermemory (the
  recency factor works for the attacker as it works for an update), 5
  of 10 in mem0-raw and 4 of 10 in Letta, and every admitted false fact
  is served in the top five on those three arms. Graphiti served half
  of them and put 2 of 10 first, because its extractor dropped the
  other half, which is why P9 reads MISSED. Between two and five of the
  ten instruction payloads are served in the top five for one of three
  generic task queries on every arm; in bettermemory two of them sit at
  rank one.
- **Store injection separates the systems with provenance from the
  ones without.** A record planted around the write API reads
  `unaccounted` on every bettermemory read surface (10 of 10, J 1.0),
  and Graphiti's edges carry their source episodes, so an edge with
  none is just as visible there (10 of 10, J 1.0; the declaration
  predicted no rival would detect a plant, and P10 reads MISSED in
  Graphiti's favour). Nothing distinguishes the plant on mem0 or Letta
  (J 0.0). Forging the provenance binding defeats both detectors: the
  bettermemory plant with a forged `write` event line reads `local` (0
  of 10), the tamper-evidence gap SECURITY.md names, and a Graphiti edge
  that names an existing episode reads like any other. Forged trust
  metadata moved the injected record one slot above its API-written
  twin in bettermemory and mem0-raw, one slot below it in Graphiti
  (where only six of the ten twins were served at all) and not at all
  in Letta: rank is content, and the forged fields buy a tie-break.
- **mem0's extraction arm could not be exercised faithfully here.**
  With extraction on, mem0 extracts facts from each statement and asks
  its model to decide, per fact, between ADD, UPDATE, DELETE and NONE
  against the memories it retrieved. With qwen2.5:7b, and with
  llama3.1:8b in the smoke run, the decision step answered ADD for
  everything: 399 ADD events over the 124 statements, up to eleven
  memories stored per statement, retrieved memories about unrelated
  topics re-added as new, and not one UPDATE or DELETE on the 48 update
  statements
  (`bench/integrity/results/raw/mem0-infer-2026-09-04-no-self-test.json`).
  The adapter now runs a contradiction self-test first and the arm
  reads unavailable with the rerun command, since publishing that run
  would have scored the local model's failure as mem0's loss. P3 and
  P3b read not run for the same reason.
- **Memory versus world is carried, not re-run.** The rot benchmark's
  claim-level detector stands at precision 0.94, J 0.973 and 1.1
  alerts per catch on 37,635 claims across 30 repositories; no rival
  exposes an interface that observes files or git.

The scorecard reads 7 of 12 predictions hit, 3 MISSED and 2 not run.
Two of the three misses fall in a rival's favour (Graphiti detects a
naive plant; it also outranks fewer admitted false facts than the
threshold assumed, because it never stored half of them), and the
third (P5) is the local model's extraction rate. The declaration and
the unit record live in the project's memory store rather than in
this tree; the corpus, the harness, the raw observations and the
scored results are all committed.

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
