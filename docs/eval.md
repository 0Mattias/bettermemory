# Evaluating memory

Published memory benchmarks (LongMemEval, LoCoMo, HaluMem, MemoryArena)
measure retrieval recall: given a query, did the system surface the
relevant stored fact? That leaves two questions unanswered:

1. Did the retrieved memory actually shape the reply, or was it pulled
   in and ignored?
2. When the model didn't retrieve, should it have?

bettermemory's telemetry (`memory_record_use`, the Stop hook's turn
audit, the auto/explicit `applied` split, `claim_excerpts`) exists to make both
answerable. This document defines the three metrics it computes.

## The three rates

### `memory_helped_rate`

Of all retrievals, what fraction were attested as load-bearing?

- Numerator: `record_use` events with `outcome="applied"`, `auto=false`,
  and non-empty `claim_excerpts`.
- Denominator: per-event retrieval occurrences across `memory_search` /
  `memory_show` in the window, counted by memory id. A
  memory surfaced N times counts N; there is no per-turn dedup.

Two attestation tiers feed the numerator:

- `attribution="model"`: the model called `memory_record_use` with the
  load-bearing excerpt.
- `attribution="hook"`: the Stop hook matched a body sentence of a
  retrieved memory (≥6 tokens, ≥30 chars, stopword-filtered) against
  the reply: verbatim (case/whitespace-normalised), or by
  distinctive-token containment (≥60% of the sentence's distinct
  content tokens appear in the reply, ≥4-token floor), catching
  paraphrases that keep the memory's vocabulary. Deep rewordings
  still slip through; thresholds precision-tuned so false positives
  stay rare.

The bare auto-fallback (`attribution="auto"`, no excerpt) is excluded,
and so is a `use` carrying `surface` (the Stop's settlement of a pointer
the recall surface showed, which no retrieval stands behind): the
surfaces section below counts it, and none of the three rates or the
tool-call count does.
The CLI's `applied_explicit` count is everything non-auto; recompute
from the raw events' `attribution` field for a stricter model-only cut.

A low rate is **ambiguous by construction**, and this metric cannot
disambiguate it. Either the ranker is firing on noise the model
silently ignores, or the retrievals did help and simply left no
attestation behind: the numerator counts only explicit,
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
reply. The per-memory companion in the health report is
`cold_endorsement_memories`: distinct memories with `retrieval_count >=
30` and zero explicit applies: retrieved often, never visibly used.
`bettermemory eval` publishes the same bucket under the same contract:
one floor (the 2026-08-30 recalibration of
`health._COLD_ENDORSEMENT_MIN_RETRIEVALS`, derived as `(1-p)^N` against
the store's own explicit-endorse rate per search delivery; parity with
eval's default is test-pinned), one counting basis (search deliveries
only; `memory_show` occurrences feed the rate denominators but not
the floor), and one honesty gate (with zero
Stop-hook settlement telemetry in the log the bucket is suppressed and
says so, rather than reporting an unwired hook as acknowledge-debt).

### `silent_miss_rate`

Of audited turns, what fraction were silent misses: the ranker would
have surfaced a high-relevance hit and the model made no
`memory_search` / `memory_show` call?

- Numerator: `search_miss` events emitted by the Stop hook's turn audit
  (`bettermemory hook stop`).
- Denominator: `turn_audited` events, excluding `no_signal` verdicts
  (probe declined: empty store, nothing relevant). Those are reported
  separately so a probe stuck at "declined" can't read as a healthy 0%.
- Each `turn_audited` row names its harness turn (`prompt_id`, from the
  Stop payload) and where the reply it settled against came from
  (`reply_source`: `payload` for `last_assistant_message`, `tail` for
  the newest assistant text of that turn; no key when the turn had no
  reply). Rows written before 9.0.0rc3 carry neither key. The Stop reads
  the turn its `prompt_id` names and no other (the newest user row's
  turn when the payload names none), from the end of the
  transcript one 1 MiB chunk at a time, 64 MiB at most; a turn it cannot
  decide within that writes nothing.
- A turn whose submitted rows are all someone else's (a task
  notification, a message from another session, an SDK submission, a
  compaction summary, an interrupt line, a prompt of reminder blocks
  only: the one definition in `owner_prompt.py`) probes nothing and
  writes verdict `no_signal` with `no_signal_reason: no_owner_prompt`,
  counted in `turns_no_signal`, and settles nothing. Before rule
  `rs-v4` (RS-F5) such a Stop probed another row instead (an older
  turn's prompt for 58 of the 60 in the 2026-10-19 replay of 122 rc3
  Stops, the turn's own SDK submission for 2) and was counted in the
  denominator.
- A Stop whose caller nothing could be read for (a folder that cannot
  be read, design 3.5) probes a pool membership admits nothing from, so
  its verdict is `no_signal`, counted in `turns_no_signal` and never a
  miss; the caller's resolution writes its `resolution_indeterminate`
  row, and the session's first output says nothing from any project is
  shown there.

The threshold rule is versioned (`THRESHOLD_RULE_V1 = "v1_top1_high"`)
and recorded on every event.

**Denominator semantics changed at 3.41.0.** The prompt-recall hook
runs the same predicate at prompt time and converts a would-be miss
into a `prompt_recall` event plus an `ok` audit verdict (the injection
is a retrieval-kind event, so the Stop hook's probe is shielded by
design, not by accident). On a hook-wired store with
`[behavior] prompt_recall` on (the default), `silent_miss_rate`
therefore measures the residual the recall path could not serve, and
trends toward zero for reasons unrelated to model behaviour; read
`prompt_recall` events as the delivery lane beside it. The two series
are not comparable across the 3.41.0 line, the same way the audited
denominators are not comparable across a `silent_miss_cutoff`
(`memory_admin(action="acknowledge_miss", before=...)`).

**And again with the recall surface (9.0).** The prompt recall no longer
has a miss lane: under the default `project` mode it delivers memories
written in the caller's project only, so a would-be miss on another
project's memory is flagged again where the old lane delivered it. A delivered recall still shields the
Stop's audit of its turn. Do not compare the rate across the switch; the
surfaces section says so whenever it is printed.

The switch keeps its meaning under membership (decision B21):
`[behavior] prompt_recall = "project"` (the default) delivers under the
prompt recall's own admission, the caller's project and nothing outside
a project; `"everywhere"` delivers under the unscoped search's admission,
every project's records and the global ones, inside a project or outside
one, never across a tenant boundary and never a record homed where
nothing could be read, with the candidate pool drawn under the same
admission; `"off"` delivers nothing. Each `prompt_recall` row names the
admission it was decided under in `admission` (`prompt_recall` or
`search_unscoped`).

**Read this rate as a floor, not a health score.** The v1 verdict fires
on a *coverage* label (the top hit matched ≥75% of the query's
distinctive tokens), and that denominator grows with the user's message.
Measured over 195 audited turns on a 185-memory store, the label's
"high" rate by message length:

| user message chars | 0-40 | 40-80 | 80-150 | 150+ |
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
should be calibrated from, against labelled turns, since length
independence can rank candidates but never confirm one.

### Invalidation

All rate surfaces (the health report and `bettermemory eval`) apply
identical invalidation semantics:

- **Bulk cutoff**: `memory_admin(action="acknowledge_miss",
  before=<ISO_TS>)` writes a `silent_miss_cutoff` event; `turn_audited` and `search_miss` events before the cutoff drop
  from both numerator and denominator. Latest cutoff wins.
- **Per-event ack**: `memory_admin(action="acknowledge_miss",
  id=<event_id>, reason=...)`
  retracts one false-positive miss from the numerator; the audited
  denominator keeps its turn (the audit wasn't wrong, the verdict was).
- **Tombstoned top-hit**: a miss pointing at a since-removed memory is
  no longer actionable and drops from the numerator.

## Reading the three together

The healthy regime is high `memory_helped_rate` and high
`endorsement_rate`. `silent_miss_rate` does **not** belong in that
sentence in the obvious direction (see the caveat under its own heading):
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
bettermemory eval [--since 30d] [--until TS] [--scope SCOPE]
                  [--min-retrievals N] [--silent-miss-limit N] [--json]
```

`--since` takes a relative span (`30d`, `12h`), `all`, or an absolute
ISO-8601 UTC timestamp (`2026-10-01T00:00:00Z`); `--until` closes the
window (exclusive) in the same forms and applies to the default mode
only. A timestamp without a UTC offset is refused.

The default report carries a `review` section: the write review's
chains issued in the window (a root `review_issued` row with its
follow-ups), answered, unanswered and pending, by client (with `none`)
and by model; the pre-answered share and `pre_answered_zero`; the p50
of review response sizes; calls per committed write (refused and
unanswered chains reported apart); filings per 100 committed writes and
per ISO week, by detector (`review`, `review-disagrees`); and the
owner's grades from `review_grade` events. A chain is unanswered when no
write carrying one of its review_ids arrives within 30 minutes of its
latest review and no closing call arrives (a call from the same session
and client without a review_id that commits within 30 minutes and names
every related id of the open review). A call carrying a review_id the
log holds no open chain for (one no review returned, or one a write
already answered) is recorded as pre-answered, with the sent id kept on
its `review_issued` row as `sent_review_id`, so every reviewed write is
in some chain. A chain issued inside the window is judged from rows up
to 30 minutes after `--until`. `--scope` does not filter the section.
`review_counters.py` holds the computation.

`bettermemory review samples [--since] [--until] [--n 40] [--seed 0]`
draws reviewed writes to grade (every refusal, link and filing up to
half the sample, then pre-answered writes up to 10, then random draws)
and shows each one's related set, answers and action. A committed write
shows its body from the store; a secret refusal shows its kinds only; any
other refusal shows the body from the transcripts under
`~/.claude/projects` (`--transcripts DIR`), found by review_id, and a
refusal from any other host shows none. `bettermemory review grade
<review_id> --answers right|wrong --action right|wrong` records a grade
as a `review_grade` event; the latest grade of a write counts.

`bettermemory recall review [--since 7d] [--until] [--deliveries 12]
[--briefs 4] [--silent 4] [--seed 0]` draws what the recall surface
showed for the owner to grade: prompt recall deliveries, session briefs,
and silent prompts (Stop turns the recall delivered nothing on, in which
the conversation then retrieved a memory written in its project). Each
sample shows its pointers with a summary from the store and the prompt
it answered, read from the session's transcript under
`~/.claude/projects` (`--transcripts DIR`) at review time and never
stored: a delivery's as the user row whose text hashes to the row's
probe (its prompt id row, labelled `unverified`, when none does), a
brief's as the first prompt after it, a silent turn's as the latest
before its Stop; a queued command, what the owner typed while a turn
ran, is read as a user row without a prompt id
(`owner_prompt.queued_text`). Each sample names its prompt kind (`owner`,
`task_notification`, `agent_message`, `cross_session`, `unknown`). A
sample whose prompt is in no row, whose transcript is missing or which
came from another harness is passed over for the next sample of its
stratum in the seeded order and listed as ungradeable, so the record
shows what the draw skipped. A sample already graded is not drawn again.
`bettermemory recall grade <sample_id> --relevant ID... --irrelevant
ID...` records a grade as a `recall_grade` event carrying the rule that
made the sample as `sample_rule` (for a silent prompt, relevant means
the recall should have pointed at the memory); the latest grade of an id
in a sample counts. The draw is meant every three to four days for two
weeks, then weekly. `recall_review.py` holds it.

When the window holds any row of the recall surface (a `session_brief`
with ids, a `prompt_recall` carrying `rule`, a settlement, a `segment_opened`, a
`review_unanswered` or a `recall_grade`), the report carries a
`surfaces` section; otherwise it is left out and the report reads byte
for byte as it did before the surface. For the brief and the prompt
recall apart:

- `deliveries` (shows), `shown` (pointers, one per id per show),
  `opened`, `used`, `ignored`, `deliveries_helped` and `chars_p50`;
- `shown_opened_rate`, `shown_used_rate` and `ignored_rate` over shown
  pointers, and `delivery_helped_rate` (a show with at least one pointer
  opened or used) over deliveries, with Wilson intervals;
- for the recall, `suppressed`: the candidates held back beside a
  delivery, by reason.

An open is a `memory_show` of the shown id later in the same
conversation, a use an applied `use` of it that is not the auto fallback
and carries an excerpt (the Stop's settlement included), an ignore a
`use` with outcome `ignored`. They count use after a show, not because
of it. A row belongs to the conversation by its `harness_session` stamp,
by its own session for the hooks' rows, and otherwise by the worktree
bridge: the conversation whose hook row was the latest in the row's
worktree. A stamp naming an id whose hooks went quiet before that
conversation's first hook row is the window's own server under the id
it had before a /clear or /resume, and its row joins that conversation,
as the hooks join it. Rows after a show are read up to the id's next
show in the same conversation, by either surface (a compact that opened
a segment re-shows a brief's ids, a resume's brief, like that of a
compact without its marker, shows only what its segment has not, a
recall may show what the brief showed), or else up to
`--until`, so an open or a use counts once, for the latest show of the
id before it; the join reads only rows before `--until`.

Beside them: `settlements` by surface; `segments` (the conversations
the surface spoke in, and the segments a compact or clear opened);
`review_unanswered` (the Stop turns that counted open review chains, and
the chains); and `grades`, per stratum of `bettermemory recall review`,
the ids graded and how many were relevant, the latest grade of an id in
a sample counting. `--scope` does not filter the section.
`recall_counters.py` holds the computation. The `--report` markdown
carries the four rates per surface and the counts, never an id, and the
split on the rule below.

A clear opens a segment. A compact opens one only on the compaction
marker its transcript holds: a `compact_boundary` row newer than the one
the session's segments last opened on, named as `marker` on the compact's
row. A compact without it is a resume and its row carries
`marker_absent: true`. A later hook (the prompt recall, the Stop, a
SessionStart) that finds a marker stamped within 60 seconds of the
compact opens the segment at the compact and records `segment_opened`
with the `marker` and the compact's moment as `opened_at`, and what was
shown since the compact belongs to it. The 60 seconds bound the
marker's stamp, not the read: a read that finds no marker leaves the
compact a resume and the next read looks again, however late, so the
state rebuilt from the log changes only on rows the log holds. Rows
before rule `rs-v4` opened a segment on every compact SessionStart.

Reads the store's log (the telemetry rows in `memory.sqlite`, through
`Store.iter_events`), joins against the active store, and reports the
three rates with Wilson 95% confidence intervals, plus the
cold-endorsement list and recent silent-miss candidates. An 8.x store's
events are read once `bettermemory migrate v8` has imported them.
`--json` for machine-readable output. The compute layer is
`src/bettermemory/eval.py` (`compute_eval` / `parse_since` /
`render_text`) for callers outside the CLI.

The default report also breaks the audit telemetry down per model
(`by_model`, from the `client_model` stamp the Stop hook reads off the
transcript, absent on pre-3.14 events) and shows how many repeat
audits the re-audit dedup absorbed (`repeat_audits`, excluded from
every denominator).

Five additional modes:

- `--report`: renders the telemetry as one publishable, self-contained
  markdown document: the rate trio over the `--since` window and all
  time side by side (the trend between windows is the story), per-model
  audit telemetry, the threshold-sweep counterfactual, and the
  tool-usage top 10, with a reading guide and a methodology footer.
  Aggregates only, by tested contract: rates, counts, CIs, and
  model/tool/rule names, never memory bodies, queries (not even the
  redacted previews), scopes, paths, or session ids, so the output is
  safe to publish as-is. `--output FILE` writes it to a file instead
  of stdout; combining `--report` with `--json` or any other mode flag
  is a hard error.
- `--tool-usage`: per-MCP-tool call counts from the event log, the
  empirical input for trimming the default tool surface. Tools without
  telemetry surface as "no telemetry" rather than a silent zero;
  unmapped event kinds get their own footer. Side-effect events
  (`search_miss`, `silent_miss_cutoff`, `use_token_expired`) are
  excluded; they're consequences of calls (or of admin CLI
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
- `--widening-preview`: the forward-looking counterpart; it replays
  candidate LOOSER rules over the `turn_audited` stream, which since
  3.14 carries per-turn `top_hits` with the raw coverage features
  (`matched_unique` / `query_unique` / `score`) plus the shadow
  `relevance_v2` label. Two bundled candidates: `w1_top1_v2_high` adds
  an absolute matched-token floor to the coverage fraction, targeting
  the documented blind spot where long natural-language queries land at
  "medium" on strong matches; `w2_top1_v2_high_from_medium` keeps only
  w1's medium→high promotions, after a 2026-07-08 labeling pass ruled
  out w1's low→high cohort. The v1 baseline is replayed from the same
  features, so the delta isolates the rule change; both sides slightly
  overcount production (the project-suppression arm isn't replayable
  from the event), so read the delta, not the absolutes. Because
  logging the RAW pair makes the record formula-agnostic, any future
  candidate rule can be back-tested the same way. Audits retracted by
  a `silent_miss_cutoff` marker (written by the acknowledge_miss
  action's `before` form) are dropped from both widening lanes under
  the rate surfaces' global latest-cutoff semantics and reported as
  `cutoff_retracted`, so the replayable population stays explicit.
- `--widening-preview --detail`: the precision-labeling surface behind
  the counts. Dumps each flagged turn's logged evidence, plus a
  per-memory concentration rollup. The evidence is the redacted
  `probe_query` preview ({hash, 32-char preview, len}; there is no
  verbatim mode), the top hit's coverage pair and both relevance
  labels, and the hit's memory id joined against the active store +
  tombstone log for a summary. Concentration is the first
  diagnostic: N flags on two memories is a ranking problem with those
  memories; N flags across N memories is a genuinely wide label change.
  The flip decision on `relevance_v2` reads this lane, not the counts.
  Both lanes share one event-filter pipeline (`_collect_replayable_
  audits`), so the counts and the listed turns can never disagree.
All honor `--since`; all but `--report` honor `--json` (the report is
markdown by construction). Rules live in `eval.THRESHOLD_RULES` /
`eval.WIDENING_RULES`; adding one is a checker function plus a registry
entry.

### Rule `rs-v5`: delivery by membership

From rule `rs-v5` (RS-F6) the brief and the prompt recall deliver a
record when membership admits its home for the caller (design 3.5): the
caller's project, decided by its checkout's evidence, where `rs-v4` and
the rules before it compared the record's origin columns with the
caller's repository and worktree. `rs-v4` (RS-F5) ships in the same
candidate and was never live, so a live log goes from `rs-v3` rows to
`rs-v5` rows. Each `session_brief` and `prompt_recall` row from `rs-v5`
carries three keys beside `rule`:

- `checkout`: the caller's checkout id, `null` where its resolution names
  none.
- `membership_rule`: the rule the caller's resolution named (how its
  checkout and project were decided), or, for a caller admitted nothing,
  the outcome: `indeterminate` (nothing could be read), `deferred` (an
  evicted file is being fetched) or `no_resolution` (the resolution
  raised).
- `rc3_admitted`: the ids rc3's rule would have delivered, computed from
  the origin columns, which are still written. For the brief, the
  printed ids rc3's project test admits; for the recall, of the hits
  every other gate passed (relevance, provenance, an open contradiction,
  the segment), those rc3's project test admits under `prompt_recall =
  "project"` and rc3's auto-scope admits under `"everywhere"`, less any
  pointer the render or the cooldown dropped from the delivery. rc3
  delivered nothing to a caller git could not read. The row of a caller
  admitted nothing carries an empty list.

Rows written before `rs-v5` carry none of the three; every reader takes
a row without them as it is.

Every read splits on the row's rule, never on a clock. The surfaces
section carries `by_rule`: for `rs-v5`, and for the earlier rules
together (`earlier`, with the rules its rows named; a show with no rule
is earlier), each surface's counts and its four rates. What the
conversation did after a show is joined over every row whatever its
rule, and an id's follow-ups end at its next show under either rule, so
the two groups add up to the section. Beside it, `membership_delta`
compares each `rs-v5` show row's delivered pointers with its
`rc3_admitted`: `rows` compared, `rows_differing`, `delivered_not_rc3`
(pointers membership delivered that rc3 would not have, a record written
in another checkout of the project, say) and `rc3_not_delivered`
(pointers rc3 would have delivered that membership held back). RS-F5's
predictions read on the `rs-v5` rows where the two agree, RS-F6's on the
rows where they differ. `bettermemory eval` prints the split and the
delta at the end of the section; `--report` carries the split's rates
for both groups, the window beside all time, and the delta's counts,
under group names only.

### Rule `rs-v6`: in force, standing, and every outcome a row

From rule `rs-v6` (RS-F7) the brief and the prompt recall deliver what
is in force: a superseded record yields its slot to its head and the
brief shows no history row (`in_force`); a record homed outside any
project whose scopes name the caller's project is admitted beside the
project's own (`global_named`); the recall holds no hit for its coverage
label and counts no deliveries per segment. The brief is two lists, the
caller's standing records in force and the records the owner's words in
the transcript bear on, eight of each at most, and its `session_brief`
row carries `standing` and `work` (each list's ids), `probe_query` (the
redaction of the probe, as a `prompt_recall` row carries its prompt's)
and `ranked_by` (`probe`, or `recency` when the probe was empty). Every
outcome is a row: a probed owner prompt that delivered nothing writes
`recall_held` with `cause` (`no_signal`, `no_hits`, `every_hit_held`
with the hits and their reasons, `nothing_rendered`, `segment_claimed`);
a shown pointer the segment never settled is written as a `use` with
outcome `unused` when the segment closes; a turn the owner interrupted
gets its `turn_audited` row from the next Stop, with `reply_source`
`interrupted`. `turn_audited` and `search_miss` rows carry `rule` from
`rs-v6`. A row without a rule (one written before its kind carried the
key) is split by the nearest earlier rule-carrying row, which is how
`bench/recall/read_rs_p5.py` and `read_rs_p11.py` read a log that spans
the rules; `bettermemory eval` counts `recall_held` among the recall's
own kinds.

## Integrity benchmark

`bench/integrity/` measures two things the rates above do
not: whether a memory system serves a fact that a later statement
superseded, and whether a poisoned record can be told apart from a
legitimate one. The protocol is system-agnostic: each arm is an adapter
in `bench/integrity/adapters.py`, and the docstring of
`bench/integrity/run.py` carries the commands. The numbers are the
frozen result artifacts under `bench/integrity/results/`, each carrying
the terms it was scored under.

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
that rule is what keeps a constant function from scoring. bettermemory's
signal is `superseded_by` or `contradicts` on the hit; every other arm's
is the one its adapter documents, and an arm without one has none.

**Write-time supersession** (arms whose adapter declares a write-side
channel; bettermemory since 7.2.0). Read off the adds, not the
searches: an update statement (a second statement, or the third of a
reversion) is linked when its write set a `supersedes` edge to the
memory holding the statement it replaces, and linked elsewhere when it
set one to any other legitimate statement; a first statement, a
distractor or a hard negative that set any edge counts as a non-update
link. For the admitted false facts: linked over the true fact when the
edge lands on a statement of the target topic (the lever `SECURITY.md`
records), and filed as a conflict when the write queued the pair for
the conflicts queue. Refused writes stay in every denominator, so the
row reads the whole write path the way the staleness row does.

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
(for bettermemory, a `memory_put` log row appended without the key; an
arm with no provenance field has its two variants coincide). The
detector is the arm's documented provenance channel applied to the
served record, bettermemory's `provenance` label.
Reported: the detection rate per variant with J beside the references,
and the injected record's rank against its API-written twin at k = 10,
which is what the forged metadata buys.

**Staleness, memory versus world.** Not re-run here. The summary
carries the rot benchmark's pooled rows with that artifact's sha256.

### Fairness accommodations

- Nothing is tuned. Every arm runs its defaults, single run, with the
  versions pinned in the artifact's provenance block. Per-arm setup is
  in the adapter docstrings in `bench/integrity/adapters.py`.
- bettermemory's hit text is the full body read back with
  `memory_show`, the documented read for a hit; the other systems return
  whole items.
- An arm that cannot execute, or fails its adapter's self-test, raises
  `SystemUnavailable` with the reason and is published as such, never
  with a number.

## Caveats

- The `v1_top1_high` rule is calibrated against the author's own usage.
  Whether it under- or over-fires on other distributions is the open
  question the eval measures first.
- Models that never call `record_use` show artificially low
  `endorsement_rate` regardless of retrieval quality; the metric
  measures the retrieval-recording loop, not raw memory quality.
- Bursty deployments have wide confidence intervals. Aim for 100+
  audited turns before drawing conclusions.
