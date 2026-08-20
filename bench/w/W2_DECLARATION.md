# W2 — the contrastive rung: unit declaration, 2026-08-20

Successor unit to the W1 family under `bench/W_PROGRAM_DECLARATION.md`,
entered through the census ladder that ended in a license. The program
frame's ladder wrote W2's entry condition as "enters only if W1 moves
the dev number and plateaus short of the horizon," and W1 did neither:
it parked. What replaced that condition is not appetite but a sweep —
W1b fixed W1's register and measured the objective blind to the prize
relation (the committed six cross-form pairs read 0 of 6 inside the
emission window, `bench/w/results/w1b-geometry-2026-08-18.json`), and
W1C spent the last in-family knob and measured the same blindness at
triple the window (0 of 6 again, expanded family 6 of 41,
`bench/w/results/w1c-geometry-2026-08-20.json`). The frame's condition
assumed W1's objective class could reach the prize at all; the sweep
answered that it cannot at any width, which converts "W1 plateaus
short" into "W1's class is blind — a different objective is the rung."
The operative entry rule became the census ladder run under the
owner's standing continuations, and its top row fired: five of the
committed six prize pairs are supported on the Stack Overflow body
surface (`bench/w/W2_SO_CENSUS_RECORD.md`,
`bench/w/results/w2-so-census-2026-08-20.json`), and the census
declaration's §5 licenses exactly this document. The entry decision
that census left downstream is taken here, on §3's supervision-mass
argument.

The question W2 asks, exactly: does a contrastive objective over
human-labeled duplicate pairs — the one objective family two parks
located as the remaining from-scratch route to the vocabulary prize —
hold the cross-form bridges substitutability training cannot, and
convert them into the first measurable movement of the asked probe any
mechanism has produced on the rebuilt dev instrument?

The enforcement record is the sha ordering, unchanged: this
declaration, then the implementation commits, then the run commits,
then the record. Every byte this unit reads is already pinned or
already derived on disk under a pinned chain; no fetch happens under
this unit. Nothing may be added after the fact; a miss is published,
never renegotiated.

## 1. Baselines this unit is judged against — committed, quoted

Dev instrument (`bench/retrieval/run.py`, n=120, corpus of 1,080
documents, asked probe unless named):

- expansion off: recall@1 0.2167, recall@5 0.475 — identical in the
  prefilter-on and prefilter-off regimes
  (`bench/retrieval/results/i1-full120-off-2026-08-18.json`);
- static hand tables: recall@1 0.2583, recall@5 0.50 prefilter-on;
  0.2167, 0.5083 prefilter-off
  (`bench/retrieval/results/i1-full120-tables-2026-08-18.json`) — the
  I1 record's paired read over these two artifacts found the tables'
  old advantage not measurable at this scale, and that mechanism
  class is exactly what this unit must not emulate;
- requery, prefilter-on: recall@1 0.6917, recall@5 0.9333
  (`bench/retrieval/results/i1-full120-off-2026-08-18.json`) — the
  standing statement of what the corpus supports when the vocabulary
  gap is closed by a human;
- the pool fact this unit's consumption path is built on: in the
  default regime the engine's own candidate slice carries the gold for
  0.80 of asked questions at a mean pool of 50.0 (gold_nominated,
  `bench/retrieval/results/i1-full120-off-2026-08-18.json`), while
  depth five surfaces 0.475 — four golds in five are already
  nominated, and fewer than half surface. The asked-probe prize sits
  between rank five and the pool boundary.

LongMemEval (`bench/longmemeval/run.py`, full 500, default engine):
macro recall@1 0.5339, macro recall@5 0.9062
(`bench/w/results/gate-w3c-lme-incumbent-2026-08-17.json`) — the
incumbent G2's floors subtract from.

The program horizon (dev as-asked recall@1 60; LongMemEval macro@5
91.6) is carried, not claimed, and the 2026-08-09 success-criterion
bars are superseded ground awaiting the owner's restatement — this
unit's bars are set against the rebuilt instrument's committed
absolutes and its paired-resolution floor, not against the old ruler.
The honest interim sentence stands throughout: the bars are unreached
by our own means, published as such.

## 2. What is trained — the architecture, the objective, own code

A small transformer text encoder, this repository's own committed
code end to end: its own subword vocabulary trainer, its own forward
and backward passes, its own optimizer — numpy is the declared
bench-side build dependency exactly as W1b declared it, and nothing
heavier. No torch, no accelerator stack, no third-party pretrained
weights anywhere in the chain — not as initialization, not as a
teacher, not as a tokenizer. The program frame's clause 3 offered W2 a
relaxed reproducibility tier for accelerator nondeterminism; that
waiver is NOT taken. Training runs on CPU, single process, one
declared integer seed, BLAS thread count pinned and recorded in the
run meta; determinism is claimed at the recorded configuration, and G3
proves it mechanically at reduced scale and in CI.

Declared defaults (tunable only under §7's protocol; the values the
run actually used land in the artifact):

- **Tokenizer**: byte-pair encoding learned from the pretraining
  slice by this unit's own committed trainer, vocabulary 32,768,
  deterministic merge order (count-then-lexical ties), lowercased
  input, seed-free by construction; the vocabulary's sha256 lands in
  the run meta.
- **Encoder**: pre-LN transformer, 4 layers, model dim 192, 4 heads,
  feed-forward 768, GELU, learned positions, sequence cap 128 subword
  tokens, mean-pool over non-pad positions, L2-normalized output,
  fp32, seed 20260820. Architecture axes are tunable within hard
  caps: at most 6 layers, model dim at most 256, sequence cap in
  {96, 128, 192}, vocabulary at most 49,152 — every re-tune a new
  published run inside this section's budgets.
- **Stage A — pretraining, brief by design**: masked-token
  prediction, 15% of positions (80/10/10), tied output embedding, one
  pass over the register slice (at most two).
- **Stage B — contrastive tuning**: symmetric InfoNCE over in-batch
  negatives, batch 256 pairs, temperature 0.05, cosine similarity on
  the pooled vectors, at most 3 epochs over the kept pair set.
- **Optimizer**: this unit's own Adam (0.9 / 0.999), learning rate
  3e-4 pretraining and 1e-4 contrastive, linear decay, gradient clip
  1.0, deterministic batch composition from the declared seed.

Budgets, hard, an overrun a published park: the pretraining stage
reads at most 200,000,000 tokens under §3's per-source caps; wall
clock at most 72 hours per full derivation and at most 150 hours
aggregate across every training run this unit performs, on this
machine, single process; resident memory at most 14 GB. The ceiling is
deliberately above the W1 family's, sized under the owner's 2026-08-18
headroom ruling — every W1b budget held with the machine cool, and the
ruling authorizes the next unit to size against the real machine; the
standing compute ruling (train on this machine, no cloud) is
unchanged. One profiling run over a declared prefix, at most 2 hours,
is training-internal and artifact-recorded before the full run.

The artifact: the weights blob is an intermediate like W1's vectors —
large, binary, not committed, regenerable bit-for-bit from the pinned
inputs and the committed code; its sha256 and byte count land in the
run meta. What is committed is the run meta under `bench/w/artifacts/`
carrying hyperparameters, corpus pins, per-source token counts, every
stage's content hash (tokenizer vocabulary, token cache, pretrain
checkpoint, final weights, memory-vector cache), the serialized sizes
at fp32 and fp16, and two informational timing rows — a single-query
encode in the harness environment, and a pure-Python reference encode
on a reduced input. The sizes and timings are the raw material any
later clause-5 ship sentence must quote; this unit ships nothing.

## 3. The corpus — the slice reused, the pair set, the supervision mass

**Pretraining register.** W1b's §3 slice, byte for byte
(`bench/w/W1B_DECLARATION.md`): the casual-technical half and the
breadth half, 100M tokens each, per-source caps and internal orders
carried unchanged, read by the committed readers
(`bench/w/w1_corpus.py`, `bench/w/w1b_corpus.py`) over the pins in
`bench/w/corpora.json`. The derived token cache is a pure function of
the pinned bytes and the committed code; it is re-derived or
hash-verified before the run, and its sha256 lands in the run meta.
The polarity balance is deliberate and inherited: the breadth half is
the conversational anchor the LongMemEval bar watches.

**The pair set.** The census's derived file
`bench/w/corpus/derived/w2-so-bodies-2026-08-20.tsv` — 1,958,801,633
bytes, sha256 recorded in
`bench/w/results/w2-so-census-2026-08-20.json`, re-verified over the
exact bytes before any read, a mismatch stopping the unit. One line
per kept pair, 752,352 pairs from 758,058 deduped duplicate edges
(same artifact); the prose surfaces (columns prose_l / prose_r) are
the training text. Keep rule at training time: a pair drops if either
prose side tokenizes under 8 subword tokens; each side truncates to
the sequence cap. The file stays uncommitted (CC BY-SA per-post
attribution burden); the pins, the committed derivation chain, and
the recorded sha are the audit surface, and the file is regenerable
from them. No LLM-synthesized pair appears anywhere — the edges are
Stack Overflow's own human curation, and doctrine excludes synthetic
pairs by name.

**The supervision-mass argument the census record assigned this
declaration.** Four legs:

1. The objective is body-level, so all 752,352 pairs supervise every
   parameter — the bridge vocabulary rides the general
   phrasing-variation structure of the whole set, not only the rows
   where a probe pair was witnessed crossing.
2. The witnessed crossings prove the alternation exists in the
   signal, not at trace level but in the thousands: error/exception
   9,437 exclusive crossings, flag/boolean 33, revert/rollback 15,
   toggle/flag 13, undo/rollback 10, and the expanded family 21 of 45
   supported with library/package at 820 exclusive over 675 distinct
   targets (`bench/w/results/w2-so-census-2026-08-20.json`).
3. The same-side mass answers W1C's design fact directly. W1C
   demanded an objective that rewards co-occurring pairs that never
   substitute; a body-level contrastive loss does exactly that
   through both halves at once — alternation across an edge pulls the
   two phrasings together, and same-side co-occurrence (error/
   exception 8,727, library/package 1,892, flag/boolean 136 — same
   artifact) binds both forms into shared contexts. Substitution
   appears nowhere in the loss; nothing pushes a co-occurring pair
   apart for failing to substitute, which is precisely where the
   window objective died.
4. The floor honesty: rows witnessed at 10 to 33 crossings are thin,
   and whether they suffice to move geometry is not assumed anywhere
   below — it is exactly what §6's census prices, and the instrument
   bars of §8 do not depend on any single row.

## 4. The consumption path — a reranker over the engine's own pool

The fork the census record left open is resolved here, with the
reasons on the record:

- **No table is emitted.** Static expansion tables are the
  not-measurable class on the rebuilt instrument — the hand tables'
  paired read against off found nothing at n=120
  (`bench/retrieval/results/i1-full120-tables-2026-08-18.json`,
  `bench/retrieval/results/i1-full120-off-2026-08-18.json`), and
  W1C's learned wide-window table read 0.2583 / 0.5083 against off's
  0.2167 / 0.475, far from significance
  (`bench/w/results/w1c-dev-syn-2026-08-20.json`,
  `bench/retrieval/results/i1-full120-off-2026-08-18.json`). An
  emission unit would owe an argument why trained tables differ from
  hand tables on that instrument; no such argument exists, so the
  sidecar route is declined rather than re-priced.
- **The declared path is query-time reranking of the engine's own
  candidate window.** §1's pool fact is the mechanism claim: the gold
  is already nominated for four asked questions in five while fewer
  than half surface at depth five, so the prize sits inside a
  50-candidate window where a pairwise semantic score can act and the
  lexical score has already spent its evidence. This is the dynamic
  half of the program frame's consumption question, at the
  granularity where it is cheap: one query vector and at most fifty
  cached memory vectors per question.
- **Instrument #2 stays sealed** (§7). This unit is the
  learned-reranker family member the doctrine's reservation clause
  names, and it deliberately does not revive that question here.

**The blend, declared exactly.** Candidates: the first 50 of the
engine's production ordering — the prefilter slice where the index
threshold engages (mean pool 50.0,
`bench/retrieval/results/i1-full120-off-2026-08-18.json`), otherwise
the top 50 of the lexical ranking. Within the window, the engine score
and the encoder cosine (query vector against cached memory vector) are
each min–max normalized over the window; the final score is
(1−λ)·engine + λ·encoder; ties break by the engine's existing order;
the ordering below the window is untouched. λ ∈ [0, 1], default 0.5,
dev-tunable under §7. λ=0 reproduces the engine ordering exactly, and
every run asserts that identity per question before reporting.

**No engine change.** The harness (`bench/w/w2_measure.py`, in the
committed `bench/w/w1_measure.py` idiom) drives both instruments'
committed entry points and applies the rerank stage bench-side; the
memory vectors are encoded once per corpus from each memory's stored
text at the sequence cap and cached, and every result artifact records
the weights sha and the vector-cache sha it ranked with. Nothing under
`src/` changes in this unit.

## 5. The arms — two, sealed now

- **W2-rerank**: the blend above, at the λ the published dev trail
  settles on. The gating arm.
- **W2-pure**: the same window ordered by encoder cosine alone (λ=1),
  published beside every W2-rerank cell. It prices the encoder
  without the lexical floor and gates nothing.

No third configuration exists — there is no grid to shop.

## 6. The geometry census — the science read two parks hand forward

The read that made W1b and W1C decisive was geometric, and this unit
carries it forward unchanged in substance: after stage B, a term's
vector is the encoder's pooled output on the bare surface form, the
term inventory is the words of the pretraining token cache at
min-count 10 capped at 150,000 (W1b's vocabulary rule, carried), and
the probe (`bench/w/w2_geometry_probe.py`) imports the enumeration
and the emission-window criterion from the committed
`bench/w/w1c_geometry_census.py` and `bench/w/w1b_geometry_probe.py`
— committed six, expanded family, morphological and near-synonym
anchors, mutual-rank window unchanged. Readings are published beside
the committed window-5 and window-15 rows
(`bench/w/results/w1b-geometry-2026-08-18.json`,
`bench/w/results/w1c-geometry-2026-08-20.json`).

**The control row, expectation stated in advance.** timeout/expiry is
under the census support floor in the training data (2 exclusive
crossings, `bench/w/results/w2-so-census-2026-08-20.json`): pair
supervision cannot teach what it never witnesses, so this row is
expected to stay outside the window. If it enters anyway, that
indicts the read's specificity — geometry arriving without
supervision is not the mechanism under test — and the record must
treat it as a flag on the census, not as a bonus.

The census is training-internal (W1b §6's unlimited license,
carried), artifact-recorded, and published under every verdict.

## 7. The read protocol — what may be read, when

- **Training-internal signals** (loss curves, §6's census, neighbor
  spot-lists): unlimited, artifact-recorded.
- **Dev instrument**: unlimited during tuning; every read published,
  numbered, in `bench/w/results/` — the dev set is the declared
  tuning surface for λ and the training-side knobs, and the record
  shows what tuning saw.
- **LongMemEval**: at most TWO reads before the gate read, both
  published. The polarity instrument does not become a tuning
  surface.
- **The gate read**, once per arm: the run meta with every content
  hash is committed first, then both instruments run once against the
  frozen artifact — dev at n=120 in both regimes with the
  expansion-off and static-tables arms re-run paired in the same
  invocation and the requery row published beside them; LongMemEval
  full 500 with the expansion-off arm re-run paired. The primary dev
  invocation runs twice; the two results blocks must be identical
  (the run-determinism bar every unit since R1 has carried).
- **Sealed stays sealed**: nothing under `bench/heldout/` is opened.
  Instrument #2 stays reserved for P2a's confirmatory question; this
  unit does not revive it, and on a PASS the confirmatory read on the
  sealed instrument is a named owner door — one plain sentence, after
  the record, not before.

## 8. The bars — fixed now

- **G1, the dev bar** (gate read, default regime, asked probe,
  n=120, W2-rerank paired by slug against the same-invocation
  expansion-off arm): net wins minus losses at recall@1 ≥ **+10 of
  120** with McNemar exact p < 0.05, AND no measurable damage at
  recall@5 (a net of −6 or worse with p < 0.05 fails the bar). Ten is
  twice the paired-resolution floor the instrument's record fixed
  (six questions, five points — `bench/retrieval/I1_RECORD.md`): the
  smallest lift that cannot be a floor artifact, on the probe where
  every static mechanism has read not-measurable.
- **G2, the polarity bar** (gate read, LongMemEval full 500,
  W2-rerank arm): macro recall@5 ≥ **0.9012** AND macro recall@1 ≥
  **0.5239** — epsilons of 0.5 and 1.0 points against the committed
  incumbent 0.9062 / 0.5339
  (`bench/w/results/gate-w3c-lme-incumbent-2026-08-17.json`). The
  depth-five epsilon is half W1b's, deliberately: this leg carries a
  retreat knob the tables never had — λ is dev-tuned before the gate
  — and a mechanism that must spend more than half a point of the
  conversational instrument to buy its technical lift is the polarity
  lesson repeating, not a pass. The floors do not move whatever the
  paired reads say at gate time.
- **G3, the determinism bar — unconditional**: (a) the full
  derivation records a content hash at every stage (tokenizer
  vocabulary, token cache, pretrain checkpoint, final weights,
  memory-vector cache) in the run meta; (b) a reduced derivation —
  every cap divided by 100, the same code paths end to end — runs
  twice consecutively on this machine and every stage hash is
  identical across the pair; (c) the CI leg
  (`tests/test_w2_determinism.py`) drives committed fixtures through
  tokenizer, stage A, stage B and the blend via subprocess trains
  under `uv run --with numpy` (the family idiom), asserting
  byte-equality across two trains and the λ=0 identity on a fixture
  pool — no corpus bytes, no numpy import in the test venv. G3 must
  hold whatever G1 and G2 say.
- **Integrity reads** (any miss demotes the verdict to PARTIAL
  pending diagnosis): the same-invocation expansion-off dev arm
  reproduces 0.2167 / 0.475 exactly
  (`bench/retrieval/results/i1-full120-off-2026-08-18.json`); the
  static-tables dev arm reproduces its committed cells within two
  questions
  (`bench/retrieval/results/i1-full120-tables-2026-08-18.json`); the
  LongMemEval expansion-off arm equals the committed 0.5339 / 0.9062
  (`bench/w/results/gate-w3c-lme-incumbent-2026-08-17.json`); requery
  ≥ asked at recall@1 on every dev arm; the λ=0 identity holds on
  every question of every run.
- **Verdicts.** W2-PASS: G3 holds and W2-rerank clears G1 AND G2.
  W2-PARTIAL: G1 clears and G2 does not — published as the polarity
  result; no ship sentence is offered. W2-PARK: G1 missed, G3 failed,
  or any budget of §2 overrun. The §6 census is published under every
  verdict, and a park must say which half failed — geometry that
  never moved (the objective missed) or geometry that moved with an
  instrument that did not (the consumption missed). Those are
  different successors, and the record owes the distinction.

## 9. The constraint ledger

- Nothing under `src/` changes: no engine, tokenizer, stemmer or
  scoring edit rides in with the harness.
- The instruments are not edited: committed corpora, committed
  questions, the runners' entry points as they stand at the
  implementation commit.
- No third-party pretrained weights, embeddings, or LLM-synthesized
  text anywhere in the chain; numpy is the sole bench-side build
  dependency, as declared since W1.
- No fetch: every byte read is pinned in `bench/w/corpora.json` or
  derived from those pins by committed code; the derived pair file
  and every archive re-verify by sha256 before any read.
- The derived pair file and the weights blob stay uncommitted; every
  artifact that reads either records its sha256.
- Artifacts are dated, sha-ordered, and published whatever they say —
  including every tuning read the protocol admits.
- No changelog entry for declaration, implementation or run commits;
  nothing user-facing changes in this unit. A ship, if a PASS earns
  the owner's sentence, is its own unit and cuts its own release.

## Declared confounds

1. **Register transfer is the bet.** Training text is Stack Overflow;
   the dev instrument is developer-note prose and LongMemEval is
   conversational stores. The bridge relation is claimed to transfer;
   the claim is measured, not assumed — G1 is the detector for the
   win and G2 for the cost, and the polarity lesson says the cost is
   real until read otherwise.
2. **Supervision concentration.** error/exception carries 9,437 of
   the committed-class exclusive crossings against 33, 15, 13 and 10
   for the other four (`bench/w/results/w2-so-census-2026-08-20.json`);
   the thin rows may ride the fat one or be washed out by it. §6's
   per-pair rows expose which happened; no bar assumes the thin rows
   move.
3. **The pool is a ceiling.** In the default regime the window holds
   the gold for 0.80 of asked questions
   (`bench/retrieval/results/i1-full120-off-2026-08-18.json`); the
   fifth that is never nominated is unreachable by any rerank. The
   bar sits far inside the ceiling, and a pass says nothing about
   nomination — that is the prefilter's ledger, priced by I1.
4. **Truncation.** The sequence cap cuts long bodies, and a crossing
   witnessed by the census in a full body may sit below the cut at
   training time. The keep and truncation rules are fixed above
   before any training; the direction of the bias is unknown and
   named rather than adjusted for.
5. **Capacity.** An encoder this small may lack room for both
   registers of the slice; the loss curves and §6 are the internal
   detectors. The answer to a capacity miss is the published park and
   the architecture caps of §2 — not mid-unit growth past them.
6. **In-batch negatives are mostly easy.** Random batches contrast
   across topics, and the objective can buy its loss down on topic
   clustering without learning phrasing bridges. Temperature and
   batch size are the declared knobs; §6 is the detector that cannot
   be fooled by topic clustering, because its probe pairs share a
   topic by construction.
7. **Near-verbatim duplicates.** Some duplicate edges join
   near-identical texts — trivially easy positives that teach lexical
   overlap rather than bridging. No overlap-based down-weighting is
   applied (rule simplicity wins); the confound runs toward a null on
   the bridge classes, so a supported geometry read survives it.

## What is not claimed

- No criterion claim; the interim sentence stands: the bars are
  unreached by our own means, published as such.
- No ship, opt-in or default. On PASS, the ship question goes to the
  owner as one plain sentence under program clause 5 — with the
  inference-dependency question (pure-Python against numpy) and the
  artifact's measured sizes inside it — as its own unit.
- No Hugging Face publication; the publication plan's gates are
  untouched by a declaration.
- No revival of instrument #2; nothing under `bench/heldout/` is
  read.
- No comparative claim against any other memory system.
- No transfer claim from the Stack Overflow register to any store
  beyond what §8's instruments read; a geometry win with an
  instrument miss is a finding about consumption, not a capability
  claim.
