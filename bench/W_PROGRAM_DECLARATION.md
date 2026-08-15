# Lane W — the own-model program: frame, doctrine contract, corpus register, 2026-08-15

The program the 2026-08-15 owner rulings opened. First the embeddings
ruling: third-party pretrained weights are out permanently — the
objective is a frontier improvement built by this project, "build your
own better one or find a better way," and the 6.0.0 strip executed the
removal the same day. Then the corpus ruling, resolving the fork the
forward plan surfaced: **public corpora, Gutenberg unparked** — the
from-scratch condition governs the *derivation*; the data axis is
public and auditable. This document is the program frame those rulings
commissioned. It is not a unit declaration: nothing here fetches,
trains, or predicts a number. Each unit below runs under the
repository's standing mold — its own declaration first, sealed
implementation, then the run, sha-ordered, artifacts dated.

## Why this program is the campaign's continuation, not a consolation

The 2026-08-09 success criterion demanded both retrieval bars **on the
default engine**. `bench/R3_DEFAULT_DECISION.md` resolved that
criterion unmeetable-as-written because the only mechanism class that
reached the bars was condition-barred from the default install — the
borrowed arm could never be the default. An own-built artifact carries
no such bar: it is this project's own derivation, eligible for the
default engine on its own declared terms. Lane W is therefore the only
open route under which the criterion as originally written could ever
be claimed. The bars stand at recall@1 as-asked 60 on the dev
instrument and macro recall@5 91.6 on LongMemEval
(`bench/retrieval/results/r1-unpadded-2026-08-13.json`,
`bench/longmemeval/results/claude-mem-full500.json`) — carried here as
the program's horizon, not claimed by it, with the honest interim
sentence standing until an own-built artifact reaches them: the bars
are unreached by our own means, published as such.

## The doctrine contract for own-trained artifacts

WaC, applied to the case where this project trains the weights:

1. **From-scratch derivation.** The trainer is this repository's own
   committed code. No third-party pretrained weights appear anywhere
   in the chain — not as initialization, not as a teacher, not as a
   distillation source. The memory store's ruling record carries
   absence claims on the stripped modules; this document extends the
   same posture to the training chain.
2. **Auditable inputs.** Every corpus is a pinned snapshot: recorded
   source URL, retrieval date, sha256 over the exact bytes used, and
   a license verified and recorded **before admission** — all in the
   corpus register (`bench/w/corpora.json`, committed in skeleton form
   beside this document and filled at pin time). The network is
   touched only at explicit, named fetch steps, each announced in
   plain language before it runs — what is fetched, from where, and
   roughly how large — per the plain-sentence consent rule this week's
   reckoning produced.
3. **Reproducibility, honestly tiered.** W1's trainer is
   single-process, integer-seeded, and order-stable, so a retrain from
   the register reproduces the artifact bit-for-bit and CI can prove
   it cheaply on a reduced register. Where hardware nondeterminism
   enters (W2's accelerator training), bit-identity is not promised
   and not pretended: the committed emitted weights, the full
   derivation chain, and the evaluation receipts are the review
   surface — the same clause the charter already provides for trained
   forms. Inference is deterministic on the committed artifact in
   every tier.
4. **Receipts discipline unchanged.** Every unit is declared first
   with kill gates; misses are published, never renegotiated. The
   sealed instruments stay sealed. Instrument #2
   (`bench/heldout/data2/`) stays reserved for P2a unless a learned
   reranker unit formally revives that question under its own
   declaration.
5. **Default-install discipline.** An own-built artifact MAY ship in
   the default engine — that is the point — but only through its own
   declared unit, honoring the zero-dependency core: pure-Python
   inference over a committed artifact, or an explicitly decided
   dependency put to the owner as one plain sentence. Artifact size on
   the wheel is part of that unit's declaration, not an afterthought.

## The corpus register

`bench/w/corpora.json` is the register. Candidates named now, admitted
only when their pin lands with license verified:

- **English Wikipedia** (text of a dated dump; CC BY-SA) — prose
  breadth and technical vocabulary in one place.
- **Project Gutenberg, curated English subset** (public-domain texts;
  unparked by the 2026-08-15 ruling) — long-form prose depth.
- **Public paraphrase / duplicate-question pair sets** for the
  contrastive stage — candidates surveyed at W2 declaration time, each
  admitted or dropped on its verified license, named either way in the
  register.
- **Excluded by doctrine, not by price:** LLM-synthesized pairs
  (borrowed geometry through a side door — neither reproducible nor
  auditable), and any corpus whose license verification fails.

## The instruments

Existing and unchanged — the program grades against what already
grades: `bench/retrieval/run.py` (as-asked recall on the blind gold
set), `bench/longmemeval/run.py` (macro recall against the reference
corpus), and the store-shape polarity lesson carried from the
campaign (a mechanism that helps technical prose may cost
conversational stores; both instruments run on every unit read, and a
unit that wins one by costing the other says so in its artifact).

## The ladder

**W1 — subword vectors into the surviving expansion architecture.**
A committed skip-gram-with-negative-sampling trainer — this
repository's own, deliberately small — producing character-n-gram-
aware word vectors from the register's corpora. The vectors do not
rank anything directly: their nearest-neighbor terms replace the
hand-committed vocabulary tables inside the rescue-expansion leg that
already shipped, gated and down-weighted, and survived the campaign
(`src/bettermemory/expansion.py`). Gate shape, exact thresholds in the
unit declaration: beat the static-tables arm on the dev instrument
as-asked; bound the LongMemEval macro cost by a declared epsilon;
prove the retrain-hash determinism check. W1 is the fastest honest
test of whether own-trained geometry closes any of the vocabulary gap,
and its artifact is plausibly default-shippable under clause 5.

**W2 — a small contrastive dual-encoder, conditional on W1's read.**
A few transformer layers pretrained briefly on the register, then
contrastively tuned on the admitted pair set, trained on this machine
per the standing compute ruling. Enters only if W1 moves the dev
number and plateaus short of the horizon; its declaration owns the
architecture, the pair-mining rules, the reproducibility tier, and the
default-vs-artifact-package question.

**W3 — the invention branch, in parallel.** Non-neural bridges from
the same auditable corpora: corpus-scale PPMI (the store-scale kill in
the campaign record governs store scale, not this), a committed
paraphrase graph, and reuse of the components P1e proved (the
sparse-PPMI precision veto; n-gram bridging). Design work needs no
fetch and starts immediately; anything that runs, runs as a declared
unit like the others.

## What this declaration does not do

No fetch happens under this document. No training run, no predicted
number, no claimed bar. The owner doors that remain owner doors: each
default-ship decision (clause 5, one plain sentence each), any revival
of instrument #2's reservation, and the criterion's eventual
ratification the day an own-built artifact reads at the bars.
