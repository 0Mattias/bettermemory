# Pre-registration — multi-repo memory-rot corpus

Committed **before any repository is screened**. Everything below is
falsifiable, and the scorecard published with the results marks each
prediction hit or **MISSED** using these exact thresholds.

The point of this document is not that the predictions are right. It is
that they were written down first, so a result that contradicts them
cannot quietly become the new hypothesis.

## Why this corpus exists

The single-repo run established two things, and the second is the reason
this document exists at all:

1. **26 positives cannot resolve a small effect.** `path_drift` fired
   *zero* times across 675 claims because bettermemory never deletes, so
   the one purpose-built detector has never been tested on anything.
2. **The corpus could not tell a detector from an oracle replay.**
   `claim_level_strict` scored J = 1.000 — arithmetically identical to
   `oracle_replica`, which peeks at the label. The window's diff *is* the
   t0→t1 transformation, so on a single repository the hunks are nearly a
   sufficient statistic for the oracle's own question.

So the multi-repo corpus is not merely a power fix. It is the only way to
find out whether the claim-level detector measures anything.

## The frame

`frame/top-pypi-packages-2026-07-01.json`, sha256
`c40ccdde2a07d48c25c31a9d9d8fcbfe8c166987b1b43aa47e02b695a01c71f1`,
15,000 packages in descending download order.

A **file**, not a query. The GitHub-stars frame was designed first and
rejected on measured grounds: identical search enumerations 15 minutes
apart returned the same set in a *different order*, with star counts
drifting; and a star-ranked Python frame is substantially a frame of
curated markdown lists containing no Python. A frame that must be pinned
by a manifest *after* the fact is reproducible by courtesy. This one is
reproducible by construction — a third party derives the identical
ordering from the committed bytes.

**The order is walked, not cut.** There is deliberately no "top N"
constant, because N would be a tunable knob. Screening runs down the
ranking until quotas fill; the rank of the last repository examined is
published. Moving the stopping point cannot change what precedes it.

**Stated bias.** Heavily-downloaded packages are mature, well-staffed and
conservative about deletion. This corpus is *widely-depended-on Python
packages*, not *Python code*. Private, under-maintained code — what this
product most often runs against — is unrepresented by construction.

## Design: case-control, named as such

The ≥20-deletions gate selects on a quantity correlated with the outcome
being measured. That **cannot be made unbiased**, so it is not pretended
away. Stratum **D** applies the gate; stratum **R** applies every other
filter and not the gate, drawn from the same walk of the same frame.
D-vs-R measures how far the gate moved the base rate. It does not close
the gap, and every prevalence figure this corpus produces is higher than
the wild.

All thresholds live in `select.py` as constants with their
justifications; those that are arbitrary say so.

## Predictions

**P1 — `path_drift` finally fires, and it is unimpressive.** In the
absolute-citation arm it flags ≥95% of claims whose file is absent at t1
and ≤2% of claims whose file survives. Reported as *an existence check
scoring well at existence checking*, not as a win. **MISSED if** TPR <
0.90 — which, after the case-collision reject, would mean a defect in
`detect_path_drift` that 675 zero-deletion claims could never reveal.

**P2 — the relative arm stays at exactly zero.** Zero path-drift flags
across every repo, class and window, despite hundreds of real deletions,
because `detect_path_drift` excludes relative paths by design. This is
the informative half: the product's *default* citation style gets no path
protection even when there is finally something to catch. **MISSED if**
any non-zero count.

**P3 — the file-level incumbent looks better here than on our own repo.**
Pooled flag rate in the relative arm lands in [0.35, 0.80] against
bettermemory's 97%, and pooled alerts-per-catch in [4, 15] against 25.1 —
because bettermemory is unusually churny. Pooled macro-J still < 0.15.
**MISSED if** macro-J ≥ 0.15, in which case the published 0.034 was a
property of one repository and the README's aggregate conclusion gets
softened in place.

**P4 — the 0.725 symbol AUROC was small-n noise and regresses.** Pooled
symbol-class AUROC of the commit count lands in [0.50, 0.65], now with
hundreds of positives instead of six. This predicts our own published
number was optimistic. **MISSED if** pooled AUROC ≥ 0.70 — in which case
the operating point really is the bug and the `> 0` threshold is worth
re-tuning.

**P5 — the retraction branch, and the most important line here.**
`claim_level_strict` stops matching `oracle_replica` on symbol claims:
pooled symbol precision ≤ 0.97 with at least 5 false positives. Named
mechanisms, so this is not a vibe — delete-then-re-add inside a window;
column-0 `def` lines inside docstrings and code samples; `@overload` and
platform-conditional re-declarations; `git log -p` omitting merge patches
so an evil-merge change reaches the tree-reading oracle but not the index.

It will **remain** identical on *path* claims, where its rule is the
oracle's own test and no corpus can separate them; registered now so a
null result there is not later sold as a finding.

**MISSED if** pooled symbol precision is exactly 1.000 — **and that
branch is pre-committed**: it would mean the claim classes are
diff-decidable *in general*, that the corpus was never the problem, and
that the roadmap's "get statistical power" item is answered in the
**negative** and must be retracted rather than celebrated.

**P6 — claim density does not generalise from n=1.** Pooled claims per
non-excluded `.py` file lands in [4.0, 9.0] against bettermemory's 8.8,
and the symbol share falls below 72%. Every power estimate made while
planning this rests on that 8.8. **MISSED if** outside [4.0, 9.0], in
which case the arithmetic was wrong in a direction that must be reported
*before* any detector number is discussed.

**P7 — mapping and screening yield.** ≥85% of frame rows walked map to a
repository (measured 37/40 on the frame head), and between 2% and 12% of
screened candidates reach stratum D. **MISSED if** < 8 stratum-D
qualifiers are found — published as underpowered, with no widening of the
frame and no extension of the walk to rescue it.

## Addendum, written after the draw and before any detector was run

The corpus was drawn (walked to rank 767, D=15 / R=15) and then inspected
before a single detector number existed. One property of stratum D was
not anticipated by the rules above and is declared here, with the
timestamp it deserves: **this text was committed before `corpus.py` ran
for the first time.**

**Seven of the fifteen D repositories are wholesale PACKAGE RELOCATIONS,
not prunings.** `mkdocstrings/griffe` moved `src/griffe/` to `packages/`
between its window ends: 46 of 46 files "deleted", while the repository's
total `.py` count went *up*, 86 → 90. The same signature — deleted equal
to the entire elected subdir — appears for `narwhals`, `dbt-core`,
`fastmcp`, `modal-client`, `chardet`, and `httpx2`.

The deletions are real in the only sense the oracle cares about: a memory
citing `src/griffe/loader.py` — a path in that repository, which does not
resolve in this one — after that move **is** stale, and `label_claim` is
right to say so. But relocation and pruning are different phenomena, and
conflating them would flatter every path-aware detector for a trivial
reason — when 100% of a repository's claims go false at once, `path_drift`
and `claim_level_strict` are perfect by construction, exactly the way
`oracle_replica` is.

The judges' review of the selection design flagged a "≥50% relocation
cut" as a threshold with no sensitivity arm. It was **not implemented**,
and that is a gap in the rules above, not a discovery made afterwards to
explain a result.

**So D is reported split, and the split is defined here, before the
numbers exist:**

- **D-relocated** — every non-excluded `.py` under the elected subdir is
  absent at t1. Reported, never used as the headline.
- **D-pruned** — partial deletion. **This is the D headline**, and the
  stratum against which P1 and P3 are scored.

If D-pruned turns out to hold fewer than 8 repositories, the corpus is
published as underpowered on the class the deletion gate existed to
create — with no re-draw, no widened frame, and no extension of the walk.

The draw itself is **not** revised. Re-drawing after seeing a property of
the sample is how a pre-registration becomes decoration.

## What is not claimed

- The oracle sees **structural** change only. A function whose behaviour
  changed under a stable name and signature reads `still_true`.
  Multi-repo does not fix that; it multiplies it, across codebases we
  know less well than our own.
- `citation_resolved_rate` is 100% **by construction** — every body is
  machine-generated and names its target in backticks. Real memory bodies
  are not like that (`bench/claims.py` measures the checkable/judgement
  split at roughly 64/36), so real-world performance is bounded by
  J_resolved × resolution_rate and only the first factor is measured.
- A private trial run is undetectable. No cryptographic fix is available
  to a single-author project. What reduces it: predictions specific
  enough to be embarrassing, and a published frame and script so a third
  party can draw an independent corpus. **Replication is the actual
  evidence**; everything here only makes this run auditable.
