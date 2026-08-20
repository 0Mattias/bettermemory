# W1C — the wide-window retrain: pricing the knob W1b never moved

`bench/POWER_AUDIT.md` re-opened exactly one W1b conclusion: "the wall
is the objective, not the corpus" was reached with every tuning read
varying emission thresholds over ONE frozen vector set, while window
width — the hyperparameter that trades paradigmatic (substitutability,
what W1b found) for syntagmatic (co-occurrence, what it wanted) — never
moved. This unit moves it, once, and grades the move on a geometry
census that is derived rather than chosen. It is science for the W2
entry decision, not a capability rung: I1 measured static expansion
tables at no detectable work at the rebuilt instrument's full scale
(`bench/retrieval/I1_RECORD.md`), so even a perfect table is not the
prize here — the shape of what a window objective CAN learn is.

Whatever the outcome, the W2 decision inherits it: if width unlocks
cross-form geometry, W2's contrastive design must beat a cheaper
in-family knob before it justifies itself; if width changes nothing,
the objective is priced as the wall with the last in-family knob spent,
and the contrastive rung is the only route left to the vocabulary
prize (the dev instrument's human-requery ceiling; two-pass feedback
requery closed for good by `bench/REQUERY_CENSUS_2_DECLARATION.md`'s
census).

## 1. The single change

The trainer, register slice, tokenizer, and every hyperparameter are
W1b's, byte for byte (`bench/w/W1B_DECLARATION.md`; trainer
`bench/w/w1_train.py`, untouched). One flag differs. The invocation,
verbatim:

```
fastvenv/bin/python bench/w/w1_train.py --w1b \
  --out /Volumes/data/bettermemory/runs/w1c-2026-08-20 \
  --seed 20260818 --token-cap 200000000 \
  --dim 100 --buckets 524288 --window 15 \
  --negatives 5 --epochs 3 --min-count 10 --vocab-cap 150000 \
  --subsample 1e-4 --lr 0.025 --batch 1024
```

W1b's dynamic window was 5; this run's is 15. Same seed deliberately:
the comparison is between recipes, and holding the seed removes one
source of variation even though the width draws necessarily produce a
different pair stream (see confound 1).

## 2. The primary read — a geometry census derived, not chosen

The probe machinery is the committed
`bench/w/w1b_geometry_probe.py`, unchanged: cosine over the same
subword-composed vectors the emitter ranks with, mutual ranks against
its committed emission-window criterion (default mutual-rank 64), the
three frozen families carried byte for byte.

New, and mechanical: an EXPANDED CROSS-FORM family enumerated from the
committed hand table itself
(`src/bettermemory/expansion.py::SYNONYM_GROUPS`) by
`bench/w/w1c_geometry_census.py` — every unordered within-group pair
whose two members map to different stems under the engine's own
stemmer, one surface form per stem (first occurrence in group order).
The table is precisely the list of cross-form relations a learned
replacement must carry, so deriving the census from it leaves zero
selection freedom; the derivation rule is this paragraph and the
script is its mechanical half.

The census reads BOTH vector sets, paired by construction:

- window 5: the retained W1b run
  (`/Volumes/data/bettermemory/runs/w1b-2026-08-18`, whose byte-identical
  retrain sits beside it) — the committed six cross-form pairs at that
  window are already published in
  `bench/w/results/w1b-geometry-2026-08-18.json`, and the expanded set
  is a new reading of existing vectors, not a new training.
- window 15: this unit's run.

## 3. The criterion, stated before any training byte

Fractions are over the expanded cross-form family's in-vocabulary
pairs, membership per the probe's committed emission-window test:

```
WINDOW-IS-THE-WALL  (W1b's conclusion REFUTED):
    at window 15, >= 50% of expanded cross-form pairs are inside the
    emission window
OBJECTIVE-IS-THE-WALL  (W1b's conclusion CONFIRMED and priced):
    at window 15, < 20% of expanded cross-form pairs are inside, AND
    >= 75% of the morphological family remains inside
PARTIAL:
    anything between — recorded as such; the only permitted follow-up
    is a declared unit naming what would settle it
```

The window-5 reading of the expanded family is reported beside every
window-15 number, and the committed six-pair family is reported from
both runs as the continuity anchor.

## 4. The secondary read — no bar, expectation stated

The syn-form table emitted from the window-15 vectors at W1b's
committed emission settings, graded on the 120-question instrument
paired against expansion-off (McNemar, the instrument's own tooling).
Expectation, in advance: nothing measurable — I1 killed the static
table class at this scale, and this read exists because omitting it
would look like hiding. A measurable move in either direction is a
finding to be named, not a bar to be claimed.

## 5. Determinism and budgets

- Single training run. The artifact carries the trainer's own content
  hashes (vectors, ctx, vocab, derived token cache). No second full
  retrain is declared: W1b proved this trainer byte-identical across
  two independent full runs, no code path changes here — only a flag
  value — and the CI fixture leg (`tests/test_w1b_determinism.py`)
  guards the class on every push.
- Budgets, hard, park-on-breach and disclosed: token reads within
  W1b's committed cap (window width does not change tokens read);
  wall-clock within W1b's committed ceiling (expected roughly three times
  W1b's measured run from mean-width scaling); RSS bounded by the same
  segmented enumeration (`--segment` untouched).

## 6. Constraint ledger

- Grading is dev-side and geometry-side only; nothing under
  `bench/longmemeval/`, `bench/msc/` or `bench/heldout/` is read.
- No engine code changes, whatever the outcome.
- The census script reads committed table data and trained-vector
  directories; it selects nothing.
- Deterministic artifacts: sorted iteration, no wall-clock content
  beyond provenance dates.
- No changelog entry.

## Declared confounds

1. **Same seed is not the same pair stream.** Width draws consume the
   RNG differently, so the two runs differ in more than window
   semantics at the update level. The unit compares RECIPES; it cannot
   attribute differences to width alone at the gradient level, and
   does not.
2. **Update count scales with width.** Mean dynamic width triples, so
   the window-15 run performs roughly three times the updates on the
   same read stream. If WINDOW-IS-THE-WALL fires, an update-matched
   control (window 5 at raised epochs) becomes the named follow-up
   BEFORE W2 inherits the conclusion; if the geometry stays flat, the
   extra updates only strengthen the null.
3. **The expanded census measures the table's relations.** Pairs the
   curated table never carried are invisible to it; the census prices
   the relations a replacement must carry, not all relations that
   exist.
4. **One register.** Everything here is the W1b technical register;
   nothing transfers to conversational stores by default.

## What is not claimed

- No recall claim on any instrument, and no default change.
- No LongMemEval read.
- No W2 verdict: this unit prices W2's premise, the decision stays an
  owner door.
