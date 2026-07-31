# Evaluating memory

Published memory benchmarks (LongMemEval, LoCoMo, HaluMem, MemoryArena)
measure retrieval recall: given a query, did the system surface the
relevant stored fact? That leaves two questions unanswered:

1. Did the retrieved memory actually shape the reply, or was it pulled
   in and ignored?
2. When the model didn't retrieve, should it have?

bettermemory's telemetry (`memory_record_use`, `memory_audit_turn`, the
auto/explicit `applied` split, `claim_excerpts`) exists to make both
answerable. This document defines the three metrics it computes.

## The three rates

### `memory_helped_rate`

Of all retrievals, what fraction were attested as load-bearing?

- Numerator: `record_use` events with `outcome="applied"`, `auto=false`,
  and non-empty `claim_excerpts`.
- Denominator: per-event retrieval occurrences across `memory_search` /
  `memory_list` / `memory_show` in the window, counted by memory id. A
  memory surfaced N times counts N; there is no per-turn dedup.

Two attestation tiers feed the numerator:

- `attribution="model"`: the model called `memory_record_use` with the
  load-bearing excerpt.
- `attribution="hook"`: the Stop hook matched a body sentence of a
  retrieved memory (≥6 tokens, ≥30 chars, stopword-filtered) against
  the reply — verbatim (case/whitespace-normalised), or by
  distinctive-token containment (≥60% of the sentence's distinct
  content tokens appear in the reply, ≥4-token floor), catching
  paraphrases that keep the memory's vocabulary. Deep rewordings
  still slip through; thresholds precision-tuned so false positives
  stay rare.

The bare auto-fallback (`attribution="auto"`, no excerpt) is excluded.
The CLI's `applied_explicit` count is everything non-auto; recompute
from the raw events' `attribution` field for a stricter model-only cut.

A low rate is **ambiguous by construction**, and this metric cannot
disambiguate it. Either the ranker is firing on noise the model
silently ignores, or the retrievals did help and simply left no
attestation behind — the numerator counts only explicit,
claim-excerpt-backed endorsements while the denominator counts every
retrieval occurrence, so the rate is a floor, not an estimate. Read it
against `endorsement_rate` (which isolates the attestation half) and
against the trend rather than either column alone. Do not read a low
rate, on its own, as "retrieval isn't working."

### `endorsement_rate`

Of retrievals tagged `applied`, what fraction had attestation (model or
hook) versus the bare auto-fallback?

- Numerator: per-memory-id references in `applied` events with
  `auto=false` (one event applying three ids contributes three).
- Denominator: per-memory-id references in all `applied` events.

A low rate means nothing produced evidence the retrievals shaped a
reply. The per-memory companion in `memory_health` is
`cold_endorsement_memories`: distinct memories with `retrieval_count >=
5` and zero explicit applies — retrieved often, never visibly used.

### `silent_miss_rate`

Of audited turns, what fraction were silent misses — the ranker would
have surfaced a high-relevance hit and the model made no
`memory_search` / `memory_show` / `memory_list` call?

- Numerator: `search_miss` events emitted by `memory_audit_turn`.
- Denominator: `turn_audited` events, excluding `no_signal` verdicts
  (probe declined: empty store, nothing relevant). Those are reported
  separately so a probe stuck at "declined" can't read as a healthy 0%.

The threshold rule is versioned (`THRESHOLD_RULE_V1 = "v1_top1_high"`)
and recorded on every event.

**Read this rate as a floor, not a health score.** The v1 verdict fires
on a *coverage* label — the top hit matched ≥75% of the query's
distinctive tokens — and that denominator grows with the user's message.
Measured over 195 audited turns on a 185-memory store, the label's
"high" rate by message length:

| user message chars | 0–40 | 40–80 | 80–150 | 150+ |
|---|---|---|---|---|
| v1 `high` | 45% | 32% | **0%** | 3% |

So a long turn is close to unflaggable, and long turns are the ones most
likely to have needed memory. A low `silent_miss_rate` is therefore
substantially a statement about message length, not about retrieval: it
can't distinguish "the model rarely misses" from "this user writes long
prompts." A **rising** rate is still meaningful; a low one is not
evidence of health.

The shadow `relevance_v2` label was built to close exactly this blind
spot and does not: adding an absolute matched-token floor takes the same
buckets to 47% / 63% / 83% / **100%**, trading a length-blind rule for a
length-credulous one. Both are pinned in `_relevance_label_v2`'s
docstring with the reasoning, and the conjunctive form
(`coverage ≥ 0.75 AND matched ≥ 4`) is the candidate a successor rule
should be calibrated from — against labelled turns, since length
independence can rank candidates but never confirm one.

### Invalidation

All rate surfaces (`memory_health`, `memory_scope_overview`,
`bettermemory eval`) apply identical invalidation semantics:

- **Bulk cutoff**: `bettermemory consolidate
  --acknowledge-misses-before <ISO_TS>` writes a `silent_miss_cutoff`
  event; `turn_audited` and `search_miss` events before the cutoff drop
  from both numerator and denominator. Latest cutoff wins.
- **Per-event ack**: `memory_acknowledge_miss(event_id, reason)`
  retracts one false-positive miss from the numerator; the audited
  denominator keeps its turn (the audit wasn't wrong, the verdict was).
- **Tombstoned top-hit**: a miss pointing at a since-removed memory is
  no longer actionable and drops from the numerator.

## Reading the three together

The healthy regime is high `memory_helped_rate` and high
`endorsement_rate`. `silent_miss_rate` does **not** belong in that
sentence in the obvious direction — see the caveat under its own heading:
low is the default whatever the store is doing, so it earns attention
when it *rises*, not when it sits near zero. Common failure signatures:

- High retrieval count, low `memory_helped_rate`: ranker over-firing;
  prune the store or tune thresholds.
- Many applies, low `endorsement_rate`: the model treats retrieval as a
  no-op; revisit the system-prompt policy on explicit `record_use`.
- High `silent_miss_rate`: the threshold rule over-triggers, or the
  model genuinely misses memory. Spot-check ~10 flagged turns.

Note these rates are complementary to recall benchmarks, not a
replacement: they require a live deployment and instrument the loop
rather than a QA endpoint.

## `bettermemory eval`

```text
bettermemory eval [--since 30d] [--scope SCOPE] [--min-retrievals N]
                  [--silent-miss-limit N] [--json]
```

Reads the active event log — the sharded `<store>/.events.00.jsonl` …
`.events.15.jsonl` segments, plus a pre-3.24 `<store>/.events.jsonl` on
stores that predate sharding (read-only legacy input, merged in by
event `ts`) — together with the rotated `.events-*.jsonl.gz`
archives, joins against the active store, and reports the three rates
with Wilson 95% confidence intervals, plus the cold-endorsement list
and recent silent-miss candidates. `--json` for machine-readable
output. The compute layer is `src/bettermemory/eval.py`
(`compute_eval` / `parse_since` / `render_text`) for callers outside
the CLI.

The default report also breaks the audit telemetry down per model
(`by_model`, from the `client_model` stamp the Stop hook reads off the
transcript — absent on pre-3.14 events) and shows how many repeat
audits the re-audit dedup absorbed (`repeat_audits`, excluded from
every denominator).

Four additional modes:

- `--report`: renders the telemetry as one publishable, self-contained
  markdown document — the rate trio over the `--since` window and all
  time side by side (the trend between windows is the story), per-model
  audit telemetry, the threshold-sweep counterfactual, and the
  tool-usage top 10, with a reading guide and a methodology footer.
  Aggregates only, by tested contract: rates, counts, CIs, and
  model/tool/rule names — never memory bodies, queries (not even the
  redacted previews), scopes, paths, or session ids, so the output is
  safe to publish as-is. `--output FILE` writes it to a file instead
  of stdout; combining `--report` with `--json` or any other mode flag
  is a hard error.
- `--tool-usage`: per-MCP-tool call counts from the event log — the
  empirical input for trimming the default tool surface. Tools without
  telemetry surface as "no telemetry" rather than a silent zero;
  unmapped event kinds get their own footer. Side-effect events
  (`search_miss`, `pending_expired`, `silent_miss_cutoff`,
  `proposals_enqueued`, `doctor_fix`, `use_token_expired`) are
  excluded — they're consequences of calls (or of admin CLI
  operations), not calls.
- `--threshold-sweep`: replays logged `search_miss` events against
  alternative STRICTER threshold rules (`v2_top1_high_score_50`,
  `v3_top1_high_dominant`, `v4_top1_high_strict_combined`) to ask
  whether `v1_top1_high` over-fires. Only stricter-than-v1 rules are
  replayable here: historical `search_miss` events exist only for turns
  v1 already flagged. Cutoff-invalidated misses are excluded (a code
  bug, not a rule decision); acked misses are deliberately kept (a
  confirmed false positive is exactly what a stricter candidate is
  judged against).
- `--widening-preview`: the forward-looking counterpart — replays
  candidate LOOSER rules over the `turn_audited` stream, which since
  3.14 carries per-turn `top_hits` with the raw coverage features
  (`matched_unique` / `query_unique` / `score`) plus the shadow
  `relevance_v2` label. Two bundled candidates: `w1_top1_v2_high` adds
  an absolute matched-token floor to the coverage fraction, targeting
  the documented blind spot where long natural-language queries land at
  "medium" on strong matches; `w2_top1_v2_high_from_medium` keeps only
  w1's medium→high promotions after the 2026-07-08 labeling pass
  (docs/eval/widening-labeling-2026-07-08.md) measured w1's low→high
  cohort at ~20% precision. The v1 baseline is replayed from the same
  features, so the delta isolates the rule change; both sides slightly
  overcount production (the project-suppression arm isn't replayable
  from the event), so read the delta, not the absolutes. Because
  logging the RAW pair makes the record formula-agnostic, any future
  candidate rule can be back-tested the same way.
- `--widening-preview --detail`: the precision-labeling surface behind
  the counts. Dumps each flagged turn's logged evidence — the redacted
  `probe_query` preview ({hash, 32-char preview, len} by default; the
  verbatim string only under `log_queries_verbatim`), the top hit's
  coverage pair and both relevance labels, and the hit's memory id
  joined against the active store + tombstone log for a summary — plus
  a per-memory concentration rollup. Concentration is the first
  diagnostic: N flags on two memories is a ranking problem with those
  memories; N flags across N memories is a genuinely wide label change.
  The flip decision on `relevance_v2` reads this lane, not the counts.
  Both lanes share one event-filter pipeline (`_collect_replayable_
  audits`), so the counts and the listed turns can never disagree.

All honor `--since`; all but `--report` honor `--json` (the report is
markdown by construction). Rules live in `eval.THRESHOLD_RULES` /
`eval.WIDENING_RULES`; adding one is a checker function plus a registry
entry.

## Comparative harness

`tests/eval/` (`python -m tests.eval.comparative`, `--json` available)
runs the real `search` and `probe_for_miss` code over a fixed synthetic
workload and feeds the derived events through `compute_eval`. Two
honesty constraints:

- The offline adapter reports `memory_helped_rate` and
  `endorsement_rate` as `n/a`, not `0.0` — they require a live agent
  emitting `record_use` events; deriving them from gold labels would
  relabel recall. The scripted driver (`tests/eval/driver.py`,
  `--driver scripted`) proves the compute path end to end but is a
  recorded transcript, not a measurement. The honest source for live
  rates is production telemetry: `bettermemory eval` over a real
  store's event log.
- Competitor adapters don't fabricate numbers. Absent the package or
  its API key they raise `SystemUnavailable` and contribute only a
  capability-matrix row sourced from public docs.

The rates are computable for any system that logs retrieval timing,
logs whether the model deliberately tagged each retrieved item as
load-bearing, and can replay a turn against its ranker post-hoc.

### The `--live` lane

`--live` swaps the stubs for executing adapters where execution is
honest (`tests/eval/live_adapters.py`): mem0 runs fully local and
keyless (`infer=False` — its LLM extraction is deliberately bypassed,
so the row measures its retrieval stack over verbatim facts), and the
reference MCP memory server is bridged over stdio with a harness-side
tokenized-OR ranker, because its native `search_nodes` is whole-query
substring matching (a pinned test shows the raw server scores 0/7 on
this workload by construction). agentmemory and claude-mem stay
documented-unavailable — their stub reasons explain why a live run
would not be honest. Maintainer runs only, via
`tests/eval/run_live.sh` (throwaway `.eval-venv/`, never CI); missing
prerequisites degrade to the stub row at runtime. Published numbers
live in [eval-results.md](eval-results.md).

## Caveats

- The `v1_top1_high` rule is calibrated against the author's own usage.
  Whether it under- or over-fires on other distributions is the open
  question the eval measures first.
- Models that never call `record_use` show artificially low
  `endorsement_rate` regardless of retrieval quality; the metric
  measures the retrieval-recording loop, not raw memory quality.
- Bursty deployments have wide confidence intervals. Aim for 100+
  audited turns before drawing conclusions.
