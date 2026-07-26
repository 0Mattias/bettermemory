# Memory-rot benchmark

The first accuracy measurement of `staleness_verdict` — the mechanism the
README leads with, and the one that had none.

```sh
venv/bin/python bench/rot/run.py --days 60
venv/bin/python bench/rot/run.py --days 30 --json
```

## Method

Ground truth comes from git, not from a model. Pick a repository and two
commits (t0, t1). Extract fact-shaped claims from the tree at t0 purely
mechanically — a path exists, a top-level symbol is defined in a named
file, a module constant holds a literal. Re-evaluate each against the
tree at t1 with a checker, not a judge.

Each claim becomes a memory body citing it, anchored with
`verified_paths` and a `last_verified_at` at t0. At t1 the three signals
production uses — calendar age, path drift, commit drift — are computed
and fed to the **shipped** `compute_staleness_verdict`, not a
reimplementation.

Three arms are reported because they answer different questions:

- **`drift_only_relative_cite`** anchors verification one day ago so the
  calendar leg cannot fire, and cites paths the way a developer naturally
  writes them (`src/pkg/mod.py`). This is the informative arm.
- **`drift_only_absolute_cite`** is identical except the body cites the
  absolute path. It exists because `detect_path_drift` excludes relative
  paths by design, so the citation style alone decides whether the path
  leg can ever fire.
- **`shipped_default`** anchors 400 days ago, past
  `DEFAULT_VERIFICATION_STALE_DAYS`. A deliberate worst case showing what
  the calendar leg alone does; not a claim about typical usage, since
  real memories get re-verified.

## Results — bettermemory's own history, 2026-07-26

`drift_only_relative_cite`, 60-day window:

| class | n | false | flagged | unflagged-stale | precision | **J** | **Fisher p** | **alerts/catch** |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| path | 77 | 0 | 91% | n/a | 0% | n/a | n/a | n/a |
| symbol | 485 | 6 | 98% | 0% | 1% | 0.023 | **0.871** | 79.0 |
| literal | 113 | 20 | 96% | 0% | 18% | 0.043 | **0.454** | 5.5 |
| **ALL** | 675 | 26 | 97% | 0% | 4% | **0.034** | **0.415** | 25.1 |

Constant classifiers on the same claims:

| detector | flag rate | unflagged-stale | **J** |
| --- | --- | --- | --- |
| `always_flag` | 100% | **0%** | **0.000** |
| `never_flag` | 0% | 100% | **0.000** |
| `shipped_default` | 100% | **0%** | **0.000** |

## What this says

**The 0% miss rate is an artifact, not an achievement.** `always_flag`
also scores 0% unflagged-stale, because a detector that flags everything
cannot miss anything. Reported alone, that number is not evidence the
mechanism works — which is exactly how the first version of this document
reported it, and why the table above now carries J, Fisher p and
alerts-per-catch in the same row.

**The verdict is not statistically distinguishable from flagging at
random at the same rate.** One-sided Fisher exact against a rate-matched
random detector gives **p = 0.415** (60d) and **p = 0.176** (30d).
Youden's J — `TPR − FPR`, exactly 0.0 for every constant classifier — is
**0.034**. The margin over a coin is real in sign and negligible in size,
and on this corpus it is not significant.

**At its shipped default the product is a constant function.**
`shipped_default` flags 100% of claims in every class and both windows:
J = 0.000, Fisher p = 1.000, arithmetically identical to `always_flag`.
The 400-day anchor makes the calendar leg fire on everything, so the
drift legs cannot contribute. That is a defect, not a configuration
choice, and it is the most actionable finding here.

**The signal that exists lives entirely in one small class.** Symbol
claims are 72% of the corpus and carry none of it — J = 0.023 at
**p = 0.871**, which is 79 alerts per genuine catch. Literals are 17% of
the corpus and hold what little discrimination there is (5.5 alerts per
catch; p = 0.056 in the 30-day window, the only cell approaching
significance). Aggregates hide this completely.

**What a user actually experiences: 25 alerts per real catch.**
`always_flag` on the same corpus costs 25.9. The differentiator, as
shipped, costs the user essentially what flagging everything would cost.

**Every single flag came from `commit_drift`.** `path_drift` fired
exactly zero times across all 674 claims, in every arm
(`path_drift_flags: 0`). Two independent reasons, and both matter:

1. **Nothing was deleted.** Across `src`, `tests` and `docs` there were
   zero deletions or renames in either window — this project only adds.
   So the path leg had nothing legitimate to catch.
2. **Relative citations are not checked at all.** `detect_path_drift`
   excludes relative paths *by design* — verify.py's module docstring
   states it plainly: without an anchor, checking them would mean
   checking the cwd at retrieval time. So a memory citing
   `src/bettermemory/store.py`, which is how a developer naturally writes
   it, receives **no path-drift protection whatsoever**. Only commit
   drift and the calendar can ever fire for it.

The benchmark runs both citation styles as separate arms
(`drift_only_relative_cite` / `drift_only_absolute_cite`). On this
repository they are **identical**, because with no deletions in the
window there is nothing for the absolute arm to catch that the relative
arm misses. The difference between them is therefore *not* demonstrated
by this run — it is pinned directly by
`tests/test_bench_rot.py::test_relative_citations_get_no_path_checking_at_all`
and shown by direct probe. Reporting the arms as if they had converged on
a finding would be reading a result out of an absent phenomenon.

So `commit_drift` is doing all the work, and it knows only that
*something* in a cited file changed, never *what*. That is why path
claims — where nothing at all went false — were still flagged 91% of the
time.

**The detectable class is the one that rots least.** Path claims had a 0%
base rate — nothing was deleted. Literal claims, which `path_drift`
structurally cannot see even when cited absolutely, had an 18% base rate,
the highest by far. The one axis with a purpose-built detector is the axis
that barely drifts; the axis that drifts most has no detector at all.

## Caveats, including one that softens the headline

- **The oracle undercounts false claims, so real precision is better than
  4%.** It catches structural change only: a file removed, a symbol gone,
  a literal altered. A function whose *behaviour* changed while its name
  and signature persisted is labelled `still_true`, and a memory
  describing that behaviour would in fact be false. Semantic-only drift
  is invisible to this harness — the same class the product itself cannot
  see. The true base rate is higher than measured and the true precision
  correspondingly better; by how much is unknown.
- **n = 1 repository, and an unusually churny one.** bettermemory took
  189 commits in the two weeks before this run, so nearly every file in
  `src/` was touched in both windows. `commit_drift` fires on any touched
  file, so a high flag rate is partly a property of *this* repo. A calmer
  codebase would show a lower flag rate and better precision. Running
  this across a set of pinned third-party repositories is the obvious and
  necessary next step, and until it happens these numbers describe one
  project rather than the mechanism in general.
- **The path leg is untested here, not vindicated.** Zero deletions in
  the window means this run says nothing about how well `path_drift`
  performs when there is something to catch. A repository that actually
  removes files is needed before any claim about it is supported.
- **`commit_drift` is modelled as a boolean.** The harness records 1 for
  any file changed in the window rather than a true commit count. The
  verdict only tests `> 0`, so this is faithful for what is being graded,
  but the reported `commit_drift` counts are not commit counts.
- **Claims are synthesised, not written by anyone.** They are the shapes a
  real memory *contains*, not real memories. A real body mixes a checkable
  claim with judgement the verdict can never speak to — `bench/claims.py`
  measures that split at roughly 64/36.

## What would actually improve the verdict

The target is **J**, not recall. Recall is already 1.0 and worthless at
this flag rate; the work is raising discrimination without giving it back.

1. **Fix the default operating point.** `shipped_default` being a
   constant function is a defect. A calendar leg that fires on everything
   older than 30 days makes the drift legs unreachable in exactly the
   configuration most users run.
2. **Make `commit_drift` ask the right question.** It currently answers
   "did this file change?" when the useful question is "did the thing this
   memory cites change?" — whether the cited *symbol* appears in the diff
   hunks, or whether the cited literal's line moved. Both are answerable
   from git data this harness already reads. Aim it at the symbol class:
   72% of claims, p = 0.871, 79 alerts per catch.
3. **Get statistical power.** 26 positives cannot resolve a small effect.
   A multi-repo corpus targeting ≥150 positives — and including
   repositories that actually delete files, so `path_drift` gets its first
   real test after 0 flags in 675 claims — is what makes any of these
   numbers conclusive rather than suggestive.

Each is now gradeable: change it, re-run, and read J and p. That is the
first time this project has been able to say that about its headline
feature, and it is worth more than the number itself.
