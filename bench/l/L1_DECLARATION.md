# L1 — the conversational lane: unit declaration, 2026-08-16

First unit of Lane L. Lane W trains vocabulary and gates primary on
the dev instrument with LongMemEval as its cost guard; Lane L is the
mirror: deterministic temporal features in the default engine,
gating primary on LongMemEval with the dev instrument as the
no-regression guard. Same criterion, same doctrine, opposite
polarity — because the two instruments' measured gaps have opposite
composition. The dev gap is vocabulary (cross-form synonym bridges,
`bench/w/W1_RECORD.md` finding 1); the LongMemEval gap is
disambiguation: the miss anatomy below shows the engine already
FINDS the evidence and ranks lookalike sessions above it. This
document fixes arms, bars, budgets, and the read protocol before
any engine line is written. The enforcement record is the sha
ordering: this commit (with its diagnosis artifacts), then the
implementation commit, then the tuning-read commits, then the gate.
Nothing may be added afterwards; a miss is published, never
renegotiated.

The question L1 asks, exactly: can deterministic, zero-dependency
query- and body-text features — temporal-scaffold reweighting and
date-anchor selection — close the default engine's LongMemEval
macro recall@5 gap to the reference line (0.8935 → 0.916) without
costing the dev instrument or any saturated question type anything
at all?

## 1. Baselines this unit is judged against — committed, quoted

LongMemEval (`bench/longmemeval/run.py`, lexical arm, n=500, corpus
sha256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`,
depth 200, expansion off — the default engine):

- macro recall@1 0.5246, macro recall@5 0.8935
  (`bench/w/results/gate-lme-off-2026-08-16.json`, W1's paired
  integrity read; equal to the committed baseline chain back to
  3.30.0);
- by type at @5: single-session-assistant 1.0000 (n=56),
  knowledge-update 0.9808 (n=78), single-session-user 0.9714
  (n=70), multi-session 0.8487 (n=133), temporal-reasoning 0.8372
  (n=133), single-session-preference 0.7333 (n=30);
- the reference line: claude-mem semantic 0.916 macro@5
  (`bench/longmemeval/results/claude-mem-full500.json`, 2026-07-27,
  the standing comparison's bar) — carried here as this unit's G1.

Dev instrument (`bench/retrieval/run.py`, asked probe, unpadded,
prefilter off, n=20, five points per question, expansion off):

- recall@1 35%, recall@5 60%
  (`bench/retrieval/results/r1-unpadded-2026-08-13.json`, reproduced
  at the W1 gate in `bench/w/results/gate-dev-off-2026-08-16.json`).

The program horizon is unchanged: the 2026-08-09 criterion demands
dev as-asked recall@1 60 AND LongMemEval macro@5 0.916 on the
default engine. Lane L can reach only the second bar; the first
stays Lane W's. An L1 pass therefore claims the LongMemEval bar on
the default engine IF the owner's ship sentence lands the lane
default-on — and claims nothing about the criterion as a whole. The
honest interim sentence stands until both bars hold at once.

## 2. The diagnosis this unit is built on — published beside this file

Two artifacts, both produced by the committed runner at `bd0e684`
before this declaration was written, published in this commit:

- `bench/l/results/census-lme-off-2026-08-16.json` — the off-arm
  full-500 read reproducing the committed macros exactly (the
  baseline this unit's gate pairs against);
- `bench/l/results/per-question/census-lme-off-pq-2026-08-16.json`
  — its per-question sidecar (analysis input, not citable evidence,
  per the sidecar rule in `bench/longmemeval/README.md`).

The anatomy, from that sidecar joined to the corpus plus a
ranking-forensics pass over the same off arm (scratch tooling, its
findings restated here in full because the design stands on them):

- **90 of 500 questions miss at @5** — multi-session 42,
  temporal-reasoning 35, single-session-preference 8,
  knowledge-update 3, single-session-user 2.
- **Found, not lost**: of the 126 evidence sessions those questions
  miss, 102 (81%) sit at distinct-session ranks 5–19, 18 more
  inside rank 50, and only 6 are unranked at depth 200. 68 of the
  90 questions have EVERY evidence session inside rank 20. The gap
  is ordering, not coverage — the same shape the coverage probe
  measured in 2026-07 (`bench/longmemeval/README.md`).
- **The dominant pathology is temporal-scaffold matching.** In miss
  after miss the sessions outranking the gold match the QUERY'S OWN
  SYNTAX, not its content: for "How many days ago did I buy a
  smoker?" the gold session matches `smoker` at distinct rank 5
  while three lookalikes above it match `ago`, `day`, `buy`,
  `many`; for "What is the order of the three trips I took in the
  past three months…" the top outranker matches `latest`, `month`,
  `three` — a stamp-collecting session. Elapsed-time, order, and
  count questions (63 + 30 elapsed/order asks among 133
  temporal-reasoning questions alone) necessarily carry
  day/week/month/ago/last/first/many vocabulary, and the ranker
  treats those tokens as content. This is the discourse-filler
  pathology the shipped filler df-floor already repairs for words
  like "basically" — recurring in temporal dress.
- **Date-anchor selection is real but second-order**: among
  elapsed-ask misses with gold at distinct ranks 5–29, the gold
  session's date is the EARLIEST among its outrankers in 6/7
  temporal-reasoning and 7/10 multi-session cases — "how many
  weeks ago did I X" is answered by the first narration, and later
  lookalike mentions outrank it.
- **Two candidate mechanisms the anatomy REFUTED, recorded so they
  stay dead**: a session-breadth vote (gold has more ranked rounds
  than the median top-5 outranker in only 19/51 multi-session and
  13/46 temporal cases — it would hurt as often as help), and a
  first-person preference-marker boost (0/8 missed preference
  golds carry such markers in their best round). The preference
  type's misses give a lexical mechanism nothing deterministic to
  hold: the retired semantic arm's +23.3 on exactly that type
  (`bench/longmemeval/README.md`) marks it embedding-shaped, and
  this unit predicts little movement there rather than pretending
  otherwise.

## 3. The mechanisms, precisely

One shared temporal parser and two mechanisms, all pure Python over
content the engine already receives, behind one new `search()`
keyword — `conversational: bool = False`, default OFF, mirroring
`rescue_expansion`'s shape. `keyword` and `bm25` modes are explicit
instrument choices and are never touched; the rescue lane is
untouched; nothing changes for any caller that does not pass the
flag.

**The temporal parser.** A query's temporal reading is: an explicit
window (a month name with optional year; a `YYYY/MM/DD` or
`YYYY-MM-DD` date; "last week/month/year", "yesterday", "N
days/weeks/months ago" — relative forms resolved against the `now`
the caller already passes), and/or a selector — elapsed-time asks
("how many days/weeks/months…", "how long since…") and order asks
("which happened first…") select FIRST NARRATION (earliest anchor);
"last / latest / most recent" selects latest. Explicit window
outranks selector when both parse. A memory's anchor is, in order:
a leading bracketed date line (`[YYYY/MM/DD …]`, the shape
conversational ingest writes), else the first `YYYY-MM-DD` /
`YYYY/MM/DD` date in the body's first 200 characters, else the
memory's `created` day.

**L1-S, temporal-scaffold reweighting** — the primary repair, aimed
at the dominant pathology. A committed closed class of
temporal-scaffold stems — the day/week/month/year/ago/last/first/
latest/past/recent/long/many/total family plus spelled and digit
numerals — is down-weighted in the BM25 legs via a document-
frequency floor whenever the query parses as temporal-shaped, the
EXACT seam the shipped filler df-floor uses (`_filler_floor_stats`,
keyed on the lane flag): a floored term still matches and still
scores, it just can no longer outprice content terms. The list is
committed in the implementation, applies only under the lane flag,
only when the query has a temporal reading, and holds ~30 stems —
generic English temporal syntax, not corpus vocabulary. The
keyword leg is left untouched in this unit: RRF fusion means the
repair reprices half the vote, and the tuning frontier will say
whether that is enough. L1-S is deliberately NOT band-limited — it
is a term-pricing repair, the same class as the filler floor, and
its blast radius is every temporal-shaped query; G2b and the
saturated types are the declared detectors for collateral damage.

**L1-T, anchor selection** — the second-order repair. Within the
near-tie band (fused score ≥ τ of the top hit's), items whose
anchor falls inside an explicit query window rise by β_w; with an
explicit window present, banded items with anchors OUTSIDE it fall
by γ_w. Under a first-narration selector, banded items rise by β_e
decayed by their anchor's ordinal among the band's distinct anchors
(earliest first, factor 0.7 per step); under a latest selector, the
mirror. Adjustments are multiplicative on the fused RRF score,
applied before the trim, with the existing `(score, created, id)`
tiebreak preserved, so determinism is structural. No temporal
reading, no effect of any kind.

Declared defaults, tunable only under §5's protocol, finals in the
artifact: scaffold floor ratio 0.5 (the filler floor's own value);
τ 0.50; β_w 0.30; γ_w 0.15; β_e 0.25. Caps, hard: every β and γ in
[0, 0.5]; τ in [0.3, 0.7]; floor ratio in [0.3, 1.0]; the scaffold
class may shrink during tuning but may not grow past 40 stems; no
constant may key on a question id, a question type, a session id,
or any label — the mechanisms read the query text, the memory
bodies, `created`, and the caller's clock, nothing else.

## 4. Instrument changes — declared, paired, controlled

Two committed runner changes, no committed-corpus or question edits:

- `bench/longmemeval/run.py` gains `--conversational on|off`
  (default off) threading the new kwarg, a `--half even|odd|all`
  selector for §5's tuning split, and passes each question's own
  `question_date` as `search(now=…)` in EVERY arm — the clock a
  live assistant has at query time. Control prediction, checked at
  the gate: the off arm with the clock passed reproduces the
  committed macros EXACTLY, because ingest-time `created` postdates
  the corpus clock and `_recency_factor` clamps that age to zero —
  uniform 1.1 across every candidate both ways, rank-neutral by
  construction.
- `bench/retrieval/run.py` gains the same flag, nothing else. Its
  queries carry no temporal triggers, so the declared expectation
  is that the lane is INERT there: lane-on equals lane-off byte for
  byte. G2 is still measured, not assumed.

## 5. The read protocol — what may be read, when

- **Tuning surface**: the EVEN-index half of the corpus (0-based
  instance order, 250 questions) via the committed `--half even`
  selector. At most TEN tuning invocations before the gate, every
  one published in `bench/l/results/`, numbered. Mechanism
  selection (dropping a sub-mechanism whose tuning read costs a
  saturated type) happens here and is recorded in the artifact of
  the read that decided it.
- **The holdout half** (odd indices) is READ ZERO TIMES before the
  gate: no retrieval number derived from it exists until the gate
  read. Disclosed plainly: the §2 anatomy studied the OFF-ARM
  ranking structure of all 500 questions — the holdout is blind to
  the MECHANISMS, not to the baseline; G1h below is the guard that
  the mechanisms' constants did not quietly fit the tuning half.
- **Dev instrument**: unlimited reads (the inertness check is
  cheap), every read published.
- **The gate read, one**: the tuned constants are committed first,
  then one paired invocation per instrument: LongMemEval full 500
  conversational-on and conversational-off in the same runner
  session; dev unpadded/prefilter-off asked, lane-on and lane-off.
  The lane-on LongMemEval invocation runs TWICE; the two results
  blocks must be identical (the standing determinism bar).
  Per-question sidecars ride along for both arms.
- **Sealed stays sealed**: nothing under `bench/heldout/` opens;
  instrument #2 stays reserved. The gate read is the last read;
  post-gate tuning does not exist.

## 6. The bars — fixed now

- **G1, the primary bar** (gate read, LongMemEval full 500,
  conversational on): macro recall@5 ≥ 0.916 AND macro recall@1 ≥
  0.5246. The reference line at @5, no regression at @1.
- **G1h, the generalization guard**: on the holdout half alone
  (computable from the gate's per-question sidecars), macro@5
  (lane-on) − macro@5 (paired lane-off) ≥ +0.005. A win the tuning
  half kept to itself is an overfit, and it parks the unit whatever
  the full-500 number says.
- **G2, the dev guard** (gate read, asked, unpadded, prefilter off,
  lane on): recall@1 ≥ 35% AND recall@5 ≥ 60% — the committed
  baseline, conceded nothing.
- **G2b, the saturated-type guard** (gate read, by-type @5): no
  question type reads below its §1 off-arm value. The three
  saturated types are the point — assistant 1.0000, knowledge-update
  0.9808, user 0.9714 — but the clause binds on all six.
- **G3, the integrity bars**: the gate's lane-off LongMemEval arm
  equals 0.5246/0.8935 exactly (the same-engine paired baseline AND
  the clock-change control in one read); the gate's lane-off dev
  arm equals 35/60 exactly; the doubled lane-on invocation is
  identical to itself; the implementation adds zero dependencies
  and no network, filesystem-order, or wall-clock dependence beyond
  the caller's `now`.
- **Verdicts.** L1-PASS: G1, G1h, G2, G2b, G3 all hold — the record
  publishes and a ship sentence is put to the owner in plain
  language (default-on is the owner's door, never this unit's).
  L1-PARTIAL: every guard holds (G1h, G2, G2b, G3) and macro@5
  gains at least a full point over the paired off arm but misses
  the reference line G1 quotes from
  `bench/longmemeval/results/claude-mem-full500.json` — the
  improvement publishes as measured; a ship sentence may still be
  put, stating plainly that the reference line is not met.
  L1-PARK: anything else — a guard broken, the gain under a full
  point, or any budget of §5 overrun. A park publishes like every
  park before it.

## 7. The constraint ledger

- Zero dependencies added; pure-Python inference; single-process
  determinism preserved (a stats repricing through the existing
  floor seam; post-fusion arithmetic on already-computed scores;
  stable sorts; the existing tiebreak).
- No label touches a mechanism: no session ids, no question types,
  no per-question constants, no gold-derived vocabulary. The
  anatomy informed WHICH mechanisms exist (published in §2); the
  mechanisms themselves read only query text, body text, `created`,
  and the caller's clock.
- The committed corpora and questions of both instruments are
  untouched; runner changes are exactly §4's.
- The flag defaults OFF in the package. Nothing user-visible
  changes in this unit; any default flip is the owner's plain
  sentence after the read, and the changelog entry belongs to that
  ship, not to this unit.
- Artifacts are dated, sha-ordered, published whatever they say —
  every tuning read included.

## Declared confounds

1. **The gate corpus is the tuning corpus's sibling.** The 500
   questions are one distribution; tuning on half and gating on all
   means 250 gate questions were visible during tuning. G1h is the
   declared control, the split is fixed here before any mechanism
   read exists, and the record will state the two halves' numbers
   separately. The alternative — gating on the untouched half alone
   — would break comparability with every committed baseline and
   the reference line, which are full-500 macros.
2. **The scaffold class is authored from the miss anatomy.** The
   §2 diagnosis read the corpus's misses, and the closed class was
   written by a person looking at them — the risk is a list fitted
   to LongMemEval's idiom wearing a generic-English costume. The
   mitigations are structural: the class is ~30 stems of closed-
   class temporal syntax with no topical vocabulary admitted, it is
   published in the implementation commit, the floor only reprices
   (never removes) a term, and G1h plus G2b measure whether the
   repair generalizes beyond the questions that suggested it.
3. **Saturated types have only downside** (W1's lesson, carried).
   Three types sit at or near their ceiling; L1-T is a reorder and
   L1-S a repricing, and either can only hurt a type at 1.0. G2b is
   the detector; knowledge-update's own elapsed-time questions
   ("how many days until…", 7 of 78) are the nearest live wire, and
   the tuning protocol's drop rule exists for exactly that reading.
4. **Small-n granularity.** The preference type is 30 questions —
   3.3 macro points per question; single questions will swing
   type-level reads. The gate judges G1 on the full-500 macro
   where a question is worth 0.2 points, and the by-type table is
   published with n beside every row so nobody reads a
   30-question type's swing as a trend.
5. **The elapsed-ask selector assumes first narration is the
   answer.** "How many weeks ago did I X" is answered by the
   session where X happened, USUALLY the earliest strong match —
   but a user can narrate X retrospectively. The decay form
   (β_e · 0.7 per anchor ordinal) keeps most of the bonus for
   second-earliest, and the tuning frontier prices the assumption
   before the gate does.

## What is not claimed

No criterion claim — the dev bar is not this lane's and is not
moved by anything here. No comparative claim against any system
from a single-system artifact. No default flip — a PASS earns a
ship sentence, the owner's yes or no earns the default. No claim
that the preference type is addressed (§2 says the opposite). No
reuse of these mechanisms outside the declared arms until a unit
declares it.
