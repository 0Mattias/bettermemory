# Dense scoring census — declared in full before any cell was run, 2026-08-12

The P1e arc so far, cited rather than restated: census 1
(`bench/retrieval/results/embed-census-2026-08-11.json`) measured a
from-scratch GloVe against P1a's emission gate and produced the
corpus finding — the only training corpus that yields usable
neighbours is the collection being ranked — plus the sensitivity
record that the trainer's declared defaults are the sweep's best.
Census 2 (`bench/P1E_CENSUS2_DECLARATION.md`, artifact
`bench/retrieval/results/embed-census2-2026-08-11.json`) fixed its
family and bars first, ran, and PARKED the emission lane at its own
criterion: the primary cell missed the incumbent's gate and no cell
in the family passed at width — the margins live in that artifact,
cited rather than restated, exactly as this document treats every
number it did not itself produce. Term EMISSION from store-trained
vectors is dead at store scale, by its own declared rules, and
nothing here relitigates it.

SCORING is a different mechanism family, and the owner's 2026-08-12
decisions put it on the board: store-trained only (no external
corpus, no fetch), both campaign tracks in parallel, this machine.
A scoring model never emits a term. It pools the query into one
vector, pools each document into one vector, and ranks documents by
similarity — the failure mode census 2 measured (imprecise
neighbour TERMS) never occurs because no term leaves the model. The
open question is whether the same store-trained geometry that
cannot name precise neighbours can nonetheless point at the right
DOCUMENT. The pre-4.0 record already shows the mechanism class
clears the campaign's bars with pretrained weights; what has never
been measured is the from-scratch, store-scale form — the only form
the WaC doctrine ships.

This census asks that question at its sharpest point: the dev
probes whose gold the shipped lexical engine ranks far or not at
all — the vocabulary-shaped pool that no rerank of lexical
candidates can ever reach, the pool that is Track B's food
precisely because it is Track A's ceiling. Everything below — the
family, every definition, the pools, the reach bar, the routing
rule, and the parking criterion — is committed before a single
census number exists. The enforcement record is the sha ordering:
this commit, then the run commit. Nothing may be added to the
family afterwards.

## 1. The mechanism family, enumerated

One model: per-store self-trained GloVe, `bench/embed_train.py
--corpus store`, at the trainer's declared defaults (dim 64, epochs
15, seed 20260811). Census 1's sensitivity artifact
(`embed-sensitivity-2026-08-11.json`) already established those
defaults, so no training parameter is swept here. The vectors are a
derived intermediate, reproduced by the committed trainer over
committed text, and are not committed themselves.

Three axes, crossed completely — **8 cells, no more**:

| axis | values | what it is |
| --- | --- | --- |
| `pooling` | `mean`, `idf` | uniform mean of member-token unit vectors, or the same weighted by ln(N/df_t) with df over the ranked collection |
| `postproc` | `raw`, `centred` | the two vector readings census 1 fixed; `centred` is `embed_census.Model` mode "centred", verbatim |
| `bridging` | off, on | census 2's n-gram composition (`embed_hybrid.bridge`), query-side only |

Every remaining definition, fixed now:

- Query tokens: the engine's own pipeline —
  `sorted(set(_strip_stopwords(_expand_kebab(tokenize(q)))))`.
- Document tokens: `engine._memory_tokens(memory).content`, the
  engine's distinct content tokens for that document.
- A token contributes its unit vector; under `idf`, its weight is
  ln(N/df_t) — df over the ranked collection, clamped below at 1
  because a bridged query token may appear in no document — applied
  to query and document tokens alike, and tokens with non-positive
  weight contribute nothing. Out-of-vocabulary query tokens are bridged when the axis
  is on and dropped otherwise; out-of-vocabulary DOCUMENT tokens
  are always dropped — queries are a few tokens and starve without
  the bridge, documents carry vocabulary mass, and bridging
  hundreds of document tokens would manufacture geometry the
  training never earned. The asymmetry is declared, not discovered.
- The pooled vector is the weighted mean, L2-normalised; the score
  is the dot product of the two pooled vectors. Ties break on store
  insertion order (corpus row order), so the ranking is
  deterministic.
- A document whose token set pools to nothing is excluded from the
  ranking and counted; a GOLD that pools to nothing has rank None
  and fails every reach test below. A query that pools to nothing
  ranks nothing and fails the same way.

## 2. The pools, read off the committed record

Pool membership comes from
`bench/retrieval/results/base-leg-labels-2026-08-12.json` (committed
`07ad967`): each record's `gold_rank_with_leg` is the shipped
engine's fused gold rank (0-indexed, observed to depth 50), and this
evening's P2a census re-validates those ranks through the shipped
engine independently. Unpadded, asked and control probes:

- FAR/ABSENT pool (primary): records with 0-indexed rank ≥ 10 or
  None. This is the vocabulary wall — the pool where the campaign's
  entire remaining as-asked headroom beyond a perfect rerank lives.
- HIT@1 pool (preservation): records with rank 0. What lexical
  already gets right, which a dense LEG would have to not lose.
- Every unpadded probe (all three classes) is scored and reported
  by stratum — full shape, no gate.

The requery probes carry no far mass and the padded-600 regime's
production question (is gold even in the prefilter's served pool?)
is recorded once, in the P2a census artifact, and cited by both
tracks' preregistrations rather than measured twice.

## 3. The reads — tabulation, no selection

- Per cell, per far/absent probe: the gold's 1-indexed dense rank
  over the full 180-document collection (None if unpooled). The
  primary count: probes with rank ≤ 10. Ten is the reach bar
  because a document inside the top ten is inside every window this
  campaign's rerank reads measure and inside fusion range for a
  leg; a document outside it is food for nothing.
- Per cell: median and quartiles of dense gold rank per stratum
  (hit@1 / near / mid / far / absent), asked and control separately.
- Per cell: pooling diagnostics — documents excluded as unpooled,
  query tokens bridged, query tokens dropped.
- LongMemEval diagnostic slice, primary cell only: the first 20
  instances of `longmemeval_s_cleaned.json` in file order, each
  scored by a model trained on that question's OWN haystack (the
  runner's own store construction, the same trainer parameters and
  seed — the product shape: train on the collection you rank).
  Items are scored, item ties break on the sha-256 of the item body
  (a content-derived key that repeats across runs), the item ranking
  collapses to distinct sessions exactly as the runner collapses it,
  and each evidence session's dense rank is recorded. Twenty questions is a glance, not a
  measurement; it is disclosed as underpowered, it gates nothing,
  and a conversational sign-flip observed here is recorded as a
  note for the Track B preregistration to take seriously.

## 4. The readiness criterion, stated before any result

The primary cell, chosen by a stated rule rather than by prospects:
every axis at the value the prior censuses' record already
supports — `centred` (census 1's stronger reading), bridging ON
(census 2 measured the bridge coverage-positive and
precision-neutral) — and the one axis with no prior record,
`pooling`, at its null value, `mean`.

- R1 (reach): the primary cell places gold at dense rank ≤ 10 on
  at least 5 of the far/absent pool's probes (the pool size is
  whatever the committed labels artifact yields; the Gate-0 read
  puts it at 9).
- R2 (preservation): the primary cell's median dense gold rank
  over the hit@1 pool is ≤ 10.

Writing the Track B preregistration is licensed if and only if R1
holds. R2 does not license or park anything — it ROUTES: with R1
and R2 both true, the preregistration may propose either fusion
shape (a dense leg, or a rerank window over lexical candidates);
with R1 true and R2 false, the dense model demonstrably buries what
lexical finds, and the preregistration may propose ONLY the
rerank-window shape, where dense opinion reorders lexical
candidates and can never remove one. That routing is exactly the
round-9 lesson applied in advance: a mechanism that helps one
population must be shaped so it cannot silently tax another.

The lane is parked if R1 fails at the primary cell AND no cell in
the family reaches 5 of the pool. That is the vocabulary wall
governing geometry as it governed emission, and it retires
store-trained dense retrieval at personal-store scale — the record
being this document plus the run artifact, and the campaign's
remaining food being Track A's window plus the requery/vocabulary
guidance line.

The anti-gate-shopping clause, census 2's verbatim rule: if the
primary cell fails R1 but some non-primary cell reaches 5, that
result licenses at most a follow-up census declaration naming that
cell as ITS primary. The maximum of 8 cells is not a preregistered
hypothesis. The remaining cells are reported as the family's shape
and are explicitly not eligible to carry the verdict.

## 5. The constraint ledger

- Both sealed instruments are untouched. Instrument #1
  (`bench/heldout/data/`) is assigned to this arc by the owner's
  sequencing decision, and it is spent ONLY under the future
  preregistration's own gates — this census reads no byte of it.
  Instrument #2 stays reserved for P2a. No file under
  `bench/heldout/` is opened.
- No engine code, whatever the outcome. Nothing under `src/`
  changes; the census imports the engine's tokenizer and token
  readers, and ranks nothing the product serves.
- Statistics only. No fusion, no weights, no threshold — the
  census scores documents in isolation and tabulates ranks.
- Dev-side by construction; the LongMemEval slice is the one
  exception, and its corpus is the gitignored download this bench
  already carries with the same reproducibility caveat
  `bench/df_census.py` records.
- Deterministic artifact: fixed seed, sorted iteration, no
  per-build identifiers, no wall-clock content beyond the
  provenance date. Two runs at the same commit produce the same
  bytes.
- No changelog entry. Census commits ship none: no user-facing
  surface changes.

## Declared confounds

1. The primary pool is nine probes. A 5-of-9 reach is a license to
   spend a preregistration's effort, not evidence of anything; the
   preregistration carries the confirmatory weight, on its own
   instrument, with its own kill lines.
2. The dev corpus is technical prose; the conversational corpus
   appears only as the 20-question glance. Round 9 killed a
   mechanism at exactly this asymmetry, which is why the glance
   exists and why its sign matters more than its power.
3. The reach bar (rank ≤ 10 over a 180-document collection) is
   generous by construction — production's above-threshold shape
   serves a 50-document pool. The bar measures whether the
   geometry points at the document at all, not whether it survives
   production fusion; the gap between those two is the
   preregistration's problem and is stated here so it cannot be
   discovered later.
4. Store insertion order as the tie-break favours no document the
   model scored differently; it exists so two runs agree byte-wise,
   not to encode a preference.
