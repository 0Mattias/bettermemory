# Widening-rule labeling pass #2 — 2026-07-22

Second precision read on `w2_top1_v2_high_from_medium`, the flip
candidate the [2026-07-08 pass](widening-labeling-2026-07-08.md) left
standing. Same methodology, same single labeler (the agent operating
the store — the same party the flip would affect), and the same
privacy posture: this document carries **methodology and aggregates
only**; the raw flagged turns include user-message previews and memory
summaries, which stay in the local event log. Reproduce the raw table
on the store that owns the events with:

    bettermemory eval --widening-preview --detail

## Setup

- Window: trailing 30 days at 2026-07-22; **280 replayable audited
  turns** (2.7× the first pass's 103).
- Candidate under test, unchanged: `w2_top1_v2_high_from_medium` —
  v1's own high arm plus the shadow floor's medium→high promotions
  only. Flagged **69** turns (Δ v1 +49).
- Labeled cohort: the **37 medium→high promotions timestamped after
  2026-07-08** — the newly-accrued set the first pass's decision
  called for. The v1-high rows are the baseline arm, not promotions,
  and are excluded exactly as in pass #1; rows at or before 2026-07-08
  fell inside the first pass's window.
- Labeling question, per flagged turn, unchanged: *would inlining the
  top hit's body have materially helped answer this message?*

## Results

| cut | labeled helpful | precision |
|---|---|---|
| charitable (weak helps count) | 20/37 | **~54%** |
| strict (clear helps only) | 11/37 | **~30%** |

Pass #1 read ~50% (charitable) on 11 promotions; pass #2 reads ~54% on
37. Two independent windows agree: the medium→high promotion is a coin
flip, far from the ≥~70% flip gate.

Texture, consistent across both passes:

- The helpful cluster is short imperative turns that NAME a project or
  artifact whose anchor memory is the top hit — deploy-this-there,
  cut-the-release, where-are-my-keys, what-do-I-do-next shapes. The
  clearest catches in the cohort are of this kind, including one where
  inlining the top hit would have prevented the exact correction the
  user then had to make by hand.
- The unhelpful cluster is consent continuations ("yes please …"),
  frustration turns, and long pasted continuations — turns whose
  answer lives in the running session, not the store. Several noise
  flags resolved to memories created minutes earlier in the same
  working session, which can never be retrieval wins: the content was
  already in context when the turn ran.

## Decision

1. **Do not flip.** Two passes at ~50% against a ≥~70% gate.
2. **Hold, per the recorded band rule** (50–70% → one more pass,
   ~mid-August 2026). The trajectory is flat across a 3.4× growth in
   labeled promotions, so the third pass is decisive: flat again, with
   no rule change, means drop the candidate and prune the roadmap
   entry.
3. The texture points the productive iteration at **rule refinement
   rather than more of the same measurement**: gate the medium→high
   promotion on the turn carrying a scope-or-proper-noun token that
   matches the top hit (the "named thing" shape every clear catch
   shares), and exclude top hits created in the same session as the
   audited turn (never a retrieval win by construction; the event
   payload carries both timestamps and session ids, so this is
   replayable). If a refined candidate exists by mid-August, the third
   pass labels it; otherwise rule 2 applies as written.
