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

## Why this exists

bettermemory's strongest retrieval claim — recall@1 rising from 10% to
30% once an embedding model routes into ranking — **reversed a shipped
default** in 3.29.0, and until this directory existed that number
lived only in a commit message. A number that changes a default and
cannot be re-derived is not evidence; this replaces it with an
artifact. It does not supersede the original measurement's *value* —
the decision it drove looks sound. It supersedes its *standing*.

The doctrine arc that ran through this instrument is settled and lives
in its own records: the 4.0.0 purist strip removed the embedding lane,
the campaign measured every deterministic mechanism against the gap
and reported it open, door C readmitted third-party weights opt-in
under a six-clause contract, and the reentry ladder reproduced the
dated records exactly (`../DOOR_C_DECISION_BRIEF.md`,
`../R1_REENTRY_DECLARATION.md`, `../R3_DEFAULT_DECISION.md`). The
default install's engine never moved. This file states the
instrument and its current results; the campaign history is in those
documents, this file's git history, and the memory store.

## Blind authoring

The corpus and the questions were written by **different authors that
never saw each other's output**. The only vocabulary they shared is a
kebab-case topic slug such as `why-integration-tests-run-serially`.
The usual way a retrieval benchmark flatters itself is that whoever
wrote the questions had the documents in front of them; here neither
author saw the other's text, because they ran as separate agents with
separate contexts. That closes the vocabulary leak structurally.

**It does not make the set neutral.** The slugs were chosen by the
project author, the authors were language models, and the corpus is
synthetic. Blindness buys one narrow thing: the questions cannot be
quoting the documents. Everything else remains open to challenge, and
PRs replacing this corpus with a harder one are the most useful
contribution this directory can receive.

## Corpus shape

- **20 gold documents**, one per slug, each with exactly one correct answer.
- **160 near-duplicate distractors** — eight per gold topic, same
  subsystem, different decision (the v2 set; v1's six-theme distractors
  proved too easy and `corpus-v1.jsonl` is retained for verification,
  with every result file recording the `corpus_sha256` it ran against).
- **Class mix matched to reality.** Roughly 64% of documents carry a
  mechanically checkable literal (a path, a `snake_case` identifier, a
  command, a `key = value` line) and roughly 36% are pure judgement with
  no literal at all. That ratio is not invented — it is what
  `bench/claims.py` measured on a real 194-memory store. A literal-dense
  corpus would flatter lexical retrieval and misrepresent what a store
  actually looks like.

## Arms

| arm | configuration | corresponds to |
| --- | --- | --- |
| `lexical` | `mode="hybrid"`, deterministic lexical ranking | every install |
| `semantic` | `mode="hybrid"`, embedding model | the `embeddings` extra — removed in 4.0.0, restored opt-in by the door C reentry, 2026-08-13 |

With no extra installed the runner skips the semantic arm with a note;
a default install's ranking is unchanged.

Each arm is probed three ways:

| probe | what it is |
| --- | --- |
| `asked` | the question as a developer would actually type it months later |
| `requery` | the same need in concrete nouns — the second attempt after the first failed |
| `control` | the `asked` question with interrogative words stripped, content words kept |

**The control arm is what keeps the story honest.** If `control`
scores like `asked`, the lift from `requery` is *vocabulary* — the
caller guessing words the document contains. If `control` scores like
`requery`, the lift was merely phrasing and guidance is the cheaper
fix. Reporting only asked-vs-requery leaves that ambiguous.

## The threshold caveat, stated before the numbers

The default corpus sits **below** `_INDEX_THRESHOLD_DEFAULT` (500), so
retrieval scores the whole corpus. Above that threshold production
prefilters through SQLite bm25 and every other ranker only *reorders*
that top-50 — a semantic leg cannot surface a document bm25 never
nominated. Reaching the other regime takes two knobs: `--pad-to N`
grows the corpus past the threshold (padding changes the corpus, so a
padded run is its own row), and `--prefilter on|both` picks the code
path, driving production's own `handlers.search.resolve_search_pool`.

**The measurement refuses to run blind.** Seven separate paths return
the full corpus quietly, and a run that hit any of them would print
full-corpus numbers under a `prefilter: true` heading — so the runner
reads the pool's corpus-statistics provider (attached if and only if
the FTS path served it) for every query and exits non-zero with an
index census if any fell back.

## Predictions, and how they scored

Written before the first run; grades are final and the wrong ones stay
wrong. Full grading narratives: this file's git history.

| # | prediction | outcome |
| --- | --- | --- |
| 1 | `lexical / asked` recall@1 lands 0–25% | **FALSIFIED — 40% on v1** (the corpus is easier than a real store) |
| 2 | semantic beats lexical on `asked` by ≥10 points | held, +25 |
| 3 | `control` tracks `asked`, not `requery` | held, decisively |
| 4 | `requery` wins in both arms, gap narrower with semantics | held, +55 → +35 |
| 5 | padding compresses the semantic advantage | mechanism FALSIFIED both halves: nomination never binds the lexical arm (2026-07-30) nor the semantic arm (R1, 2026-08-13) |

Prediction 1's miss is the important one: v1 landed at 40% against the
original store's 10%, on a set built to be harder — and the v2
near-duplicate hardening only moved it to 35%. The corpus is easier
than a real store for reasons not yet isolated (clean topic
partitions, uniform document quality), so **no absolute number in this
directory is comparable to the original 185/190-memory figures**;
within-corpus comparisons are what the instrument supports. The
blindness itself held — measured on the `asked` probe, query→gold
content-token overlap averages 0.25 against 0.33 for the best
distractor, so the questions did not borrow their documents' words
(`results/unpadded-2026-07-26.json` and the corpus files carry the
inputs). One authoring incident is kept because a test now pins it:
the first filler vocabulary shared eight terms with the gold documents
and was competing for gold probes;
`test_filler_shares_no_vocabulary_with_gold_documents` caught it, and
the published padded numbers postdate the fix.

## Results — canonical (v2 corpus)

Measured 2026-07-26 at 3.29.0 (`results/unpadded-2026-07-26.json`),
reproduced unchanged at the 3.43.0 engine
(`results/unpadded-2026-08-08.json`), and reproduced byte-for-byte
through the restored opt-in arm at R1
(`results/r1-unpadded-2026-08-13.json`, with determinism repeat):

| arm | probe | recall@1 | recall@5 |
| --- | --- | --- | --- |
| lexical | asked | 35% | 60% |
| lexical | requery | 80% | 100% |
| lexical | control | 35% | 60% |
| semantic | asked | **60%** | 75% |
| semantic | requery | 90% | 100% |
| semantic | control | 60% | 70% |

Padded to 600: lexical/asked 25%, semantic/asked 60% — dilution hurts
the lexical arm through corpus-IDF shift and leaves the semantic arm
flat (`results/r1-padded600-2026-08-13.json`).

Two findings, each measured on two corpora of different difficulty and
across five engine releases:

- **The semantic lift is +25 points at recall@1 on both corpora**
  (65 vs 40 on v1; 60 vs 35 on v2) — the clearest evidence the 3.29.0
  default flip was correct, and the honest size of what the 4.0.0
  strip cost the as-asked probe.
- **The lift is vocabulary, not phrasing.** On v2, `control` (35%)
  equals `asked` (35%) exactly, against `requery` at 80%. Stripping
  interrogatives buys nothing; content words the document contains buy
  45 points.

Cell-level drift across the engine re-runs stayed within one to three
questions on n=20 (largest: `lexical / requery` recall@1, 95% → 80%
between `results/unpadded-2026-07-26.json` and the 2026-08-08 re-run);
every pre-registered verdict keeps its grade.

## Results — the prefilter's own cost

Paired runs, prefilter on versus off, same queries, same store
(`results/prefilter-above-threshold-2026-07-30.json`,
`results/prefilter-forced-180-2026-07-30.json`; reproduced at
3.43.0 in the `*-2026-08-08.json` siblings): **zero recall@5 lost in
all six lexical cells**, with gold nomination 90–100% against a
recall the ranker only takes to 60%. The finding is deliberately
narrow: nomination is not the bottleneck *at this recall level* — on
a corpus where the ranker reached 90%, a 90% nomination ceiling would
bind. The `+5` recall@1 in the padded run is one question out of
twenty, read as no measurable change.

The semantic arm's version of this question — the one the threshold
caveat was always aimed at — was unmeasurable for the whole pre-4.0
era and was answered by R1
(`results/r1-prefilter-above-threshold-2026-08-13.json`,
`results/r1-prefilter-forced-180-2026-08-13.json`): the asked probe's
semantic cells are **identical with the prefilter on and off** —
60%/75% in both regimes — because the arm's advantage lives in
top-five ordering of a pool whose nominator already reaches 19 of 20
golds.

**The rescue lane is the exception, and the erratum stands.** With
`rescue_expansion` on, the prefilter costs 15 points of recall@5 on
the as-asked probe and 10 on control
(`results/prefilter-above-threshold-2026-08-09.json`,
`results/prefilter-forced-180-2026-08-09.json`; a 2026-08-10
correction to the first published wording — "15 on the casual probes,
plural" — is folded in here, the artifacts' own `prefilter_delta` rows
being the authority). Nomination runs on the caller's words, so a
document only the lane's synthesized vocabulary would find never
reaches the pool. Those two artifacts also carry a self-check note in
error, left in place because they are receipts: they claim their
off half re-measures `v2-padded600-2026-07-26.json`, and with the
lane on it does no such thing (the off half ranks with the lane's
repairs and reads 45%/85% as-asked against the reference's 25%/60%).
`run.py` now gates that claim on the lane and emits a
not-a-reproduction note instead; the published files are unchanged,
because editing a receipt to match a later reading is the failure
this directory exists to refuse.

## The campaign record, 2026-08-09 → 2026-08-13

Each entry was preregistered or declared-first, ran against this
instrument, and has its full narrative in this file's git history and
its numbers in the named artifact. One line each, newest last:

- **5.1 rescue-expansion lane — SHIPPED OPT-IN** (2026-08-09): filler
  df-floor + confidence-gated expansion leg, `[behavior]
  rescue_expansion`; the held-out check killed default-on, and the
  polarity is corpus-semantic — technical stores gain, conversational
  stores are best served lane-off (`results/*-2026-08-09.json`;
  `src/bettermemory/expansion.py`).
- **Round 2, df gate** (2026-08-10): preregistered pre-run kill fired
  (`results/df-census-2026-08-10.json`).
- **Rounds 3–5, capping / self-calibration / evidence arc**
  (2026-08-10): three preregistered mechanisms, three kills at their
  gates; the 5.1.1 re-baseline underneath is
  `results/rebaseline-*-2026-08-10.json`
  (`results/round3-cap-*-2026-08-10.json`,
  `results/round4-standout-*-2026-08-10.json`).
- **P1a PPMI expansion** (2026-08-11): killed at Gate 0, 0.46×
  incumbent precision (`results/ppmi-census-2026-08-11.json`).
- **P1e from-scratch dense lane** (2026-08-11): two censuses, declared
  first; parked by its own criterion — sparse-PPMI veto and n-gram
  bridging survive as proven components
  (`results/embed-census-2026-08-11.json`,
  `results/embed-census2-2026-08-11.json`).
- **RM3 as an equal leg** (2026-08-11): measured and killed
  (`results/embed-hybrid-2026-08-11.json`).
- **P2a learned linear rerank** (2026-08-12): parked at
  personal-store scale by its own feature census — zero R1
  qualifiers; near-missed golds are lexically dominated
  (`results/rerank-feature-census-2026-08-12.json`).
- **Dense scoring form** (2026-08-12): closed negatively
  (`results/dense-scoring-census-2026-08-12.json`); the MSC scale
  census closed the scale question the same day
  (`results/msc-scale-census-2026-08-12.json`).
- **Register/df census** (2026-08-12): the wall is dev-shaped and the
  gap lives in-store (`results/register-df-census-2026-08-12.json`).
- **Requery census** (2026-08-13): feedback requery parks itself
  (`results/requery-census-2026-08-13.json`).
- **R1, the reentry** (2026-08-13): the restored opt-in arm
  reproduces the dated record exactly — +25-point margin, determinism
  repeat identical, prefilter-semantic cells answered
  (`../R1_REENTRY_DECLARATION.md`; `results/r1-*-2026-08-13.json`).

## What this does not measure

- Helpfulness. Recall is not usefulness; a retrieved memory can still
  be wrong or irrelevant.
- Staleness-verdict accuracy. `bench/rot` measures it, against ground
  truth taken from git; `bench/claims.py` is the census that bounds
  it.
- Any competitor. This is a self-measurement; nothing here licenses a
  comparative claim.
- Real memories. The corpus is synthetic by necessity: a real store is
  private, and one that could be published would no longer be one.
- **The recency knob.** The corpus is written in a single pass, so
  `_recency_factor` in `src/bettermemory/search.py` — the one ranking
  knob live by default, configured by `recency_boost_half_life_days`
  in `src/bettermemory/config.py` — sees ages that differ by
  microseconds across the whole store. Every published number
  describes ranking with that factor held flat.
- **`auto_scope`.** `build_store` writes every memory with no
  `Origin`, and `should_include_for_caller`
  (`src/bettermemory/origin.py`) treats a null memory origin as
  global, so every published recall figure describes a store where
  scope filtering structurally cannot bite.
