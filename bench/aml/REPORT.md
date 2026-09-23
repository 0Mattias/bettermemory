# bettermemory on the Agent Memory Leaderboard: technical report

System: **bettermemory** (github.com/0Mattias/bettermemory, MIT), entered in the
Open-source Methods division of the Agent Memory Leaderboard (AML) Cycle 2, Textual
and Coding tracks. Author: Mattias Rask. The adapter and the measurements below were
built with Claude Code (Anthropic) as the coding assistant; commits it co-authored
carry a `Co-Authored-By` trailer.

The evaluated version is the commit the endpoint serves, named in the submission. This
file describes that commit; the code it cites is under `bench/aml/` and
`src/bettermemory/`.

## 1. What is evaluated

bettermemory is a local-first memory store for AI coding agents: memories are Markdown
files with metadata, ranked by a lexical engine (`src/bettermemory/search.py`). The AML
entry is that engine behind AML's Add/Search contract, with no change to the engine's
ranking code. `bench/aml/service.py` is the whole adapter; `bench/aml/server.py` is its
HTTP face.

**No model is called on the Add or Search path.** There are no LLM calls, no embedding
models and no network calls; what Search returns depends only on the stored text and
the order it was written. AML's checklist says open-source entries are expected to use
gpt-4o-mini during Add; this entry calls no model at all, and neither does the
SQLite-FTS-Baseline entry on the Cycle 1 open-source board.

## 2. Add

- **Isolation.** One store per AML `user_id`, in a directory named by
  `sha256(user_id)[:32]`; Search reads only that store.
- **Unit.** One memory per *round*: a `user` turn and the `assistant` turn beside it,
  in either order. A turn with no such neighbour, or with any other role (a `system`
  prompt, for one), is stored alone, so it can never shift the pairing of the rounds
  after it (`service.round_spans`, `service.rounds_of`).
- **Rounds split across Add requests.** AML cuts a session into Add requests at 20
  messages or 2,000 words, so a request can end on a user turn whose reply opens the
  next request. That unpaired turn is written at once (so it is searchable
  immediately, as the contract requires) and recorded as the session's tail; when the
  next request of the same `session_id` opens with the other role, the whole round is
  written and the lone copy is hidden from Search. The stored rounds are then exactly
  the rounds the whole session would produce. `bench/aml/chunk_census.py` counts how
  often this matters: 35% of rounds on BEAM-1M, 7% on LongMemEval-S, 2% on
  LoCoMo-Refined.
- **Time.** When a message carries a `timestamp`, the round's body starts with
  `[YYYY/MM/DD (Day) HH:MM]` in UTC. AML's answer model sees only what Search returns,
  and temporal questions are unanswerable without the date. The source time is also
  kept in a sidecar, since a memory's own `created` field is storage time; it becomes
  Search's `created_at`, and the store's latest source time is the engine's clock.
- **Idempotency.** Completed `request_id`s are recorded with the write under the
  store's lock, so AML's retries of an Add write nothing twice.

## 3. Search

- **Query.** AML's `query`, with any multiple-choice `options` appended: a lexical
  ranker can use only the words it is given, and the candidate answers name what the
  question is about. The options never carry the gold label.
- **Ranking.** The engine's `hybrid` mode: reciprocal-rank fusion (k = 60) of a keyword
  scorer (term frequency and query-term coverage) and Okapi BM25 over stemmed tokens,
  in conversational mode, with a bounded recency factor relative to the store's latest
  source time.
- **What is returned.** Up to `top_k` (at most 100) rounds in rank order, within a
  budget of **90,000 characters** (`service.SERVING_BUDGET_CHARS`): ranked rounds are
  taken while they fit, the first is always served, and a round too long for what is
  left is skipped for a later one that fits. Each item is `{id, content, score,
  created_at}`.

## 4. Local reproduction

AML's answer model and judge run on AML's side, so every choice above was measured
locally first. `bench/aml/run.py` adds each dataset's sessions through the same
`service.MemoryService` the endpoint serves, searches at top_k = 100, answers with the
answer prompt AML publishes for that dataset (github.com/AML-memory/agent-memory-leaderboard,
commit 1b8142b, copied verbatim and pinned by hash in `tests/test_bench_aml*.py`), and
grades with AML's published evaluator. The reader is `openai/gpt-4o-mini`, AML's answer
model. The judge is `qwen/qwen3-14b`, the model AML's pipelines name, served through
OpenRouter. The numbers are an instrument for comparing configurations, not a
prediction of AML's scores.

The calibration arm is plain SQLite FTS5 (`bench/aml/fts.py`: the same rounds, an
any-word query, rank order), the closest local analogue of AML's SQLite-FTS-Baseline
entry.

| Dataset (questions) | bettermemory | SQLite FTS5 | Paired read | Artifacts |
|---|---|---|---|---|
| LongMemEval-S, dev split (150) | 0.6067 | 0.5333 | 15 vs 4 discordant, exact McNemar p = 0.019 | `results/dev-C0.json`, `results/dev-F0-fts-qwen.json` |
| LoCoMo-Refined (1,382) | 0.6469 | 0.6310 | 98 vs 76 discordant, p = 0.111 | `results/locomo-C0.json`, `results/locomo-LF.json` |
| BEAM-1M (700, rubric mean) | 0.3698 | 0.3701 | bettermemory minus FTS5 -0.0003, 95% CI [-0.0242, +0.0236] | `results/beam1m-C0.json`, `results/beam1m-BF.json` |
| PersonaMem v1, 32k tier (589, multiple choice) | 0.5042 | 0.5110 | 25 vs 29 discordant, p = 0.683 | `results/pm1-P0.json`, `results/pm1-PF.json` |

The LongMemEval-S split is fixed (`run.split`, seed 20260922, stratified by question
type). The 350-question holdout is not used to choose configurations. Paired reads come
from `bench/aml/compare.py`.

### Measured and not adopted

Each was run as an arm against the configuration above, with the same reader and judge,
and none was better on every dataset:

- showing each round's age relative to the newest conversation (the sign reversed
  between datasets)
- source-time and session ordering in place of rank order (worse on LongMemEval-S)
- padding with neighbouring rounds
- capping the number of rounds served (worse on LoCoMo-Refined, whose turns are short)
- eliding assistant lines that share no word with the question
- one memory per message in place of per round (it served less labelled evidence on
  BEAM-1M, measured offline)
- a 150,000-character budget (worse on PersonaMem v1, 0.4805 vs 0.5042, 14 vs 28
  discordant, p = 0.044, and on LongMemEval-S dev, 0.5800 vs 0.6067, p = 0.388;
  `results/pm1-P150.json`, `results/dev-C150.json`)
- reciprocal-rank fusion with the FTS5 ranking (`bench/aml/fused.py`): it serves slightly
  more labelled evidence, but that does not turn into more correct answers

The measurements are in `bench/aml/results/` and the commit messages that added them.

## 5. Reuse and attribution

- **bettermemory's engine and the adapter** are original work under the MIT license.
  No other memory system's code is used.
- **SQLite FTS5** (public domain) is used only as a local calibration arm and is not
  part of the entry.
- **AML's prompts and evaluators** are copied into the local harness only, to grade
  local runs as AML grades them; they are not on the Add or Search path.
- **Benchmark data** (LongMemEval, LoCoMo-Refined, BEAM, PersonaMem, CL-bench,
  ScriptMem) is downloaded locally under each dataset's own license, used only for
  evaluation, and never committed or redistributed. CL-bench is evaluation-only by
  license. ScriptMem's public release withholds its scripts, so it is not reproduced
  locally.

## 6. Deployment

The endpoint runs `bench/aml/server.py` (Starlette under uvicorn, one process) behind
Caddy for TLS, on one AWS t4g.large instance. `server.serving_service` is the only
place the served configuration is built.

- **Auth.** Add and Search accept `Authorization: Bearer <key>`, `Authorization:
  Token <key>` or `X-Api-Key: <key>`. `GET /health` is unauthenticated.
- **Errors.** A malformed payload gets a 400, never a retryable 500.
- **Speed.** Search keeps each store's token streams between requests and skips the
  display snippet it never serves. Ranking is unchanged, and a test pins that.
- **Evaluation data.** It is not logged (access logs are off, Caddy discards its
  request log) and is deleted within 30 days of a run, as the contract requires.

## 7. Limitations

- **Stand-in grading.** Local numbers use a stand-in judge and OpenRouter's serving
  of both models; AML's own runs will differ in level.
- **Local coverage.** Four of the seven textual datasets have full local runs.
  PersonaMem v2 (5,000 questions) and CL-bench have loaders and graders in
  `bench/aml/ds_*.py` but no paid run yet;
  ScriptMem cannot be reproduced from public data, and CAMBench is unreleased.
- **Coding track.** It runs the same configuration as the Textual track. There is no
  coding-specific tuning, because nothing about the Coding track can be measured
  locally.
