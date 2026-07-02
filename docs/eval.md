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

Denominator: per-event retrieval occurrences across `memory_search` / `memory_list` / `memory_show` in the window, counted by memory id — a memory surfaced N times counts N (there is no per-turn dedup; the event schema carries no `turn_id`). This is the `Retrieval occurrences` figure in the report output.

A high rate means most retrievals shaped a sentence the model wrote, and there's evidence on the record for which sentence. A low rate means the ranker is firing on noise the model is silently ignoring.

Two attestation tiers feed in:

- **Model-explicit** (`attribution="model"`): the model called `memory_record_use` with `claim_excerpts`, deliberately attaching the load-bearing phrase to the retrieval.
- **Hook-attributed** (`attribution="hook"`): the Stop hook ran a substring match over the assistant's reply text against each retrieved memory's body sentences and emitted an `applied` event with the matched phrase. Heuristic — substring match misses paraphrases — but precision-tuned (≥6-token, ≥30-char, stopword-filtered candidate sentences), so false positives stay rare.

Both tiers count toward the numerator because both represent evidence the retrieval shaped a reply. The third tier — `attribution="auto"`, the bare auto-fallback with no excerpts — is excluded. The eval CLI's `applied_total` / `applied_explicit` counts split the **auto-fallback** tier from the two attested tiers (`applied_explicit` is everything non-auto — model-explicit *and* hook-attributed). The CLI does not itself break model-explicit apart from hook-attributed; a consumer wanting a stricter "model only" definition recomputes from the raw events' `attribution` field.

This is the headline metric. It is the closest existing instrument to *"did memory help me?"* and it costs zero additional compute — every byte needed to compute it falls out of the existing `memory_record_use` event stream plus the Stop hook's attribution pass.

### `endorsement_rate`

> Of retrievals tagged `applied`, what fraction had attestation (model-explicit OR hook-attributed) vs. the bare auto-fallback?

Numerator: per-memory-id references inside `record_use` events with `outcome="applied"` AND `auto=false` (a single event applying three ids contributes three to the numerator).

Denominator: per-memory-id references inside all `record_use` events with `outcome="applied"` (same per-id granularity as the numerator).

This is the dead-letter detector. A low rate (mostly auto-applied) means nothing produced evidence the retrieval shaped a reply — the model didn't explicitly endorse, and the hook didn't find a substring match either. The companion view in `memory_health` is `cold_endorsement_memories`: distinct memories (per-memory, not per-turn) with `retrieval_count >= 5` AND `explicit_applied_count == 0`. With hook attribution counting toward `explicit_applied_count`, this bucket narrows to memories that retrieve frequently but never visibly shape a reply — a tighter signal for what's worth pruning.

### `silent_miss_rate`

> Of turns where the configured ranker would have surfaced a high-relevance hit, what fraction had **no** `memory_search`, `memory_show`, or `memory_list` call?

Numerator: `search_miss` events emitted by `memory_audit_turn`.

Denominator: total audited turns (`turn_audited` events).

This is the opposite failure mode of `endorsement_rate`. A high rate means the model is failing to reach for memory when it should. The threshold rule is versioned (`THRESHOLD_RULE_V1 = "v1_top1_high"`); the event records which rule fired so cross-version comparison stays meaningful.

**Escape hatch for pre-fix events.** When a fix lands that invalidates a batch of historical misses (e.g. the v2.7.3 cwd-suppression change), `bettermemory consolidate --acknowledge-misses-before <ISO_TS>` writes one additive `silent_miss_cutoff` event with `cutoff_ts=<ISO_TS>`. Subsequent `memory_health` / `memory_scope_overview` rollups drop any `turn_audited` *or* `search_miss` events earlier than the cutoff — invalidating both numerator and denominator so the rate isn't skewed. The rollup honors the latest cutoff seen; an earlier cutoff is ignored. Reversible by a later cutoff or by pruning the event manually.

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

bettermemory's LongMemEval numbers will land in the comparative-publication pass on the [roadmap](ROADMAP.md) — the `[embeddings]` extra shipped in 1.0.0 and the lighter `[embeddings-fast]` extra in 2.5.0, so the install-friction blocker for apples-to-apples retrieval comparisons is closed. In the meantime, the three rates above are computable today from any deployment's `.events.jsonl` and don't depend on embeddings.

## Reference implementation: `bettermemory eval`

```text
bettermemory eval [--since 30d] [--scope SCOPE] [--min-retrievals N] [--silent-miss-limit N] [--json]
```

Shipped in 2.5.0. Reads
`<store>/.events.jsonl` plus any rotated `.events-*.jsonl.gz` archives
via `iter_all_events`, joins against the active store, and reports the
three rates with Wilson 95% confidence intervals. The pure compute
layer lives in `src/bettermemory/eval.py`
(`compute_eval` / `parse_since` / `render_text`) so callers outside
the CLI (notebooks, custom dashboards, CI checks) can drive it
directly with their own event iterators.

```text
bettermemory eval — last 30d
────────────────────────────────────────────────────────────
Events scanned                        768
Retrieval occurrences                 198
Applied use events (auto+explicit)    142
Turns audited                         412

memory_helped_rate   0.61 [0.54, 0.68]   ▇▇▇▇▇▇▁▁▁▁   (k=121, n=198)
endorsement_rate     0.74 [0.66, 0.80]   ▇▇▇▇▇▇▇▁▁▁   (k=105, n=142)
silent_miss_rate     0.09 [0.07, 0.12]   ▇▁▁▁▁▁▁▁▁▁   (k=37, n=412)

Cold-endorsement memories (retrievals ≥ 5, 0 explicit applied): 2
  01HXYZ123ABC  tools             "Use ripgrep instead of grep…"  (12 retrievals)
  01HXYZ456DEF  learning-style    "User prefers terse explan…"    (7 retrievals)

Silent-miss candidates (last 20):
  2026-05-19 14:02  session=sess_abcd1234…  missed=01HXYZ789GHI relevance=high
  2026-05-17 09:18  session=sess_efgh5678…  missed=01HXYZ555JKL relevance=high
  …

Threshold rule: v1_top1_high
```

`--json` emits the same numbers as machine-readable JSON for CI pipelines. `--scope` filters to a single scope (useful for catching e.g. `projects:foo` going feral while `tools` stays healthy). `--min-retrievals` controls the cold-endorsement floor (default 5); `--silent-miss-limit` controls how many recent miss events are surfaced inline (default 20).

### `--tool-usage` — per-MCP-tool call-count rollup

```
$ bettermemory eval --tool-usage --since 30d
bettermemory eval --tool-usage — last 30d
────────────────────────────────────────────────────────────
Events scanned       768
Tool calls           737

tool                              count  share
  memory_audit_turn                 143  19.4%  ▇▇▁▁▁▁▁▁▁▁
  memory_record_use                 117  15.9%  ▇▇▁▁▁▁▁▁▁▁
  memory_show                       114  15.5%  ▇▇▁▁▁▁▁▁▁▁
  …
  memory_write_confirm                1   0.1%  ▁▁▁▁▁▁▁▁▁▁
  memory_health                       0  —  (no telemetry)
```

A second mode of `bettermemory eval` that answers a different question: *which MCP tools is the model actually reaching for?* One row per tool with absolute counts and the share of total tool calls. Intended as the empirical input for the roadmap's "trim the MCP surface" decision — tools that haven't been called in months across multiple installs are candidates to move behind a power-user flag.

The event-kind → tool-name map lives in `eval._TOOL_EVENT_KIND_TO_TOOL`. Tools without a dedicated event (today: `memory_health`) surface with a zero count and a "no telemetry" annotation rather than being silently dropped, so the reader can distinguish "this tool is not counted" from "this tool was never called." If a new tool ships and the map isn't updated, the unmapped event kinds surface in their own footer section as a guardrail. Side-effect events (`search_miss`, `pending_expired`, `silent_miss_cutoff`, `proposals_enqueued`) are filtered out — they're consequences of other tool calls rather than tool calls in their own right. `silent_miss_cutoff` is a CLI admin operation that invalidates stale events; `proposals_enqueued` is the Stop hook's write-reflex capture (the model never invokes it — accepting/dismissing the resulting proposals goes through the `memory_proposals` tool, which *is* counted); same rationale.

Honours `--since` and `--json`; ignores the rate-mode knobs (`--scope`, `--min-retrievals`, `--silent-miss-limit`) so a shell loop piping the same args into both modes doesn't have to strip them.

### `--threshold-sweep` — counterfactual replay over alternative rules

```
$ bettermemory eval --threshold-sweep --since all
bettermemory eval --threshold-sweep — last all time
────────────────────────────────────────────────────────────
Events scanned           768
Replayable misses         12
  (skipped 19 legacy events carrying top_hit_ids only — no relevance label to replay against)

rule                             flagged     Δ v1    % v1
  v1_top1_high                        12        —  100.0%
  v3_top1_high_dominant               11       -1   91.7%
  v2_top1_high_score_50                6       -6   50.0%
  v4_top1_high_strict_combined         6       -6   50.0%

Caveat: this is a *relative* sweep over events the v1 rule
already flagged. Strictly looser rules cannot be evaluated
from the log alone — turn_audited does not carry top_hits.
```

The third mode of `bettermemory eval`: walks logged `search_miss` events and asks each named rule the counterfactual question *would this event have been flagged under rule X?* Answers the calibration question `audit.py`'s docstring flags as open — *is `v1_top1_high` over-firing? would tightening reduce the noise?*

Bundled rules (all at least as strict as v1, so the sweep is well-defined):

| Rule | Tightening over v1 |
|---|---|
| `v1_top1_high` | reference (top-1 relevance == "high", no recent retrieval) |
| `v2_top1_high_score_50` | + top-1 score >= 50 — filters single-token high-coverage hits |
| `v3_top1_high_dominant` | + top-1 score >= 2× top-2 score — distinguishes obvious match from borderline tie |
| `v4_top1_high_strict_combined` | intersection of v2 and v3 |

**Why only strictly-stricter rules.** A looser rule would also fire on turns where v1 *didn't* — but those turns aren't in the event log to replay, because the companion `turn_audited` event doesn't carry `top_hits`. The pre-2.6.4 hook-originated `search_miss` events lack `top_hits` too (they wrote `top_hit_ids` only) and surface in the `skipped_legacy_event_count` so the denominator stays honest. To gain the ability to replay *looser* rules, a future change would need to add `top_hits` to `turn_audited` — which inflates the log meaningfully and is therefore a deliberate trade-off, not a roadmap commitment.

Honours `--since` and `--json`; mutually exclusive with `--tool-usage`. The implementation is `compute_threshold_sweep` in `eval.py`; the rule registry is `THRESHOLD_RULES`. Adding a new rule is two lines (a checker function + a `ThresholdRule` entry in the registry).

The implementation lives in `src/bettermemory/eval.py`. The compute pass is deliberately independent of `health.compute_health` — both layers join events against the active store, but the eval module is single-responsibility (the three rates, the tool-usage rollup, and the threshold sweep) and the rendering is screenshot-friendly. The CLI is wired into `server.py`'s argparse table the same way `health` and `consolidate` are.

## Comparing systems honestly

The three rates can be computed for any system that:

1. Logs the timing of retrieval calls.
2. Logs whether the model deliberately tagged each retrieved item as load-bearing, or whether the system auto-attributed.
3. Provides a post-hoc audit hook that can replay a turn's user message against the retrieval ranker.

Most systems do (1). Few do (2). Almost none do (3). When you're comparing memory systems and a vendor can't tell you their `endorsement_rate`, that's a structural answer about what their telemetry exposes — not a defeat for the metric.

## Publication plan

The numbers from running this eval against bettermemory's own dogfood usage, plus the same workload re-run against Mem0 (OpenMemory self-host), Anthropic's reference `server-memory`, claude-mem, and agentmemory, will go into a follow-up post: *"What memory actually helped, by the numbers."* If you'd like to contribute a system to the comparison, open an issue with the eval harness output for your system.

Runnable harness code has landed at `tests/eval/` (`python -m tests.eval.comparative`, add `--json` for machine-readable output). It runs the real `search` and `probe_for_miss` code over a fixed synthetic workload (`workload.py`) and feeds the genuinely-derived audit events through `compute_eval`, so bettermemory's `recall@k` and `silent_miss_rate` are measured end-to-end. Two deliberate honesty constraints shape what it does and doesn't report:

- `memory_helped_rate` and `endorsement_rate` come back as `n/a` from the offline `BetterMemoryAdapter`, not `0.0`. They require a live agent emitting `record_use` events with claim excerpts; deriving them from the gold labels would just relabel recall, so the offline adapter reports `n/a` by construction. **The agent *driver* has landed** (`tests/eval/driver.py`, `python -m tests.eval.comparative --driver scripted`): it runs the real ranker for retrieval, asks an `Agent` to decide which retrieved memory it cited, emits genuinely-shaped `search` + `use` events, and feeds them through `compute_eval` so all three rates compute. The bundled `ScriptedAgent` is a recorded transcript — authored citations that prove the compute path end-to-end and give CI a reproducible trio, **not** a published measurement; its `memory_helped_rate` sits below recall precisely because it cites only some of what it retrieves. (A key-gated `LiveAgent` — a one-shot API role-play of an agent turn — shipped in 3.7.0 and was later removed: a staged single-turn completion is not an agent session, and it needed a raw `ANTHROPIC_API_KEY` the project's own agent workflow never holds. The honest source for the live rates is **production telemetry** — `bettermemory eval` over a real store's event log, which is exactly what the publication plan above runs against.) So the open piece before publication is the dogfood-telemetry numbers and the competitor runs below.
- Competitor adapters (`adapters.py`) do not fabricate numbers. Absent the competing package or its API key they raise `SystemUnavailable` and contribute only a **capability matrix** row sourced from public docs — which is the structural finding the publication rests on: only bettermemory logs all three signals (1)–(3) above, so only bettermemory can compute the trio at all.

## Caveats and open calibration

- The `v1_top1_high` threshold rule for silent-miss detection is calibrated against the author's own usage (~14 memories, daily Claude Code interaction). Whether it under- or over-fires on real distributions is the open question the eval will measure first.
- `auto=true` is the *default* outcome two turns after retrieval if `record_use` isn't called. Models that are bad at recording use will show artificially-low `endorsement_rate` regardless of retrieval quality. The metric measures the retrieval-recording loop, not raw memory effectiveness.
- These rates require continuous usage. A bursty deployment with a few turns will have wide confidence intervals; aim for 100+ turns of audited history before drawing conclusions.
