# Widening-rule labeling pass #3 — 2026-07-29

Third and, per the recorded band rule, **decisive** precision read on
`w2_top1_v2_high_from_medium`. Same methodology, same single labeler
(the agent operating the store — the same party the flip would
affect), and the same privacy posture as
[pass #1](widening-labeling-2026-07-08.md) and
[pass #2](widening-labeling-2026-07-22.md): this document carries
**methodology and aggregates only**. The raw flagged turns include
user-message previews and memory summaries, which stay in the local
event log. Reproduce the raw table on the store that owns the events
with:

    bettermemory eval --widening-preview --detail

## Setup

- Window: trailing 30 days at 2026-07-29; **361 replayable audited
  turns** (3.5× pass #1's 103, 1.3× pass #2's 280).
- Candidate under test, unchanged: `w2_top1_v2_high_from_medium`.
  Flagged **108** turns (Δ v1 +79).
- Labeled cohort: the **31 medium→high promotions timestamped after
  2026-07-22** — the accrual since pass #2's cutoff. The v1-high rows
  are the baseline arm, not promotions, and are excluded exactly as in
  passes #1–2.
- Labeling question, unchanged: *would inlining the top hit's body
  have materially helped answer this message?*
- Accrual note: 31 new promotions in 7 days, versus 37 in the 14 days
  pass #2 covered. The cohort was already decision-sized a fortnight
  ahead of the "~mid-August" target, so the pass ran early rather than
  waiting to add data to an already-flat trend.

## The same-session artifact, now measured

Pass #2 observed that several noise flags resolved to memories written
minutes earlier in the same working session — content that was already
in context when the turn ran, so **never a retrieval win by
construction**. This pass measured that class instead of noting it.

For each flagged turn, the top hit's `write`/`update` events were
correlated against the audited turn. A flag is counted **in-context**
when the top-hit memory was created or updated within 15 minutes
before the turn (or created within 2 minutes of it):

| | count | share |
|---|---|---|
| in-context by construction | 10 | 32% |
| labelable on merits | 21 | 68% |

**Correction to pass #2's replay assumption.** Pass #2 expected this
exclusion to be replayable by session id. It is not: `write`/`update`
events carry the MCP **server** session (`sess_<hex>`), while
`turn_audited` events carry the **client** session UUID. The two
namespaces do not join, and 17% of audited-turn hours contain two or
more concurrent client sessions, so a server session cannot be
attributed to a client session by containment either. Time proximity
to the top-hit memory's own mutations is the usable signal, which is
what the table above uses; the tight sub-15-minute bound is deliberate
because the coincidence it would take for a *concurrent* session to
mutate exactly this turn's top hit in that window is remote.

## Results

Two denominators, because the honest comparison to prior passes and
the honest read on the refined rule are different questions:

| cut | comparable (denom 31) | refined (denom 21) |
|---|---|---|
| charitable (weak helps count) | 15/31 — **~48%** | 15/21 — **~71%** |
| strict (clear helps only) | 8/31 — **~26%** | 8/21 — **~38%** |

The **comparable** column applies pass #2's treatment, which counted
in-context flags in the denominator as unhelpful. It is the number the
gate is scored against, and it is flat:

| pass | promotions labeled | charitable | strict |
|---|---|---|---|
| #1 (2026-07-08) | 11 | ~50% | — |
| #2 (2026-07-22) | 37 | ~54% | ~30% |
| #3 (2026-07-29) | 31 | ~48% | ~26% |
| **combined** | **79** | **~51%** | — |

Three independent windows, a 7.2× growth in labeled promotions from
first to combined, and the reading has not moved off a coin flip.

## Concentration splits cleanly — and diagnostically

31 flags across **16 distinct memories**. Pass #2's instruction was to
check concentration first, on the theory that many flags on one memory
indicate a ranking problem rather than a label-change signal. This
window shows concentration is only diagnostic once crossed with the
in-context test — the two most-flagged memories behave oppositely:

- The **autonomy/delegation contract** memory took 7 flags (23% of the
  cohort), and 5 of 7 label charitable-helpful. That is not an
  over-matched memory; it is a real recurring retrieval need. Every
  short long-horizon imperative ("do what needs to be done end to
  end") genuinely wants that contract inlined, and one flag is the
  pattern pass #2 called its clearest catch — inlining it would have
  prevented the correction the user then made by hand.
- The **go-forward-stack** memory (4 flags) and the **brand-asset-kit**
  memory (3 flags) are 100% in-context artifacts: every flag resolved
  to a memory that session had just written or updated. Pure noise, and
  invisible to a concentration count alone.

Remaining 11 memories took 1 flag each.

## Texture, third consecutive confirmation

- **Helpful cluster:** short imperative turns that name a project or
  artifact whose anchor memory is the top hit, plus explicit
  memory-invoking turns ("you have memory of us building…"), plus one
  clean diagnostic catch where the top hit recorded the exact site
  structure the user was reporting broken. Mid-context handoffs also
  land here: a model switch mid-loop is precisely when the delegation
  contract is missing from fresh context.
- **Unhelpful cluster:** continuations and apologies, scheduling
  questions, and long in-session meta turns — answers that live in the
  running session, not the store.
- **Ranking misses: 2 clear, 1 near.** A where-is-my-artifact turn
  anchored to a style directive instead of the memory holding the path;
  a personal-confidence turn anchored to the audit-loop autonomy
  contract instead of the career/personal-context memories. Both are
  the failure mode pass #1 logged once and is now recurring: the right
  memory exists and ranks below the wrong one. A third turn (a readme
  imperative) took the launch-readiness verdict while the docs-style
  directive ranked below — labeled charitable-helpful on the weak
  reading, but the better anchor existed.

Caveats carry over from pass #1 unchanged, both directionally
favorable to the rule: absolute counts are upper bounds (the
production project-suppression arm is not replayable from the event
payload), and this is single-user, single-store data.

## Decision

1. **Drop `w2_top1_v2_high_from_medium` as the flip candidate.** The
   band rule recorded in pass #2 was explicit — *flat again, with no
   rule change, means drop the candidate*. Pass #3 is flat (~48%
   against a ≥~70% gate; ~51% combined over 79 promotions). The live
   relevance label does not flip to w2. The shadow contract stays
   intact, and `w2` stays in `WIDENING_RULES` as a preview-only
   candidate — it costs nothing there and remains the baseline any
   successor is measured against.

2. **The refined candidate is now measured, not hypothesized, and it
   is the successor worth one pass.** Excluding same-session-mutated
   top hits — pass #2's refinement (b), and nothing else — lifts the
   charitable read from ~48% to ~71%, i.e. to the gate, on this
   window. That single exclusion is doing all the work, and it is
   principled rather than fitted: those flags are impossible wins by
   construction, not merely low-precision ones. Strict precision still
   reads ~38%, so this is a candidate for a labeling pass, **not** a
   flip on one window of one labeler's charitable cut.

3. **Implementing it needs a signature change first — this is the
   blocking next step, and it is code, not measurement.**
   `ThresholdRule.check(top_hits, recent_retrieval_count)` is a pure
   per-turn predicate with no access to event history, so the
   exclusion cannot be added as a registry entry the way `w2` was. It
   requires plumbing a per-memory mutation index (built from the
   `write`/`update` event stream) through both widening lanes — the
   counting lane and the detail lane — which the shared
   `_ReplayableAudits` walk makes tractable but is a real change with
   its own tests. Pass #2's other proposal (gate the promotion on a
   scope-or-proper-noun token shared with the top hit) is **not**
   carried forward: the in-context exclusion already captures the
   noise it was aimed at, and stacking two unmeasured gates would make
   the next pass uninterpretable.

4. **No further labeling pass is scheduled on w2.** Scheduling a
   fourth read of a rule three windows have agreed on would be
   measurement for its own sake. The next labeling pass is gated on
   the refined candidate existing in the registry (step 3), at which
   point it is replayable over history already on disk — no new
   observation window required.
