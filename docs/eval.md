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

A low rate means the ranker is firing on noise the model silently
ignores.

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

The healthy regime is high `memory_helped_rate`, high
`endorsement_rate`, low `silent_miss_rate`. Common failure signatures:

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

Reads `<store>/.events.jsonl` plus rotated `.events-*.jsonl.gz`
archives, joins against the active store, and reports the three rates
with Wilson 95% confidence intervals, plus the cold-endorsement list
and recent silent-miss candidates. `--json` for machine-readable
output. The compute layer is `src/bettermemory/eval.py`
(`compute_eval` / `parse_since` / `render_text`) for callers outside
the CLI.

Two additional modes:

- `--tool-usage`: per-MCP-tool call counts from the event log — the
  empirical input for trimming the default tool surface. Tools without
  telemetry surface as "no telemetry" rather than a silent zero;
  unmapped event kinds get their own footer. Side-effect events
  (`search_miss`, `pending_expired`, `silent_miss_cutoff`,
  `proposals_enqueued`) are excluded — they're consequences of calls,
  not calls.
- `--threshold-sweep`: replays logged `search_miss` events against
  alternative threshold rules (`v2_top1_high_score_50`,
  `v3_top1_high_dominant`, `v4_top1_high_strict_combined`) to ask
  whether `v1_top1_high` over-fires. Only stricter-than-v1 rules are
  replayable: `turn_audited` events don't carry `top_hits`, so turns v1
  didn't flag can't be re-evaluated. Cutoff-invalidated misses are
  excluded (a code bug, not a rule decision); acked misses are
  deliberately kept (a confirmed false positive is exactly what a
  stricter candidate is judged against).

Both honor `--since` and `--json`. Rules live in
`eval.THRESHOLD_RULES`; adding one is a checker function plus a
registry entry.

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

## Caveats

- The `v1_top1_high` rule is calibrated against the author's own usage.
  Whether it under- or over-fires on other distributions is the open
  question the eval measures first.
- Models that never call `record_use` show artificially low
  `endorsement_rate` regardless of retrieval quality; the metric
  measures the retrieval-recording loop, not raw memory quality.
- Bursty deployments have wide confidence intervals. Aim for 100+
  audited turns before drawing conclusions.
