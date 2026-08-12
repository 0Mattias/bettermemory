# Retrieval gold set

A committed, reproducible answer to one question: **how often does a
memory come back when you ask for it the way you would actually ask?**

Run it:

```sh
venv/bin/python bench/retrieval/run.py
venv/bin/python bench/retrieval/run.py --json
venv/bin/python bench/retrieval/run.py --pad-to 600   # above-threshold corpus
venv/bin/python bench/retrieval/run.py --pad-to 600 --prefilter both
```

## 4.0.0 — the semantic arm is gone from the product, 2026-08-09

The 4.0.0 purist strip removed the embedding lane from bettermemory: no
extras, no models, every ranker deterministic lexical code. The runner
follows the product — asking it for the `semantic` arm drops the arm
with a note instead of measuring a lane that no longer exists. The
semantic figures below stay as a dated record of the pre-4.0 engine,
kept because the margin they measure is the honest size of the problem.

What that record says the strip costs at this bench's scale: semantic /
asked beat lexical / asked by 25 points at recall@1 (60% vs 35%
unpadded; 60% vs 25% padded to 600), with the control arm tracking
asked — the lift was vocabulary, not corpus size. That gap is the
target of the deterministic-code retrieval campaign: closed in code, or
reported as open. No figure in this file claims it closed.

## Why this exists

bettermemory's strongest retrieval claim — recall@1 rising from 10% to
30% once an embedding model routes into ranking — **reversed a shipped
default** in 3.29.0. Until this directory existed, that number lived only
in a commit message and a handful of docstrings, and the store it was
measured on was cited inconsistently as 185 in two places and 190 in
four. Nobody outside the author's laptop could re-derive it.

A number that changes a default and cannot be re-derived is not
evidence. This replaces it with an artifact.

It does not supersede the original measurement's *value* — the decision
it drove looks sound. It supersedes its *standing*.

## Blind authoring

The corpus and the questions were written by **different authors that
never saw each other's output**. The only vocabulary they shared is a
kebab-case topic slug such as `why-integration-tests-run-serially`.

This is the part that matters. The usual way a retrieval benchmark
flatters itself is that whoever wrote the questions had the documents in
front of them, so the questions accidentally echo the documents' wording
and every ranker looks strong. Here the document author was told the slug
and nothing else; the question author was told the slug and nothing else,
and was explicitly instructed not to restate it in tidy keywords. Neither
could see the other's text, because they ran as separate agents with
separate contexts.

That closes the leak structurally rather than promising it was avoided.

**It does not make the set neutral.** The slugs were chosen by the
project author, the authors were language models, and the corpus is
synthetic. What blindness buys is narrow and specific: the questions
cannot be quoting the documents. Everything else remains open to
challenge, and PRs that replace this corpus with a harder or more
realistic one are the most useful contribution this directory can
receive.

## Corpus shape

- **20 gold documents**, one per slug, each with exactly one correct answer.
- **~170 distractors** on deliberately adjacent themes — near-neighbours
  in the same domains, so a gold hit has to beat plausible competition
  rather than an empty field.
- **Class mix matched to reality.** Roughly 64% of documents carry a
  mechanically checkable literal (a path, a `snake_case` identifier, a
  command, a `key = value` line) and roughly 36% are pure judgement with
  no literal at all. That ratio is not invented — it is what
  `bench/claims.py` measured on a real 194-memory store. A literal-dense
  corpus would flatter lexical retrieval and misrepresent what a store
  actually looks like.

## Arms

Two arms, chosen to mirror what a user actually gets rather than
artificial mode flags:

| arm | configuration | corresponds to |
| --- | --- | --- |
| `lexical` | `mode="hybrid"`, deterministic lexical ranking | every install |
| `semantic` | `mode="hybrid"`, embedding model | the pre-4.0 `embeddings` extra — **removed from the product; dated record only** |

Only `lexical` is runnable. The `semantic` row is kept because the
pre-4.0 figures below are quoted against it and a table that omits the
arm they name would make those rows unreadable; asking the runner for
it today drops the arm with a note.

Each is probed three ways:

| probe | what it is |
| --- | --- |
| `asked` | the question as a developer would actually type it months later |
| `requery` | the same need in concrete nouns — the second attempt after the first failed |
| `control` | the `asked` question with interrogative words stripped, content words kept |

**The control arm is what keeps the story honest.** If `control` scores
like `asked`, then the lift from `requery` is *vocabulary* — the caller
guessing words the document contains — and no amount of query-wording
guidance recovers it. If `control` scores like `requery`, the lift was
merely phrasing, and guidance is the cheaper fix. Reporting only
asked-vs-requery leaves that ambiguous, which is how a measurement turns
into a talking point.

## The threshold caveat, stated before the numbers

The default corpus sits **below** `_INDEX_THRESHOLD_DEFAULT` (500), so
retrieval scores the whole corpus. Above that threshold, production
prefilters through SQLite bm25 and every other ranker only *reorders*
that top-50 — meaning a semantic leg cannot surface a document bm25 never
nominated.

The 3.29.0 default flip was justified entirely by a below-threshold
measurement. That is the sharpest fair criticism of it, and reaching the
other regime turns out to take **two** knobs rather than one:

- `--pad-to N` grows the **corpus** past the threshold. Padding changes
  the corpus, so a padded run is reported as its own row and never merged
  with an unpadded one.
- `--prefilter on|both` picks the **code path**. Default `off` ranks the
  full corpus in-process. `on` drives production's own
  `handlers.search.resolve_search_pool`, so bm25 nominates the pool and
  corpus-IDF prices the terms.

The two padded artifacts published before 2026-07-30 turned only the
first knob, which measures *dilution*, not prefiltering: the pool was
still the whole corpus. They are honest upper bounds and nothing more.
The prefilter's own cost is measured below, and only in the paired form —
same queries, same store, same process, prefilter on versus off.

**The measurement refuses to run blind.** Seven separate paths return the
full corpus quietly — six inside the loader, plus the cap-starvation
reload one layer up in `resolve_search_pool` — and a run that hit any of
them would print full-corpus numbers under a `prefilter: true` heading.
The seventh is why the runner passes no scope, repo or worktree filter:
that reload is gated on one of them being set. So the
runner reads `resolve_search_pool`'s corpus-statistics provider — which
is attached if and only if the FTS path served the pool — for every
single query, and exits non-zero with an index census if any of them fell
back.

## Pre-registered predictions

Written **before the first run**, so a reader can diff prediction against
outcome rather than take a post-hoc story on trust. If these are wrong,
they stay here and the results section says so.

1. `lexical / asked` recall@1 lands **low, 0–25%**. The original
   measurement said 10%, and blind authoring should make this harder, not
   easier.
2. `semantic / asked` beats `lexical / asked` at recall@1 by **at least
   10 points**. This is the claim that reversed the default; if it does
   not reproduce directionally, the flip deserves re-examination.
3. `control` scores **within a few points of `asked`**, not of `requery`
   — i.e. the lift is vocabulary, not phrasing. This is the original's
   most interesting finding and the one most worth falsifying.
4. `requery` beats `asked` in **both** arms, with the gap **narrower** in
   the semantic arm — an embedding model is supposed to buy you exactly
   the vocabulary guess you would otherwise have to make yourself.
5. Padding above the threshold **compresses the semantic arm's
   advantage**, because bm25's nomination becomes the binding constraint.

## Results — v2 corpus (canonical)

`corpus.jsonl`, 180 documents: 20 gold plus **160 near-duplicate
distractors — eight per gold topic, same subsystem, different decision.**
This replaced the v1 distractor set, which was drawn from six broad
themes and proved too easy. Questions were deliberately *not*
regenerated, so v1→v2 measures corpus difficulty rather than two
unrelated question sets. Corpus size was held comparable (180 vs 188) so
IDF shifts are not a confound.

| arm | probe | recall@1 | recall@5 | v1 recall@1 |
| --- | --- | --- | --- | --- |
| lexical | asked | 35% | 60% | 40% |
| lexical | requery | 80% | 100% | 95% |
| lexical | control | 35% | 60% | 45% |
| semantic | asked | **60%** | 75% | 65% |
| semantic | requery | 90% | 100% | 100% |
| semantic | control | 60% | 70% | 60% |

Padded to 600: lexical/asked 25%, semantic/asked 60%.

**The hardening worked, and it was not enough.** Every arm moved down and
the competition is measurably real — on the `requery` probe, the best
distractor's overlap with the query rose from 0.37 to 0.44 and the number
of questions where the gold document out-overlaps every distractor fell
from 18/20 to 15/20. But `lexical / asked` only moved 40% → 35%, still
**three and a half times** the original store's 10%.

So the honest status is unchanged from v1, and the claim made when this
work started — that near-duplicate distractors would turn the floor check
into a replication — **was wrong.** It hardened the floor. It did not
reach the floor.

What remains different from a real store is not yet isolated. Candidates,
none tested: real memories vary far more in length, density and quality
than 1,100 characters of uniformly well-written synthetic prose; a real
store's topics overlap in messy ways rather than partitioning into twenty
clean subsystems; and the original's questions were written by the person
who wrote the memories, months later, which may be a harder probe than
any synthetic author produces. Until one of those is tested, **no
absolute number in this directory is comparable to the 185/190 figures.**

### What got stronger

Both findings that survive are within-corpus comparisons, and both are
now measured on two corpora of different difficulty:

- **The semantic lift is +25 points at recall@1 on BOTH corpora** (65 vs
  40 on v1; 60 vs 35 on v2). A constant advantage across a difficulty
  shift is a stronger result than either number alone, and it is the
  clearest evidence the 3.29.0 default flip was correct.
- **The vocabulary finding sharpened.** On v2, `control` (35%) equals
  `asked` (35%) exactly, against `requery` at 80%. Stripping
  interrogatives buys literally nothing; content words the document
  contains buy 45 points.

### Reproducing the superseded v1 figures

`corpus-v1.jsonl` is retained, and every result file records the
`corpus_sha256` it ran against:

```sh
venv/bin/python bench/retrieval/run.py --corpus corpus-v1.jsonl
```

## Results — what the prefilter actually costs, 2026-07-30

bettermemory 3.30.0, 12-core arm64 / Darwin 25.5.0. Two paired runs, raw
JSON in `results/`:

- `prefilter-above-threshold-2026-07-30.json` — the canonical corpus
  padded to 600, crossing production's real 500 threshold with no
  override.
- `prefilter-forced-180-2026-07-30.json` — the same 180 documents with
  `--index-threshold 100`, which reaches the same code path without
  adding a single filler document. Padding is the confound here: 420 of
  the padded corpus's 600 documents are deliberately off-domain, and bm25
  will never nominate them, so the cap has an easier job than it would on
  a store where all 600 contend.

**Lexical arm only.** The machine that produced these has no embeddings
extra installed; both artifacts record that in `notes`. Everything below
describes a default install. Production ranking is unchanged in 3.30.0,
so these numbers reflect shipped ranking.

Padded to 600 (production's own threshold, `prefilter_cap` 50):

| probe | recall@1 off → on | recall@5 off → on | gold reached the pool | mean pool |
| --- | --- | --- | --- | --- |
| asked | 25% → 30% | 60% → 60% | 95% | 50.0 |
| requery | 70% → 75% | 100% → 100% | 100% | 48.55 |
| control | 25% → 30% | 60% → 60% | 90% | 50.0 |

The 180-document corpus, threshold forced to 100, no filler:

| probe | recall@1 off → on | recall@5 off → on | gold reached the pool | mean pool |
| --- | --- | --- | --- | --- |
| asked | 35% → 35% | 60% → 60% | 90% | 50.0 |
| requery | 80% → 80% | 100% → 100% | 100% | 48.55 |
| control | 35% → 35% | 60% → 60% | 95% | 50.0 |

**The prefilter cost zero recall@5 in all six cells, and the corpus
property that makes that true is not "bm25 nominates everything".** It
does not: on the casual probes bm25 leaves the gold document out of its
top-50 on 5–10% of questions. Those questions are simply ones the
full-corpus ranker also failed to answer at k=5. The nomination ceiling
is real — 90% on `control` at 600 documents — and it sits *above* the 60%
either arm reaches, so it never becomes the binding constraint.

That is a much narrower finding than "prefiltering is free". It says
nomination is not the bottleneck **at this recall level**. On a corpus
where the full-corpus ranker reached 90%, a 90% nomination ceiling would
start cutting into it directly.

Two more things worth stating precisely rather than rounding off:

- **The +5 points at recall@1 in the padded run is one question out of
  twenty** — the smallest step this question set can resolve. Read it as
  "no measurable change", not as the prefilter improving retrieval. The
  forced-180 run, which has no filler to remove, shows no change at all.
- **The cap binds on every casual question and not always on `requery`.**
  `asked` and `control` come back with a full 50-document pool every
  time; `requery` averages 48.55 and nominates the gold document 20/20 in
  both runs. When the caller supplies content words the document actually
  contains, bm25 runs out of plausible candidates before it runs out of
  slots. The cap only presses on casual phrasing — which is exactly where
  the ranker was already the weak link, not the nominator.

### What this still does not measure

- **The semantic arm.** Unmeasured, and it is the arm the threshold
  caveat was always aimed at: an embedding model cannot rescue a
  document bm25 never nominated. This once read as the one remaining
  increment, closable by installing the `embeddings` extra and
  re-running `--pad-to 600 --prefilter both`. 4.0.0 removed the extra,
  so the increment is not available at any commit from 4.0.0 on and
  the gap stays permanently open on this instrument.
- **Realistic competition at 600 documents.** The padded corpus reaches
  the threshold with filler that cannot compete; the forced-180 run has
  genuine competition but only 180 documents against a 50-slot cap. A
  corpus of 600 genuinely contending documents is neither.
- **Anything at finer than 5-point resolution.** Twenty questions.

## Results — 3.43.0-engine re-run, 2026-08-08

The sections above measured 3.29.0 (corpus tables) and 3.30.0 (the
prefilter runs). Nine releases of engine change later — snippet
windowing on the match, ranking-input threading, and the 2026-08-08
repair that prices a compound query token's `_kebab_parts` off corpus
IDF under the prefilter — the same four invocations were re-run
unchanged at commit `7b63e07`, the engine the 3.43.0 release ships.
Raw JSON in `results/` (`*-2026-08-08.json`); artifacts now carry a
`provenance` block naming the version, commit, and machine that
produced them.

Unpadded corpus (`results/unpadded-2026-08-08.json`):

| arm | probe | recall@1 | recall@5 |
| --- | --- | --- | --- |
| lexical | asked | 35% | 60% |
| lexical | requery | 80% | 100% |
| lexical | control | 35% | 60% |
| semantic | asked | 60% | 75% |
| semantic | requery | 90% | 100% |
| semantic | control | 60% | 70% |

Padded to 600 (`results/padded600-2026-08-08.json`): `semantic / asked`
holds 60% at recall@1 while `lexical / asked` drops to 25%, so the
semantic margin widens there.

What held and what moved — read against n=20 per cell, where one
question is five points:

- **The headline margin reproduces exactly.** `semantic / asked` beats
  `lexical / asked` at recall@1 by **+25 points (60% vs 35%)** on the
  unpadded corpus — the same +25 the 2026-07-26 artifacts measured —
  and `control` still tracks `asked` in both arms, so the lift remains
  VOCABULARY, not phrasing, now as then.
- **The prefilter still costs zero recall@5, six cells of six**
  (`results/prefilter-above-threshold-2026-08-08.json`): every
  `recall_loss_at_5` reads 0.0, every `recall_loss_at_1` stays within
  one question, and `gold_nomination_rate` spans 0.9 to 1.0. The
  forced-regime run (`results/prefilter-forced-180-2026-08-08.json`)
  reads 0.0 at `recall_loss_at_5` in every cell as well.
- **Individual cells drifted by one to three questions.** The largest:
  `lexical / requery` recall@1 gave back three questions (95% in
  `results/unpadded-2026-07-26.json`, 80% now). recall@5 moved at most
  one question in any cell. Movement of this size on n=20 is single
  questions changing rank, not a regime change; every pre-registered
  verdict above keeps its grade.

## Results — 5.1 rescue-expansion lane, 2026-08-09 (ships OPT-IN)

The campaign lane the 4.0.0 section above promised ("closed in code,
or reported as open"), first installment — and the honest verdict up
front: **the lane ships opt-in, not default-on. Its own preregistered
held-out check killed the default.** What follows is the dev-set
record; the kill and its ablations live in `bench/longmemeval/`.

The lane: a document-frequency floor for listed discourse-filler
words plus a confidence-gated, down-weighted BM25 leg over synthesized
vocabulary (committed tables — `src/bettermemory/expansion.py`),
enabled per store via `[behavior] rescue_expansion = true`. Same four
invocations, re-run with the lane on; raw JSON in `results/`
(`*-2026-08-09.json`):

```sh
.venv/bin/python bench/retrieval/run.py --rescue-expansion on
.venv/bin/python bench/retrieval/run.py --rescue-expansion on --pad-to 600 --prefilter both
```

**This gold set was the lane's DEVELOPMENT set, stated plainly.**
Every parameter — the 0.60 confidence gate, the 0.7 leg weight, the
half-the-collection df floor, the 3-character expansion-term floor,
every word in every table — was tuned against these 20 questions, so
the numbers below are a dev-set fit, not a generalization claim. The
held-out check was `bench/longmemeval/`: predictions committed before
the run (PREREGISTRATION.md addendum 3), and the kill criterion fired
— macro recall@5 0.8770 against a 0.8935 baseline and a 0.8900 kill
line. The ablations split the lane cleanly: the filler df-floor alone
reproduces the LongMemEval baseline to four decimals (0.8935 — the
mechanism is corpus-shape-neutral), while the expansion leg carries
the entire regression: inflection variants of common chat verbs
("recommended" → "recommend") are promiscuous matchers in
conversational stores, the exact inverse of this corpus, where
expansion vocabulary is rare and discriminating. The experiment that
could earn the default back is named and unrun: df-gate the EMITTED
expansion terms against the pool (the promiscuous-variant class is
detectable in code), then re-preregister on both instruments.

**Round 2 preregistered that experiment and its pre-run kill fired.**
`bench/longmemeval/PREREGISTRATION.md` addendum 4 (2026-08-10) binds
both instruments and fixes τ from a df census alone — no recall input
at any stage. Its Gate 0 asks, before any gate exists in code, whether
an emitted term's document frequency separates the class that helps
this corpus from the class that harms the conversational one. It does
not: on the held-out set's regressed questions the emitted terms are
RARER than this gold set's (median df/N 0.027 against 0.036, a 0.74x
ratio where 5x was required). The two populations occupy the same
band, so no threshold can tell them apart. See that addendum and
`results/df-census-2026-08-10.json` for the distributions, and the
round-2 results section in `bench/longmemeval/README.md` for the
scored verdict.

**Round 3 takes the mechanism the kill named.** Addendum 5 (2026-08-10)
leaves the vocabulary alone and conditions the leg's VOTE: `_hybrid_fuse`
fuses by rank, so a leg contributes `_RESCUE_LEG_WEIGHT / (rrf_k + rank)`
however thin the evidence behind its rank-1 is. This gold set's leg
census (`results/leg-census-2026-08-10.json`) says the leg's own
separation predicts whether it is about to vote correctly — correct legs
sit at `margin_ratio` 0.189, incorrect ones at 0.047 — so a leg whose
rank-1 barely clears its rank-2 withholds its vote. θ = 0.12, derived
from this instrument alone; the held-out set is deliberately not read
before the run.

**Round 4 replaces that fixed threshold with a self-calibrating one.**
Round 3 gained the campaign's first held-out ground (macro@5 0.8770 →
0.8830) and missed on calibration: θ sat above this corpus's median leg
separation and below the held-out corpus's, so one constant was
aggressive where it came from and permissive where it was aimed.
Addendum 6 (2026-08-10) judges a leg against its OWN internal gap
structure — `gap[0] / mean(other gaps) ≥ 2.5` over the leg's top 12
candidates — so the comparison set is drawn from the store being
ranked. The derivation rule, not a value, is what is preregistered.

**Round 5 removes the proxy every earlier round was fitted to.**
`results/leg-labels-2026-08-10.json` labels each engaged leg by whether
its vote actually moved the gold document, and the answer reframes the
problem: of 39 engaged legs, 21 help, 3 hurt, 15 are neutral. Rounds 3
and 4 withheld 25 and 17 legs to catch those three, paying 9 and 7
helpful legs for it. Addendum 7 requires the leg's rank-1 to match at
least two synthesized terms — one is a coincidence, two agreeing is
evidence — which on these labels withholds 3 of 3 harmful legs and 0
of 21 helpful ones.

**P1a was preregistered against this gold set and killed at its gate.**
Addendum 8 asked whether store-derived co-occurrence (PPMI over the
collection being ranked) could replace the committed tables. Measured
here on 40 probes: the static tables emit 5.65 terms per probe at
precision 0.2743, and the best of 36 PPMI grid cells emits 9.78 at
**0.1253** — 0.46×, with no cell reaching parity. The signal is real
(PPMI finds 150+ gold terms the tables miss) but arrives with 10–65
terms per probe, and a rank-based fusion has no way to discount a leg
that imprecise. See `results/ppmi-census-2026-08-11.json`.

Unpadded corpus (`results/unpadded-2026-08-09.json`):

| arm | probe | recall@1 | recall@5 |
| --- | --- | --- | --- |
| lexical | asked | **50%** | **90%** |
| lexical | requery | 80% | 100% |
| lexical | control | 45% | 85% |

Against the 35%/60% this corpus has measured since v2: +15 at
recall@1 and +30 at recall@5 on the casual probe, control moving with
it (+10/+25) — and requery byte-stable at 80%/100%, which is the gate
doing its one job: a query the base ranking already answers
confidently never sees the rescue leg.

What moved, in the failure classes the misses were diagnosed into:

- **The filler class** — false winners riding "supposed"/"remember"/
  "know" matches that corpus rarity priced like discriminating terms.
  The df floor deflates them without deleting anything: hard-strip
  variants were measured first and rejected because filler words are
  sometimes the only hooks a casual query has.
- **The morphology class** — "splitting the repos" against a body
  that says "split"; the shipped stemmer folds plurals only, on
  purpose. Rule-generated -ing/-ed variants ride the rescue leg
  instead of widening the index.
- **The vocabulary class** — "toggles" vs "feature flags", "creds"
  vs "credentials", "hash" vs "digest". Clipping and synonym tables,
  small and general enough to read as ordinary engineering
  vocabulary.
- **Still open** — the deep paraphrase chasms (the billing-cutover
  and monorepo questions) where query and body share nothing any
  static table reaches, plus the within-cluster discrimination the
  near-duplicate corpus design makes brutal. These are the
  store-derived co-occurrence (P1a) and learned-rerank (P2) targets.

Padded to 600 (`results/padded600-2026-08-09.json`): asked 45%/85%
against the old engine's 25%/60% — the dilution regime keeps most of
the gain. The cost worth stating in the same breath: requery reads
70%/**95%** there, giving back one question at recall@5 that the old
engine's padded run held (IDF shifts under dilution push one query's
top-hit coverage below the gate, and the engaged rescue jostles a
previously-safe hit out of the top five). One question on n=20, in
the artificial-dilution regime only — the unpadded contract stays
byte-stable — but it is a real edge of the gate calibration, recorded
rather than rounded away.

The prefilter pairs (`results/prefilter-above-threshold-2026-08-09.json`,
`results/prefilter-forced-180-2026-08-09.json`) measure a new,
sharper version of the threshold caveat: **above the index threshold
the rescue re-ranks the bm25-nominated pool, and nomination still
runs on the caller's words** — a document only the synthesized
vocabulary would find never reaches the pool. Measured: the prefilter
now costs **15 points of recall@5 on the as-asked probe** (85% → 70%
above threshold; 90% → 75% forced-180) and **10 on the control probe**
(85% → 75% in both runs) where the old engine measured zero, while
recall@1 survives intact in every cell and requery is unchanged.
(Correction, 2026-08-10: this read "15 points on the casual probes",
plural, while quoting only the as-asked cells. Control lost 10, not
15 — the artifacts' own `prefilter_delta` rows say so.)

Two of these artifacts also carry a note in error, left in place
because they are receipts. `prefilter-above-threshold-2026-08-09.json`
and `prefilter-forced-180-2026-08-09.json` say "the prefilter=off half
re-measures v2-padded600-2026-07-26.json … so its rows double as a
regression check on the harness itself." With the lane on it does no
such thing: the off half ranks with the repairs and reads 45%/85%
as-asked against v2-padded600's 25%/60%. The reference artifacts
predate 5.1 and are lane-off by construction, so nothing in a lane-on
run reproduces them. `run.py` now gates that claim on the lane and
emits an explicit not-a-reproduction note instead; the published files
are unchanged, because editing a receipt to match a later reading is
the failure this directory exists to refuse. The pre-5.1 "prefilter costs zero recall@5" finding was a
property of a ranker whose reach ended at the caller's vocabulary; the
rescue's reach is wider than the nominator's, and the gap is now the
measured size of the next increment (nominate on query + expansion
variants), not a surprise waiting in a large store.

### P1e — a from-scratch dense embedding, censused against the same bar

P1a's kill was about an ESTIMATOR, not about the idea: raw co-occurrence
counts over 180 documents are noisy, and PPMI is famously worst exactly
there. The obvious successor is to factorize the same statistic instead
of reading it raw. That became admissible on 2026-08-11, when the owner
settled the WaC question — *"I never said 'no neural weights', I said no
sloppy bullshit. You can add neural weights as long as we built the
model from scratch."* Pretrained third-party weights stay banned; a
model this repository trains, from text this repository ships, with no
network anywhere, does not.

So `bench/embed_train.py` trains GloVe — same objective, written out,
pure Python because the dependency tree has carried no numpy since 4.0 —
and `bench/embed_census.py` scores its emitted terms against **P1a's
gate, quoted unchanged**: at least 1.0x the committed tables' precision,
measured identically on the same 40 dev probes.

**No corpus was staged.** Every training input is repository text that
was already committed, under this repository's own MIT grant, so the
census costs zero new bytes and no new fetch policy. `bench/` prose and
`tests/` are both excluded from the `repo` corpus deliberately — this
README names the instrument's paraphrase pairs in plain text and the
census's own test module cites its morphology examples, so a model
trained on either would be reading the answer key.

**Measured** (`results/embed-census-2026-08-11.json`); incumbent
0.2743 (62/226 terms) at 5.65 terms per probe, 95% CI [0.220, 0.336]:

| training corpus | tokens | query-token coverage | best cell | precision | x bar | terms/probe |
| --- | --- | --- | --- | --- | --- | --- |
| the store itself | 35k | 0.686 | k1, cos>=0.995 | 0.2712 | **0.989** | 2.95 |
| — the same arm at the incumbent's width | | | k2, cos>=0.995 | 0.2168 | **0.790** | 5.65 |
| repo prose (docs, changelog, docstrings) | 474k | 0.897 | k1, cos>=0.7 | 0.1015 | 0.370 | 9.85 |
| repo + store | 509k | 0.923 | k3, cos>=0.8 | 0.0788 | 0.287 | 22.52 |
| LongMemEval haystacks (conversational) | 966k | 0.877 | k1, cos>=0.9 | 0.1034 | 0.377 | 3.62 |

Reference: P1a's raw PPMI best was 0.1253, **0.46x**, at 9.78 per probe.

**The mechanism is not inert, and it does not pass.** Factorizing the
same statistic roughly doubles what reading it raw achieved — 0.46x to
0.989x at the tightest cell, 0.790x at the incumbent's exact term
budget. No cell passes, on any corpus, in either vector reading.

**The miss is not statistically resolvable, and that is stated rather
than argued around.** At the incumbent's own width the comparison is 49
hits of 226 against 62 of 226, p = 0.155; at the tightest cell it is 32
of 118 against 62 of 226, p = 0.950. Neither separates from the bar.
P1a's did: 0.1253 against 0.2743 is p < 0.001. **A gate is a point
comparison and this misses it at every reading — but "misses by 1.1% on
118 terms" is a different finding from "misses by 54% on 391", and
publishing the first as if it were the second would be the same
overclaim in the other direction.**

**More text makes it worse.** This is the census's sharpest result and
it runs opposite to the obvious prediction. The `repo` corpus is 13.5x
larger and covers 89.7% of the probes' query tokens against the store's
68.6% — better coverage, five times the vocabulary — and it lands at
0.370x. Adding the store back on top (`repo+store`) reaches the best
coverage of any committed arm, 92.3%, and the worst precision, 0.287x.
Coverage was never the binding constraint. **Topicality is**: the only
corpus that produces usable neighbours is the collection being ranked,
and that collection is 35k tokens.

**More training makes it worse too**, so "undertrained" is foreclosed
(`results/embed-sensitivity-2026-08-11.json`, store corpus, the declared
`dim=64, epochs=15` default against four alternatives):

| dim | epochs | final loss | best x | x at or above the incumbent's width |
| --- | --- | --- | --- | --- |
| 32 | 15 | 0.011314 | 0.957 | 0.847 |
| **64** | **15** (declared default) | 0.011431 | **0.989** | **0.790** |
| 128 | 15 | 0.011409 | 0.744 | 0.744 |
| 64 | 60 | 0.002876 | 0.496 | 0.496 |
| 64 | 150 | 0.000542 | 0.416 | 0.416 |

Ten times the epochs fits the co-occurrence matrix twenty times better
(loss 0.0005 against 0.011) and halves the precision. That is textbook
overfitting on 33,691 cells, and it means the headline is not a
lucky under-trained corner: the reported configuration is the one the
trainer declared as its default before any census ran, and the sweep
finds nothing better.

**What this retires.** Not "dense embeddings carry no signal" — they
carry roughly twice what raw PPMI did, and 32 of their hits are gold
terms the committed tables never emit. What it retires is the hope that
the ESTIMATOR was P1a's problem. The wall is the corpus: there is no
text that is simultaneously licence-clean, committable, in the store's
domain, and large enough to train on, because **the store IS the domain
and a personal memory store is tens of thousands of tokens.** Every
corpus that escapes that size constraint leaves the domain, and leaving
the domain costs more than the size buys.

### Two mechanisms built for this regime, and what they measured

The textbook family missing is not a reason to stop — the book was
written for corpora four orders of magnitude larger than a personal
memory store. `bench/embed_hybrid.py` proposes two mechanisms sized for
the regime the census actually described, and scores them against the
same unchanged bar (`results/embed-hybrid-2026-08-11.json`).

**A. The agreement rule — a count-dense hybrid. Hypothesis withdrawn by
its own measurement.** PPMI is unbiased and noisy; the factorization is
smoothed and biased; so a term both rank highly should be far likelier
to be a real associate. Emit only the intersection of the two top-k
lists, buying precision with recall — the trade the bar rewards.

Measured, it is **worse than the dense rule alone: 0.44x at the
incumbent's width against 0.79x.** The reason is the result. GloVe
factorizes the very matrix PPMI reads, so these are not independent
estimators — what they agree on is the high-count pairs, and high-count
pairs are the frequent, least discriminating terms. The premise was
independence and the data withdrew it.

**B. N-gram bridging — coverage without a bigger corpus.** An
out-of-vocabulary query token borrows a vector from the in-vocabulary
terms it shares character n-grams with, Jaccard-weighted over the best
five. No new training, no new parameters, no corpus — it exploits the
one thing English morphology guarantees, that 'splitting' and 'split'
share most of their characters, which is the same *morphology class*
the shipped lane attacks with hand-written rules.

It works, at what it was built for: **coverage 0.686 to 0.796, fifty
query tokens given vectors they had none for.** It does not move
precision. Recorded as a mechanism that does its job and is aimed at
the wrong quantity — the bar prices precision, and coverage is recall.

**Named and deliberately not tested here.** The agreement rule's
failure points somewhere specific: the two estimators should not be
intersected as ranks, because rank agreement selects for frequency.
The variant the diagnosis suggests is to keep the cosine THRESHOLD,
which is the one dial that demonstrably buys precision — 0.69x at
cos>=0.0 rising to 0.99x at cos>=0.995, unevenly (it dips at 0.7 and
again at 0.9 before recovering, so the trend is real and the curve is
not clean) — and to use the sparse signal only to *veto* candidates
whose PPMI is explained by frequency alone. That is a different
mechanism, it was not run, and it is written down here rather than
swept: a grid explored until something passes is how a census stops
being evidence.

### P1e census 2 — declared first, and it parks the lane

Census 1 left one mechanism named and deliberately unrun: use the sparse
counts to *veto* candidates rather than to confirm them. Census 2 ran
it, and ran it under a discipline the first census did not have —
**every cell, both readings of the bar, the primary cell, the readiness
criterion and the parking criterion were committed before a single
number existed**, in [`../P1E_CENSUS2_DECLARATION.md`](../P1E_CENSUS2_DECLARATION.md).
The enforcement record is the sha ordering: declaration `155d6f0`, then
the run commit. Artifact: `bench/retrieval/results/embed-census2-2026-08-11.json`.

The corpus question is closed and does not reappear here: the arm under
test is **per-store self-trained**, which is the product's natural form
— every install would derive its own vectors from its own store,
locally, with no external corpus and no network. Gutenberg is parked on
census 1's topicality wall, recorded in
[`../THIRD_INSTRUMENT.md`](../THIRD_INSTRUMENT.md).

**The declared prediction held, and it matters.** Reading B re-estimated
the incumbent at each challenger width by uniformly subsampling its
emitted terms. Across 25 widths the mean lands between 0.2736 and
0.2750 against the incumbent's own 0.2743 — unbiased, as declared,
because the committed tables emit an unordered set with no score a
narrower cut could exploit. **So the width comparison is not doing the
work, and census 1's narrow-cell readings stand.** Had this drifted, the
0.989x headline would have been an artifact of narrowing; it was
declared as a falsifiable check in advance rather than assumed.

**The veto works.** It improves precision in every one of the sixteen
`centred / nobridge` cells, and it does so by removing candidates rather
than by adding any:

| cell | veto off | veto on |
| --- | --- | --- |
| k1, cos>=0.95 | 0.1984 | 0.2311 |
| k1, cos>=0.98 | 0.2146 | 0.2419 |
| k1, cos>=0.995 | 0.2712 | **0.3214** |
| k2, cos>=0.995 | 0.2168 | 0.2524 |
| k3, cos>=0.995 | 0.1719 | 0.2128 |

That is the census's genuine positive: census 1's agreement rule made
things worse by intersecting ranks, and the diagnosis it produced — that
rank agreement selects for frequent, undiscriminating pairs — pointed at
a veto instead, and the veto behaves as the diagnosis predicted.

**Four cells clear the gate. All of them are narrow, and none of them
counts.**

| cell | precision | x bar | terms/probe | p vs incumbent |
| --- | --- | --- | --- | --- |
| centred, k1, cos>=0.995, veto | 0.3214 | **1.172** | 2.80 | 0.3691 |
| centred, k1, cos>=0.995, veto, bridge | 0.3167 | 1.155 | 3.00 | 0.4084 |
| raw, k1, cos>=0.995, veto | 0.2952 | 1.076 | 2.62 | 0.6938 |
| raw, k1, cos>=0.995, veto, bridge | 0.2870 | 1.046 | 2.88 | 0.8059 |

Every one emits roughly half the incumbent's 5.65 terms per probe, so
every one fails **Reading A** — the replacement reading, declared in
advance: *a source that matches the incumbent's precision while emitting
half as many terms is narrower, not better.* And none is statistically
distinguishable from the incumbent anyway; the p-values run from 0.37 to
0.81 on samples of about 110 terms.

**The primary cell fails.** `centred / k2 / cos>=0.99 / veto / no
bridge`, named by the declaration's stated rule before any run: 0.1953
at 8.45 terms per probe, **0.712x**, 95% interval [0.1565, 0.2409].
R1 fails (below the gate) and R3 fails (its interval's lower bound sits
under the incumbent's). R2 and R4 pass. The best cell anywhere in the
family at or above the incumbent's width is 0.858x.

**Verdict, by the criterion written before the numbers: the lane is
parked.** The primary cell misses R1 and no cell in the declared family
reaches the gate while emitting at least the incumbent's terms per
probe. That is the at-width reading failing family-wide, which is
exactly what the declaration said would retire the self-trained dense
source at personal-store scale.

**What the declaration bought.** Without it, the honest-looking headline
here is "the veto clears the bar at 1.172x" — a real number, from a real
mechanism, that would have been selected as the maximum of 128 cells
after the fact and would have quietly dropped the width it was won at.
The declaration's primary cell and Reading A were both fixed in advance
precisely so that number could not become the finding. **Naming the
cell first is what turns a grid into evidence.**

### P2a — the feature census parks the learned rerank, 2026-08-12

The natural mechanism after round 9: a learned linear rerank over the
head of the shipped ranking, feeding on the near-miss mass the
committed labels locate (a perfect top-5 rerank on the asked probes
lands exactly on the 60% bar). Before any preregistration,
[`../P2A_CENSUS_DECLARATION.md`](../P2A_CENSUS_DECLARATION.md)
(committed `3c26ea1`) froze seven features with a-priori directions,
the gold-vs-distractor pair rule on both corpora, and the R1/R2
criterion that alone could license the prereg. Artifact:
`bench/retrieval/results/rerank-feature-census-2026-08-12.json`, with
every recomputed dev rank validated against the base-leg labels and
every LongMemEval evidence rank against the round-9 sidecar.

**Zero R1 qualifiers. Every eligible feature anti-separates, on both
corpora, in the same direction.**

| feature (window 2..8) | dev share | LME share |
| --- | --- | --- |
| `leg_agreement` | — (83/83 ties) | — (322/322 ties) |
| `best_leg_rank` | 0.31 | 0.15 |
| `evidence_max` / `evidence_sum` / `coverage` | 0.23 | 0.13 |
| `recency` (LME only) | — | 0.54 |

The population reading is the finding: a near-missed gold's
distractors are not coincidental tokens the fusion over-trusted — they
match MORE query terms, sit higher in BOTH legs, and both legs list
every head candidate (leg agreement is a universal tie, so it cannot
discriminate at all). An oracle picking the best eligible feature per
probe recovers 10 of 169 LongMemEval near-misses — six percent, with
zero training generalisation spent. Lexically, the near-miss head is
DOMINATED, and a linear model over lexical features can only re-derive
the ranking that put gold behind.

**The family-shape footnote proves the clauses earned their place.**
In the one slice a shopper would have led with — dev asked probes,
window 2..5 — `best_leg_rank` reads 0.83/0.86. The declaration
pre-labelled exactly that slice non-qualifying, and LongMemEval
disposes of it independently: the same feature at the same window
reads **0.13** on the conversational corpus. The C1 sign-flip that has
now killed nine fusion-weight mechanisms claimed this one before it
was born, at the cost of one census instead of a round.

**Verdict, by the criterion written before the numbers: Q′ is empty,
Addendum 13 is not licensed, and P2a is parked at personal-store
scale.** What stays live is the door the same artifact measures: on
the padded corpus, production's own prefilter serves the gold in its
pool on 37 of 40 asked+control probes — the reach exists, and what is
missing is any NON-lexical opinion about what deserves the head. That
is Track B's question, and the two lanes were partitioned exactly so
this park would sharpen it rather than end the campaign.

### Dense scoring — the census parks the arc's last form, 2026-08-12

Track B asked Track A's open question at its sharpest point: emission
is dead (census 2), but can store-trained geometry at least RANK the
right document, on the far/absent pool no lexical rerank reaches?
[`../DENSE_SCORING_CENSUS_DECLARATION.md`](../DENSE_SCORING_CENSUS_DECLARATION.md)
(committed `7ab21f8`) fixed the 8-cell family — pooling × postproc ×
query-side bridging over the census-1 trainer at its declared
defaults — plus the reach bar, the routing rule, and the parking
criterion. Artifact:
`bench/retrieval/results/dense-scoring-census-2026-08-12.json`.

**Zero reach, family-wide.** No cell of the eight places a single
far/absent gold inside rank 10 of 180; the primary cell's medians on
that pool run 72–131 — chance territory. The preservation read fails
with it (hit@1 pool median 65), and the strata INVERT: the near-miss
pool dense-ranks worst of all (median 131), so no tie-breaker
fallback hides in the family either. The anti-gate-shopping clause
never fires because there is nothing to shop. The 20-question
LongMemEval glance — per-haystack training, the product shape, on
corpora twice the store's token mass — reads at chance: first
evidence in the top ten on 5 of 20 questions of ~45 sessions each.

Training is not the excuse: the model converges (final loss 0.011,
vocab 1104 over 35,025 tokens, deterministic by the trainer's own
`--twice` property). **The wall is information, not optimization.** A
personal store's worth of text does not carry enough co-occurrence to
place documents, only — as census 1 already measured — not enough to
name neighbours either. Emission and scoring are different mechanism
families with the same corpus, and the corpus is the ceiling.

**Verdict, by the criterion written before the numbers: store-trained
dense retrieval is parked at personal-store scale, in both of its
forms.** Both sealed instruments remain unspent — protected now
through P1a, rounds 6–9, censuses 1–2, and both of tonight's parks,
and never scored. What the two censuses bought, jointly, is the
campaign's sharpest negative result at its cheapest possible price:
the as-asked bar has no live mechanism at this store scale, measured
twice from opposite directions in one evening, with zero
preregistrations spent and the pre-4.0 record still standing as proof
the mechanism CLASS clears the bar the moment the corpus constraint
moves.

### Measured and killed on the way: RM3 as an equal leg

Pseudo-relevance feedback — top-k first-pass documents contributing
expansion terms, fused as an equal-weight third leg — measured
**25% recall@1 as-asked against the 35% base**, a ten-point
regression, while gaining five at recall@5. On a corpus built of
near-duplicate distractors the mechanism is legible: feedback
vocabulary is cluster-level, so the gold document's eight
same-subsystem siblings gain exactly as much as it does, and
whichever sibling is densest in subsystem vocabulary wins the leg.
What survived the kill is the shape constraint the shipped lane obeys:
expansion must be a gated, down-weighted RESCUE, never a peer ranker.

### MSC scale census — the wall holds in register and at scale, 2026-08-12

The dense park above was measured on this instrument's technical
corpus; the one lever it left standing was the corpus itself. The MSC
scale census moved it:
[`../MSC_SCALE_CENSUS_DECLARATION.md`](../MSC_SCALE_CENSUS_DECLARATION.md)
(committed `04a1907`, before any cell ran) fixed three mechanical
store scales over the MSC test split — every episode alone (`E1`), the
40-episode aggregate (`A40`, 1208 items over 200 sessions), and a
160-episode aggregate (`A160`, 4841 items over 800) — with probes
drawn solely from MSC's own per-turn persona annotations: 16591
declared lines, gold fixed to the annotated session, ambiguous lines
dropped by rule, and a turn-for-turn alignment gate between the
annotation files and the store construction. No authored gold, no
corpus text in the artifact:
`results/msc-scale-census-2026-08-12.json` (run `7247c8e`).

**Park, family-wide, on the bottom rung of the declared ladder.** The
`A40` far/absent pool — the 225 probes whose gold the shipped engine
ranks outside its top ten or not at all — is reached at ten by the
best of the eight cells on 16 probes (share 0.0711) and by the
primary on 13 (0.0578), against a licensing bar of 0.50 and a twitch
line of 0.25. Preservation fails alongside: on the probes lexical
already serves at rank one, the dense medians run 67–73 of 200
sessions — barely off chance — and the per-stratum medians are flat
everywhere, which is what a geometry that carries no document signal
looks like.

Two reads sharpen the dev park's finding rather than merely repeating
it. The `E1` anchor: among just the FIVE sessions of one speaker
pair, the primary cell puts the gold session first on 0.169 of probes
— below the one-in-five chance line — while the shipped engine does
it on 0.6599. Register alone defeats the store-trained model before a
single distractor pair enters. And the mass axis: `A160` doubles the
in-register training text and the primary's unconditional reach share
halves, 0.0722 to 0.0361 — each scale riding just above its own
chance rate (top ten of 200 vs of 800). More conversational text is
not the missing input; the information wall the dev census named
holds in register.

The overlap diagnostic says where that wall is. Probe tokens overlap
the gold session's content at 0.89 mean in the hit@1 stratum, 0.58 in
far, 0.03 in absent: the far/absent pool is a genuine
annotator-paraphrase vocabulary gap — C1's failure domain, measured
at store scale — and vectors trained on the store cannot bridge what
its text never co-locates. Worth recording as shape, no gate
attached: the shipped lexical engine serves the annotated gold at
rank one on 697 of 1385 probes and inside the top five on 1069, on
first-person paraphrase probes over a mixed-pair conversational
store.

**What the three censuses now say jointly: store-trained dense
retrieval is dead in every register-by-scale quadrant measured** —
emission and scoring on the technical corpus, scoring at
conversational store scale from five sessions to 800. The campaign's
door D closes negatively, at the cost of one declaration and zero
preregistrations; both sealed instruments remain unspent. The one
dense question left standing is the pretrained-weights doctrine
(door C) — the pre-4.0 record proves the mechanism class clears the
bar the moment the from-scratch constraint moves, and this census,
like the last, neither opens nor closes that door: it is the owner's,
not a measurement's.

## Results — v1 corpus (superseded), 2026-07-26

bettermemory 3.29.0, corpus of 188 (20 gold + 168 distractors), 12-core
arm64 / Darwin 25.5.0. Raw JSON in `results/`.

| arm | probe | recall@1 | recall@5 |
| --- | --- | --- | --- |
| lexical | asked | 40% | 65% |
| lexical | requery | 95% | 100% |
| lexical | control | 45% | 60% |
| semantic | asked | **65%** | 80% |
| semantic | requery | 100% | 100% |
| semantic | control | 60% | 85% |

Padded to 600 (above the index threshold): lexical/asked 35%,
semantic/asked 60%, lexical/requery 85%, semantic/requery 100%.

### Predictions scored

| # | prediction | outcome |
| --- | --- | --- |
| 1 | `lexical / asked` recall@1 lands 0–25% | **FALSIFIED — 40%** |
| 2 | semantic beats lexical on `asked` by ≥10 points | held, +25 |
| 3 | `control` tracks `asked`, not `requery` | held, decisively |
| 4 | `requery` wins in both arms, gap narrower with semantics | held, +55 → +35 |
| 5 | padding compresses the semantic advantage | mechanism FALSIFIED on the lexical arm; semantic half still unscored |

**Prediction 1 was wrong, and the direction matters.** The original
measurement put `lexical / asked` at 10%; this corpus yields 40%. The
gap is not noise — it is four times the original, on a set built to be
*harder*. **This corpus is easier than a real store, and no absolute
number here should be compared to the 185/190-memory figures.**

The diagnostic that establishes it, rather than assuming it: on the
`asked` probe, query→gold content-token overlap averages **0.25** while
query→best-*distractor* overlap averages **0.33**, and the gold document
out-overlaps every distractor on only **3 of 20** questions. So the
questions did not borrow their documents' vocabulary — blindness held.
What is too easy is the *corpus*: 20 gold topics spread across distinct
subsystems, with distractors clustered into six broad themes, so rare
discriminative terms survive IDF weighting far better than they would in
a real store where a dozen memories concern the same subsystem.

The fix attempted was to generate distractors as near-duplicates of each
*gold* topic — same subsystem, different decision — instead of same-theme
neighbours. **That is what the v2 corpus above is, and it did not close
the gap**: `lexical / asked` moved only 40% → 35%. The predicted
replication did not arrive; see the v2 section for what remains
untested.

Two findings do survive that caveat, because they are within-corpus
comparisons rather than absolute levels:

- **The semantic lift reproduces**, +25 points at recall@1 on the casual
  probe. The 3.29.0 default flip holds up on an independently
  constructed, blind-authored set.
- **The lift is vocabulary, not phrasing.** `control` (45%) sits beside
  `asked` (40%) and nowhere near `requery` (95%). Stripping interrogative
  words buys nothing, because the ranker already discards them; what
  `requery` buys is content words the document actually contains. This
  independently reproduces the original's most interesting claim, and it
  is the load-bearing argument for query-wording guidance *and* for
  shipping an embedding model to people who will not reword.

**Prediction 5's mechanism has now been tested, and it did not happen.**
The prediction was that bm25's nomination becomes the binding constraint
above the threshold. On 2026-07-30 the runner gained a `--prefilter` arm
that drives the production handler path, and on the lexical arm
nomination never binds: 90–100% of gold documents reach the top-50 pool,
against a recall the ranker only takes to 60%. Zero recall@5 lost in six
of six cells. See the 2026-07-30 section above for the tables and the
caveats.

**The semantic half of prediction 5 remains unscored** — the machine that
ran the prefilter arm has no embeddings extra, so the arm the prediction
is actually about has still never been through the real handler path.

What the pre-2026-07-30 padded runs measured was *dilution*, not
*prefiltering*: the runner ranked the full corpus even when padded, so
the mechanism the prediction named was never exercised. On the dilution
it did measure, the semantic advantage at recall@1 held at +25 (60% vs
35%) rather than compressing: adding 412 off-domain documents hurt the
lexical arm (40% → 35%) by shifting corpus IDF statistics, and left the
semantic arm flat.

Note that the padded figures moved once during authoring, for a reason
worth recording. The first filler vocabulary was plausible ops
terminology and shared eight terms with the gold documents —
`cutover`, `rollout`, `telemetry`, `cache` among them — so filler was
competing for gold probes and the run was partly measuring the filler
generator. `test_filler_shares_no_vocabulary_with_gold_documents` caught
it; the vocabulary is now deliberately off-domain and pinned. The
published padded numbers are from after that fix.

## What this does not measure

- Helpfulness. Recall is not usefulness; a retrieved memory can still be
  wrong or irrelevant.
- Staleness-verdict accuracy. Not measured here — `bench/rot` measures
  it, against ground truth taken from git rather than from a judge, over
  a pre-registered multi-repository corpus. `bench/claims.py` is the
  census that bounds it: how much of a real store is checkable at all.
- Any competitor. This is a self-measurement. Nothing here licenses a
  comparative claim.
- Real memories. The corpus is synthetic by necessity: a real store is
  private, and one that could be published would no longer be
  representative.
