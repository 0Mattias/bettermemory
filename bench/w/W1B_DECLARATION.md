# W1b — the wide-register retrain: unit declaration, 2026-08-18

Successor unit to `bench/w/W1_DECLARATION.md` under
`bench/W_PROGRAM_DECLARATION.md`, entered exactly where the W1 record
and the W3-C record both point: W1 parked with the diagnosis that the
mechanism was real but the register was wrong — encyclopedia-core and
literary prose do not carry the casual-technical alternations the dev
instrument's hand tables encode — and W3-C's gate measured the
completed polarity lesson from the learned side, an expansion table
that damaged every conversational type while lifting the keyword
instrument above its guard
(`bench/w/results/gate-w3c-dev-bridge-2026-08-17.json`). The dev bar
is the program's open front, and this unit is its whole queue.

The question W1b asks, exactly: does the same from-scratch trainer,
fed the register W1's diagnosis named — casual technical question
text, at four times the token budget, over corpus pins that already
sit on disk under the 2026-08-17 standing grant — clear the dev bar
the hand tables set, while holding the conversational floor on the
engine as it ships today?

The enforcement record is the sha ordering, unchanged: this commit,
then the implementation commit, then the run commits. Every corpus
byte this unit reads was pinned before this declaration existed
(`bench/w/corpora.json`; acquisition commit 25b19e9); no fetch happens
under this unit. Nothing may be added after the fact; a miss is
published, never renegotiated.

## 1. Baselines this unit is judged against — committed, quoted

Dev instrument (`bench/retrieval/run.py`, unpadded, prefilter off,
corpus sha256
`c40acee95ce1bb70ac6ea788e0fb4a9a1c6eff1fc55fe4569b651b5b156ea2ea`,
n=20, five points per question, lexical arm, `asked` probe):

- expansion off: recall@1 35%, recall@5 60%
  (`bench/retrieval/results/r1-unpadded-2026-08-13.json`);
- static hand tables: recall@1 55%, recall@5 90%
  (`bench/w/results/gate-dev-static-2026-08-16.json`, reproducing the
  committed `bench/retrieval/results/shipped-unpadded-2026-08-11.json`
  exactly) — the arm W1b must beat;
- W1's learned arms, the ruler this retrain must move past on the
  learned side: W1-full 45%/60%, W1-syn 50%/70%
  (`bench/w/results/gate-dev-full-a-2026-08-16.json`,
  `bench/w/results/gate-dev-syn-a-2026-08-16.json`).

LongMemEval (`bench/longmemeval/run.py`, n=500, corpus sha256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`),
re-anchored to the engine as it ships today per the W1 record's third
diagnosis — the 5.1.1-era epsilons were set against a ruler the engine
had already moved from, and this unit's floors are set against a
same-engine committed read instead:

- expansion off, default engine (6.1.0, conversational lane on):
  macro recall@1 0.5339, macro recall@5 0.9062
  (`bench/w/results/gate-w3c-lme-incumbent-2026-08-17.json`) — the
  baseline G2's floors subtract from.

The program horizon (dev as-asked recall@1 60; LongMemEval macro@5
91.6) is carried, not claimed. A W1b pass is a pass of THIS unit's
bars on an opt-in-measured arm; no criterion claim follows from
anything below, and the honest interim sentence stands: the bars are
unreached by our own means, published as such.

## 2. What is trained — the mechanism held, one revision declared

The trainer is W1's: skip-gram with negative sampling over
character-n-gram subword bags, this repository's own committed code
(`bench/w/w1_train.py`), no third-party pretrained weights anywhere in
the chain — not as initialization, not as a teacher, not as a
vocabulary prior. numpy stays the declared bench-side build
dependency; the SHIPPED surface gains nothing because this unit ships
nothing (§5).

**The one declared revision: segmented epoch enumeration.** W1's
trainer materializes each epoch's full skip-gram pair set in memory at
once; at W1's 50M-token budget that was ~185M pairs per epoch
(`bench/w/artifacts/w1_table_2026-08-16.json`, run block), and at this
unit's budget the same shape would exceed this machine's memory. The
revision enumerates each epoch's pairs in fixed-size segments of the
kept stream, holding everything else fixed: the update mathematics,
the batch schedule, the learning-rate decay arithmetic, the RNG
consumption order (per-epoch keep-mask, then per-epoch window widths,
then per-batch negatives), and the global pair order (ascending center
position, W1's tie order within a center). Segment size is a memory
knob and MUST NOT be a result knob: the emitted bytes must be
invariant to it, and the CI leg (§7, G3) asserts that invariance
mechanically. The revision lands in the implementation commit; G3
gates it.

Determinism measures otherwise unchanged, program clause 3's strict
tier: single process, BLAS pinned to one thread, one declared integer
seed, fixed corpus read order, fixed vocabulary order
(count-then-lexical), no wall-clock or filesystem-order dependence
anywhere in the chain. A retrain from the register must reproduce
every emitted byte. The trainer MAY materialize a derived token cache
under `bench/w/corpus/derived/` to avoid re-decompressing the archives
per pass; the cache is a pure function of the pinned bytes and the
committed code, its sha256 lands in the run meta, and each G3 retrain
rebuilds it independently — the pair's cache hashes must match.

Declared defaults (the values W1's published tuning settled on where
one exists; tunable only under §6's protocol; the values the run
actually used land in the artifact): vector dim 100; char n-grams 3–5
hashed to 2^19 buckets; dynamic window 5; 5 negatives; 3 epochs;
vocabulary min-count 10, capped at the **150,000** most frequent
tokens — double W1's cap, because W1's diagnosis found dev-relevant
terms below the vocabulary floor and this unit quadruples the token
budget; subsample threshold 1e-4; learning rate 0.025 with linear
decay; batch 1024; seed 20260818. Tokenizer: W1's, byte for byte —
lowercase, split on non-alphanumeric runs, keep tokens of length
2–30. The ENGINE's tokenizer and stemmer are not touched.

Budgets, hard: the trainer reads at most **200,000,000 tokens** from
the register under §3's per-source caps (actual counts in the
artifact), and at most **24 wall-clock hours per training run** on
this machine, single process, per the standing compute ruling. W1's
committed receipt carries its own run block — pair volume and wall
seconds — and this unit's pair volume is several times W1's, so the
24-hour cap is headroom rather than a target. A budget overrun is a
published park, not a quiet cap raise.

## 3. The corpus slice — pinned already, budgeted per source

Everything below is admitted in `bench/w/corpora.json` with sha256
pins and verified licenses; the acquisition is commit 25b19e9 under
the 2026-08-17 standing grant, and the enwiki parts and Stack Overflow
archive carry the 2026-08-16 consent the W1 record already names. No
network touch happens under this unit.

The slice is a per-source token budget, read as a deterministic prefix
of each source in its declared internal order, sources concatenated in
the order listed; a source smaller than its cap contributes its whole
stream, and the 200M global cap of §2 binds regardless. The budget
splits the register question down the middle, because the unit's two
motivations pull on two registers:

**The casual-technical half, 100M — the register W1's diagnosis
named:**

1. `stackoverflow-posts-archive` — 80,000,000 tokens;
2. `superuser-archive` — 10,000,000;
3. `apple-stackexchange-archive` — 5,000,000;
4. `android-stackexchange-archive` — 5,000,000.

**The breadth half, 100M — the polarity anchor and the running-text
registers the 2026-08-17 roll located:**

5. English Wikipedia, parts 1–71 in ascending (part index, first page
   id) order (`enwiki-20260801-pages-articles-part1`,
   `enwiki-20260801-pages-articles-parts2-71`) — 50,000,000;
6. `simplewiki-20260801-pages-articles-multistream` — 10,000,000;
7. `enwiktionary-20260801-pages-articles`, parts in ascending part
   order — 5,000,000;
8. `gutenberg-curated-2026-08-15`, books in ascending id order —
   9,500,000;
9. the fifteen lifestyle-and-preference site archives, alphabetical by
   register name (`academia`, `beer`, `coffee`, `cooking`, `diy`,
   `fitness`, `gardening`, `interpersonal`, `lifehacks`, `movies`,
   `music`, `outdoors`, `parenting`, `pets`, `travel`
   `-stackexchange-archive`) — 1,700,000 each, 25,500,000 in all.

Readers: the wiki-family sources ride W1's committed article reader
and wikitext strip unchanged (`bench/w/w1_corpus.py`); the Stack
Exchange sources get a new committed reader in the W3-P2 idiom —
`Posts.xml` streamed from the pinned `.7z` via `bsdtar` exactly as
`bench/w/w3p_pairs.py` established, rows in document order, question
rows (PostTypeId 1) contributing title plus body, answer rows
(PostTypeId 2) contributing body, HTML stripped by this unit's own
committed code, one post per document. Document boundaries bound
training windows, as in W1. Reading stops mid-source exactly at the
budget; the truncation point is a function of the pinned bytes and
nothing else.

## 4. The artifact — a readable table, same caps as W1

The vectors are an intermediate: large, binary, not committed. The
unit's artifact is the emitted neighbor table — surface-form word →
nearest-neighbor surface forms, generated source in the idiom of the
hand tables, committed under `bench/w/artifacts/` beside a run JSON
carrying hyperparameters, corpus pins and per-source token counts, the
segment size used, and the sha256 of the vectors blob, the token
cache, and the table source.

Emission (declared defaults = the profile W1's published tuning
frontier found, `bench/w/artifacts/w1_table_2026-08-16.json`: cosine
floor 0.65, at most 3 neighbors per term, mutual rank 8, the 500 most
frequent heads skipped, the rule-covered pairs dropped, at most 1,500
entries; finals in the artifact): neighbors by cosine over the
subword-composed vectors; emitted terms pass the same floors the leg
already enforces — the learned table rides `expansion_terms`' existing
filters, it does not bypass them. Caps, hard and unchanged from W1: at
most 5,000 head terms; the table source at most 300 KB on disk — the
wheel-size answer clause 5 of the program demands, declared even
though this unit ships nothing.

Nothing under `src/` changes in this unit. The measurement harness is
W1's, byte for byte (`bench/w/w1_measure.py`): the same
`ExpansionTables` shape, built through the live stemmer by the same
`build_tables` path, `QUERY_FILLER_WORDS` and `morph_variants`
untouched, the ranking path otherwise identical to shipped
`search.search(rescue_expansion=True)`. Every result artifact records
the table-source sha256 it ranked with. Wiring anything into the
package — opt-in or default — is an owner door with its own plain
sentence, after the read.

## 5. The arms — two, sealed now

- **W1b-full**: the learned table replaces all three hand lookup
  tables (`SYNONYM_GROUPS`, `CLIPPINGS`, `IRREGULAR_PAST`).
- **W1b-syn**: the learned table replaces `SYNONYM_GROUPS` only; the
  two high-precision hand tables stay.

Both arms run every declared cell. The unit passes on an arm if that
arm clears BOTH bars of §7 on the same committed artifact; if both
arms clear, W1b-full is the named result and W1b-syn is published
beside it. No third configuration exists — there is no grid to shop.

## 6. The read protocol — what may be read, when

- **Training-internal signals** (loss curves, committed neighbor
  spot-lists): unlimited, artifact-recorded.
- **Dev instrument**: unlimited during tuning; every read is
  published, numbered, in `bench/w/results/` — the dev set is the
  declared tuning surface, and the record shows what tuning saw. The
  expected tuning surface is emission-side; a training-side re-tune,
  if the frontier demands one, is a new published run inside §2's
  budgets, recorded in the same trail.
- **LongMemEval**: at most TWO reads before the gate read, both
  published. The instrument the polarity bar lives on does not become
  a tuning surface.
- **The gate read**, one per arm: the final artifact is committed
  first (table + hashes), then both instruments run once against it —
  dev unpadded/prefilter-off, LongMemEval full 500 — with the
  static-tables arm and the expansion-off baseline re-run PAIRED in
  the same invocation set. The primary dev invocation runs twice; the
  two results blocks must be identical (the run-determinism bar,
  kept from R1 through every unit since).
- **Sealed stays sealed**: nothing under `bench/heldout/` is opened;
  instrument #2 stays reserved for P2a. The gate read is the last
  read; post-gate tuning does not exist.

Prefilter cells (above-threshold and forced-180) are measured at gate
time and published; they gate nothing here — they feed any ship
sentence and any W2 declaration, exactly as in W1.

## 7. The bars — fixed now

- **G1, the dev bar** (gate read, unpadded, prefilter off, asked,
  W1b tables on): recall@1 ≥ 60% AND recall@5 ≥ 90%. Unchanged from
  W1: sixty is the smallest strict beat of the static arm's 55% this
  twenty-question instrument can express, and 90% concedes nothing at
  @5.
- **G2, the polarity bar** (gate read, LongMemEval, W1b tables on):
  macro recall@5 ≥ **0.8962** AND macro recall@1 ≥ **0.5139** —
  epsilons of 1.0 and 2.0 points against the committed same-engine
  expansion-off baseline of §1 (0.9062 / 0.5339,
  `bench/w/results/gate-w3c-lme-incumbent-2026-08-17.json`). The
  epsilons sit strictly inside the static tables' own same-engine
  costs as recorded at W1's gate (1.12 points at @5, 2.32 at @1:
  `bench/w/results/gate-lme-static-2026-08-16.json` against
  `bench/w/results/gate-lme-off-2026-08-16.json`): the learned table
  must not merely tie the hand tables' damage; it must shrink it, at
  both depths. The floors are these two numbers, and they do not move
  whatever the paired reads say at gate time.
- **G3, the determinism bar**: two consecutive full retrains from the
  pinned register, same seed, on this machine — sha256 of the vectors
  blob AND of the emitted table source identical across both, the
  token-cache hashes identical across both, all hashes in the
  artifact. In CI, on every push: the existing reduced-register check
  retrains the committed Gutenberg-derived slice and asserts hash
  equality across two in-process trains, extended by (a) a second
  committed reduced register derived from the pinned Stack Exchange
  bytes (at most 2 MB, itself pinned in `bench/w/corpora.json` at the
  implementation commit) exercising the new reader end to end, and
  (b) a segment-size invariance assert — the same reduced train at
  two declared segment sizes must emit identical bytes. G3 is
  unconditional: it must hold whatever G1 and G2 say.
- **Integrity reads** (any miss demotes the verdict to PARTIAL
  pending diagnosis): the same-set static-tables dev arm reproduces
  55%/90% within two questions; the same-set expansion-off dev arm
  reproduces 35%/60% exactly; the same-set expansion-off LongMemEval
  macros equal the committed 0.5339/0.9062; requery ≥ asked at
  recall@1 on every dev arm.
- **Verdicts.** W1b-PASS: G3 holds and an arm clears G1 AND G2.
  W1b-PARTIAL: an arm clears G1 but none clears G2 — published as the
  polarity result; no ship sentence is offered. W1b-PARK: no arm
  clears G1, or G3 fails, or a budget of §2 is overrun — the park
  publishes like every park before it, and the diagnosis feeds the
  W2-entry decision the program ladder conditions on this unit's
  read.

## 8. The constraint ledger

- The engine is not edited: no `src/` change, no tokenizer or stemmer
  change, no scoring change rides in with the measurement harness.
- The instruments are not edited: committed corpora, committed
  questions, the runners' entry points as they stand at the
  implementation commit.
- No third-party pretrained weights, embeddings, or LLM-synthesized
  text anywhere in the chain.
- No fetch: every byte read is already pinned in
  `bench/w/corpora.json`; the trainer runs offline over pinned bytes
  and asserts it.
- Artifacts are dated, sha-ordered, and published whatever they say —
  including every tuning read the protocol admits.
- No changelog entry for declaration or run commits; nothing
  user-facing changes in this unit.

## Declared confounds

1. **Register match is not answer coverage.** Stack Overflow text
   carries the toggle/flag and undo/rollback register at high
   frequency, but the dev instrument moves in whole questions: a
   mechanism must convert vocabulary into at least one net rank-one
   question over the static arm's 55% to register at G1. The register
   was W1's named killer; it is not the only way to miss.
2. **Scale moves the emission frontier.** Four times the corpus and
   twice the vocabulary cap widen the candidate neighbor space; the
   emission profile W1's tuning found (floor 0.65, mutual rank 8) was
   tuned at W1's scale and may sit elsewhere at this one. The dev
   surface is the declared place to find it; the LME budget of two
   pre-gate reads is the only conversational look tuning gets.
3. **The mixed register can pull both ways at once.** Casual-technical
   mass produces hub terms whose neighbor sets behave exactly like
   W3-C's evidence-mass table — dilution the conversational instrument
   will not pay. The re-anchored G2 floors are the detector;
   head-frequency skipping is the declared mitigation whose value the
   tuning trail may move.
4. **The memory budget is a real wall.** The segmented revision exists
   because this machine's memory ceiling binds before the corpus does;
   if the 200M-token stream still cannot run inside it, the park
   prices the machine honestly — the cap is not quietly cut to fit.

## What is not claimed

No criterion claim (§1). No default-ship and no opt-in ship — the
unit ends at a published read and, on PASS, a ship sentence put to the
owner in plain language. No W2 entry decision — that is the program
ladder's, conditioned on this read. No comparative claim against any
other memory system from a single-system artifact. No reuse of the
trained vectors anywhere outside the declared arms until a unit
declares it.
