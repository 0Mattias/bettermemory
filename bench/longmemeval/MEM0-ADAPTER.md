# mem0 adapter — reconnaissance and design

Notes for the mem0 arm of the comparative-breadth unit, written while it
is unbuilt; no published number comes from this file. Everything here
was probed against the **published artifact** (`mem0ai==2.0.18`,
Apache-2.0, PyPI), installed into the gitignored throwaway venv exactly
as a user installs it, and against the same corpus instance the
claude-mem recon used (`e47becba`), so the two recon trails stay
comparable.

Fetch/probe environment:

```sh
python3 -m venv .eval-venv && .eval-venv/bin/pip install -e . \
  'mem0ai==2.0.18' qdrant-client sentence-transformers
```

`.eval-venv/` is gitignored. `MEM0_TELEMETRY=False` must be exported
**before the import** — mem0 constructs its posthog client at module
load.

## There are two search stacks, and the base install runs the smaller one

`pip install mem0ai` retrieves with **semantic scoring only**. The 2.0.x
search path is written as a hybrid — vector search with 4x over-fetch, a
BM25 keyword leg, spaCy entity boosts, additively fused
(`mem0/utils/scoring.py::score_and_rank`:
`combined = (semantic + bm25 + entity) / max_possible`) — but both extra
legs disable themselves at init in the base install, each with a log
line naming its fix:

```
fastembed not installed - BM25 keyword search disabled. Install it with: pip install "mem0ai[extras]"
Failed to load spaCy full model: spaCy is not installed. Install it with: pip install mem0ai[nlp]
```

Both optional stacks are local and keyless, so the autonomy criterion
admits them. The claude-mem precedent (both Chroma states published side
by side) applies here unchanged: **base and extras go side by side;
neither alone is "the" mem0 number.** In the base install the fusion
divisor is 1.0, so the base arm's ordering is exactly the raw cosine
ordering — worth stating because it makes the base arm interpretable as
a pure MiniLM-vector baseline.

## The default threshold is this adapter's recency window

`Memory.search()` (mem0/memory/main.py:1379):

```python
search(query, *, top_k=20, filters=None, threshold=0.1, rerank=False, ...)
```

`threshold=0.1` drops every candidate whose **semantic** score is below
0.1 — the docstring's "Minimum score" gates the raw cosine *before*
hybrid fusion (scoring.py: "Threshold gates the semantic score BEFORE
combining"). Measured on the e47becba store: the default form at
top_k=200 returns **3 hits of 277 stored**. A benchmark that published
that would measure a list-truncation default, not retrieval — the same
class of error as claude-mem's 90-day recency window, caught the same
way: probe first, number never written.

The depth form is public API, no code surgery: `top_k=200,
threshold=0.0`. Two boundaries that must be declared beside any number:

- `threshold=None` does **not** disable the cut — `_search_vector_store`
  coerces None back to 0.1 ("Guard against None threshold (backward
  compat)"). 0.0 is the floor the validator admits.
- At threshold=0.0 the e47becba store returns 50 of 277 items (the
  neighbor instance 56 of 244): sub-zero-cosine items are unreachable
  through the public API. The per-question record already counts this
  honestly (`n_ranked`, depth-truncated@k), and 50 hits collapsed to 24
  distinct sessions of 53, so recall@5 and @10 stay measurable.

## API landmines, each probed rather than assumed

- **`limit=` is not a parameter.** It lands in `**kwargs` and is
  silently ignored; the real name is `top_k` (default 20). The 10-fact
  matrix's live adapter (tests/eval/live_adapters.py) passes `limit=k`
  — its committed 7/7 stands regardless because default top_k=20 ≥ its
  k=5, and it ran mem0ai 2.0.11 where the signature may have differed;
  the parameter should be re-verified before that harness's next live
  run.
- Top-level entity params are rejected in `search()`; the accepted form
  is `filters={"user_id": ...}` (add() still takes `user_id=` directly).
- **Unfiltered search refuses outright** (`filters must contain at least
  one of: user_id, agent_id, run_id`) — cross-question isolation is
  structural, not merely observed.
- mem0 eagerly constructs its LLM client even though `infer=False` means
  it is never called; a dummy key satisfies the constructor (the
  live_adapters.py trick, unchanged at 2.0.18).

## The attribution rule maps onto their schema

| | bettermemory | claude-mem | mem0 |
| --- | --- | --- | --- |
| ingest unit | one memory per round | one observation per round | one memory per round |
| round body | `rounds_of()` + date prefix | same texts | same texts, byte-identical |
| session link | side map, never in content | native `memory_session_id` | `metadata={"session_id"}`, round-trips on hits |
| isolation key | per-question store | `project` column | `user_id` = question id |
| capture pipeline | bypassed (`Store.write`), declared | bypassed (SessionStore), declared | bypassed (`infer=False`), declared |

The session id lives in qdrant payload metadata, which is not embedded
and not searchable content — the same property the side map enforces on
our side and the native column enforces on theirs. Hits promote
`user_id`/`role` to the top level and nest `session_id` under
`metadata`; the runner must read the metadata field only.

## No dedup, no recency — two prior traps that do not recur

- `add(..., infer=False)` performs no content dedup: the same text added
  twice under one user_id produced two ADD events and two retrievable
  copies. The forced-uniqueness workaround the claude-mem ingest needed
  does not apply; all arms receive identical corpora naturally.
- `score_and_rank` carries no recency, created_at, or decay term (grep
  of mem0/utils/scoring.py: zero hits), and the corpus dates live only
  in the body text, identically for every arm. Ingest order cannot leak
  into ranking.

## End-to-end proof, instance e47becba

Same instance and question as the claude-mem recon ("What degree did I
graduate with?", evidence session `answer_280352e9`):

- ingest: **277 rounds / 53 sessions — exactly the claude-mem arm's
  counts** (`sessions_written: 53`, `items_written: 277`)
- depth search (top_k=200, threshold=0.0): 50 hits → 24 distinct
  sessions, evidence session at distinct-session rank 1 (0-based) —
  recall@5 = 1.0
- isolation: 0 hits outside the filtered user's session set, in both
  directions, on top of the structural refusal above
- throughput ≈ 90 rounds/s on this machine → a full 500-question arm on
  the order of half an hour, comfortably inside one session

## Open questions before this can run

1. **Isolation at 500 users in one embedded qdrant collection.** The
   two-instance probe held; the cm precedent demands the leak probe at
   full scale against the live store (their read: 500 projects, 0
   leaks) plus a readiness check — exact per-user count equals rounds
   offered — before any question is scored. That is the half-built-index
   lesson, and it is the one that has bitten three times.
2. **The extras arm's exact install set.** `mem0ai[extras]` + `[nlp]`
   (fastembed + spaCy, with their model downloads) — versions pinned in
   provenance, and the BM25 normalization parameters
   (`get_bm25_params`) recorded as theirs, untuned.
3. **rerank.** `rerank=False` is the default and no reranker is
   configured in either keyless arm; check once whether any local
   reranker ships, then the flag stays at its default and the
   declaration names it.
4. **Whether claude-mem is re-measured at 13.15.2** (npm latest) or the
   dated 13.12.4 record stands — optional scope; the standing comparison
   stays honest either way, dated and disclosed.
5. **Store growth curve.** One collection holding ~124k vectors of 384
   dims is small for qdrant, but embedded-mode memory behavior at that
   size on a 19GB machine should be watched once during the first full
   ingest, not assumed.

## What must not happen

Writing a mem0 number before a declaration fixes the arms, the depth
form, the readiness and isolation gates, and the per-question record
shape. The threshold finding is exactly why this file exists: the first
number a naive run would have produced (3 hits, near-zero recall) would
have been a false accusation, and the discipline that caught it is the
one this directory already paid for three times.
