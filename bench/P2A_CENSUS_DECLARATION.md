# P2a feature census — declared in full before any cell was run, 2026-08-12

Round 9 closed the fusion-weight arc: the same relative-evidence rule
that buys the technical corpus its largest gains of the campaign costs
the conversational corpus on all three macros, and no constant — graded
or zero-one — satisfies both (`bench/longmemeval/README.md`, round-9
section). What round 9 also established is the method this document
leans on: a committed design census whose counterfactual labels
reproduced through the shipped engine cell-exact, validated end-to-end
before a preregistration fixed anything against it.

P2a is the campaign's next mechanism family: a learned linear rerank
over features computable at query time, reordering the head of the
shipped ranking rather than reweighting the votes that produced it.
The mass it would feed on is already in the committed record and is
deliberately not restated here: the per-probe shipped ranks live in
`bench/retrieval/results/base-leg-labels-2026-08-12.json` (committed
`07ad967`; each record's `gold_rank_with_leg` under both legs is the
same fused rank, both legs voting), and the conversational equivalent
lives in the round-9 per-question sidecars
(`bench/longmemeval/results/per-question/round9-off-pq-2026-08-12.json`,
committed `a1fd750`, `evidence_ranks` per question). A perfect top-5
rerank on the dev asked probes lands exactly on the campaign's 60%
recall@1 bar — the ceiling is tight, which is precisely why the next
question is not "is there mass" but "is there signal".

This document declares that question in full before a single number
exists: the feature family, every definition, every direction, the
pair rule, both corpora's pools, and the criterion under which writing
the P2a preregistration (Addendum 13, owning P71+) is licensed. The
enforcement record is the sha ordering: this commit, then the run
commit. Nothing may be added to the family afterwards.

## The question

For a gold document that the shipped engine ranks close behind
distractors, do features a rerank could actually use — computable from
the fusion's own inputs, no training, no corpus statistics beyond the
store being ranked — order the gold ABOVE those distractors? And do
they do so on BOTH corpora, or does the ordering sign-flip the way
every fusion-weight mechanism has (the C1 pattern eight rounds have
now measured)?

A learned linear rerank is only worth preregistering if some feature
carries transferable sign. Round 9 died at the conversational gate
after a dev-only justification; this census buys that death cheaply,
before a prereg, by reading both corpora first.

## 1. The pools, defined exactly

A NEAR-MISS is a probe whose gold lands at 1-indexed fused rank 2..W.
The primary window is W=8; W=5 and W=10 are reported as sub-reads.
Windows are read off the same ranks the committed artifacts already
record; the census recomputes them through the shipped engine and
FAILS if the recomputed dev ranks disagree with
`base-leg-labels-2026-08-12.json` (the round-9 reproduction property,
used here as a self-check rather than re-earned trust).

- Dev (`bench/retrieval`): all three probe classes (asked, requery,
  control), unpadded and padded-600, lane off, no prefilter — the
  regime the labels artifact measured. Distractors for a near-miss are
  the documents ranked above its gold.
- LongMemEval (`bench/longmemeval`, `longmemeval_s_cleaned.json`, all
  500 questions, lexical arm, lane off): a near-miss is an EVIDENCE
  SESSION at distinct-session rank 2..W (the runner's own
  `distinct_sessions` collapse). Distractors are the non-evidence
  sessions ranked above it; other evidence sessions of the same
  question are excluded from the pair set. A session is represented by
  its first-occurring item in the item ranking — the item that
  determined its distinct rank — and every session-level feature below
  is that item's feature.

## 2. The feature family, enumerated

For candidate c in a probe with `query_unique` u > 0 (the engine's own
token count, as `bench/base_leg_census.py` computes it), read off the
two base legs' lists and the fused output, all captured from the
shipped engine's own `_hybrid_fuse` call:

| feature | definition | gold-favoured direction |
| --- | --- | --- |
| `leg_agreement` | c present in both legs' depth lists | present beats absent |
| `best_leg_rank` | min of c's per-leg ranks under `_id_order`'s ordering; a leg not listing c contributes its `leg_size` | smaller |
| `evidence_max` | max over legs of len(c's matched-term list in that leg); 0 where absent | larger |
| `evidence_sum` | sum over legs of the same | larger |
| `coverage` | `evidence_max` / u | larger |
| `length_tokens` | len(`tokenize`(c's body)) | none declared — shape only |
| `recency` | LongMemEval only: the bracketed date prefix the runner itself writes into the body, compared lexicographically | newer |

Directions are declared here, before any number, and the win-share
tables below are computed under them — a feature is never allowed to
pick its sign after the fact. `length_tokens` has no defensible prior
direction, so it is reported as distribution shape and is NOT eligible
for the criterion in §4. `recency` is degenerate on dev by
construction (the bench store is written in one batch), so it is read
on LongMemEval only; the date format is asserted against
`YYYY/MM/DD (Day) HH:MM` — zero-padded, so lexicographic order is
chronological — and if any prefix deviates the recency read is voided
and recorded as voided rather than silently skipped. The fused score
itself is excluded from the family: distractors above gold beat it by
construction, and re-reading the identity would only decorate the
tables. It appears once, as the score-deficit diagnostic in §3.

Nothing else. A feature not in this table does not exist for this
census, for the readiness criterion, or for Addendum 13.

## 3. The reads — tabulation, no selection

- Pairwise win-share, the primary table. For every (gold, distractor
  ranked above it) pair in a pool, per feature: win, tie, or loss
  under the declared direction. Win-share is wins / (wins + losses);
  ties are reported beside it and do not count toward the effective n.
  Reported per corpus, per dev regime, per probe class, per window.
- Perfect-single-feature ceiling. Per feature, window and regime: the
  count of near-misses whose gold strictly beats EVERY distractor
  above it — the recall@1 a rerank sorting the top-W by that feature
  alone would recover. Plus the any-eligible-feature ceiling. No
  criterion keys on these; the mass they bound is already in the
  committed artifacts.
- Score-deficit shape. Per near-miss: the fused-score gap between gold
  and the rank-1 candidate, and between gold and the distractor
  directly above it. Distributional context for how far behind a
  near-miss actually sits; diagnostic only.
- Prefilter reachability. Padded-600, asked and control probes,
  production's own loader (`resolve_search_pool`): is the gold in the
  pool the prefilter would serve? Reported by stratum. Above the index
  threshold every ranker only reorders that pool, so this count bounds
  the production ceiling of ANY rerank — recorded here once, cited by
  both tracks' preregistrations, gated by neither.
- Population validation. The recomputed rank strata are cross-checked
  against the two committed artifacts named above; a mismatch fails
  the run rather than shipping a quietly different population.

## 4. The readiness criterion, stated before any result

This census exists to answer one question: does anything license
writing the P2a preregistration? The answer is decided by rules named
now.

- R1 (dev separation). A feature qualifies if its win-share over the
  dev asked+control pools, both regimes pooled, window 2..8, is at
  least 0.60 with at least 25 effective pairs. Eligible features:
  `leg_agreement`, `best_leg_rank`, `evidence_max`, `evidence_sum`,
  `coverage`.
- R2 (no sign-flip). An R1 qualifier survives if its LongMemEval
  win-share, window 2..8, is at least 0.50 with at least 50 effective
  pairs. Fewer than 50 effective pairs is not a pass.
- The surviving set Q′ is the R1 qualifiers that survive R2.

Writing Addendum 13 is licensed if and only if Q′ is non-empty, and
the rerank it preregisters may use ONLY features in Q′ — plus
`recency` if and only if recency's LongMemEval win-share is at least
0.60 with at least 50 effective pairs, a clause declared here because
recency cannot appear in R1 at all.

The lane is parked if Q′ is empty: either nothing separates on dev, or
everything that does sign-flips on the conversational corpus — the C1
pattern claiming its ninth mechanism, and worth exactly one census
instead of a round. The parking record is this document plus the run
artifact.

The anti-gate-shopping clause: the win-share tables are reported for
every pool, window and probe class, and none of those slices can
substitute for R1/R2 as written. A feature that separates only on
requery probes, or only at window 2..5, or only on one regime, is
family shape — reported, not qualifying. Widening a window or swapping
a pool after seeing the tables is the move this document exists to
prevent.

## 5. The constraint ledger

- Both sealed instruments are untouched. No file under
  `bench/heldout/` is read — not instrument #1 (assigned to the dense
  arc), not instrument #2 (reserved for P2a by Addendum 12, and
  therefore the one this census must be strictest about). This census
  reads dev gold and LongMemEval only.
- No engine code, whatever the outcome. Nothing under `src/` changes;
  the census taps `_hybrid_fuse` exactly as `bench/base_leg_census.py`
  does — the shipped engine plus a read-only intercept.
- Statistics only. No feature weight is fitted here; a win-share table
  is not a trained model. Fitting anything is Addendum 13's business,
  under its own kill lines, on features this document has already
  frozen.
- Dev-side by construction, and the LongMemEval read is of the
  committed-corpus copy this bench already carries; its per-question
  stores are rebuilt by the runner's own `build_question_store`.
- Deterministic artifact. No per-build ULIDs, no wall-clock content
  beyond the provenance date; two runs at the same commit produce the
  same bytes.
- No changelog entry. Census commits ship none, matching the base-leg
  census: no user-facing surface changes.

## Declared confounds

1. Dev pools are small — a handful of near-misses per probe class per
   regime. Pooling to pair level buys power at the cost of
   independence: pairs sharing a gold are correlated, and the
   effective-n floors in §4 count pairs, not probes. Stated rather
   than adjusted for; the preregistration's gates, not this census,
   carry the confirmatory weight.
2. The census reads the shipped v5.4.0 engine (lane off, default
   flags). Its distributions describe a rerank's input only while the
   base engine stays byte-identical — which round 9's P63/P64 record
   pins at this commit.
3. The session-representative rule compresses a session to its
   first-occurring item. A session-aggregate feature (say, summed
   evidence over all its items) is a different family member and is
   deliberately not in the table.
4. The engine already half-life-weights recency in scoring, so the
   recency read measures residual separation beyond what the ranking
   spent — understating what a recency-aware rerank would see on an
   engine without that weighting, overstating nothing.
5. The prefilter-reachability read is the only prefilter statement
   here; every win-share table runs the no-prefilter path the labels
   artifact measured.
