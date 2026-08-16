# L1 — the gate read and the partial, 2026-08-16

The unit `bench/l/L1_DECLARATION.md` declared ran whole: diagnosis
published, mechanisms implemented sealed, five tuning reads mapped
the frontier, constants committed, gate read taken. The verdict
grid's second row fires: **L1-PARTIAL — every guard holds, the gain
clears a full macro point, the reference line is not met.** This
record publishes the read and what it bought; nothing below
renegotiates a bar.

## The gate grid

Gate configuration (commit `30f45e6`): scaffold floor ratio 1.0,
forty scaffold stems, anchor selector dropped (§ "what tuning
killed"), window boost 0.30 demote 0.0, band τ 0.50.

LongMemEval, lexical, full 500, depth 200
(`bench/l/results/gate-lme-conv-a-2026-08-16.json`, determinism
repeat `-b`, paired control `gate-lme-off-2026-08-16.json`):

- **conversational on: macro recall@1 0.5339, macro recall@5
  0.9062** — against the paired off arm's 0.5246 / 0.8935, **+0.93
  and +1.27 points**, with macro@10 0.9494 against 0.9443;
- by type at @5, on vs off: temporal-reasoning 0.8675 vs 0.8372
  (+3.03), multi-session 0.8663 vs 0.8487 (+1.76), and the other
  four types IDENTICAL to four decimals — single-session-assistant
  1.0000, knowledge-update 0.9808, single-session-user 0.9714,
  single-session-preference 0.7333. The movement is exactly where
  the mechanisms aim and nowhere else; the preference type stayed
  flat as the declaration predicted it would.
- **G1 — MISS**: 0.9062 against the 0.916 reference line
  (`bench/longmemeval/results/claude-mem-full500.json`). @1's
  no-regression clause holds with room.
- **G1h — PASS, the strong way**: the tuning half moved +0.88
  (0.8979 → 0.9067) and the untouched holdout half moved **+1.67**
  (0.8891 → 0.9057), computable from the gate sidecars under
  `bench/l/results/per-question/`. The mechanisms generalized
  BEYOND the questions that suggested them — the overfit confound
  the declaration named did not materialize.
- **G2 — PASS**: dev asked, unpadded, prefilter off, lane ON:
  recall@1 35%, recall@5 60%
  (`bench/l/results/gate-dev-conv-2026-08-16.json`) — the committed
  baseline, conceded nothing. The whole dev results block is
  byte-identical between lane-on and lane-off: the lane is INERT
  where its triggers do not fire, measured rather than assumed.
- **G2b — PASS**: no type below its off-arm value; the three
  saturated types are untouched exactly.
- **G3 — PASS in every clause**: the gate's off arm reproduces the
  committed macros and by-type table exactly (which doubles as the
  clock-change control — passing each question's own date as the
  engine `now` is rank-neutral, as predicted from
  `_recency_factor`'s age clamp); the doubled lane-on invocation is
  identical to itself modulo wall-clock seconds; the lane adds zero
  dependencies and no nondeterminism.

The five gate invocations ran sequentially at one commit in one
sitting (the declaration's "same runner session", which the
committed runner realizes as back-to-back invocations — it has no
multi-flag-arm mode, and adding one was outside §4's declared
changes).

## What tuning killed, and what the miss says

The read ledger (`bench/l/results/tune-01` … `tune-05`, each with
its config commit; six invocations issued — one died with a session
outage before producing a byte and was reissued unchanged):

1. **The scaffold floor is the mechanism.** Isolated (read 2) it
   was worth +0.54 @5 on the tuning half with @1 exactly preserved;
   at its ratio cap with the fortieth stem (read 3) +0.68 and +0.40.
   The L1 anatomy's dominant pathology — lookalike sessions matching
   the question's own day/week/ago/many syntax — prices out of the
   BM25 legs and the gains land precisely in the two types the
   pathology owns.
2. **The anchor selector is dead, and deservedly.** Read 1 measured
   the declared defaults net-negative at @1 (−2.6 on the half): the
   anatomy's gold-is-earliest evidence holds among MISSES and
   inverts among already-correct tops, so every magnitude large
   enough to rescue a rank-5–19 gold displaces rank-1 golds it
   cannot see. Dropped under §5's drop rule; the boost constant's
   zero is the drop, not a tuning artifact.
3. **Window boosts carry their weight only boost-only and only at
   interior magnitudes.** Read 1's window losses all ran through
   the demote side (a gold outside the parsed window demoted under
   a lookalike inside it); read 4's boost-only arm added +0.20 on
   the half, and read 5 priced the caps (boost 0.50, τ 0.30) and
   LOST ground — the deeper band displaces as much as it rescues.
4. **The remaining gap is the keyword leg.** The floor reprices
   half the fusion's vote; the keyword leg still credits scaffold
   in full, and the gate's residual misses are concentrated where a
   scaffold-heavy lookalike rides that leg. Repricing the keyword
   leg — or conditioning its vote the way round 9 conditioned the
   base legs — is the successor unit's declared target, not a
   change this unit may smuggle.

## What the partial licenses, exactly

The default engine's honest LongMemEval sentence, IF the owner
ships the lane default-on, becomes: macro recall@5 90.6% against
the reference stack's 91.6% — one point behind, from 2.3. The
criterion as written stays unclaimed (its dev bar is Lane W's and
is not moved here), the interim sentence stands, and no comparative
claim follows from a single-system artifact. The flag ships OFF in
this unit; nothing user-facing changed.

## Owner doors

One ship sentence is put, per the declaration's PARTIAL clause:
ship `conversational=True` as the default engine's ranking behavior
— deterministic, zero-dependency, dev-inert, +1.27 LongMemEval
macro@5 — stating plainly that the reference line is not met. The
owner's yes flips the default under its own release; the owner's no
leaves an opt-in documented the way `rescue_expansion` already is.
Instrument #2 stays sealed. The criterion stays unclaimed and the
honest interim sentence stands with it.
