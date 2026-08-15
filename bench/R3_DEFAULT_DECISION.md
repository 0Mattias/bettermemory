# R3 — the default question, decided: no flip remains, and the criterion resolves unmeetable-as-written, 2026-08-14

Stage three of the reentry ladder `bench/DOOR_C_DECISION_BRIEF.md`,
reserved there in these words: *"R3 — the default question. Whether
any install shape flips a default is its own preregistration, decided
on R1+R2 evidence, not bundled here. Until R3 passes, the success
criterion as written ('on the DEFAULT engine') remains unmet and the
record keeps saying so."* R1 passed (`bench/R1_REENTRY_DECLARATION.md`)
and R2 passed (`bench/R2_REENTRY_DECLARATION.md`); this document is
stage three, rendered under the owner's delegation of 2026-08-14
("go for r3, you have the wheel") — the same delegation shape door C
itself was decided under, and recorded as delegated, like door C.

It is a decision document rather than a preregistration, and the first
finding below is why: the design-pass found nothing left that a
preregistration could license. No cell would run, no code would
change, no default would move. What remains for R3 to do is what the
ladder actually reserved for it: say, honestly, what the success
criterion's words now mean.

## Finding 1 — the only doctrine-legal default already carries the arm

There are exactly two install shapes, and each has one default
retrieval behavior:

- **The default install** (no extra named): deterministic lexical,
  the two-leg hybrid fusion. Door C's condition 1 — opt-in extra,
  core inviolate — makes this permanent. This is not a shape awaiting
  a flip; it is a shape the doctrine forbids flipping, by the same
  decision that admitted the weights at all.
- **The extra install** (`bettermemory[embeddings]`, configuration
  untouched): the three-leg hybrid. The package default is
  `search_mode = "hybrid"`, and the search handler resolves the model
  for hybrid whenever an embeddings extra imports
  (`src/bettermemory/handlers/search.py`, and
  `src/bettermemory/semantic_setup.py` — a citation that does not
  resolve since 6.0.0 removed the module with the lane) — the
  pre-strip shape
  restored by R1's implementation, whose lineage is the 3.29.0
  reversal itself (`ba7e857`, "installing an embeddings extra now
  enables semantic search": the extra used to be inert without an
  unrelated flag, and flipping that was a measured decision the
  pre-4.0 record already carries).

So the flip R3 was reserved to preregister has no subject. The only
default the doctrine permits the arm to occupy is the extra install's
default, and the arm already occupies it — shipped in 5.5.0 under
R1's declaration as part of "the pre-4.0 shape, unchanged", and then
measured by R2 as exactly the shape it is: both bench runners'
`semantic` arms invoke `mode="hybrid"` with the model present, which
is what an extra-installing user gets by naming the extra and
touching nothing else.

One alternative flip exists in configuration space and is declined on
the ladder's own terms: making pure `mode="semantic"` the extra
install's default. R3 is decided on R1+R2 evidence, and that evidence
contains no pure-semantic cell — on both instruments the measured
"semantic" arm is the hybrid with the model as an equal third leg.
A default flip to a never-measured mode is not a decision this ladder
can license, and nothing in the R1/R2 record suggests it would be an
improvement over the fusion. The write path (`semantic_dedup`,
default false) is outside door C's retrieval-arm admission and
outside this decision.

## Finding 2 — the criterion, read with both halves kept

The campaign's success criterion, as written and standing since
2026-08-09: bench/retrieval recall@1 as-asked ≥ 60 AND LongMemEval
macro recall@5 ≥ 91.6, **both on the DEFAULT engine**.

**On the default install's engine** the criterion is unmet and now
unmeetable:

- dev instrument, as-asked recall@1: 35% against the arm's 60%
  (`bench/retrieval/results/r1-unpadded-2026-08-13.json`);
- LongMemEval, macro recall@5: 89.3% against the 91.6% reference
  (`bench/longmemeval/results/r2-both-arms-2026-08-14.json`,
  `bench/longmemeval/results/claude-mem-full500.json`).

Unmeetable is a
measured word, not a resigned one: every from-scratch mechanism the
WaC doctrine permits was priced to a park or kill — the door C
brief's evidence section is the ledger — and the terminal finding was
structural (the store cannot teach what the store does not contain).
The one mechanism class that reaches both bars is barred from this
install shape by door C's condition 1. The two constraints together
close the question: no path to the bars exists on the default
install, and none is permitted to.

**On the extra install's default engine** both bars were reached,
declared-first, determinism-checked, and reproduced: recall@1
as-asked 60% at the bar exactly (R1,
`bench/retrieval/results/r1-unpadded-2026-08-13.json`), and macro@5
0.9185 against the 0.916 reference (R2,
`bench/longmemeval/results/r2-both-arms-2026-08-14.json`,
`bench/longmemeval/results/claude-mem-full500.json`).

The temptation this document exists to refuse: reading "the DEFAULT
engine" as "the default engine of whichever install the user chose",
under which the criterion would be met and the campaign victorious.
That is a post-hoc reinterpretation in the direction that flatters
the result — the selectable-headline move this repository's whole
discipline exists to prevent. The criterion was written when the
default install and the default engine were one thing, by a plan that
intended to close the gap with code in the core. Its words bind to
the default install. The bar is not claimed.

## The verdict

**R3 resolves the criterion UNMEETABLE-AS-WRITTEN, and the
retrieval-recall campaign closes at the opt-in lane.**

- No default changes and no code ships. The ladder completes with R3
  as a decision: R1 reproduced the dated record, R2 reproduced the
  held-out record, R3 finds no flip left to license and says so.
- The close is the charter's own clean-close rule (owner ROI
  directive, 2026-08-11), applied to the case the ladder actually
  produced: not a failed gate but a criterion whose referent the
  amended doctrine removed. The campaign does not end defeated; it
  ends measured.
- The record keeps all three standing sentences, none traded away:
  1. Default against default, bettermemory retrieves behind the
     reference stack at macro recall@5 — 89.3% vs 91.6%
     (`bench/longmemeval/results/r2-both-arms-2026-08-14.json`,
     `bench/longmemeval/results/claude-mem-full500.json`) — and the
     deficit concentrates in multi-session.
  2. Capability-matched — their default embedding stack against our
     engine with the extra named — the parity headline is current:
     91.8% vs 91.6% at recall@5, ahead at recall@1, behind at
     recall@10, per the dated table and its R2 reproduction.
  3. The success criterion as written was never met and is now
     resolved unmeetable-as-written; nothing in this repository
     claims it.
- What would change this resolution is charter surgery — a v2
  criterion written for the two-install reality (per-install-shape
  terms, or a default-install criterion that names what lexical
  alone must hold). That is the owner's voice, not a delegated
  verdict; this document leaves the door explicitly open and takes
  nothing through it.

## What stands after R3

Every park stands. The sealed instruments stay sealed and instrument
#2 stays reserved for P2a. The six-clause provenance contract governs
the arm exactly as door C wrote it. The write-path question is not
reopened. Lane 2 (a third blind instrument over MSC) remains open as
doctrine-neutral validation, now optional rather than load-bearing.
The competitive claim this project carries forward lives where the
LongMemEval record already put it: retrieval recall on conversational
corpora is near-saturated for competent designs — the differentiating
axis is correctness, verification, and receipts, where the reference
stack scores a structural N/A. That axis was never on trial in this
campaign, and it is the war the project now returns to.
