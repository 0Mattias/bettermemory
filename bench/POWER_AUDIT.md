# The power audit — what the twenty-question instrument can and cannot say, 2026-08-18

This document adds a reading. It renegotiates nothing.

Every gate verdict in this repository stands exactly as its record
published it. W1 parked, W1b parked, R3 resolved its criterion
unmeetable-as-written, and none of that changes here: a bar is a point
comparison, each declaration wrote its bar before its run, and each
record published the miss it got. What this audit does is state, once
and in one place, **how much of each dev-side number is the instrument
rather than the engine** — because that question was asked of the
census work and never of the gate reads, and the answer turns out to
govern how several published sentences should be read.

The prompt was a direct one from the owner: make sure we are not
delusional. This is the part of the answer that is arithmetic.

## The finding

`bench/retrieval/questions.jsonl` holds **twenty questions**. At that
size the 95% Wilson interval on a recall figure is about forty points
wide, and the dev bars the campaign has been writing are five points
apart.

| arm | recall@1 | 95% CI | recall@5 | 95% CI |
|---|---|---|---|---|
| expansion off | 7/20 = 35% | [18%, 57%] | 12/20 = 60% | [39%, 78%] |
| static hand tables | 11/20 = 55% | [34%, 74%] | 18/20 = 90% | [70%, 97%] |
| W1b-full | 10/20 = 50% | [30%, 70%] | 13/20 = 65% | [43%, 82%] |
| W1b-syn | 11/20 = 55% | [34%, 74%] | 14/20 = 70% | [48%, 85%] |
| **G1 bar** | **12/20 = 60%** | **[39%, 78%]** | 18/20 = 90% | [70%, 97%] |

The bar sits inside the interval of the incumbent it was written to
beat. That is not a criticism of how G1 was set —
`bench/w/W1B_DECLARATION.md` derives it honestly, as "the smallest
strict beat of the static arm's 55% this twenty-question instrument
can express." The trouble is that the smallest expressible increment
and the smallest *resolvable* one are different numbers, and only the
first was ever computed.

The arms are paired — every arm answers the same twenty questions — so
the correct test is McNemar's, not a two-proportion test, and the
paired form is the instrument's one statistical advantage. It does not
rescue the design:

| discordant questions, all one direction | exact p |
|---|---|
| 1 | 1.000 |
| 2 | 0.500 |
| 3 | 0.250 |
| 4 | 0.125 |
| 5 | 0.063 |
| **6** | **0.031** |

**Six questions must move one way before anything registers at
alpha=0.05.** On twenty questions that is a thirty-point swing. The
largest dev-side gap the campaign has ever recorded is four questions
— the static arm's 90% against W1b-syn's 70% at recall@5 — and at four
discordant, best case, p=0.125.

So: **no dev cell in the entire campaign has ever been measurable.**

## What that does to specific published sentences

None of these were wrong to publish. Each is re-read here, not
retracted.

- "**rank one gained five points on both arms over W1**"
  (`bench/w/W1B_RECORD.md`) — that is *one question* on each arm.
  `bench/retrieval/README.md` already fixes the reading for exactly
  this case: "one question out of twenty, read as no measurable
  change." The record's own framing should have been that sentence.
- **The seven-read tuning trail** moved between 8/20 and 11/20 at rank
  one. Every step in it is one or two questions. The trail is real and
  published and the reads happened; what cannot be concluded from it
  is that the frontier *plateaued*, because the instrument cannot
  distinguish a plateau from noise at that amplitude.
- **The recall@5 half of the G2 miss** — the arm
  (`bench/w/results/gate-w1b-lme-full-2026-08-18.json`) lands under
  the floor its declaration fixed *in the fourth decimal*. Across five
  hundred questions that is about a tenth of one question's worth of
  recall mass. As an EFFECT it is
  negligible, and a bar decided on it is decided on nothing.
  Whether it is statistically distinguishable from zero is a separate
  question, and one this audit deliberately does not answer from the
  single-arm interval: LongMemEval's macro carries about ±2.1 points
  of spread at recall@5 (measured, see below), but that spread is
  dominated by questions differing in difficulty, and that variance
  CANCELS between two arms scored on the same questions. Waving the
  gap away with the single-arm interval would be the mirror image of
  the error this audit is about — under-reading instead of
  over-reading. The paired difference is the right test; it needs both
  arms' per-question records, and the runner now keeps them.
- "**the learned table stays twenty points behind at rank five**" —
  four questions, p=0.125. The direction is consistent across arms and
  is probably real; it has not been shown.

## What survives the audit intact

Rigor cuts both ways, and most of what this instrument has published
holds up.

- **The requery finding is solid.** Re-running the dev instrument with
  the paired test now wired in: requery beats asked by 9 questions at
  recall@1 (p=0.022) and 8 at recall@5 (p=0.008). The README's central
  claim — that content words the document contains buy a large,
  real lift — is measurable and confirmed.
- **The control finding is solid.** control vs asked is 0 questions,
  p=1.000. "Stripping interrogatives buys nothing" is exactly right,
  and now has a test statistic behind it.
- **The LongMemEval half of every Lane W read is well-powered** at
  n=500, and its finding — that learned synonym mass deepens the
  conversational cost rather than shrinking it, replicated at four
  times the corpus — is the trustworthy result those units produced.
  Re-running the expansion-off arm with the interval wiring in place
  reproduces the committed incumbent exactly, 0.5339 at recall@1 and
  0.9062 at recall@5, with 95% intervals of [0.5045, 0.5633] and
  [0.8852, 0.9272]. Those are single-arm intervals — the instrument's
  absolute spread, not the resolution of a two-arm comparison.
- **The W1b geometry probe is sound.** Its committed spot-list
  (`bench/w/results/w1b-geometry-2026-08-18.json`) puts four of four
  morphological pairs inside the emission window against zero of six
  cross-form pairs — a split Fisher's exact test separates well below
  the conventional five-percent threshold — and the cosines it records
  are direct measurements of the geometry rather than question-level
  noise: `toggle`/`flag` at 0.284, `flag`/`boolean` negative outright,
  each word ranking the other past position eighty thousand. The diagnosis that skip-gram
  geometry does not hold cross-form pairs stands on its own evidence.
- **G3, the determinism bar, is untouched by any of this.** Byte
  identity is not a statistical claim.

## The paired reading, measured

The fix was run on the instrument it argues about. Expansion-off
against the static hand tables, same five hundred questions, via the
new `--compare`
(`bench/longmemeval/results/paired-reading-2026-08-18.json`, with the
off arm's per-question record beside it at
`bench/longmemeval/results/per-question-off-2026-08-18.json`):

| depth | difference, tables minus off | 95% CI | reading |
|---|---|---|---|
| macro@1 | -0.0268 | [-0.0419, -0.0118] | measurable |
| macro@5 | -0.0088 | [-0.0153, -0.0023] | measurable |
| macro@10 | +0.0016 | [-0.0034, +0.0066] | no measurable change |

Both arms reproduce their committed point estimates exactly, which is
the integrity check on all of this.

Two things follow, and they pull in opposite directions — which is the
point of doing it properly.

**The campaign's polarity lesson is now established rather than
asserted.** The hand tables really do cost conversational retrieval at
shallow depths; the intervals exclude zero at both depths the campaign
argues about. That result has been carried since the 5.1 lane on point
estimates alone, and it survives.

**And it could not have been shown any other way.** The off arm's own
95% interval at recall@5 is about two points wide. The effect is under
one point. Had this audit stopped at the single-arm interval it
printed first, it would have declared a real, reproducible finding to
be noise — the exact error, in the exact opposite direction, that the
rest of this document is about. Both failure modes are live on this
instrument and each needs its own test.

**A new fact, not previously recorded:** the cost vanishes by depth
ten. Whatever the tables do to conversational ranking, they do it to
the top of the list rather than to the pool.

## The one conclusion that should be re-opened

W1b's record concludes that "the wall this unit reaches is the
objective, not the corpus." The geometry evidence for the *symptom* is
sound. The attribution to the objective is not established, for a
reason that has nothing to do with sample size: **all seven tuning
reads varied emission thresholds over one frozen set of vectors.** The
training side was sealed at a single point in its own hyperparameter
space — dynamic window 5, dim 100, three epochs, subsample 1e-4
(`bench/w/w1_train.py`).

Window width is precisely the knob that trades the geometry the unit
found for the geometry it wanted. Small windows favour paradigmatic
similarity — words that substitute for one another, which is what the
morphological pairs are and what the unit found tightly held. Large
windows favour syntagmatic relatedness — words that co-occur, which is
what `toggle`/`flag` and `flag`/`boolean` are and what the unit found
missing. A dynamic window of 5 averages about three tokens of context:
the far substitutability end of that trade.

The unit's conclusion may well be right. But it was reached without
moving the one training hyperparameter most likely to move the result,
so "the objective is the wall" is currently a hypothesis with a
suggestive geometry probe behind it, not a priced finding. A single
retrain at a wide window, graded on an expanded geometry census rather
than on twenty questions, would settle it for a fraction of what the
W2 rung costs.

## What changed in the tree

Additive, all of it. No published artifact was edited and no bar moved.

- `bench/interval.py` — Wilson intervals, exact paired McNemar, CI on
  a mean, and a power calculator, in one importable place. The Wilson
  and two-proportion arithmetic was lifted from `bench/embed_census.py`
  (which now delegates to it, pinned at its original `z=1.96` so the
  committed census artifacts stay bit-reproducible — verified across
  1,161 cells).
- `bench/retrieval/run.py` — retains per-question outcomes; emits
  `recall_at_*_ci95` and `per_question` in JSON; prints a reading
  section with intervals, the resolution floor, and paired McNemar
  between probes. The results table itself is byte-unchanged. A new
  `--compare PRIOR.json` does the paired reading a gate read actually
  needs — one table against another, across invocations. Artifacts
  written before `per_question` existed are reported as unpairable
  rather than silently compared unpaired.
- `bench/longmemeval/run.py` — emits `macro_ci95`, computed as the
  standard error of a mean rather than a Wilson interval, because
  macro recall averages per-question fractions and is not a
  proportion. A new `--compare PRIOR.json` reads a run paired against a
  prior `--per-question` sidecar and reports the mean DIFFERENCE with
  its own interval, which is the only correct way to compare two arms
  here. Both directions of error are now avoidable on this instrument:
  the single-arm interval stops a number being over-read, and the
  paired interval stops a real difference being dismissed as spread.

## What this does not do

It does not change a verdict, move a bar, or reopen a park. It does
not claim the dev instrument is worthless — it resolves a forty-five
point effect comfortably, which is how the requery finding was found.
It claims something narrower and more useful: **the instrument cannot
resolve the five-point differences the campaign's most recent bars
were written around**, and the fix for that is a larger instrument,
which is declared separately rather than slipped in here.
