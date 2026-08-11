# P1e census 2 — declared in full before any cell was run, 2026-08-11

Census 1 (`retrieval/README.md`, "P1e — a from-scratch dense embedding",
artifact `bench/retrieval/results/embed-census-2026-08-11.json`) measured a
from-scratch GloVe embedding against P1a's gate and missed it at both
readings — short of the committed tables' precision at their own term
budget, and nearer to it only at roughly half that width. Every figure
lives in that artifact and is deliberately not restated here: a
declaration fixes criteria, and a criterion that duplicates a
measurement is a second copy free to drift from the first. It also
produced the finding that decides
what comes next — **the only corpus that yields usable neighbours is the
collection being ranked.** Every larger corpus covered more of the
probes' query vocabulary and emitted markedly less precise terms.

This document exists because that finding points at a specific
follow-up, and a follow-up chosen after seeing a grid is worth nothing
unless its bars were fixed first. **Everything below — the mechanism
family, every cell, both readings of the bar, the readiness criterion,
and the parking criterion — is committed before a single census-2 number
exists.** The enforcement record is the sha ordering: this commit,
then the run commit. Nothing may be added to the family afterwards.

## What census 1 settled, and why this is not a fishing trip

**The shape under test is the product's natural form.** A model trained
on the collection it ranks is not a benchmark artifact — it is what
bettermemory would actually ship: every install derives its own vectors
from its own store, locally, at derivation time. No external corpus, no
download, no third-party weights, no network. That is WaC-clean by
construction, and the bench is admissible for it precisely because
`bench/retrieval/corpus.jsonl` plays the store's role. Census 1's
`store` arm already is this configuration.

**Gutenberg is parked, by the data rather than by preference.** A
public-domain external corpus was flagged for the owner in
[`THIRD_INSTRUMENT.md`](THIRD_INSTRUMENT.md) with a favourable licence
analysis and deliberately not fetched. Census 1's topicality wall
retires it: scale was measured and is not the missing input, so no
fetch-with-pinned-hash policy gets established for a low-probability
bet. The licence analysis stays on file; the arm does not run.

## 1. The mechanism family, enumerated

One corpus: **per-store self-trained**, `bench/embed_train.py --corpus
store`, at the trainer's declared defaults (dim 64, epochs 15, seed
20260811). Census 1's sensitivity sweep already established those
defaults are the sweep's best and that more training is worse, so no
training parameter is swept again here.

Four axes, crossed completely — **128 cells, no more**:

| axis | values | what it is |
| --- | --- | --- |
| `top_k` | 1, 2, 3, 5 | narrow-width emission: neighbours kept per query token |
| `tau` | 0.95, 0.98, 0.99, 0.995 | cosine-threshold selection, the one dial census 1 showed buys precision |
| `veto` | `none`, `ppmi_positive` | the sparse-veto census 1 named and deliberately did not run |
| `bridging` | off, on | census 1's n-gram composition for out-of-vocabulary query tokens |
| `postproc` | `raw`, `centred` | the two vector readings census 1 reported |

**The veto, defined exactly.** For query token `t` and candidate `c`,
emit `c` only if `t` and `c` have an above-chance co-occurrence in the
store: `ppmi(t, c) > 0` under P1a's own `associates` (min_df 2, shift
1.0, `bench/ppmi_census.py`). It is a VETO, not a selector — the dense
model still chooses and ranks; the counts only remove candidates they
do not independently support.

This is deliberately not census 1's agreement rule, which intersected
the two top-k lists and measured **worse** than the dense model alone
(`retrieval/results/embed-hybrid-2026-08-11.json`). That failure's
diagnosis is the reason for this shape: rank agreement selects for
high-count pairs, so it concentrates on frequent, undiscriminating
terms. A positivity veto has no such preference — it removes only
candidates with no count support at all.

**Excluded, and why.** A document-frequency cap on emitted terms was
considered and is NOT in the family: addendum 8 states that round 2
measured df as a separator for emitted terms and killed it, and that
"no df gate on emitted terms appears here". Round 2 is not relitigated.

## 2. The bars, quoted from P1a's gate

The gate is addendum 8's, unchanged and not restated in friendlier
terms: **the best grid cell must reach at least the committed static
tables' own precision**, measured identically on the same dev probes as
the fraction of emitted terms appearing in the gold document.

The incumbent's precision, its emitted-term count and its Wilson
interval are the ones recorded in
`bench/retrieval/results/embed-census-2026-08-11.json`, and this
document cites them rather than copying them. `bench/embed_round2.py`
holds the same values as named constants, RECOMPUTES the incumbent from
the same probes at run time, and aborts the run if either disagrees —
so a token-pipeline drift voids the census instead of quietly rebasing
the bar.

`MIN_GATE_TERMS = 30` is retained from census 1: no cell resting on
fewer emitted terms may carry a verdict.

### The width problem, and the dual reading declared before the numbers

Census 1's best cell came closest to the bar while emitting roughly
half the incumbent's terms per probe
(`bench/retrieval/results/embed-census-2026-08-11.json`). Precision
generally rises as emission narrows, so a narrow cell compared against
a wide incumbent could be
winning on width rather than on quality. Two readings are therefore
declared, and both are reported for every cell:

**Reading A — at width.** Only cells emitting **at least the
incumbent's own terms per probe** are compared against it. This is the
replacement reading: a
source that matches the incumbent's precision while emitting half as
many terms is narrower, not better, and cannot replace it.

**Reading B — at matched narrow width.** The incumbent is re-estimated
at the challenger cell's own width by uniformly subsampling its emitted
terms to that width, over the declared number of seeds, reporting the
mean and the 5th/95th percentiles.

**Reading B carries a prediction, declared now.** Uniform subsampling of
a set is unbiased, and the committed tables emit an unordered set with
no score to narrow by — so the incumbent has no mechanism that would
make it more precise when narrowed. Reading B should therefore
**reproduce the incumbent's precision to within sampling noise at every
width**. If it does, the two readings collapse and a narrow challenger
may be compared against the incumbent directly. **If Reading B instead
moves materially with
width, that is itself the finding, and it invalidates every narrow-cell
comparison in census 1 — including its closest-to-the-bar headline, as
recorded in `bench/retrieval/results/embed-census-2026-08-11.json`.**

## 3. The readiness criterion, stated before any result

Census 2 exists to answer one question: **does anything license writing
the P1e preregistration?** The answer is decided by a single cell named
now, not by the best of 128.

**The primary cell** — `store / centred / top_k=2 / tau=0.99 /
veto=ppmi_positive / bridging=off`.

Chosen by a stated rule rather than by prospects, with every width below
read off `bench/retrieval/results/embed-census-2026-08-11.json`: *the
narrowest cell in the declared grid whose census-1 width is at least
one and a half times the incumbent's own terms per probe*, so the veto has room to
remove up to a third of the emission and still satisfy Reading A. That
rule selects `k2_t0.99` uniquely — the next-narrowest candidate,
`k3_t0.995`, falls below that threshold. Its census-1 value without the
veto is published in that same artifact, so the veto is the only
untested change at this cell and its effect is a within-cell delta
against a number already in the record.

**Writing the P1e preregistration is licensed if and only if ALL of:**

- **R1** — the primary cell's precision reaches the incumbent's, i.e. a
  gate multiple of at least one. The point comparison, exactly as
  addendum 8 fixes it.
- **R2** — the primary cell emits at least the incumbent's terms per
  probe (Reading A).
- **R3** — the lower bound of the primary cell's Wilson interval is at
  least the incumbent's own lower bound. This stops a pass resting on a
  point estimate over a thin sample.
- **R4** — the primary cell emits at least `MIN_GATE_TERMS` in total.

All four bounds are the incumbent's own, carried in
`bench/embed_round2.py` as `READING_A_MIN_WIDTH`, `R3_CI_LOWER_BOUND`,
`GATE_MULTIPLE` and `MIN_GATE_TERMS`, and checked against the census-1
artifact by `tests/test_bench_embed.py`.

**The lane is parked if:** the primary cell fails R1, **and** no cell in
the family reaches the gate (against the incumbent recorded in
`bench/retrieval/results/embed-census-2026-08-11.json`) while emitting
at least the incumbent's terms per probe. That is
the at-width reading failing family-wide, and it retires the
self-trained dense source at personal-store scale.

**The anti-gate-shopping clause.** If the primary fails but some
non-primary cell satisfies R1-R4, that result **does not license a
preregistration**. It licenses at most a census-3 declaration naming
that cell as *its* primary, to be run against fresh evidence. A cell
selected as the maximum of 128 is not a preregistered hypothesis, and
promoting one would be exactly the move this document exists to
prevent. The remaining 127 cells are reported as the family's shape and
are explicitly not eligible to carry the verdict.

## 4. The constraint ledger

- **The sealed instrument is untouched and unspent.** No file under
  `bench/heldout/` is opened by census 2 — not `questions.json`, not
  `personas.json`, not any gold label. It has now been protected
  through P1a, rounds 6-8, census 1 and census 2, and has never been
  scored.
- **`THIRD_INSTRUMENT.md` still binds.** A clean held-out check for any
  vocabulary-adapting mechanism needs a third instrument that does not
  exist. Passing census 2 licenses **writing a preregistration** and
  nothing else; it does not license running a held-out check, and the
  preregistration would itself have to budget for that instrument.
- **No engine code, whatever the outcome.** Nothing under `src/`
  changes. No ranking path, no threshold, no default. Census 2 is
  statistics.
- **Dev-side by construction.** The census needs gold labels, so it runs
  on `bench/retrieval`'s blind-authored gold set, whose contamination
  status is unchanged and already recorded.
- **No changelog entry.** Census commits ship none, matching `d2465d9`
  and `d4fb544`: no user-facing surface changes.
