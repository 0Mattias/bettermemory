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

`drift_only_relative_cite`, aggregated over all claim classes:

| window | claims | actually false | base rate | flagged | unflagged-stale | false alarm | precision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 60 days | 674 | 26 | 3.9% | **96.7%** | **0%** | 96.6% | **4.0%** |
| 30 days | 820 | 15 | 1.8% | **89.2%** | **0%** | 88.9% | **2.1%** |

Per class, 60-day window:

| class | n | false | flagged | false alarm | precision |
| --- | --- | --- | --- | --- | --- |
| path | 77 | 0 | 91% | 91% | 0% |
| symbol | 484 | 6 | 98% | 98% | 1% |
| literal | 113 | 20 | 96% | 96% | 18% |

`shipped_default` flags **100%** of claims in every class and both
windows, which is what a 400-day-old anchor is supposed to do.

## What this says

**The verdict never misses.** `unflagged_stale_rate` is 0% in every
class and both windows — no claim that had actually gone false was served
as `fresh`. For a product whose stated job is refusing to let a rotted
memory be quoted back at you, that is the number it needed to hit.

**It never misses because it flags almost everything.** Precision is
2–4%. A user working in this repository would see
`spot_check_recommended` on roughly nineteen memories in twenty, and
roughly nineteen of those twenty flags would be on claims that were still
perfectly true. A signal that fires that often does not carry
information; it trains the reader to ignore it.

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

Nothing here is a proposal to weaken the signal — a 0%
unflagged-stale rate is worth protecting. The problem is that
`commit_drift` answers "did this file change?" when the useful question
is "did the thing this memory cites change?". Both harder questions are
answerable with the same git data the harness already reads: whether the
cited *symbol* appears in the diff hunks, or whether the cited literal's
line changed. Either would cut the false-alarm rate without touching
recall on the classes that matter.

That is a design direction this benchmark now exists to grade, rather
than a change to make on intuition.
