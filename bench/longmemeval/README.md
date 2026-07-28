# LongMemEval — session-level retrieval on third-party labels

Build-order item (e). `bench/retrieval/` ends by disclaiming a
comparative claim; this directory exists to earn one, on a corpus and
against labels that neither this project nor claude-mem authored.

```sh
.venv/bin/python bench/longmemeval/run.py --limit 20      # smoke
.venv/bin/python bench/longmemeval/run.py                 # full, ~27 min
.venv/bin/python bench/longmemeval/run.py --json
```

The corpus is not vendored (265 MB). Fetch it:

```sh
mkdir -p bench/longmemeval/data && cd bench/longmemeval/data
curl -LO https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
```

**Read [PREREGISTRATION.md](PREREGISTRATION.md) first.** It fixes the
attribution rule, the metric, and five falsifiable predictions, and it
was committed before the corpus was downloaded. Two of those predictions
are scored **MISSED** below, one of them against a shipped default.

## The headline: parity, not victory

On third-party ground, against labels neither party authored,
**bettermemory and claude-mem retrieve about equally well.**

| system / arm | @1 | @5 | @10 |
| --- | --- | --- | --- |
| bettermemory lexical | 52.5% | 89.3% | 94.4% |
| bettermemory semantic | **56.2%** | **91.8%** | 95.6% |
| claude-mem lexical | 0.1% | 0.1% | 0.1% |
| claude-mem semantic | 54.2% | 91.6% | **96.9%** |

Best arm against best arm, macro recall@5: **91.8% vs 91.6% — a
+0.2-point difference.** At recall@1 we are ahead by 2.0. At recall@10
**they are ahead by 1.3.** Per question type the two trade places:

| question type | bettermemory | claude-mem | Δ | n |
| --- | --- | --- | --- | --- |
| single-session-assistant | 100.0% | 100.0% | — | 56 |
| single-session-preference | 96.7% | 96.7% | — | 30 |
| temporal-reasoning | 86.1% | 86.2% | −0.1 | 133 |
| knowledge-update | 98.1% | 95.5% | +2.6 | 78 |
| single-session-user | 97.1% | 92.9% | +4.3 | 70 |
| multi-session | 86.7% | **89.3%** | **−2.6** | 133 |

**This is the answer to build-order item (e), and it is not the answer
the build order was hoping for.** Item (e) existed to establish that
bettermemory out-retrieves claude-mem on neutral ground. It does not. It
ties. Any competitive claim this project makes has to rest somewhere
other than retrieval recall — see "What this means" below.

Run validity, since three earlier runs were discarded for exactly this:
`chroma: {"embedded": 124361, "complete": true}`, ingest shortfall
0.000%, **zero questions returning empty** in the semantic arm.

## Results — bettermemory 3.30.0, `longmemeval_s_cleaned.json`

500/500 instances scored. sha256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`.
Retrieval depth 200 items, collapsed to distinct sessions. Raw JSON in
`results/`.

Session-level recall@k, macro-averaged, **[ceiling]** = maximum
achievable at that k given questions with more evidence sessions than k:

| arm | @1 | @5 | @10 |
| --- | --- | --- | --- |
| lexical | 52.5% **[64%]** | 89.3% [100%] | 94.4% [100%] |
| semantic | **56.2%** **[64%]** | **91.8%** [100%] | **95.6%** [100%] |

Micro-averaged (evidence-weighted):

| arm | @1 | @5 | @10 |
| --- | --- | --- | --- |
| lexical | 43.6% | 86.7% | 93.2% |
| semantic | 46.6% | 89.0% | 94.4% |

**The @1 ceiling is 64%, not 100%**, because 324 of 500 questions need
two or more evidence sessions and only one slot exists. So lexical@1 is
82% of what is arithmetically reachable, not 52% of a possible 100.

### By question type, macro recall@5

| question type | lexical | semantic | n | Δ |
| --- | --- | --- | --- | --- |
| single-session-assistant | 100.0% | 100.0% | 56 | — |
| knowledge-update | 98.1% | 98.1% | 78 | — |
| single-session-user | 97.1% | 97.1% | 70 | — |
| multi-session | 84.9% | 86.7% | 133 | +1.8 |
| temporal-reasoning | 83.7% | 86.1% | 133 | +2.4 |
| **single-session-preference** | 73.3% | **96.7%** | 30 | **+23.3** |

Cost: lexical 328 s, semantic 1,286 s — the embedding arm is **~4×**
slower for +2.5 points pooled.

## Predictions scored

| # | prediction | outcome |
| --- | --- | --- |
| P1 | claude-mem's arm spread exceeds ours by ≥10 pts | **HELD — +89.0** (91.5 vs 2.5) |
| P2 | semantic beats lexical at @5 by >5 and <25 pts | **MISSED — +2.5** |
| P3 | multi-session ≥15 pts below the other types | **MISSED — 5.5 pts** (the good branch) |
| P4 | we do *not* win knowledge-update | **HELD — +2.6 pts** |
| P5 | ≥2% of offered rounds lost to dedup | **MISSED — 0.000%**, and badly posed |

### P4 held, and it is the prediction that earned its keep

P4 was written to stop a future overclaim by us: knowledge-update is this
project's differentiating axis, so the temptation is to point at that
column as proof the correctness machinery works. Measured: **98.1% vs
95.5%, a 2.6-point difference** — well inside the 10-point band that
pre-registered a non-win.

The reasoning behind it is now empirically confirmed rather than merely
argued. Recall@k asks whether the evidence session comes back. It cannot
ask whether the store *knows a fact was superseded*, which is the actual
claim. Both systems retrieve a superseded fact perfectly well. **Anyone
citing a knowledge-update recall number as evidence of bettermemory's
verification mechanism is misreading this benchmark**, and the
pre-registration said so before the number existed.

### P1 held, but read what it actually measures

Their arm spread is 91.5 points against our 2.5. That is not a statement
about retrieval quality — it is the phrase-query defect, and it is
config sensitivity rather than a headline. Their FTS **index is
correct**: a single content word retrieves the evidence session in 25 of
25 sampled questions. Any multi-word query is wrapped into an FTS5
phrase and requires contiguity, so it returns nothing, and stripping
stopwords does not rescue it. Report it as a defect in multi-word query
handling on their *fallback* path — Chroma-on is what ships by default,
and on that path they score 91.6%.

### P2 is the finding, and it is against us

`bench/retrieval/` measures a **+25-point** recall@1 lift from routing an
embedding model into ranking, on both its v1 and v2 corpora. That lift is
**the load-bearing evidence for the 3.29.0 default flip.** On
third-party ground it is **+2.5 points at recall@5 and +3.7 at recall@1.**

Part of that is a ceiling effect and saying so is fair rather than
exculpatory: at k=5 lexical is already at 89.3%, leaving 10.7 points of
headroom, so the arms cannot separate much. But at k=1 there are 11.5
points of headroom against the 64% ceiling and semantic still takes only
3.7 of them. **The +25 lift does not reproduce at anything like its
published magnitude, and ceiling effects account for only part of the
gap.**

This does not overturn the flip. It relocates it. The per-class table
shows why:

**The entire pooled lift is one class.** `single-session-preference`
moves +23.3 points; every other class moves between 0.0 and 2.4. Those 30
questions are precisely the ones where the question and its evidence
share no vocabulary — a user asking what they'd prefer, against a session
where they mentioned a preference obliquely. Everywhere a literal exists
to match on, lexical retrieval already wins and the embedding model adds
nothing.

That is **`bench/retrieval/`'s own conclusion, sharpened.** That directory
found the lift was *vocabulary*, not phrasing — `control` scored like
`asked`, and only `requery` (content words the document contains) moved
the number. This corpus says the same thing from the other direction: the
lift is confined to the class where no shared vocabulary is available.
So the mechanism replicated on independent ground even though **the
magnitude did not**, and a 30-question class carrying a pooled average is
exactly the shape that a single-corpus benchmark reports as a general
+25.

**What this changes:** `bench/retrieval/`'s +25 should be read as a
property of a corpus whose 20 gold topics sit in distinct subsystems, not
as the expected lift on arbitrary content. The flip still looks correct —
+23 points on the vocabulary-gap class is a real user benefit, and 4×
retrieval cost for it is a defensible trade — but "embeddings buy +25
points of recall" is not a claim this project should keep making.

### P3 missed on the good branch

Multi-session reasoning sits 5.5 points under the mean of the other
classes (84.9% vs 90.4%), not the ≥15 predicted. The pre-registration
committed in advance to reporting that as a genuinely good result, so:
retrieving evidence spread across two to six sessions degrades far less
than expected. Note what it is *not* evidence of — recall finding the
sessions says nothing about whether anything downstream can reason across
them.

### P5 was a badly posed prediction, not a finding

It predicted ≥2% of offered rounds would be lost to dedup. Measured:
**124,361 items written from 124,361 rounds offered, 0.000%.** The same
document that made the prediction also specifies ingest through
`Store.write`, the raw storage layer, which performs no dedup at all — so
the shortfall was zero *by construction*. The prediction was of an effect
whose mechanism it had disabled two sections earlier. Recorded as a flaw
in the pre-registration rather than dressed up as a result.

### P4 cannot be scored, and that is worth noticing

`knowledge-update` scores **98.1% in both arms** — effectively saturated.
P4 predicted we would *not* out-retrieve claude-mem on this class because
recall@k cannot see correctness. At 98.1% there is 1.9 points of headroom,
so the metric cannot separate two competent retrievers here at all. This
**strengthens** P4's reasoning: whatever bettermemory's knowledge-update
advantage is, **this instrument structurally cannot show it.** That axis
belongs to `bench/rot/`, and any writeup that points at a
knowledge-update recall number as evidence of the correctness mechanism
would be misreading its own benchmark.

## Data-integrity notes

- **13 questions repeat a session id** inside their own haystack. Deduped
  on ingest, counted in every result file, not dropped.
- **Depth truncation is negligible**: 0 questions at k=1, 2 at k=5, 9 at
  k=10 failed to yield k distinct sessions from 200 ranked items.
- **Zero abstention questions** exist in the distributed corpus, so one
  of the five abilities the paper advertises is unmeasurable here. Four
  are scored. See PREREGISTRATION.md addendum item 3.

## What this does not measure

- **Any competitor.** Not yet. The comparative claim is unearned until
  the claude-mem arms exist.
- **End-to-end capture.** Ingest bypasses `memory_write`'s dedup,
  transient screening and confirmation flow (`src/bettermemory/store.py:411`).
  This is store + retrieval.
- **The above-threshold regime.** Per-question stores hold ~249 items
  against a 500-item index threshold, so SQLite bm25 prefiltering never
  engages and the full store is ranked — the same gap
  `bench/retrieval/` declares about its own unpadded runs. **Neither
  directory has yet measured what a large real store would hit.**
- **Answer correctness.** No judged arm, by design: it requires a GPT-4o
  judge and an API key, which collides with the autonomy criterion.
- **Staleness accuracy.** See P4. `bench/rot/` owns that axis.

## What this means for the competitive case

Item (e) was commissioned to prove bettermemory out-retrieves claude-mem
on ground neither party authored. **It measured a tie.** That result is
kept as the headline rather than buried under the per-class rows where
we win three and lose one.

Two consequences follow, and both are more useful than a win would have
been:

1. **Retrieval is not the differentiator.** Two systems with entirely
   different architectures — memory files plus SQLite FTS5 and an
   optional embedding model, versus observations plus ChromaDB — land
   within 0.2 points of each other on 500 third-party questions. That is
   a strong hint that session-level recall on this kind of corpus is
   near saturation for any competent design, and that competing on it is
   competing on a solved axis.
2. **The correctness axis is where the claim has to live**, and this
   instrument is structurally blind to it (see P4). `bench/rot/` owns
   that, on a 30-repository corpus, where claude-mem scores a structural
   **N/A** because it has no `verified_at`, no `superseded_by`, and no
   lifecycle verb but DELETE. The defensible sentence is "we verify and
   here is the measured accuracy," not "we retrieve better."

## Three discarded runs, and why they are worth recording

Every one of these produced a number that flattered bettermemory, and
none survived:

| run | claude-mem @5 | why it was void |
| --- | --- | --- |
| first 40-question | 7.5% | 20 s fixed sleep; index barely built |
| full 500 (#1) | 54.1% | Chroma 57% built, 210/500 empty |
| **full 500 (#2)** | **91.6%** | valid — index 100%, 0 empty |

The middle row is the one to keep in mind. It would have published a
**+37.7-point win** over a competitor. The margin was almost entirely a
half-built vector index on this machine. It was caught because
`await_chroma_backfill` measures readiness and marks the run
`complete: false`, not because the number looked wrong — 54% was
perfectly plausible.

**The dominant failure mode in a comparative benchmark is not
mismeasuring yourself, it is mismeasuring the competitor in your own
favour.** Three for three here. The invalid artifact is retained as
`results/claude-mem-full500-INVALID-partial-index.json`.

## Next

1. **An above-threshold arm.** Per-question stores hold ~249 items
   against a 500-item index threshold, so bm25 prefiltering never
   engages. Neither retrieval directory has measured the regime a large
   real store would hit, and it is the most likely place for the tie to
   break in either direction.
2. **Enrichment parity.** claude-mem's `observations_fts` spans six
   columns its own pipeline fills by LLM extraction; this harness fills
   one. Their 91.6% is therefore a floor, not a ceiling — see
   PREREGISTRATION.md addendum 2.
3. Not a judged QA arm. It needs a GPT-4o judge and an API key, which
   collides with the autonomy criterion this project publishes against.
