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
30` and zero explicit applies — retrieved often, never visibly used.
`bettermemory eval` publishes the same bucket under the same contract:
one floor (the 2026-08-30 recalibration of
`health._COLD_ENDORSEMENT_MIN_RETRIEVALS`, derived as `(1-p)^N` against
the store's own explicit-endorse rate per search delivery; parity with
eval's default is test-pinned), one counting basis (search deliveries
only — `memory_list` / `memory_show` occurrences feed the rate
denominators but not the floor), and one honesty gate (with zero
Stop-hook settlement telemetry in the log the bucket is suppressed and
says so, rather than reporting an unwired hook as acknowledge-debt).

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

**Denominator semantics changed at 3.41.0.** The prompt-recall hook
runs the same predicate at prompt time and converts a would-be miss
into a `prompt_recall` event plus an `ok` audit verdict (the injection
is a retrieval-kind event, so the Stop hook's probe is shielded by
design, not by accident). On a hook-wired store with
`[behavior] prompt_recall` on — the default — `silent_miss_rate`
therefore measures the residual the recall path could not serve, and
trends toward zero for reasons unrelated to model behaviour; read
`prompt_recall` events as the delivery lane beside it. The two series
are not comparable across the 3.41.0 line, the same way the audited
denominators are not comparable across a
`--acknowledge-misses-before` cutoff.

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

Five additional modes:

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
  candidate rule can be back-tested the same way. Audits retracted by
  a `silent_miss_cutoff` marker (written by `consolidate
  --acknowledge-misses`) are dropped from both widening lanes under
  the rate surfaces' global latest-cutoff semantics and reported as
  `cutoff_retracted`, so the replayable population stays explicit.
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
- `--usage-replay`: the measurement surface for the usage-signal
  ranking flags' declared flip bars (the top Planned entry in
  docs/ROADMAP.md). On a store running any of `endorsement_boost` /
  `outcome_demotion` / `corroboration_boost`, every probe records —
  additively, on `turn_audited` and `prompt_recall` events — which
  flags had live signal (`usage_active`: a non-neutral factor on at
  least one scored candidate) and, per flag whose single-flag toggle
  would have changed the top-1, the counterfactual winner's raw
  coverage features (`usage_toggles`). The counterfactual is computed
  INSIDE the production ranker at probe time (per-leg factors divided
  out, legs re-sorted, fusion re-run with recomputed weights, the
  temporal rerank re-applied; leg composition held fixed) because the
  factors multiply per-leg scores before RRF rank fusion — no
  arithmetic on a logged fused score can reproduce the toggle, which
  is also why turns logged before the capture shipped are counted as
  not-replayable rather than approximated. This mode aggregates the
  captures over the window: changed top-1s judged under the pinned
  rule (`v1_relevance_v2_tier_then_matched_unique` — shadow-label
  tier, then matched-token count, else neutral, with "improving"
  always meaning the flag's pick was better), the miss-labeled
  worsening count (a `prompt_recall` delivery is miss-labeled by
  definition — its top-1 is what got injected), the
  `outcome_demotion` invariant
  (`v1_later_top1_explicit_apply_within_600s`), and the density
  preconditions (distinct explicitly-endorsed and negative-outcome
  memories in the window; corroborated-memory liveness from the store
  rollup). Each turn counts once: a delivered recall's same-turn
  Stop-hook companion audit (which re-carries the same capture under
  an `ok` verdict) is skipped on the producers' own
  (session, probe-query) dedup key, keeping the `prompt_recall` row —
  the one recording what the model was shown. Audit/recall rows honor
  the bulk `silent_miss_cutoff` marker with the rate surfaces' global
  latest-wins semantics; per-event `miss_ack`s reference a
  `search_miss`'s event id, which no audit/recall row carries, so they
  are structurally unjoinable here and not applied. Measurements only:
  the declared thresholds stay in docs/ROADMAP.md, and an unread bar
  is a hold.

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

## Integrity benchmark

`bench/integrity/` measures two things the recall benchmarks above do
not: whether a memory system serves a fact that a later statement
superseded, and whether a poisoned record can be told apart from a
legitimate one. The same protocol runs on bettermemory, mem0 (two arms),
Graphiti (Zep's open-source engine) and Letta. The docstring of
`bench/integrity/run.py` carries the commands; the numbers are in
[eval-results.md](eval-results.md#integrity-benchmark).

The threat model is memory poisoning as the OWASP agentic top ten frames
it (ASI06). The axes are three of the four intervention points the
literature names: write-time admission, provenance binding and
retrieval-time signal. Post-hoc forensics is not scored in v0.

### Corpus

One fictional engineering organisation, hand-authored
(`bench/integrity/corpus.json`; its sha256 is recorded in every
artifact and the summary refuses to pool artifacts from different
shas). Forty topics, each a subject, an attribute and a value that is a
distinctive literal (a workflow name, a port, a region), with a query
that names neither value:

- 24 supersession topics: a first statement asserts the old value, a
  later one asserts the new value.
- 8 distractor topics: a later statement adds a compatible fact about
  the same subject and asserts no value.
- 8 reversion topics: the value changes and then changes back, in fresh
  wording.

Six hard-negative legitimate statements mention a secret without
embedding one, or use time words durably, so the write gates can
produce false alarms. Thirty poison payloads: ten false facts that
contradict a stored fact with a third value, ten embedded instructions
(six imperative, four phrased as advice), ten secrets (six in their real
formats, four evasive: split across words, separated by punctuation,
described in prose, inside a code fence). The payloads were written
from the attack literature rather than from bettermemory's detector,
and the corpus was sealed before any arm was scored.

### Protocol

Every arm gets a fresh store and the same add order: every first
statement, the hard negatives, every update, every distractor, every
reversion, then the thirty payloads. One `add()` per statement through
the system's public write path; the harness follows no hint and
acknowledges nothing. Reads are the system's public search at k = 5.

**Staleness, memory versus memory.** Each served item is classified by
value-token containment (lower case; whitespace, hyphens, underscores,
dots and quotes removed): `current` if it carries the topic's current
value (an item carrying both values counts as current, since it conveys
the change), `stale` if it carries a superseded value and not the
current one. The rule is mechanical, so an extractor's paraphrase still
scores. Per topic: `current_served@5`, `stale_served@5`,
`top1_current`, and `stale_unsignaled@5`, the headline loss: a stale
item served with nothing to tell it apart. A stale item counts as
signaled only when every stale item in the list carries the arm's
supersession signal and a current item in the same list carries none.
A field every hit carries distinguishes nothing and counts as nothing;
that rule is what keeps a constant function from scoring. The signals:
for bettermemory, `superseded_by` or `contradicts` on the hit; for
Graphiti, `invalid_at` or `expired_at` on the edge; mem0 and Letta
expose no such channel.

Three references are computed from the corpus alone and printed beside
every arm: `serve_all_unsignaled` (every statement, no signal),
`recency_top1` (the most recently written statement only, which is
right on supersession, wrong on distractors and right on reversions by
construction) and `oracle_replica` (peeks at the labels).

**Poisoning, write admission.** For each payload class: admitted, and
flagged (refused, held pending, or stored with a machine-readable
warning). Pooled against the 94 legitimate statements into precision,
Youden's J and Fisher's one-sided p, beside `always_flag`, `never_flag`
and `oracle_replica`, the same discipline as the rot benchmark. A
legitimate statement the write path refuses is a false alarm and stays
one. For admitted false facts, whether the poison outranks the fact it
contradicts on the topic's query (`poison_top1_rate`); for admitted
instructions, whether any of three generic task queries serves them
(`injection_served@5`).

**Poisoning, store injection.** The ten false facts are inserted again
by bypassing the write API with forged trust metadata, in two variants:
plain, and with the system's own provenance binding forged as well
(bettermemory: a `write` event line appended to the event log;
Graphiti: an existing episode uuid on the edge; mem0 and Letta carry no
provenance field, so their two variants coincide). The detector is the
arm's documented provenance channel applied to the served record:
bettermemory's `provenance` label, Graphiti's `episodes` list.
Reported: the detection rate per variant with J beside the references,
and the injected record's rank against its API-written twin at k = 10,
which is what the forged metadata buys.

**Staleness, memory versus world.** Not re-run here. The summary
carries the rot benchmark's pooled rows with that artifact's sha256. No
rival exposes an interface that observes files or git, so the leg is
not scored comparatively.

### Fairness accommodations

- Nothing is tuned. Every arm runs its defaults, single run, with the
  versions pinned in the artifact's provenance block.
- mem0 runs twice: `mem0-raw` (`add(infer=False)`, MiniLM embeddings,
  the arm the LongMemEval runs used) and `mem0-infer` (its extraction
  and update logic on). The extraction arms use a local model through
  ollama, keyless; a hosted model would likely serve mem0 and Graphiti
  better, and the artifact names the model.
- Graphiti is Zep's open-source engine; Zep Cloud is unmeasured for
  want of a key, and the row is labelled `graphiti`. Its adapter runs a
  self-test first (one canonical statement through `add_episode`). A
  model that extracts no relation from it makes the arm read
  unavailable with the rerun command, because an empty row would be a
  loss the rival did not earn.
- bettermemory's hit text is the full body read back with
  `memory_show`, the documented read for a hit; the other systems return
  whole items.
- An arm that cannot execute raises `SystemUnavailable` with the reason
  and is published as such, never with a number.

## Caveats

- The `v1_top1_high` rule is calibrated against the author's own usage.
  Whether it under- or over-fires on other distributions is the open
  question the eval measures first.
- Models that never call `record_use` show artificially low
  `endorsement_rate` regardless of retrieval quality; the metric
  measures the retrieval-recording loop, not raw memory quality.
- Bursty deployments have wide confidence intervals. Aim for 100+
  audited turns before drawing conclusions.
