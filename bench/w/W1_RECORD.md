# W1 — the gate read and the park, 2026-08-16

The unit `bench/w/W1_DECLARATION.md` declared ran whole: corpus
pinned, trainer implemented, model trained twice, table emitted,
tuning frontier mapped on published reads, gate read taken on the
committed artifact. The verdict grid's third row fires: **W1-PARK —
no arm clears G1.** This record publishes the miss and the diagnosis
it bought; nothing below renegotiates a bar.

## The gate grid

Committed artifact: `bench/w/artifacts/w1_table_2026-08-16.py`
(receipt beside it carries the trainer's run block and the emission
finals: 1,500 entries at cosine floor 0.65, three neighbors, the
rule-covered pairs dropped, the five hundred most frequent heads
skipped — the best profile the published tuning frontier found).

Dev instrument, asked probe, unpadded, prefilter off:

- expansion off: recall@1 35%, recall@5 60%
  (`bench/w/results/gate-dev-off-2026-08-16.json`) — equal to the
  committed baseline;
- static hand tables: recall@1 55%, recall@5 90%
  (`bench/w/results/gate-dev-static-2026-08-16.json`) — equal to the
  committed record; the paired integrity reads both hold exactly;
- **W1-full: recall@1 45%, recall@5 60%** — G1 MISS
  (`bench/w/results/gate-dev-full-a-2026-08-16.json`; the second
  invocation's results block is identical, the declaration's
  run-determinism bar);
- **W1-syn: recall@1 50%, recall@5 70%** — G1 MISS
  (`bench/w/results/gate-dev-syn-a-2026-08-16.json`; second
  invocation identical).

LongMemEval, lexical, full 500:

- expansion off: macro recall@1 0.5246, macro recall@5 0.8935
  (`bench/w/results/gate-lme-off-2026-08-16.json`) — equal to the
  committed baseline, the integrity read;
- static hand tables: macro recall@1 0.5014, macro recall@5 0.8823
  (`bench/w/results/gate-lme-static-2026-08-16.json`);
- **W1-full and W1-syn, identical to four decimals: macro recall@1
  0.4580, macro recall@5 0.8574** — G2 MISS against both floors
  (`bench/w/results/gate-lme-full-2026-08-16.json`,
  `bench/w/results/gate-lme-syn-2026-08-16.json`).

G3 — the determinism bar — PASSES in every declared form: two full
independent trains from the pinned register reproduced the vectors,
vocabulary, and emitted table byte for byte (the committed receipt's
sha block names the vectors blob; the committed artifact regenerates
identically from the second train's output), and the
reduced-register CI check (`tests/test_w1_determinism.py`) holds the
mechanism on every push.

Budgets: every one held. The trainer read its full token cap and
finished in under a sixth of the wall budget per run (the receipt's
run block carries the counts and seconds).

## What the miss says, exactly

1. **The mechanism is real but the corpus was the wrong register.**
   Learned geometry added ten to fifteen rank-one points over the
   expansion-off baseline on the dev instrument — subword morphology
   the hand tables had to write by hand, plus genuine topical
   neighbors. It could not touch the rank-five gap: the six
   questions the hand tables rescue into the top five are cross-form
   synonym bridges ("toggle" to "flag", "undo" to "rollback"), and
   encyclopedia-core prose does not carry that alternation — several
   of those words never reached the vocabulary floor at all. The
   declaration's first confound, verbatim.
2. **Breadth without curation inverts the leg on conversational
   stores.** The learned arms cost LongMemEval more than the hand
   tables do at both depths, and the two arms score identically to
   four decimals — the hand precision tables fire so rarely there
   that the whole difference is the learned synonym mass. The
   polarity lesson generalizes: the leg's value is precision, and a
   corpus-frequency table is the opposite shape.
3. **The engine moved under the static tables since their 5.1.1
   record.** Their gate-read LongMemEval cost is smaller than the
   cost their 2026-08-10 artifacts recorded
   (`bench/longmemeval/results/rebaseline-lane-2026-08-10.json`);
   the off baseline reproduced exactly, so the shift is the lane's
   own evolution across the intervening releases, not drift. G2's
   epsilons were set against the committed 5.1.1 costs; the learned
   arms miss against today's static reading too, so no verdict turns
   on this — recorded because a future epsilon should be set against
   a same-engine paired read.

## What the park feeds

The successor's corpus is already pinned in the register under the
2026-08-16 owner consent: the remaining English Wikipedia parts and
the Stack Overflow posts archive — casual technical question text,
the register this diagnosis says was missing. A successor unit
declares its own bars before reading a byte of either; this record
is its opening argument. The trainer, harness, and determinism
chain carry over unchanged.

## Owner doors, untouched

No ship sentence follows a park. Instrument #2 stays sealed. The
criterion stands unclaimed, and the honest interim sentence stands
with it: the bars are unreached by our own means, published as such.
