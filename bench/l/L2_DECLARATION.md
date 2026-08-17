# L2 — keyword-leg scaffold repricing: unit declaration, 2026-08-16

Second unit of Lane L. L1 repriced temporal scaffold in the BM25
legs and shipped default-on as 6.1.0; its record names the residual
in one sentence: the keyword leg still credits scaffold in full, and
the fusion's other half is the successor unit's declared target
(`bench/l/L1_RECORD.md`, § "what tuning killed"). This unit takes
that target, and one thing the fresh anatomy adds to it: the pricing
trigger itself is too narrow. The count asks that dominate the
multi-session residual — "how many projects have I led", "how many
different doctors did I visit" — carry the same scaffold vocabulary
the floor exists for, and never fire it, because they parse no
window and no selector. L2 is therefore one repair with two edges:
the scaffold repricing reaches the keyword leg, and the pricing gate
widens from "parses temporal" to "parses temporal, or is
scaffold-shaped". This document fixes arms, bars, budgets, and the
read protocol before any engine line is written. The enforcement
record is the sha ordering: this commit, then the implementation
commit, then the tuning-read commits, then the gate. Nothing may be
added afterwards; a miss is published, never renegotiated.

The question L2 asks, exactly: can extending the temporal-scaffold
repricing to the keyword leg's TF and coverage stream, under a
trigger widened to scaffold-shaped count asks, close the default
engine's remaining LongMemEval gap to the reference line (0.9062 →
0.916) without costing the dev instrument, any question type, or
anything the shipped lane already bought?

## 1. Baselines this unit is judged against — committed, quoted

The incumbent is the shipped engine: 6.1.0, conversational lane
default-on, the exact configuration of the L1 gate read.

LongMemEval (`bench/longmemeval/run.py`, lexical arm, n=500, depth
200, expansion off, each question's own date as the engine clock):

- lane-on, the incumbent read: macro recall@1 0.5339, macro recall@5
  0.9062; by type at @5: single-session-assistant 1.0000 (n=56),
  knowledge-update 0.9808 (n=78), single-session-user 0.9714 (n=70),
  temporal-reasoning 0.8675 (n=133), multi-session 0.8663 (n=133),
  single-session-preference 0.7333 (n=30)
  (`bench/l/results/gate-lme-conv-a-2026-08-16.json`);
- its halves, computable from the per-question sidecar
  (`bench/l/results/per-question/gate-lme-conv-a-pq-2026-08-16.json`):
  tuning half (even indices) 0.9067 at @5, holdout half (odd)
  0.9057;
- lane-off, the paired control: macro recall@1 0.5246, macro
  recall@5 0.8935 (`bench/l/results/gate-lme-off-2026-08-16.json`,
  equal to the committed baseline chain);
- the reference line: claude-mem semantic 0.916 macro@5
  (`bench/longmemeval/results/claude-mem-full500.json`) — carried
  here as this unit's G1, exactly as L1 carried it.

Dev instrument (`bench/retrieval/run.py`, asked probe, unpadded,
prefilter off, n=20): recall@1 35%, recall@5 60%, identical lane-on
and lane-off (`bench/l/results/gate-dev-conv-2026-08-16.json`,
`bench/l/results/gate-dev-off-2026-08-16.json`).

The program horizon is unchanged: dev as-asked recall@1 60 AND
LongMemEval macro@5 0.916 on the default engine, own-built machinery
only. Lane L can reach only the second bar. An L2 pass claims the
LongMemEval bar on the default engine IF the owner's ship sentence
lands the mechanisms in a release; it claims nothing about the
criterion as a whole, and the honest interim sentence stands until
both bars hold at once.

## 2. The diagnosis this unit is built on

No fresh retrieval was run for this diagnosis. Every number below is
computable from the committed L1 gate sidecars
(`bench/l/results/per-question/gate-lme-conv-a-pq-2026-08-16.json`,
`.../gate-lme-off-pq-2026-08-16.json`) joined to the corpus text,
plus the engine's own committed temporal parser and scaffold class
applied to the question strings (scratch tooling; findings restated
here in full because the design stands on them).

- **82 of 500 questions miss at @5 on the incumbent arm** —
  multi-session 38, temporal-reasoning 31, single-session-preference
  8, knowledge-update 3, single-session-user 2. Against the paired
  off arm the lane fixed 16 and broke 3, net exactly where L1 aimed.
- **Ordering, still not coverage.** Of the 113 evidence sessions
  those questions miss, 88 sit at distinct-session ranks 5–19, 19
  more inside rank 50, and 6 are unranked at depth 200. 59 of the 82
  have EVERY evidence session inside rank 20. The shape L1 opened
  with survives L1: the engine finds the evidence and prices
  lookalikes above it.
- **The trigger hole is the larger residual.** Only 34 of the 82
  parse temporal under the shipped gate (temporal-reasoning 25,
  multi-session 8, single-session-user 1). The other 48 never enter
  the lane at all, and the multi-session count asks own that
  cluster: "how many model kits have I worked on", "how much total
  money have I spent on bike-related expenses", "how many times did
  I bake something in the past two weeks". Their queries carry the
  scaffold class — many, much, total, times, past, last, month,
  week, spelled numerals — as densely as the elapsed asks do; they
  simply parse no window and no selector, so neither leg reprices
  anything.
- **The widened trigger, quantified on the same sidecars.** Requiring
  at least two distinct scaffold stems plus at least one content
  term admits 62 questions the temporal gate does not reach (244 of
  500 gated in all): 21 of the 82 residual misses (multi-session 20)
  newly enter the lane, 13 of them with every evidence session
  inside rank 20, against 45 newly gated questions that already sit
  at full recall. At three stems the same predicate admits 11 misses
  (7 inside rank 20) against 14 at full recall. The threshold is a
  real frontier and §5's reads price it.
- **A count-head trigger was considered and is rejected here.**
  Keying on the idiom heads alone (many, much, total, long, time,
  often) gates 330 of 500 — 124 of them already at full recall —
  because single common stems fire on non-count discourse ("long
  hash things", one dev question's phrasing). Co-occurrence is the
  structural signal; heads alone are not. Recorded so it stays dead.
- **Round-9-style vote conditioning is not taken either.** The
  base-leg withhold ships dark (`_BASE_LEG_TRAILING_WITHHOLD`
  False), so there is no live vote to condition; and with the
  keyword leg's scaffold contribution priced at the declared zero, a
  candidate matching only scaffold cannot hold that leg's rank-1 at
  all — the conditioning's target case is repriced out of existence.
  If a successor's anatomy shows the keyword leg still voting
  scaffold-heavy tops above content, that unit declares it.
- **Headroom, honestly.** With temporal-reasoning and multi-session
  both at 0.90 the full-500 macro@5 sits at 0.9238, computable from
  §1's by-type table. If every gated residual miss whose evidence
  all sits inside rank 20 flipped whole, the ceiling is 0.9424 —
  an upper bound no mechanism reaches, quoted so the realistic
  target is plain: the reference line is reachable at the optimistic
  end of this unit alone, and a gain that clears half a point
  without reaching it is the expected middle.

## 3. The mechanisms, precisely

One pricing gate, two legs, no new public surface. Everything below
engages only in hybrid mode under `conversational=True`, never on
the stopword fallback, and reads only the query text, the memory
bodies, `created`, and the caller's clock. `keyword` and `bm25`
modes are explicit instrument choices and are never touched; the
rescue lane and the window/selector rerank are untouched.

**The pricing gate.** A query is PRICED when it has a temporal
reading (the committed parser, unchanged), OR — the widening — when
it carries at least `_CONV_SCAFFOLD_MIN_STEMS` distinct scaffold
tokens (the committed forty-stem class plus the standing one-and-
two-digit numeral rule, both frozen) AND at least one content token
after the stopword strip. `_CONV_SCAFFOLD_MIN_STEMS: int | None`,
declared default 2; None removes the widening and the gate is L1's
exactly.

**Under the pricing gate, the BM25 side** applies the shipped
scaffold df-floor exactly as L1 committed it — the floor ratio stays
at its committed value untouched by this unit — with only its key
changed: it fires on the pricing gate rather than on the temporal
reading alone.

**Under the pricing gate, the keyword side — L2-K, the new
mechanism.** In the keyword scorer, a scaffold term's contribution
(its TF-capped body hits plus the scope weighting, unchanged in
form) is multiplied by `_CONV_KEYWORD_SCAFFOLD_WEIGHT` (α), and the
coverage multiplier is computed over content terms alone — matched
content over distinct content — so scaffold can neither pay for rank
nor dilute coverage. Declared default α = 0.0: scaffold terms price
at nothing in this leg, and a candidate whose every match is
scaffold scores zero and leaves the leg — deliberate, stated
plainly; the BM25 leg still carries such a candidate at the floored
price, so it remains in the fusion. Scaffold hits that coexist with
content hits still append to the matched list, so match terms,
relevance labels, and snippets read as before. L2-K requires at
least one content token in the query; a temporal-gated query with
none (all scaffold) leaves the keyword leg stock, which is the
shipped behavior. The recency factor, the scope 2x weighting, the TF
caps, and the single-term log1p carve-out are untouched.

**Dark until shipped.** `_CONV_KEYWORD_SCAFFOLD_WEIGHT: float |
None`, default None in the implementation commit: None short-
circuits every new branch, min-stems None likewise, and the engine
is behaviorally identical to 6.1.0 — provable by the unchanged test
suite plus new tests covering both states. Tuning-read config
commits set the arms; the gate config commit pins the finals; the
default engine changes only under the owner's ship sentence.

Declared constants, tunable only under §5's protocol, finals in the
gate config commit: α default 0.0, cap [0, 0.5]; min stems default
2, admissible set {2, 3, None}. The scaffold class is L1's committed
forty stems verbatim — it may shrink under a tuning read's drop
rule, it may not grow, and no stem may be swapped in. The floor
ratio, band τ, window boost and demote are L1's committed values and
are outside this unit's tunable set. No constant may key on a
question id, a question type, a session id, or any label.

Predictions, falsifiable at the gate:

1. The movement lands in multi-session and temporal-reasoning at @5
   and nowhere else; the widened trigger's reach makes multi-session
   the larger mover this time — the mirror of L1, whose gains ran
   temporal-first.
2. The three saturated types hold within a question of their §1
   values; preference does not move at all (embedding-shaped, per
   the standing decomposition; nothing here reads meaning).
3. Dev: the numeric bars hold untouched. Byte-identity between
   lane-on and lane-off is NOT predicted this time and its loss is
   not a failure: under the default trigger, one dev question parses
   temporal and one is scaffold-shaped, so up to two dev result
   blocks may differ while the recall figures stand. If tuning
   settles min stems at None, dev byte-identity returns with it.
4. The trigger frontier orders 2 over 3 over None on the tuning
   half, tracking the reachable-miss counts in §2 — unless the
   newly gated full-recall questions lose more than the flips gain,
   which is exactly what a read at each threshold detects.
5. The off arm reproduces §1's lane-off figures exactly, and the
   doubled lane-on invocation is identical to itself.

## 4. Instrument changes

None. Both runners already carry the lane flag and the half
selector; tuning arms are module-constant commits, the shape every
lane read has used; the committed corpora and question sets of both
instruments are untouched. This unit adds no runner flag, no corpus
byte, no question edit.

## 5. The read protocol — what may be read, when

- **Tuning surface**: the EVEN-index half (0-based instance order,
  250 questions) via the committed `--half even` selector — the SAME
  split L1 declared, kept deliberately: the odd half has never fed a
  constant in this lane, and a fresh split would rotate
  once-tuned-on questions into the holdout and thin the guard. At
  most EIGHT tuning invocations before the gate, numbered
  `tune-l2-01` onward, every one published in `bench/l/results/`
  with its config commit in the artifact. L1's unspent budget does
  not carry; nothing carries.
- **The holdout half** (odd indices) is read zero times before the
  gate: no retrieval number derived from it exists until the gate
  read. Disclosed plainly, as L1 disclosed it: §2's anatomy read the
  incumbent's ranking structure across all 500 questions from the
  committed L1 gate sidecars — the holdout is blind to this unit's
  MECHANISM CONSTANTS, not to the baseline; G1h is the guard that
  the constants did not quietly fit the tuning half.
- **Dev instrument**: unlimited reads, every read published.
- **The gate read, one**: the tuned constants are committed first,
  then one runner session, back-to-back: LongMemEval full 500
  lane-on TWICE (the doubled invocation is the standing determinism
  bar), lane-off once; dev lane-on and lane-off. Per-question
  sidecars ride along for every arm.
- **Sealed stays sealed**: nothing under `bench/heldout/` opens;
  instrument #2 stays sealed.

## 6. The bars — fixed now

- **G1, the primary bar** (gate read, full 500, lane on): macro
  recall@5 ≥ 0.916 AND macro recall@1 ≥ 0.5339. The reference line
  at @5; no regression at @1 against the incumbent — the shipped
  lane's own gate figure, not the softer off-arm floor.
- **G1h, the generalization guard**: on the holdout half alone,
  computable from the gate sidecars, lane-on macro@5 ≥ 0.9107 — the
  incumbent's holdout half plus half a point. A win the tuning half
  keeps to itself parks the unit whatever the full-500 number says.
- **G2, the dev guard** (gate read, asked, unpadded, prefilter off,
  lane on): recall@1 ≥ 35% AND recall@5 ≥ 60% — the committed
  baseline, conceded nothing.
- **G2b, the type guard** (gate read, by-type @5): no question type
  reads below its §1 incumbent value — the six values quoted there,
  the three saturated types the point, the clause binding on all
  six. Where L1 guarded against the off arm, L2 guards against the
  shipped lane: nothing the lane already bought may be sold back.
- **G3, the integrity bars**: the gate's lane-off LongMemEval arm
  equals §1's lane-off macros and by-type table exactly; the gate's
  lane-off dev arm equals the committed 35/60 exactly; the doubled
  lane-on invocation is identical to itself modulo wall-clock
  seconds; the implementation adds zero dependencies and no network,
  filesystem-order, or wall-clock dependence beyond the caller's
  `now`.
- **Verdicts.** L2-PASS: G1, G1h, G2, G2b, G3 all hold — the record
  publishes and a ship sentence is put to the owner in plain
  language (the release is the owner's door, never this unit's).
  L2-PARTIAL: every guard holds (G1h, G2, G2b, G3) and the gate's
  lane-on macro@5 reaches at least 0.9112 — half a point over the
  incumbent — but G1 is missed; the improvement publishes as read,
  and a ship sentence may still be put, stating plainly that the
  reference line is not met. L2-PARK: anything else — a guard
  broken, the gain under the partial floor, or any budget of §5
  overrun. A park publishes like every park before it.

## 7. The constraint ledger

- Zero dependencies added; pure-Python inference; single-process
  determinism preserved (a weighted contribution inside the existing
  scorer loop, a predicate over already-computed token streams, the
  existing stable sorts and tiebreaks).
- No label touches a mechanism: no session ids, no question types,
  no per-question constants, no gold-derived vocabulary. The anatomy
  informed the trigger's SHAPE (published in §2); the mechanisms
  read only query text, body text, `created`, and the caller's
  clock.
- The committed corpora and questions of both instruments are
  untouched; there are no runner changes to audit.
- The implementation lands dark (§3) and the default engine is
  behaviorally identical to 6.1.0 until the gate verdict earns a
  ship sentence and the owner takes it. The changelog entry belongs
  to that ship, not to this unit.
- Artifacts are dated, sha-ordered, published whatever they say —
  every tuning read included.

## Declared confounds

1. **The gate corpus is the tuning corpus's sibling** — L1's
   confound, unchanged, with the same control: the split is fixed
   here before any mechanism line exists, G1h binds on the untouched
   half, and the record states both halves separately.
2. **The anatomy read the incumbent's whole-corpus structure.** The
   widened trigger's reach and risk counts in §2 come from all 500
   questions, holdout included. The constants the counts justify
   (min stems, α) are priced on the tuning half alone, and G1h
   exists because a trigger shaped on the full corpus could fit the
   half it was allowed to read against the half it was not.
3. **The risk pool is real and concentrated in knowledge-update.**
   Under the default trigger, 16 knowledge-update questions at full
   recall newly enter the lane, and that type sits a question and a
   half from its guard. The mitigations are structural — repricing
   only ever cheapens scaffold, never boosts a candidate — and
   G2b's clause binds; the temporal-gated slice of the same risk
   pool crossed L1's gate without a type moving, which is evidence
   the polarity is safe, not proof this widening is.
4. **Small-n granularity** — L1's confound, carried: the preference
   type's 30 questions move 3.3 macro points each; the by-type table
   publishes with n beside every row.
5. **The scaffold class doubles as a count-ask detector only by
   coincidence of authorship.** The class was written for temporal
   syntax; the widening leans on its numerals and quantity stems
   (many, much, total) to catch count asks. If the frontier shows
   count asks need vocabulary the class lacks (few, different,
   various are NOT in it), this unit does not add them — the class
   is frozen — and the record says so plainly for a successor to
   take up.

## What is not claimed

No criterion claim — the dev bar is not this lane's and is not moved
by anything here. No comparative claim against any system from a
single-system artifact. No default flip — the verdict earns at most
a ship sentence, the owner's yes or no earns the release. No claim
that the preference type is addressed (§2's decomposition says the
opposite; its repair is Lane W's paraphrase-bridge unit). No reuse
of these mechanisms outside the declared arms until a unit declares
it.
