# Evaluating memory: the metrics bettermemory wants the field to adopt

Most published memory benchmarks (LongMemEval, LoCoMo, HaluMem, MemoryArena) measure *retrieval recall* — given a query, did the system surface the relevant stored fact? That's a useful question, but it's only the first of three.

The two questions almost no system can answer today:

1. **Did the retrieved memory actually shape the reply?** — i.e., was it load-bearing, or was it pulled in and ignored?
2. **When the model didn't retrieve, should it have?** — silent misses on the opt-in retrieval contract.

bettermemory's closed-loop telemetry (`memory_record_use`, `memory_audit_turn`, the auto/explicit `applied` split, the `claim_excerpts` field) was designed to make these answerable. This document defines the three metrics and the methodology so they're citable and comparable across systems.

## The three rates

### `memory_helped_rate`

> Of all retrievals, what fraction were **attested as load-bearing** — either by the model that retrieved them or by the Stop hook's post-hoc substring-match attribution?

Numerator: `record_use` events where `outcome="applied"` AND `auto=false` AND `claim_excerpts` is non-empty.

Denominator: all distinct `(turn_id, memory_id)` retrieval pairs in the window.

A high rate means most retrievals shaped a sentence the model wrote, and there's evidence on the record for which sentence. A low rate means the ranker is firing on noise the model is silently ignoring.

Two attestation tiers feed in:

- **Model-explicit** (`attribution="model"`): the model called `memory_record_use` with `claim_excerpts`, deliberately attaching the load-bearing phrase to the retrieval.
- **Hook-attributed** (`attribution="hook"`): the Stop hook ran a substring match over the assistant's reply text against each retrieved memory's body sentences and emitted an `applied` event with the matched phrase. Heuristic — substring match misses paraphrases — but precision-tuned (≥6-token, ≥30-char, stopword-filtered candidate sentences), so false positives stay rare.

Both tiers count toward the numerator because both represent evidence the retrieval shaped a reply. The third tier — `attribution="auto"`, the bare auto-fallback with no excerpts — is excluded. The eval CLI splits the tiers in the `applied_total` / `applied_explicit` counts so consumers can recompute against a stricter "model only" definition if they want it.

This is the headline metric. It is the closest existing instrument to *"did memory help me?"* and it costs zero additional compute — every byte needed to compute it falls out of the existing `memory_record_use` event stream plus the Stop hook's attribution pass.

### `endorsement_rate`

> Of retrievals tagged `applied`, what fraction had attestation (model-explicit OR hook-attributed) vs. the bare auto-fallback?

Numerator: `record_use` events with `outcome="applied"` AND `auto=false`.

Denominator: all `record_use` events with `outcome="applied"`.

This is the dead-letter detector. A low rate (mostly auto-applied) means nothing produced evidence the retrieval shaped a reply — the model didn't explicitly endorse, and the hook didn't find a substring match either. The companion view in `memory_health` is `endorsement_debt`: memories with `retrieval_count >= 5` AND `explicit_applied_count == 0`. With hook attribution counting toward `explicit_applied_count`, this bucket narrows to memories that retrieve frequently but never visibly shape a reply — a tighter signal for what's worth pruning.

### `silent_miss_rate`

> Of turns where the configured ranker would have surfaced a high-relevance hit, what fraction had **no** `memory_search` or `memory_show` call?

Numerator: `search_miss` events emitted by `memory_audit_turn`.

Denominator: total audited turns (`turn_audited` events).

This is the opposite failure mode of `endorsement_rate`. A high rate means the model is failing to reach for memory when it should. The threshold rule is versioned (`THRESHOLD_RULE_V1 = "v1_top1_high"`); the event records which rule fired so cross-version comparison stays meaningful.

## The three rates together

| | Low | High |
|---|---|---|
| `memory_helped_rate` | Retrievals are mostly noise the model doesn't use. | Memory is doing visible work. |
| `endorsement_rate` | Model isn't deliberately endorsing retrievals — auto-fallback is doing the work. | Model is actively engaging with each hit. |
| `silent_miss_rate` | Model reaches for memory when it should. | Model is missing relevant memory; retrieval contract slipping. |

The healthy regime is **high `memory_helped_rate`, high `endorsement_rate`, low `silent_miss_rate`**. Other configurations diagnose specific failures:

- High retrieval count but low `memory_helped_rate` → ranker over-firing; tune relevance thresholds or prune the store.
- High `applied_count` but low `endorsement_rate` → model is treating retrieval as a no-op; revisit the system-prompt addendum's emphasis on explicit `record_use`.
- High `silent_miss_rate` → either the threshold rule is over-triggering, or the model is genuinely missing memory; spot-check 10 flagged turns to disambiguate.

## Why not LongMemEval?

LongMemEval is a question-answering benchmark. It scores whether a system, given a stored history, can answer a question correctly. That measures *recall-via-storage*; it doesn't separate "the right fact was retrieved" from "the right fact was used."

The three rates above are complementary: they require an actual deployment with real user-model interaction, and they instrument the loop rather than the QA endpoint. A system can have great LongMemEval recall and a terrible `endorsement_rate` (lots of facts pulled in, model ignores them). It can have great `endorsement_rate` on a tiny memory store but be useless for the questions LongMemEval cares about.

We'll publish bettermemory's LongMemEval numbers once the optional embedding mode lands (see [ROADMAP](../ROADMAP.md)). In the meantime, the three rates above are computable today from any deployment's `.events.jsonl` and don't depend on embeddings.

## Reference implementation: `bettermemory eval`

```text
bettermemory eval [--since 30d] [--scope SCOPE] [--min-retrievals N] [--silent-miss-limit N] [--json]
```

Shipped in the Unreleased section of the CHANGELOG. Reads
`<store>/.events.jsonl` plus any rotated `.events-*.jsonl.gz` archives
via `iter_all_events`, joins against the active store, and reports the
three rates with Wilson 95% confidence intervals. The pure compute
layer lives in `src/bettermemory/eval.py`
(`compute_eval` / `parse_since` / `render_text`) so callers outside
the CLI (notebooks, custom dashboards, CI checks) can drive it
directly with their own event iterators.

```text
Memory eval — last 30 days
─────────────────────────────────────────────────
Turns audited                          412
Retrievals (distinct turn × memory)    198
Memories surfaced                       47

memory_helped_rate     0.61 ± 0.07   ▇▇▇▇▇▇▇▁▁▁
endorsement_rate       0.74 ± 0.06   ▇▇▇▇▇▇▇▇▁▁
silent_miss_rate       0.09 ± 0.03   ▇▁▁▁▁▁▁▁▁▁

Endorsement-debt memories (≥5 retrievals, 0 explicit applied):
  01HXYZ123ABC   tools           "Use ripgrep instead of grep…"   (12 retrievals)
  01HXYZ456DEF   learning-style  "User prefers terse explan…"     (7 retrievals)

Silent-miss candidates (last 20):
  2026-05-19  scope=projects:foo   missed: 01HXYZ789GHI  (relevance=high, rule=v1_top1_high)
  2026-05-17  scope=tools          missed: 01HXYZ555JKL  (relevance=high, rule=v1_top1_high)
  …

Threshold rule: v1_top1_high
Window: 2026-04-20 → 2026-05-20
```

`--json` emits the same numbers as machine-readable JSON for CI pipelines. `--scope` filters to a single scope (useful for catching e.g. `projects:foo` going feral while `tools` stays healthy). `--min-retrievals` controls the endorsement-debt floor (default 5); `--silent-miss-limit` controls how many recent miss events are surfaced inline (default 20).

The implementation lives in `src/bettermemory/eval.py`. The compute pass is deliberately independent of `health.compute_health` — both layers join events against the active store, but the eval module is single-responsibility (the three rates and their two row lists) and the rendering is screenshot-friendly. The CLI is wired into `server.py`'s argparse table the same way `health` and `consolidate` are.

## Comparing systems honestly

The three rates can be computed for any system that:

1. Logs the timing of retrieval calls.
2. Logs whether the model deliberately tagged each retrieved item as load-bearing, or whether the system auto-attributed.
3. Provides a post-hoc audit hook that can replay a turn's user message against the retrieval ranker.

Most systems do (1). Few do (2). Almost none do (3). When you're comparing memory systems and a vendor can't tell you their `endorsement_rate`, that's a structural answer about what their telemetry exposes — not a defeat for the metric.

## Publication plan

The numbers from running this eval against bettermemory's own dogfood usage, plus the same workload re-run against Mem0 (OpenMemory self-host), Anthropic's reference `server-memory`, claude-mem, and agentmemory, will go into a follow-up post: *"What memory actually helped, by the numbers."* If you'd like to contribute a system to the comparison, open an issue with the eval harness output for your system; runnable harness code lives at `tests/eval/` once the CLI ships.

## Caveats and open calibration

- The `v1_top1_high` threshold rule for silent-miss detection is calibrated against the author's own usage (~14 memories, daily Claude Code interaction). Whether it under- or over-fires on real distributions is the open question the eval will measure first.
- `auto=true` is the *default* outcome two turns after retrieval if `record_use` isn't called. Models that are bad at recording use will show artificially-low `endorsement_rate` regardless of retrieval quality. The metric measures the retrieval-recording loop, not raw memory effectiveness.
- These rates require continuous usage. A bursty deployment with a few turns will have wide confidence intervals; aim for 100+ turns of audited history before drawing conclusions.
