# Door C — the pretrained-weights doctrine, briefed both ways and called, 2026-08-13

## Standing

Door C is the campaign's name for the one dense question every census
left open: whether third-party pretrained weights — banned by the
4.0.0 purist strip, and banned still under the amended WaC doctrine's
"derived, not borrowed" clause — may re-enter the product. The record
has said the same thing at every park: the door "is the owner's, not
a measurement's" (`bench/retrieval/README.md`, MSC scale census
record). The forward plan bound it tighter still: owner decision, no
default, brief on commission only. Its decision-value clause, written
2026-08-12, fired on 2026-08-13 when the requery census parked:
*requery parks → door C becomes forced rather than optional.*

The commission arrived the same day, and it carries a delegation. The
owner's words, verbatim: **"continue, make the best choice for door
C."** So this document is both halves of Lane 3 at once — the
two-sided brief the plan promised, and the call itself, rendered
under that delegation and recorded as delegated.

Two standing obligations bind what this document may say, and it
keeps both. The register/df census declaration licenses this brief to
cite its gap-absent shares "as the ceiling decomposition they are"
and forbids presenting them as a mechanism forecast in either
direction (`bench/REGISTER_DF_CENSUS_DECLARATION.md`, §4). And
nothing below relitigates any parked lane: every park stands exactly
as its own pre-committed criterion left it.

## The question, precisely

The WaC doctrine, as the owner amended it on 2026-08-11 and as the
record quotes it (`bench/retrieval/README.md`, P1e section): *"I
never said 'no neural weights', I said no sloppy bullshit. You can
add neural weights as long as we built the model from scratch."*
From-scratch training became explicitly legal, neural included;
pretrained third-party weights stayed banned. Door C asks whether
that last clause survives now that the campaign has priced everything
the clause permits.

Door C is not door D. Door D — store-trained dense retrieval, the
from-scratch implementation of the same idea — closed negatively on
2026-08-12, family-wide, in both of its forms (term emission and
document scoring) and in every register-by-scale quadrant measured.
Door C is what remains: the same mechanism class with the corpus
constraint moved. The corpus constraint is exactly what the doctrine
forbids moving.

## Evidence, side one: the from-scratch program ran to completion, and its terminal finding is structural

Nothing here was abandoned; everything WaC permits was measured to a
criterion committed before the numbers existed, and every verdict
below is a park or kill that stands untouched.

- **Term emission, raw counts**: PPMI over the store measured 0.1253
  best-cell precision — under half the committed tables' incumbent —
  killed at its Gate 0
  (`bench/retrieval/results/ppmi-census-2026-08-11.json`).
- **Term emission, factorized**: the from-scratch GloVe lane, two
  declared censuses; the veto mechanism genuinely improves precision
  and the family still parks at-width by its own named-first primary
  cell (`bench/retrieval/results/embed-census-2026-08-11.json`,
  `bench/retrieval/results/embed-census2-2026-08-11.json`).
- **Document scoring, technical register**: zero reach family-wide —
  no cell places a single far/absent gold inside rank ten, medians in
  chance territory
  (`bench/retrieval/results/dense-scoring-census-2026-08-12.json`).
- **Document scoring, conversational register and scale**: measured
  park on the bottom rung; the E1 anchor puts the primary cell's gold
  session first less often than one-in-five chance among five
  candidate sessions while the shipped lexical engine clears it about
  two times in three, and doubling the in-register training mass
  halves the reach share
  (`bench/retrieval/results/msc-scale-census-2026-08-12.json`).
- **Learned lexical rerank**: the P2a feature census found zero
  qualifying features — near-missed golds are lexically dominated, on
  both corpora, in the same direction
  (`bench/retrieval/results/rerank-feature-census-2026-08-12.json`).
- **Requery from store-internal sources**: the feedback family parked
  at its bottom rung — best cell 0.35 equals the pass-1 baseline,
  far/absent recovery 0.00 in every cell, and the oracle-min bound
  0.45 sits under the 0.50 license bar, closing the family by its own
  artifact (`bench/retrieval/results/requery-census-2026-08-13.json`).

The terminal finding is structural, and two of its measurements run
opposite to the obvious rescue predictions: in the emission census,
adding more text made precision worse (topicality, not coverage, is
binding), and training the model harder made it worse again
(`bench/retrieval/results/embed-sensitivity-2026-08-11.json`). A
personal store is tens of thousands of tokens; the store is the
domain; every corpus large enough to train on leaves the domain, and
leaving the domain costs more than the size buys. That is an
information wall, not an optimization failure — the one wall no
committed derivation from auditable local inputs can cross, because
the inputs themselves do not contain the signal.

## Evidence, side two: the dated pretrained record

The pre-4.0 engine carried an embedding arm, and its figures are a
dated record this directory has kept precisely for this decision.

- On the committed dev instrument, semantic/asked measured 60%
  recall@1 against lexical/asked 35%, with the control probe tracking
  asked in both arms — the lift is vocabulary, not phrasing
  (`bench/retrieval/results/unpadded-2026-08-08.json`).
- Padded above the index threshold the margin widens: semantic holds
  60% while lexical falls to 25%
  (`bench/retrieval/results/padded600-2026-08-08.json`).
- The margin is stable across two corpus generations and nine engine
  releases — the same instrument re-measured it unchanged the day
  before the strip.
- The reference stack on the held-out conversational instrument: our
  default lexical engine reads 0.8935 at macro recall@5
  (`bench/longmemeval/results/baseline-both-arms-2026-08-08.json`)
  against the 0.916 an embedding stack holds on the same instrument
  (`bench/longmemeval/results/claude-mem-full500.json`) — and the
  multi-session slice, 0.867, is where the deficit concentrates.

What the dated record does **not** establish, stated with the same
care:

- The 60% sits exactly at the campaign's bar, on an instrument of
  twenty questions where one question is five points. Reproduction is
  a real question, not a formality.
- The semantic arm never ran through the production prefilter path —
  the one caveat the instrument was built to test is unscored for the
  arm it was aimed at. Above the index threshold, nomination runs on
  the caller's words, and the arm's lift there is unmeasured.
- No LongMemEval run has ever combined our engine with an embedding
  arm. The 0.916 belongs to a different system's whole stack, not to
  our engine plus weights.

## The ceiling decomposition, cited as licensed

The register/df census
(`bench/retrieval/results/register-df-census-2026-08-12.json`)
decomposed every as-asked probe's vocabulary against its store.
Median probe-gold overlap: 0.28 on the technical dev instrument, 0.77
on LongMemEval, 0.86 on MSC — the wall is register-specific to the
dev instrument, whose register is this product's own. And wherever
the gap exists, it is overwhelmingly in-store: pooled gap-elsewhere
versus gap-absent runs 0.60 against 0.10 on dev, 0.24 against 0.03 on
LongMemEval, 0.19 against 0.02 on MSC.

This is a ceiling decomposition, and it cuts both ways; per the
declaration it forecasts nothing in either direction. Read for KEEP:
the slice only external knowledge could ever bridge at token level —
gap-absent — is thin everywhere, so external weights' *exclusive*
territory is small. Read for OPEN: the bulk of the wall is in-store
linkage, and every store-internal mechanism measured against that
linkage is parked or dead, while the dated record above is direct
measurement — not forecast — that pretrained geometry bridges it.
Both readings are in this brief because both are true.

## Side KEEP, priced

What keeping the ban preserves:

- **The absolute provenance story.** Every parameter in the product
  derives from committed code and auditable local inputs; nothing in
  the tree or the runtime has an origin the repository cannot show.
  "The code is the model" stays literally true everywhere.
- **Zero dependency surface, zero fetch policy.** Pure-Python core,
  no model artifacts, no runtime for them, no download step to
  govern, nothing to pin.
- **A defensible position without further work.** The conversational
  baseline is strong (the 0.8935 above), the register census shows
  the conversational corpora mostly share their gold's vocabulary
  already, and the campaign closes cleanly at the opt-in lane — the
  charter's own closing rule for the case where P2a misses.
- **The caller mitigation.** This product's caller is a language
  model that already carries external vocabulary knowledge at query
  time; the search contract coaches requery, and the census's human
  requery decomposition shows vocabulary substitution is exactly what
  works. The external knowledge exists in the loop — upstream of the
  engine.

What keeping the ban pays:

- **The success criterion dies.** Recall@1 as-asked ≥60 has no
  measured live mechanism inside the doctrine — that is not a gap in
  effort but the exhaustively measured finding above. Keeping the ban
  converts the criterion from unmet to unmeetable.
- **The first-attempt experience stays at the wall.** The caller
  mitigation is real but unpriced: no instrument prices
  caller-requery recall end-to-end, the one attempt to mechanize
  requery parked at its bottom rung, and a first attempt that fails
  roughly two times in three on the product's own register is the
  experience the campaign exists to fix.
- **The macro deficit stands by choice.** The reference stack's lead
  on the held-out instrument is an embedding-stack lead. Under KEEP
  the record must say: the gap is known, the class that closes it is
  known, and the project declines it on doctrine.

## Side OPEN, priced

What opening buys:

- **The only measured path to the bar.** The pre-4.0 arm is the sole
  mechanism in the whole record that reaches the as-asked criterion,
  and the reference holder of the macro criterion is the same class.
- **The war rejoined on the losing front.** Multi-session and
  paraphrase — the measured deficits — are exactly what the class
  addresses.

What opening pays:

- **The absolute becomes a boundary.** "No third-party weights
  anywhere" dies as a sentence. What replaces it must be enforced,
  not assumed: a provenance contract on an opt-in surface, with the
  core untouched.
- **Dependency and fetch surface return**, extra-scoped: a runtime, a
  model artifact, a pinned revision, a download step — the first
  network-touching artifact policy since the strip.
- **Distinctiveness narrows.** The category-of-one claim keeps its
  load-bearing half — local, owned, deterministic core, self-auditing,
  receipts — and loses its purist absolute. The trust thesis (drift,
  claims anchoring, curation telemetry) was never on trial and does
  not move.
- **Reproduction risk is real.** The dated record could fail to
  reproduce through the modern engine and the prefilter path; opening
  the door licenses the measurement, not the outcome.

## The call

**Door C opens — conditionally.** Rendered 2026-08-13 under the
owner's delegated commission, recorded above.

The reasoning, compressed to its load-bearing steps:

1. The from-scratch program was executed to completion under
   declared-first discipline and its terminal finding is structural:
   the store cannot teach what the store does not contain. Waiting
   longer produces no new from-scratch option; the record itself says
   the class clears the bar "the moment the corpus constraint moves."
2. The campaign's success criterion is unreachable inside the ban.
   The charter's mandate — this project gets built and is the best —
   and the ban now contradict each other, and one of them has to
   yield. The mandate is the older and the louder instruction.
3. The owner's canonical principle is *"no sloppy bullshit"* — their
   own correction of the record when it over-read the strip as
   anti-neural. From-scratch was an implementation of that principle,
   and it has been priced at zero. The principle itself survives
   intact in the conditions below; what is dropped is one
   implementation of it that the evidence retired.
4. Everything KEEP protects that is load-bearing — zero-dependency
   deterministic core, offline operation, receipts, the trust thesis
   — is preserved under a conditional opening, because the default
   install and default engine do not change. What KEEP alone protects
   is the absolutist sentence, and the owner has already said the
   sentence was never the principle.
5. The costs are real, so they are priced into the shape of the
   opening rather than argued away: opt-in only, provenance
   contract, declared-first reentry, default untouched pending its
   own preregistration.

### The doctrine amendment

WaC clause 2 amends from *derived, not borrowed* to **derived, or
borrowed under contract**. Third-party pretrained weights become
admissible in the retrieval arm only, under all of the following,
each a hard condition:

1. **Opt-in extra, core inviolate.** The default install stays
   pure-Python, dependency-free, deterministic lexical. The arm
   ships as an extra a user must name.
2. **Pinned provenance.** Named model, pinned revision, recorded
   artifact digest — in config, and stamped into every bench
   artifact's provenance block like every number this repo publishes.
3. **License-clean.** The model's license is verified compatible and
   recorded alongside the pin.
4. **Offline after fetch.** Network is touched once, by an explicit
   user-invoked fetch step; derivation and serving never touch it.
5. **Deterministic on the pinned artifact.** Same input, same
   vectors, byte-stable caches — the pre-4.0 lane's own cache design,
   kept.
6. **Receipts unchanged.** Every claim made with the arm ships its
   artifact; misses publish as retractions; the sealed instruments
   stay sealed and instrument #2 stays reserved for P2a.

### The reentry ladder

Opening the door licenses an experiment, not a claim. Three stages,
strictly ordered, each declared-first in this directory's own
tradition:

- **R1 — reproduce the dated record.** Resurrect the embedding lane
  from its pre-strip ancestry (removed whole in `1bb73bc`; providers
  and defaults recorded there), modernized to the current engine, and
  re-measure the committed dev instrument — including the cells the
  dated record never scored: the arm through the production prefilter
  path, and padded. Bars fixed before any run, against the dated
  record's own band.
- **R2 — the held-out read.** LongMemEval with the arm on, against
  the macro criterion. This is the first measurement anywhere of our
  engine plus weights, and the C1 polarity lesson rides along:
  conversational stores may want the arm off, and the knob precedent
  applies.
- **R3 — the default question.** Whether any install shape flips a
  default is its own preregistration, decided on R1+R2 evidence, not
  bundled here. Until R3 passes, the success criterion as written
  ("on the DEFAULT engine") remains unmet and the record keeps saying
  so — opening the door creates the measured path to the bar, and
  nothing below R3 may claim the bar.

### What stays closed

Every park stands: store-trained dense in both forms, PPMI, RM3 as a
peer leg, feedback requery, P2a at personal-store scale. The WaC
doctrine still governs every from-scratch mechanism exactly as
amended. Door D stays shut. Nothing in this decision reopens a
measured question; it opens the one question the record was
structurally forbidden from measuring.

## What this document does not do

No product code changes in this commit. No fetch has occurred; no
dependency has been added; no weight file exists in or near the tree.
The R1 declaration is the next document, and its first run is gated
on an explicit fetch step that has not happened. This brief changes
the doctrine and nothing else — the discipline that priced every
lane above is the same discipline the reentry now owes.
