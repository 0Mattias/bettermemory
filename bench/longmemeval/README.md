# LongMemEval — session-level retrieval on third-party labels

Build-order item (e). `bench/retrieval/` ends by disclaiming a
comparative claim; this directory exists to earn one, on a corpus and
against labels that neither this project nor claude-mem authored.

```sh
.venv/bin/python bench/longmemeval/run.py --limit 20      # smoke
.venv/bin/python bench/longmemeval/run.py                 # full, ~27 min
.venv/bin/python bench/longmemeval/run.py --json
.venv/bin/python bench/longmemeval/run.py --json \
  --per-question results/per-question/YYYY-MM-DD.json     # + per-question sidecar
```

`--per-question` writes one record per scored question — `qid`, `type`,
`n_evidence`, `evidence_ranks`, `n_ranked` — and every published
aggregate is a function of those fields (`recall@k` counts evidence
ranks below k over `n_evidence`). Sidecars live one directory below the
summaries deliberately: `tests/test_number_claims.py` globs
`bench/*/results/*.json` one level deep for its pin pool, and a file of
1,000 rank integers would let almost any small number find a "pin".
Sidecars are analysis input, not citable evidence.

The corpus is not vendored (265 MB). Fetch it:

```sh
mkdir -p bench/longmemeval/data && cd bench/longmemeval/data
curl -LO https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
```

**Read [PREREGISTRATION.md](PREREGISTRATION.md) first.** It fixes the
attribution rule, the metric, and five falsifiable predictions, and it
was committed before the corpus was downloaded.

## The standing comparison

**bettermemory's default install retrieves with deterministic lexical
code only and scores 90.6% macro recall@5 on this harness; claude-mem's
embedding-native stack scores 91.6%. The default is 1.0 point behind,
by our own measurement** — from 2.3 before the 6.1.0 conversational
lane (`../l/L1_RECORD.md`; the gate artifact is
`../l/results/gate-lme-conv-a-2026-08-16.json`, its paired lane-off
control reproducing the prior 89.3% default reading exactly). The
now-removed opt-in `embeddings` extra read **91.8%** in its day
(restored 5.5.0 under the door C contract, revoked by owner doctrine in
6.0.0), and the retrieval-recall campaign that governed this gap is
closed (`../R3_DEFAULT_DECISION.md`): every deterministic mechanism was
measured to its own declared criterion, the default install stays
lexical by doctrine, and the campaign's default-engine success bar was
never claimed. The 6.1.0 default reading comes from Lane L's own
declared unit, gated primary on this instrument with the dev instrument
as its no-regression guard.

The dated best-arm-vs-best-arm record behind those numbers, measured
2026-07-26/27 (`results/s-cleaned-both-arms.json`,
`results/claude-mem-full500.json`):

| system / arm | @1 | @5 | @10 |
| --- | --- | --- | --- |
| bettermemory lexical | 52.5% | 89.3% | 94.4% |
| bettermemory semantic | **56.2%** | **91.8%** | 95.6% |
| claude-mem lexical | 0.1% | 0.1% | 0.1% |
| claude-mem semantic | 54.2% | 91.6% | **96.9%** |

Per question type the two systems trade places — bettermemory wins
knowledge-update (+2.6) and single-session-user (+4.3), claude-mem
wins multi-session (−2.6) — and the honest headline is **parity, not
victory**: two entirely different architectures land within 0.2 points
on 500 third-party questions. Retrieval recall on this kind of corpus
is close to saturated for any competent design. The competitive claim
lives on the correctness axis instead, where the reference stack
scores a structural N/A (`bench/rot/` — no `verified_at`, no
`superseded_by`, no lifecycle verb but DELETE): the defensible
sentence is "we verify and here is the measured accuracy," not "we
retrieve better."

The claude-mem side is dated 2026-07-27 at `claude-mem@13.12.4` and
stays so (the tooling is no longer installed here); the bettermemory
side has since reproduced bit-for-bit through twelve engine releases
and the restored arm (below).

## Results — canonical

500/500 instances, corpus sha256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`,
retrieval depth 200, distinct-session collapse. First measured at
3.30.0; reproduced **bit-for-bit** at the 3.42.0 engine
(`results/baseline-both-arms-2026-08-08.json`, provenance-stamped —
a 2026-08-14 correction fixed this section's earlier "3.43.0 / nine
releases" wording against the artifact's own provenance block), and
again through the restored opt-in arm at R2 (below).

Session-level recall@k, macro-averaged, **[ceiling]** = maximum
achievable at that k. These rows are the LANE-OFF record — the engine
before the 6.1.0 conversational lane, still reproducible via
`--conversational off` and re-proven exactly as the L1 gate's paired
control; the 6.1.0 default reading (53.4% / 90.6% / 94.9%) lives in
`../l/results/gate-lme-conv-a-2026-08-16.json`:

| arm | @1 | @5 | @10 |
| --- | --- | --- | --- |
| lexical | 52.5% **[64%]** | 89.3% [100%] | 94.4% [100%] |
| semantic | **56.2%** [64%] | **91.8%** [100%] | **95.6%** [100%] |

The @1 ceiling is 64%, not 100%: 324 of 500 questions need two or more
evidence sessions and only one slot exists, so lexical@1 is 82% of
what is arithmetically reachable.

By question type, macro recall@5:

| question type | lexical | semantic | n | Δ |
| --- | --- | --- | --- | --- |
| single-session-assistant | 100.0% | 100.0% | 56 | — |
| knowledge-update | 98.1% | 98.1% | 78 | — |
| single-session-user | 97.1% | 97.1% | 70 | — |
| multi-session | 84.9% | 86.7% | 133 | +1.8 |
| temporal-reasoning | 83.7% | 86.1% | 133 | +2.4 |
| **single-session-preference** | 73.3% | **96.7%** | 30 | **+23.3** |

## R2 — the arm returns opt-in and the dated record reproduces exactly, 2026-08-14

Stage two of the door C reentry ladder ran here under
`../R2_REENTRY_DECLARATION.md` — bars fixed before the runner regained
the arm, sha ordering declaration → implementation → run. **Every
figure of the dated both-arms record reproduces exactly through the
modern 5.5.0 engine** — macro, micro, by-type and depth-truncation —
and the determinism repeat reproduced the artifact byte-identically
modulo wall-clock (`results/r2-both-arms-2026-08-14.json`,
`results/r2-both-arms-repeat-2026-08-14.json`). Verdict by the
declaration's own bars: **R2-PASS**. The lexical identity doubles as
the restoration's held-out inertness proof: the 5.5.0 default engine
is byte-identical to the pre-restoration engine on this corpus.

The declared reads: the reference line is met (91.8% ≥ 91.6%) for an
install that names the extra, while the default-install restatement
stays true; no class is harmed by the arm (single-session-preference
+23.3 carries the pooled lift; three classes move 0.0); the
multi-session slice stays behind the reference (86.7% vs 89.3%) arm or
no arm; and the embedding arm's cost factor reads ~3× today (332.9s
lexical, 1,079.9s semantic, against the dated 331.4/1,229.3). R3
followed the same day and closed the campaign
(`../R3_DEFAULT_DECISION.md`): no default remained to flip, and the
success criterion resolved unmeetable-as-written with the bar never
claimed.

## Predictions scored

| # | prediction | outcome |
| --- | --- | --- |
| P1 | claude-mem's arm spread exceeds ours by ≥10 pts | **HELD — +89.0** (91.5 vs 2.5) |
| P2 | semantic beats lexical at @5 by >5 and <25 pts | **MISSED — +2.5** |
| P3 | multi-session ≥15 pts below the other types | **MISSED — 5.5 pts** (the good branch) |
| P4 | we do *not* win knowledge-update | **HELD — +2.6 pts** |
| P5 | ≥2% of offered rounds lost to dedup | **MISSED — 0.000%**, and badly posed |

P4 is the prediction that earned its keep: it was written to stop a
future overclaim by us — knowledge-update is this project's
differentiating axis, and the measured +2.6 is a retrieval margin, not
evidence the correctness machinery works; that axis is `bench/rot/`'s.
P2's miss is the finding that ran against us: the embedding arm's lift
here is +2.5, well under the predicted band, which is what made the
campaign's later cost/benefit arithmetic honest. Full grading prose:
this file's git history.

## Data integrity

- 13 questions repeat a session id inside their own haystack — deduped
  on ingest, counted in every result file, not dropped.
- Depth truncation is negligible: 0 questions at k=1, 2 at k=5, 9 at
  k=10 failed to yield k distinct sessions from 200 ranked items.
- Zero abstention questions exist in the distributed corpus, so one of
  the five abilities the paper advertises is unmeasurable here
  (PREREGISTRATION.md addendum 3).

**Three discarded runs, kept because they flattered us:**

| run | claude-mem @5 | why it was void |
| --- | --- | --- |
| first 40-question | 7.5% | 20 s fixed sleep; index barely built |
| full 500 (#1) | 54.1% | Chroma 57% built, 210/500 empty |
| **full 500 (#2)** | **91.6%** | valid — index 100%, 0 empty |

The middle row would have published a +37.7-point win over a
competitor, and the margin was almost entirely a half-built vector
index on this machine — caught by `await_chroma_backfill` measuring
readiness, not by the number looking wrong. **The dominant failure
mode in a comparative benchmark is not mismeasuring yourself, it is
mismeasuring the competitor in your own favour.** Three for three
here. The invalid artifact is retained as
`results/claude-mem-full500-INVALID-partial-index.json`.

## Read-side diversification: measured, and closed

The largest remaining error was once diagnosed as a coverage problem —
partial questions on the two multi-evidence classes, with a prescribed
read-side re-ranker worth +3.2 pooled recall@5. The coverage probe
killed the diagnosis, and its answer is the reason the item is closed:
**the evidence a search drops does not carry query terms the survivors
lack.** Measured over 65 partial questions and 87 dropped evidence
sessions (`results/coverage-probe-2026-07-30.json`), the dropped
session carries zero novel terms in 93.1% of cases against the broad
reference (85.1% strict), matches *fewer* terms than the survivors it
lost to (median 2 against 3), and in 337 of 500 questions the top 5
already carries every term anything in the corpus matched. The probe
also bounds an **omniscient** rescue: the best novelty signal available
lifts precision 1.22× over blind promotion on a 4.5% base rate, where
clearing the pre-stated +2.00 gate needs on the order of 25–30%
precision. The rescue was built anyway (a plausible mechanism is not a
result) and measured +0.06 pooled against the +2.00 gate with
evidence-weighted recall moving backwards
(`results/co-evidence-rescue-2026-07-30.json`) — reverted, artifacts
kept.

The headroom itself is real and stays open: perfect rescue of evidence
already inside the first 10 distinct sessions is worth +5.0 pooled
(dropped sessions sit at median distinct-session rank 8), and every
per-class ceiling at k=5 is ~100%
(`results/baseline-both-arms-2026-07-30.json`). What is excluded is
that the matched-term set expresses *why*. A future attempt needs a
signal the fused ranking does not already contain — and should point
`coverage_probe.py` at whatever it proposes to key on before building
anything.

## The campaign record on this instrument, 2026-08-09 → 2026-08-14

This corpus was the held-out check for every round of the retrieval
campaign; each entry's full narrative is in this file's git history,
its dev-side twin in `bench/retrieval/README.md`'s record, and its
numbers in the named artifacts. One line each:

- **5.1 rescue-expansion lane** (2026-08-09): the held-out check fired
  its kill — dev gains, LongMemEval losses — so the lane shipped
  opt-in, default-off (`results/rescue-expansion-2026-08-09.json` +
  ablations).
- **Rounds 2–5** (2026-08-10): df gate killed pre-run
  (`results/df-census-2026-08-10.json`, `results/gate0-2026-08-10.json`);
  cap, self-calibration and evidence arcs each killed at their gates
  (`results/round3-*`, `round4-*`, `round5-*`; re-baseline
  `results/rebaseline-*-2026-08-10.json`).
- **Rounds 6–7** (2026-08-11): the evidence-scaled vote cleared this
  corpus and the dev gate stopped it; the structural curve located the
  trade-off exactly (`results/round6-*`, `round7-*-2026-08-11.json`).
- **P1a / P1e** (2026-08-11): PPMI killed at Gate 0; the trained form
  measured to the same bar and parked
  (`results/embed-census-2026-08-11.json`).
- **Round 9, base-leg withholding** (2026-08-12): the campaign's
  largest dev gains, killed here on all three macros — kill 3 of the
  census-counterfactual method (`results/round9-off-2026-08-12.json`,
  `results/round9-withhold-2026-08-12.json`).
- **R2** (2026-08-14): the reentry's held-out read, R2-PASS (section
  above).

One correction from that record outlives its narrative, because the
honest marker is all that stands between a reader and a claim the
files contradict: an ablation is a working-tree patch on the imported
engine and cannot be anything else — an earlier wording said the 5.1
leg-only rerun came from the clean committed tree, and it did not.
Both published ablation
artifacts carry `tree_dirty: true` and say so themselves; an ablation
artifact is dirty BY CONSTRUCTION.

## What this does not measure

- **Any competitor inside `run.py`.** This runner scores bettermemory
  alone; the claude-mem arms run through `cm_run.py` and publish their
  own artifacts. The comparison rests on the paired artifacts and the
  preregistered attribution rule, never on one run's numbers.
- **End-to-end capture.** Ingest bypasses `memory_write`'s dedup,
  transient screening and confirmation flow: `run.py` calls
  `Store.write` directly. This is store + retrieval.
- **The above-threshold regime.** Per-question stores hold ~249 items
  against a 500-item index threshold, so bm25 prefiltering never
  engages. `bench/retrieval/` measured that regime for itself; the
  result does not transfer, this directory has never run above the
  threshold, and it remains the most likely place for the tie to break
  in either direction.
- **Enrichment parity.** claude-mem's `observations_fts` spans six
  columns its pipeline fills by LLM extraction; this harness fills
  one. Their 91.6% is a floor, not a ceiling (PREREGISTRATION.md
  addendum 2).
- **Answer correctness.** No judged arm, by design: it requires a
  GPT-4o judge and an API key, which collides with the autonomy
  criterion.
- **Staleness accuracy.** See P4. `bench/rot/` owns that axis.
