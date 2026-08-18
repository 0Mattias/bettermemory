# W1b — the gate read and the park, 2026-08-18

The unit `bench/w/W1B_DECLARATION.md` declared ran whole: the register
slice built, the trainer revised and sealed, the model trained twice,
the table emitted, the tuning frontier mapped across seven published
reads, the gate read taken on the committed artifact. The verdict
grid's third row fires: **W1b-PARK — no arm clears G1.** This record
publishes the miss, the two bars that did hold, and the diagnosis the
unit bought; nothing below renegotiates a bar.

The sha chain: declaration 1ac8a83, a lint repair to that declaration's
prose disclosed in full below (4d35291), implementation f1a03a0,
artifact and tuning trail 997756a, then the gate invocation set at that
commit. Every gate artifact carries `tree_dirty: false` at 997756a.

## The gate grid

Committed artifact: `bench/w/artifacts/w1b_table_2026-08-18.py`, 3,200
entries, emitted at the profile the published trail found best — cosine
floor 0.50, three neighbors per term, mutual rank 64, the two thousand
five hundred most frequent heads skipped, two components removed,
rule-covered pairs dropped.

Dev instrument, asked probe, unpadded, prefilter off:

- expansion off: recall@1 35%, recall@5 60%
  (`bench/w/results/gate-w1b-dev-off-2026-08-18.json`) — equal to the
  committed baseline, the integrity read;
- static hand tables: recall@1 55%, recall@5 90%
  (`bench/w/results/gate-w1b-dev-static-2026-08-18.json`) — equal to
  the committed record, the second integrity read;
- **W1b-full: recall@1 50%, recall@5 65%** — G1 MISS
  (`bench/w/results/gate-w1b-dev-full-a-2026-08-18.json`; the second
  invocation's results block is identical,
  `bench/w/results/gate-w1b-dev-full-b-2026-08-18.json`);
- **W1b-syn: recall@1 55%, recall@5 70%** — G1 MISS
  (`bench/w/results/gate-w1b-dev-syn-a-2026-08-18.json`; second
  invocation identical,
  `bench/w/results/gate-w1b-dev-syn-b-2026-08-18.json`).

LongMemEval, full 500:

- expansion off: macro recall@1 0.5339, macro recall@5 0.9062
  (`bench/w/results/gate-w1b-lme-off-2026-08-18.json`) — equal to the
  committed incumbent to four decimals, the third integrity read;
- static hand tables: macro recall@1 0.5071, macro recall@5 0.8974
  (`bench/w/results/gate-w1b-lme-static-2026-08-18.json`);
- **W1b-full and W1b-syn, identical to four decimals: macro recall@1
  0.5041, macro recall@5 0.8960** — G2 MISS against both floors
  (`bench/w/results/gate-w1b-lme-full-2026-08-18.json`,
  `bench/w/results/gate-w1b-lme-syn-2026-08-18.json`).

G2's floors were 0.8962 at depth five and 0.5139 at depth one. The
arms land two ten-thousandths under the first and about one point
under the second. The bar asked for more than proximity: it asked the
learned table to shrink the hand tables' own damage at both depths,
and against the same-run off arm the hand tables give up 0.88 and 2.68
points while the learned table gives up 1.02 and 2.98. It does not
shrink that damage; it slightly deepens it.

G3 — the determinism bar — PASSES in every declared form. Two full
independent retrains from the pinned register reproduced the vectors,
the context matrix, the vocabulary, and the derived token cache byte
for byte, and the committed table regenerates byte-identically from
the second train's output; all four equalities are recorded in the
artifact's receipt beside it. The CI half is committed and green:
`tests/test_w1b_determinism.py` retrains the SE-derived reduced
register on every push, asserts byte-identity across two trains, and
asserts the same bytes at two segment sizes — the invariance clause
the segmented revision was declared under.

Budgets: every one held. The trainer read its full declared token cap
and finished each run well inside the twenty-four-hour ceiling, and
the segmented revision kept resident memory far under the machine's.
The integrity reads hold in all three forms above, and requery beats
asked at rank one on every dev arm.

## What the miss says, exactly

1. **The register fix worked, and it was not enough.** W1 parked with
   the diagnosis that its corpus was the wrong register; W1b changed
   the register and the numbers moved for it. The same two arms read
   45%/60% and 50%/70% at W1's gate
   (`bench/w/results/gate-dev-full-a-2026-08-16.json`,
   `bench/w/results/gate-dev-syn-a-2026-08-16.json`) and read 50%/65%
   and 55%/70% at this one. Rank one gained five points on both arms,
   and the words W1's park named as missing — `toggle`, `rollback`,
   `undo`, `revert` — all cleared the vocabulary floor this time. The
   learned table now ties curated precision at rank one. It stays
   twenty points behind it at rank five, which is where the hand
   tables earn their keep, and G1 wants both.

2. **The geometry is strongest where hand rules already cover, and
   blind where the hand table earns its keep.** The unit's committed
   spot-list (`bench/w/results/w1b-geometry-2026-08-18.json`, the
   training-internal class §6 licenses without limit) puts every one
   of the four morphological pairs inside the emission window at
   cosine 0.70 and above — `config`/`configuration`,
   `deploy`/`deployment`, `auth`/`authentication`, `cache`/`caching` —
   and none of the six cross-form pairs inside it. `toggle`/`flag`
   sits at cosine 0.284; `flag`/`boolean` sits at −0.019, each word
   ranking the other past position eighty thousand. The near-synonym
   family splits, one pair in and four out. That contrast is the
   finding: the morphological family is exactly what `morph_variants`
   already generates by rule, so the emission drops it by design,
   while the cross-form family is exactly what `SYNONYM_GROUPS` was
   hand-written to carry.

3. **So the wall this unit reaches is the objective, not the corpus.**
   Skip-gram with negative sampling learns which words substitute for
   each other in context. Morphological variants substitute, and share
   character n-grams besides, so the geometry holds them tightly.
   `toggle` and `flag` do not substitute — they co-occur, one acting
   on the other — and a window-based objective pushes such a pair
   apart rather than together. No emission threshold can emit a pair
   the geometry does not hold, which is why the trail plateaued: the
   head-frequency floor was its strongest lever and the table sits at
   that lever's peak, with a deeper floor regressing rank one
   (`bench/w/results/w1b-dev-read7-syn-2026-08-18.json`) and a fourth
   removed component tying rather than gaining
   (`bench/w/results/w1b-dev-read6-syn-2026-08-18.json`).

4. **The conversational polarity replicates at four times the corpus.**
   The two arms score identically to four decimals on LongMemEval, as
   they did at W1's gate — the hand precision tables fire so rarely on
   that instrument that the whole difference is the learned synonym
   mass. Quadrupling the corpus and doubling the vocabulary cap did
   not change that structure. The polarity lesson the campaign has
   carried since the 5.1 lane survives its largest test.

## The prefilter cells, and one that could not be reached as written

Declared to be read at gate time and to gate nothing, and they gate
nothing. The forced-threshold cell reads recall@1 50%, recall@5 65%
with gold nomination 0.90
(`bench/w/results/gate-w1b-dev-syn-prefilter-180-2026-08-18.json`).
The above-threshold cell cannot be reached on a 180-document corpus at
the shipped index threshold — the runner says so and writes nothing —
so it was reached the way the instrument's own prior artifact reaches
it, by padding: recall@1 45%, recall@5 70%, gold nomination 0.95
(`bench/w/results/gate-w1b-dev-syn-prefilter-padded-2026-08-18.json`).
The unpadded form of that cell is unreachable rather than unrun, and
is recorded here as such rather than quietly dropped.

## A declaration repair, disclosed

The declaration committed at 1ac8a83 was red on the repository's
own number-claims gate from the moment it was committed, and nobody
knew, because that gate enumerates git-tracked surfaces and the file
was untracked when the gate last ran before its commit. Once tracked,
three chunks failed: a citation pointing at an `artifacts/` receipt,
which is not one of the result classes the checker admits; a cue word
that made a derived bar threshold read as a claim about something
already observed; and a bare machine-memory figure with nothing behind
it. Commit 4d35291 repaired the prose, before the artifact was
committed and before any instrument ran against it.

That repair changed no bar and no budget. G1 stands at 60 and 90, G2
at 0.8962 and 0.5139, the token and wall-clock budgets, both arms, the
read protocol and the verdict grid all stand exactly as sealed at
1ac8a83; the diff removes a dangling citation with its illustrative
projection, one cue word, and one uncited figure. A reader who wants
to check that claim can diff the two commits. It is disclosed here
rather than smoothed over, because a declaration edited after its runs
is exactly the thing this repository's mold exists to make visible,
and the honest defense is the diff rather than the assurance.

The generalisable half is an operations lesson the ledger should
carry: a new markdown surface is invisible to the claims gates until
it is staged, so a document can commit red and stay red until some
later run trips over it. Stage first, then gate. It is the same trap
the walk-fallback guard sets for untracked modules, in a second
costume.

## What the park feeds

Nothing ships and no ship sentence is put. The engine is unchanged;
the table remains a bench artifact riding no default.

The successor argument this record hands forward is stronger than the
one W1 handed it, because the two units together separate a data
question from a mechanism question and answer both. W1 asked whether
the corpus was wrong and the answer was yes. W1b fixed the corpus,
kept every other declared thing fixed, and the remaining gap did not
close — so the gap is not the corpus, and among from-scratch
mechanisms it is now located in the training objective itself.
Substitutability is learnable from running text; the cross-form
bridges the rescue leg needs are not, because they are not
substitution relations at all.

W2 — the contrastive dual-encoder the program ladder already holds as
the conditional rung — is the one declared route whose objective
targets that gap directly, since a contrastive objective learns from
labeled pairs what a window objective cannot learn from context. This
record does not take that entry decision. The ladder owns it, on this
read, and W3-P2's census stands as the honest caution alongside: a
labeled-pair corpus carried the preference class poorly, and a W2
declaration would owe its own accounting of whether the technical
synonym class fares differently before it spends a training run.

## Owner doors

- **Lane T criterion v1** stands where the last record left it.
- Any wiring of any bench table into the package remains a separate
  unit with its own plain sentence; nothing from this unit is a
  candidate.
- The W2 entry decision is the program ladder's, conditioned on this
  read and not taken here.

## What is not claimed

No criterion claim; the interim sentence stands, and stands unchanged
by a unit that missed its bar: the bars are unreached by our own
means, published as such. No ship, opt-in or default. No claim that a
different emission profile reaches a different verdict — the frontier
is published, including the reads that went backwards. No claim that
the objective is refuted for every corpus or every scale; what is
priced is this objective, at this budget, on these two instruments.
No comparative claim against any other memory system.
