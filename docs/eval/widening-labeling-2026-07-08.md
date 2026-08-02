# Widening-rule labeling pass — 2026-07-08

First precision read on the relevance-v2 widening candidates, run five
days after 3.14.0 started logging per-turn calibration features. This
document carries **methodology and aggregates only** — the raw flagged
turns include user-message previews and memory summaries, which stay in
the local event log. Reproduce the raw table on the store that owns the
events with:

    bettermemory eval --widening-preview --detail

## Setup

- Window: trailing 30 days; 4,007 events scanned; **103 replayable
  audited turns** (non-repeat, miss-capable `turn_audited` events
  carrying the 3.14+ `top_hits` payload).
- Replayed v1 baseline: **1** turn flagged.
- Candidate under test: `w1_top1_v2_high` — top-1 shadow
  `relevance_v2 == "high"` (coverage ≥ 0.75 OR `matched_unique >= 4`)
  and no retrieval in the lookback window. Flagged **32** turns
  (Δ v1 +31).
- Labeling question, per flagged turn: *would inlining the top hit's
  body have materially helped answer this message?* Labeled by the
  agent operating the store (single labeler; the same party the flip
  would affect), from the logged 32-char query preview, coverage pair,
  both relevance labels, and the resolved memory summary.

## Results

Split by the flagged turn's **v1 label** (what the promotion is
promoting *from*):

| v1 label | flagged | labeled helpful | precision (charitable) |
|---|---|---|---|
| low      | 20 | 4 weak | ~20% (strict: ~5%) |
| medium   | 11 | 5–6    | ~50% |
| high     | 1  | —      | (v1's own baseline behavior) |

Coverage-fraction texture: the low→high promotions cluster at
f = matched/query ≈ 0.17–0.38 on long messages (probe query lengths
200–1,700 chars — pasted logs, tool output, multi-paragraph
continuations), which cross the absolute `matched_unique >= 4` floor
against any domain-adjacent memory. The medium→high promotions cluster
at f ≈ 0.40–0.57 on short-to-mid messages, and contain every
clearly-real catch in the cohort.

Flag concentration: 32 flags across **20 distinct memories** — a rule
problem, not a single over-matched memory.

Two caveats, both directionally favorable to the rules:

- Absolute counts are upper bounds — the production
  project-suppression arm isn't replayable from the event payload, and
  several noise flags (project-scoped memories surfacing in unrelated
  sessions) would be suppressed live.
- Single-user, single-store data; the same limitation as the
  silent-miss threshold calibration.

## Decision

1. **Do not flip to w1.** ~15–30% precision would translate directly
   into `expand_top` inlining junk bodies and the miss probe nagging
   on conversational continuations — the false-positive cascade the
   opt-in retrieval design exists to prevent.
2. The original blind-spot thesis said long queries land at **medium**
   on strong matches; the data agrees — and shows the floor's
   low→high promotions are a different, junk-dominated population.
   Added `w2_top1_v2_high_from_medium` (3.16.0): v1's high arm plus
   medium→high promotions only. On this window it flags 12 (Δ v1
   +11) at ~50% labeled precision.
3. **Flip gate, restated:** run a follow-up labeling pass after a few
   more weeks of accumulation; flip the live label to the w2 formula
   if its precision holds at ≥~70%, otherwise iterate or drop.

## By-catch (not rule evidence)

- Two flags resolved to a memory id that is neither active nor
  tombstoned ("unknown"). Traced in the event log: written and then
  removed at explicit user request as part of a project trace-removal,
  tombstone included. The detail lane's `unknown` status rendered a
  deliberate purge accurately — no curation action.
- One flagged turn's top hit was a plainly wrong memory while the
  right one (same domain, explicitly named in the message) existed but
  ranked below it — a ranking miss, single occurrence, noted for the
  next ranking pass.
