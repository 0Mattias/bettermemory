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
| `lexical` | `mode="hybrid"`, no embedding model | a default install |
| `semantic` | `mode="hybrid"`, embedding model | `bettermemory[embeddings]` |

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
  caveat was always aimed at: an embedding model cannot rescue a document
  bm25 never nominated. Running `--pad-to 600 --prefilter both` on a
  machine with `bettermemory[embeddings]` installed closes this and is
  the one remaining increment.
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

## Results — 5.1 rescue-expansion engine, 2026-08-09

The campaign lane the 4.0.0 section above promised ("closed in code,
or reported as open"), first installment: hybrid ranking gains a
document-frequency floor for listed discourse-filler words and a
confidence-gated, down-weighted BM25 leg over synthesized vocabulary
(committed tables — `src/bettermemory/expansion.py`). Same four
invocations, re-run unchanged; raw JSON in `results/`
(`*-2026-08-09.json`).

**This gold set was the lane's DEVELOPMENT set, stated plainly.**
Every parameter — the 0.60 confidence gate, the 0.7 leg weight, the
half-the-collection df floor, the 3-character expansion-term floor,
every word in every table — was tuned against these 20 questions, so
the numbers below are a dev-set fit, not a generalization claim. The
held-out check is `bench/longmemeval/`: predictions committed before
the run (PREREGISTRATION.md addendum 3), result published beside them.

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
now costs **15 points of recall@5 on the casual probes** (85% → 70%
above threshold; 90% → 75% forced-180) where the old engine measured
zero, while recall@1 survives intact in every cell and requery is
unchanged. The pre-5.1 "prefilter costs zero recall@5" finding was a
property of a ranker whose reach ended at the caller's vocabulary; the
rescue's reach is wider than the nominator's, and the gap is now the
measured size of the next increment (nominate on query + expansion
variants), not a surprise waiting in a large store.

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
