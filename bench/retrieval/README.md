# Retrieval gold set

A committed, reproducible answer to one question: **how often does a
memory come back when you ask for it the way you would actually ask?**

Run it:

```sh
venv/bin/python bench/retrieval/run.py
venv/bin/python bench/retrieval/run.py --json
venv/bin/python bench/retrieval/run.py --pad-to 600   # above-threshold regime
```

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
measurement. That is the sharpest fair criticism of it, and it is why
`--pad-to` exists: it appends filler until the corpus crosses the
threshold so the other regime can be measured too. Padding changes the
corpus, so a padded run is reported as its own row and never merged with
an unpadded one.

The runner still ranks the full corpus even when padded, so a padded
result is an **upper bound** on the semantic arm, not a simulation of
production. Closing that gap means driving the real handler path, and is
the obvious next increment.

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
| 5 | padding compresses the semantic advantage | not properly tested |

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

**Prediction 5 is untested, and what padding did measure went the other
way.** The runner still ranks the full corpus when padded, so it measured
*dilution*, not *prefiltering* — the mechanism the prediction named was
never exercised. On the dilution it did measure, the semantic advantage
at recall@1 held at +25 (60% vs 35%) rather than compressing: adding 412
off-domain documents hurt the lexical arm (40% → 35%) by shifting corpus
IDF statistics, and left the semantic arm flat. A real test of the
prediction drives the production handler path so SQLite bm25 actually
nominates the candidate pool.

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
- Staleness-verdict accuracy. That is the headline differentiator and it
  has **no** accuracy measurement anywhere yet — see `bench/claims.py`
  for the bounds work that precedes it.
- Any competitor. This is a self-measurement. Nothing here licenses a
  comparative claim.
- Real memories. The corpus is synthetic by necessity: a real store is
  private, and one that could be published would no longer be
  representative.
