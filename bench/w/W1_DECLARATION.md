# W1 — subword vectors into the surviving expansion leg: unit declaration, 2026-08-15

First unit of Lane W under `bench/W_PROGRAM_DECLARATION.md`. The
program frame carries the doctrine (own derivation, auditable public
inputs, tiered reproducibility); this document is the unit contract:
arms, budgets, bars, and the read protocol, all fixed before any
corpus byte is fetched, any trainer line is written, or any number
exists. The enforcement record is the sha ordering: this commit, then
the corpus pins (each fetch behind its own plain-sentence yes), then
the implementation commit, then the run commits. Nothing may be added
afterwards; a miss is published, never renegotiated.

The question W1 asks, exactly: can geometry trained from scratch on
auditable public text replace the hand-committed vocabulary tables in
the rescue-expansion leg (`src/bettermemory/expansion.py`) and beat
them on the technical-prose instrument without deepening the
conversational-store cost that keeps the leg opt-in?

## 1. Baselines this unit is judged against — committed, quoted

Dev instrument (`bench/retrieval/run.py`, unpadded, prefilter off,
corpus sha256 `c40acee95ce1bb70ac6ea788e0fb4a9a1c6eff1fc55fe4569b651b5b156ea2ea`,
n=20, five points per question, `asked` probe):

- expansion off: recall@1 35%, recall@5 60%
  (`bench/retrieval/results/r1-unpadded-2026-08-13.json`);
- static tables on: recall@1 55%, recall@5 90%
  (`bench/retrieval/results/shipped-unpadded-2026-08-11.json`) —
  the arm W1 must beat.

LongMemEval (`bench/longmemeval/run.py`, lexical arm, n=500, corpus
sha256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`):

- expansion off: macro recall@1 0.5246, macro recall@5 0.8935
  (`bench/longmemeval/results/rebaseline-baseline-2026-08-10.json`;
  reproduced exactly at 5.5.0 in
  `bench/longmemeval/results/r2-both-arms-2026-08-14.json`);
- static tables on: macro recall@1 0.4772, macro recall@5 0.8770
  (`bench/longmemeval/results/rebaseline-lane-2026-08-10.json`) —
  the polarity cost W1 must shrink: −4.74 points at @1, −1.65 at @5.

The program horizon (dev as-asked recall@1 60; LongMemEval macro@5
91.6) is carried, not claimed: a W1 pass is a pass of THIS unit's
bars on an opt-in-measured arm. The criterion as written demands the
bars on the default engine; no criterion claim follows from anything
below, and the honest interim sentence stands.

## 2. What is trained, precisely

A skip-gram-with-negative-sampling trainer with character-n-gram
subword bags — this repository's own code, written for this unit,
deliberately small. No third-party pretrained weights anywhere in the
chain: not as initialization, not as a teacher, not as a vocabulary
prior. The trainer is bench-side (`bench/w/`), never shipped; numpy
is admitted as a bench-side build dependency exactly as the census
scripts already use it — the SHIPPED surface gains no dependency
because this unit ships nothing (§4).

Determinism measures, per program clause 3's strict tier:
single process, BLAS pinned to one thread, one declared integer seed,
fixed corpus read order, fixed vocabulary order (count-then-lexical),
no wall-clock or filesystem-order dependence anywhere in the chain.
A retrain from the register must reproduce every emitted byte.

Declared defaults (tunable only under §6's protocol; the values the
run actually used land in the artifact): vector dim 100; char
n-grams 3–5 hashed to 2^19 buckets; dynamic window 5; 5 negatives;
≤ 3 epochs; vocabulary min-count 10, capped at the 75,000 most
frequent tokens; subsample threshold 1e-4; linear learning-rate decay
from 0.05. Tokenizer: lowercase, split on non-alphanumeric runs, keep
tokens of length 2–30 — the trainer's own; the ENGINE's tokenizer and
stemmer are not touched.

Budgets, hard: the trainer reads at most 50 M tokens from the
register (deterministic prefix in register order; actual count in the
artifact) and at most 12 wall-clock hours on this machine, single
process, per the standing compute ruling. A budget overrun is a
published park, not a quiet cap raise.

## 3. The corpus plan — candidates named, nothing fetched

No fetch happens under this document. Each fetch is one plain
sentence to the owner naming what, from where, and roughly how large,
and the yes must be on that sentence — the program's clause 2, the
reckoning's rule. Candidates this unit intends to put forward:

1. **English Wikipedia, bounded slice**: the first pages-articles
   part-file of a dated dump from dumps.wikimedia.org (the lowest
   page-id range — encyclopedia-core articles), roughly 250–300 MB
   compressed, roughly 1 GB of wikitext; license CC BY-SA, recorded
   at pin time.
2. **Project Gutenberg, curated English subset**: on the order of a
   hundred public-domain English books as plain text from
   gutenberg.org, roughly 40 MB total, each file pinned and
   license-verified individually (public domain in the US; PG
   boilerplate stripped before training, no PG trademark carried).

Both land in `bench/w/corpora.json` as pinned snapshots — source
URL, retrieval date, sha256 over the exact bytes, verified license —
BEFORE the trainer reads them. Markup stripping (wikitext → prose,
PG header removal) is the trainer's own committed code; the register
pins the fetched bytes, the artifact records the post-strip token
count. A corpus that fails license verification is recorded
`admitted=false` with the reason and not read.

## 4. The artifact — a readable table, not a shipped change

The vectors themselves are an intermediate: large, binary, and not
committed. The unit's artifact is the EMITTED NEIGHBOR TABLE —
surface-form word → nearest-neighbor surface forms, generated source
in the exact idiom of the hand tables it replaces, committed under
`bench/w/artifacts/` beside a run JSON carrying hyperparameters,
corpus pins, token counts, and the sha256 of both the vectors blob
and the table source.

Emission rule (declared defaults; finals in the artifact): for each
vocabulary term, neighbors by cosine over the subword-composed
vectors, kept only above cosine floor 0.60 AND within mutual rank 8,
at most 4 neighbors per term; emitted terms pass the same floors the
leg already enforces (minimum length 3, filler stems excluded — the
learned table rides `expansion_terms`' existing filters, it does not
bypass them). Caps, hard: at most 5,000 head terms; table source at
most 300 KB. The cap is the wheel-size answer clause 5 demands,
declared now even though this unit ships nothing.

Nothing under `src/` changes in this unit. The measurement harness
(bench-side) swaps ONLY the table contents into the leg: the same
`ExpansionTables` shape, built through the live stemmer by the same
`build_tables` path, with `QUERY_FILLER_WORDS` and the
`morph_variants` rule untouched — the ranking code path is otherwise
byte-identical to shipped `search.search(rescue_expansion=True)`.
Every W1 result artifact records the table-source sha256 it ranked
with. Wiring any of this into the package — opt-in or default — is
an owner door with its own plain sentence, after the read.

## 5. The arms — two, sealed now

- **W1-full**: the learned table replaces all three hand lookup
  tables (`SYNONYM_GROUPS`, `CLIPPINGS`, `IRREGULAR_PAST`). The
  strong claim: subword geometry subsumes hand curation — clippings
  and irregulars fall out of shared character n-grams.
- **W1-syn**: the learned table replaces `SYNONYM_GROUPS` only; the
  two high-precision hand tables stay. The fallback claim: learned
  geometry widens the synonym assault without giving up curated
  precision.

Both arms run every cell of §6. The unit passes on an arm if that
arm clears BOTH bars of §7 on the same committed artifact; if both
arms clear, W1-full is the unit's named result and W1-syn is
published beside it. No third configuration exists — there is no
grid to shop.

## 6. The read protocol — what may be read, when

- **Training-internal signals** (loss curves, committed neighbor
  spot-lists): unlimited, artifact-recorded.
- **Dev instrument**: unlimited during tuning; every read is
  published, numbered, in `bench/w/results/` — the dev set is the
  declared tuning surface, and the record shows what tuning saw.
- **LongMemEval**: at most TWO reads before the gate read, both
  published. The instrument the polarity bar lives on does not
  become a tuning surface.
- **The gate read**, one per arm: the final artifact is committed
  first (table + hashes), then both instruments run once against it
  — dev unpadded/prefilter-off, LongMemEval full 500 — with the
  static-tables arm and expansion-off baseline re-run PAIRED in the
  same invocations. The primary dev invocation runs twice; the two
  results blocks must be identical (R1's determinism bar, kept).
- **Sealed stays sealed**: nothing under `bench/heldout/` is opened;
  instrument #2 stays reserved for P2a. The gate read is the last
  read; post-gate tuning does not exist.

Prefilter cells (above-threshold and forced-180) are measured at
gate time and published — the nomination regime cost the 5.1 lane
fifteen points of recall@5 on this instrument
(`bench/retrieval/results/prefilter-above-threshold-2026-08-09.json`)
and W1's table rides the same leg — but they gate nothing here; they
feed the ship sentence and any W2 declaration.

## 7. The bars — fixed now

- **G1, the dev bar** (gate read, unpadded, prefilter off, asked,
  W1 tables on): recall@1 ≥ 60% AND recall@5 ≥ 90%. Sixty is the
  smallest strict beat of the static arm's 55% this twenty-question
  instrument can express (one question), and it happens to be the
  level the stripped semantic arm held; 90% concedes nothing at @5.
- **G2, the polarity bar** (gate read, LongMemEval lexical, W1
  tables on): macro recall@5 ≥ 0.8835 AND macro recall@1 ≥ 0.5046 —
  epsilons of 1.0 and 2.0 points against the expansion-off baseline,
  strictly inside the static tables' measured −1.65 and −4.74. The
  learned table must not merely tie the hand tables' damage; it must
  shrink it, at both depths.
- **G3, the determinism bar**: two consecutive full retrains from
  the pinned register, same seed, on this machine — sha256 of the
  vectors blob AND of the emitted table source identical across
  both, all four hashes in the artifact; and a CI check retraining
  a committed reduced register (Gutenberg-derived bytes only, ≤ 2 MB,
  itself pinned in the register) asserting the same equality on
  every push. G3 is unconditional: it must hold whatever G1 and G2
  say.
- **Integrity reads** (any miss demotes the verdict to PARTIAL
  pending diagnosis, R1's rule): the same-run static-tables arm
  reproduces 55%/90% within two questions; the same-run
  expansion-off LongMemEval macros equal their committed values;
  requery ≥ asked at recall@1 on every dev arm.
- **Verdicts.** W1-PASS: G3 holds and an arm clears G1 AND G2.
  W1-PARTIAL: an arm clears G1 but none clears G2 — published as
  the polarity result; no ship sentence is offered. W1-PARK: no arm
  clears G1, or G3 fails, or a budget of §2 is overrun — the park
  publishes like every park before it, and the diagnosis feeds the
  W2-entry decision the program ladder already conditions on W1's
  read.

## 8. The constraint ledger

- The engine is not edited: no `src/` change, no tokenizer or
  stemmer change, no scoring change rides in with the measurement
  harness. The harness swaps table contents and nothing else.
- The instruments are not edited: committed corpora, committed
  questions, the runners' entry points as they stand at the
  implementation commit.
- No third-party pretrained weights, embeddings, or LLM-synthesized
  text anywhere: not in training, not in vocabulary selection, not
  in neighbor filtering, not in the curated Gutenberg book list
  (chosen by hand, listed in the register).
- Fetches are enumerable and announced: exactly the pins the
  register records, each behind its own plain-sentence yes; the
  trainer runs offline over pinned bytes and asserts it.
- Artifacts are dated, sha-ordered, and published whatever they say
  — including every tuning read the protocol admits.
- No changelog entry for declaration or run commits; nothing
  user-facing changes in this unit.

## Declared confounds

1. **Corpus-domain mismatch, the live risk.** The dev instrument is
   technical engineering prose; encyclopedia and public-domain book
   text may place "rollback" nearer to "retreat" than to "revert".
   The hand tables encode exactly the dev-relevant senses — a
   learned table diluting them is the expected failure mode, and G1
   is the honest detector. A G1 miss on both arms parks the unit
   and prices the corpus choice, not just the mechanism.
2. **Granularity asymmetry.** Five points per dev question against
   sub-point LongMemEval macros: G1 can only move in whole
   questions, so the beat threshold is one full question above the
   static arm — a real bar, not a rounding artifact, and a
   two-point epsilon at LongMemEval @1 is finer than anything the
   dev side can resolve. Named so the artifact's readers price the
   two scales correctly.
3. **The static arm's dev record predates the 6.0.0 strip.** Its
   55%/90% was produced on the 5.x engine; the strip removed the
   semantic lane and should not have moved the lexical path. The
   paired integrity read is the control: if the static arm cannot
   reproduce its own committed numbers in the gate read, the engine
   moved, and the verdict is PARTIAL pending that diagnosis rather
   than a beat judged against a stale ruler.

## What is not claimed

No criterion claim (§1). No default-ship and no opt-in ship — the
unit ends at a published read and, on PASS, a ship sentence put to
the owner in plain language. No W2 entry decision — that is the
program ladder's, conditioned on this read. No comparative claim
against any other memory system from a single-system artifact. No
reuse of the trained vectors anywhere outside the declared arms
until a unit declares it.
