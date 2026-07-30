# Dedup breadcrumb floor

A negative result, kept because the reasoning is worth more than the change
would have been.

```sh
venv/bin/python bench/dedup/run.py                     # your own store
venv/bin/python bench/dedup/run.py --json
venv/bin/python bench/dedup/run.py --corpus bench/retrieval/corpus.jsonl
```

## The proposal

`find_similar` blocks a write at Jaccard >= 0.75 and, between 0.40 and
0.75, attaches the matched memory to the response as a `related`
breadcrumb. A 2026-07-30 audit found real paraphrase pairs in the
author's store sitting at 0.17-0.33 — below the floor, so they commit as
parallel entries with no hint that a sibling exists. Proposal: lower the
medium floor to 0.30.

The breadcrumb rides on **every** write, so a floor is not free. This
bench was built to price it before shipping it.

## Result — 2026-07-30: do not ship. Item closed.

The proposal fails for a reason that is arithmetic rather than empirical,
and the two ways to implement it fail differently.

### "Lower the floor" is inert above 0.25

`_pairwise_content_jaccard` does not return raw Jaccard. Once the
containment score (|intersection| / |smaller|) clears `MEDIUM_SIMILARITY`
it returns `max(jaccard, containment)`. Containment is never below
Jaccard, and tightening that: a pair whose Jaccard reaches *f* has
containment of at least **2f / (1 + f)**. That bound hits 0.40 exactly at
*f* = 0.25.

So every pair a 0.30 floor could newly admit has already been lifted into
the band by containment. The knob is disconnected. Measured on the live
store — the report floor swept with the containment gate left alone:

| floor | related pairs | mean per write | writes with none |
| --- | --- | --- | --- |
| 0.40 (shipped) | 248 | 2.147 | 73 |
| 0.35 | 248 | 2.147 | 73 |
| **0.30 (proposed)** | **248** | **2.147** | **73** |
| 0.25 | 248 | 2.147 | 73 |
| 0.20 | 253 | 2.190 | 72 |
| 0.15 | 397 | 3.437 | 43 |

Four identical rows, then the numbers move at 0.20 — one step past where
the closed form says the bound stops covering. The prediction and the
measurement break at the same place.

The one escape is `_CONTAINMENT_MIN_TOKENS`: under 8 tokens on the
smaller side containment never fires, so a short body can still land
between the floors. The live store has **zero** memories that short,
which is why the measured delta is exactly zero rather than merely small.

### Editing the constant is a different change, and a bad one

`MEDIUM_SIMILARITY` is read in three places — the reported floor, the
gate that lets containment fire at all, and `_CONTAINMENT_CEILING =
(HIGH + MEDIUM) / 2`. Setting it to 0.30 widens the containment gate,
which is where the damage is:

| | related pairs | mean per write | median | writes with >= 5 |
| --- | --- | --- | --- | --- |
| shipped (0.40) | 248 | 2.147 | 1 | 13 |
| edited to 0.30 | 1,366 | 11.827 | 8 | 170 |

5.5x the breadcrumbs. At a measured 419 B per breadcrumb (the 200-char
snippet plus its metadata) the extra 9.68 hits per write are **+4,056 B
attached to every single write**, roughly a thousand tokens, forever.

And it buys nothing the proposal asked for. Of the 2,236 hits the edit
adds, **zero** have a real Jaccard at or above 0.30 — the new floor its
own author was aiming at. Their actual overlap:

| min | p25 | median | p75 | max |
| --- | --- | --- | --- | --- |
| 0.015 | 0.067 | 0.091 | 0.119 | 0.228 |

A median of 9% shared vocabulary. Containment is generous exactly where
Jaccard is not — a short body against a long one — so what the edit
admits is long-document vocabulary coincidence, not paraphrase. Reading a
random sample of the added pairs confirms the shape: topically unrelated
long-form notes that share nothing but ordinary English and a few project
nouns.

The public `bench/retrieval` corpus reproduces the shape at its own
scale: no pairs in the band at any floor down to 0.20, and the single
pair the constant edit adds has a real Jaccard of 0.164.

### Why neither number rescues the pairs that motivated the item

The audit's pairs sat at 0.17-0.33. A 0.30 floor cannot reach the ones
below 0.30, and the ones at or above 0.30 are already surfaced by
containment. Checked rather than argued: the live store holds exactly one
pair whose real Jaccard lands in [0.30, 0.40), and it carries a
breadcrumb today (`target_band_already_surfaced`, 2 of 2 counting the
pair from both sides). The lexical scorer cannot see those
paraphrases at any floor that a store can afford; the knob that can is
the semantic dedup leg (`[behavior] semantic_dedup`, cosine 0.85 / 0.65),
which is scored on meaning rather than tokens. That is a separate
decision with its own cost, not a threshold tweak.

The `removed_related` (tombstone) leg reads the same constants and shows
the same flatness: 49 hits at 0.40, 49 at 0.30.

**Decision: `MEDIUM_SIMILARITY` stays at 0.40. No code change shipped.**
`tests/test_bench_dedup_floor.py` pins the reasoning so the constant does
not get "simplified" back into a knob by someone reading it as one.

## Method

- **No re-implementation.** Every arm calls the shipped `find_similar`.
  The floor sweep uses its existing `medium_threshold` parameter; the
  constant-edit arm rebinds the `search` module globals, the same
  technique `bench/rot` uses on `verify.py`. A bench that re-derives the
  scorer measures the re-derivation.
- **One scan, derived floors, verified.** The sweep filters a single
  O(n^2) pass at the lowest floor instead of running one pass per floor.
  `SimilarHit.similarity` is rounded to four places while the threshold
  test above it is not, so `_verify_derivation` re-runs the shipped floor
  directly and fails the run if the derived count disagrees.
- **Leave-one-out.** Future writes are not available, so each stored
  memory stands in for "a write of this content" scored against the other
  N-1. A store member's nearest neighbour is closer than a novel write's
  would be, so the per-write figures are a ceiling, not an expectation —
  which cuts against the proposal, since the noise cost is the number
  being overstated.
- **Read-only, and it stays that way.** The default corpus is the
  operator's own store. The runner parses files through
  `store._parse_memory_file` rather than constructing a `Store`, which
  would mkdir and chmod its root.

## The corpus is private; the artifact is not

`results/live-store-2026-07-30.json` was produced from the author's real
memory store, so the report is **counts only** — no bodies, no ids, no
scopes, no store path, not even the directory name.
`test_report_carries_no_memory_content` pins that: it plants a marker
string in a body and fails if the marker reaches the serialized report.
Anyone can reproduce the shape on a public corpus with `--corpus`, or on
their own store with `--store`.

The committed artifact composes two `--json` runs — the live store and
the `bench/retrieval` corpus — under one `decision` field. Re-running
either arm reproduces its half verbatim; only the wrapper is written by
hand.

A full live-store run took 5m39s at 231 memories (the public-corpus arm,
180 rows, took 1m07s). The cost is O(n^2) in memories with a large
constant — each pair re-tokenises both bodies — so it grows fast. This is
a bench, not a CI check.
