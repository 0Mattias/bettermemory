# Memory-rot benchmark

The first accuracy measurement of `staleness_verdict` — the mechanism the
README leads with, and the one that had none.

Two runs are reported. A single-repository pilot on bettermemory's own
history, and a **pre-registered 30-repository corpus** (37,635 claims,
8,627 positives) that supersedes the pilot's aggregates and retracts one of
its roadmap items outright. If you read one section, read
[the scorecard](#the-scorecard-4-of-7-hit).

```sh
venv/bin/python bench/rot/run.py --days 60
venv/bin/python bench/rot/run.py --days 30 --json
venv/bin/python bench/rot/run.py --t0 053ab9de4520   # reproduce a published run
```

**Pin `--t0` for anything you intend to compare.** Without it t0 is resolved
from `--days` against the wall clock, so it slides between runs — during the
session that produced the tables below it moved from `5910a39a` to `053ab9de`
in under an hour, changing the commit count from 368 to 363. Every result
file records its full t0 and t1 shas and whether t0 was pinned.

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
  `DEFAULT_VERIFICATION_STALE_DAYS`. It exists to answer "what does a
  user who has not re-verified in a year actually get?".
  Until 3.30.0 the answer was "an alert on everything" — see the
  constant-function finding below, which this arm is what surfaced.
  Since the fix it scores **identically to `drift_only_relative_cite`**,
  and that convergence is the point: the calendar leg no longer erases
  the drift legs, so the arm with the calendar disabled and the arm with
  it maximally expired now agree. Kept as the regression guard for
  exactly that (`test_the_shipped_default_is_not_a_constant_function`).

## Results — bettermemory's own history, 2026-07-26

60-day window, t0 `053ab9de4520` -> t1 `388b5be75472`, 675 claims, 363
commits / 3,941 hunks indexed, 0 hunk-parse mismatches.

**The shipped signal (`commit_drift`, file-level):**

| class | n | false | flagged | unflagged-stale | precision | **J** | **Fisher p** | **alerts/catch** | AUROC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| path | 77 | 0 | 91% | n/a | 0% | n/a | n/a | n/a | n/a |
| symbol | 485 | 6 | 98% | 0% | 1% | 0.023 | **0.871** | 79.0 | 0.725 |
| literal | 113 | 20 | 96% | 0% | 18% | 0.043 | **0.594** | 5.5 | 0.484 |
| **ALL** | 675 | 26 | 97% | 0% | 4% | **0.034** | **0.415** | 25.1 | 0.550 |

**The claim-level detector, same claims, same arm:**

| detector | class | flagged | unflagged-stale | precision | **J** | **alerts/catch** |
| --- | --- | --- | --- | --- | --- | --- |
| strict | ALL | 4% | 0% | 100% | **1.000** | **1.0** |
| weak | symbol | 6% | 0% | 19% | 0.948 | 5.2 |
| weak | literal | 18% | 0% | 100% | 1.000 | 1.0 |
| weak | **ALL** | 8% | 0% | 51% | **0.962** | **2.0** |

Reference classifiers on the same claims:

| detector | flag rate | unflagged-stale | **J** |
| --- | --- | --- | --- |
| `always_flag` | 100% | **0%** | **0.000** |
| `never_flag` | 0% | 100% | **0.000** |
| `shipped_default` *(pre-3.30.0)* | 100% | **0%** | **0.000** |
| `shipped_default` *(3.30.0, same window)* | 96.7% | 0% | **0.034** |
| `oracle_replica` *(peeks at the label)* | 3.85% | 0% | **1.000** |

Both `shipped_default` rows are real measurements of this same 675-claim
window; the first is what the product scored before the constant-function
defect was fixed and is kept so the fix is auditable rather than
retroactive. The committed `results/bettermemory-*d-*.json` artifacts —
the published-run shape, one per window — carry the 3.30.0 numbers.
Read the glob, not the directory: `results/` also holds
`escalation-off-60d-2026-07-31.json`, a counterfactual over this same
675-claim window whose *drift* arms were produced with
`_COMMIT_DRIFT_ESCALATES` monkeypatched off and describe no build that
ever shipped. Its `shipped_default` arm *does* carry the 3.30.0 numbers,
bit-identically, because the demotion branch never reads that switch —
the seam the retraction section below turns on. The artifact says as
much in its own `counterfactual` prefix block.

## The claim-level detector reaches 1.000, and that is not a win

`claim_level_strict` scores **J = 1.000, precision 100%, zero misses** — it
flags exactly 26 of 675 claims, and exactly the 26 that went false. Compare
the `oracle_replica` row: 3.85% of 675 is **the same 26**. The detector is
*arithmetically identical to peeking at the answer*, on both windows
(15/820 at 30 days).

That is a fact about this benchmark, not about the product. The window's
diff **is** the transformation from the t0 tree to the t1 tree. The oracle
asks "is `foo` still a top-level def at t1?"; the detector asks "did a
column-0 `def foo` net-disappear across that transformation". Given the
claim was true at t0, those are nearly the same question — the bag of hunks
is very close to a *sufficient statistic* for the oracle's own decision. So
`oracle_replica` is printed in the same table as the result it defuses,
exactly as `always_flag` is printed beside the 0% miss rate.

**What survives the objection is the contrast, not the score.** The shipped
file-level signal costs **25.1 alerts per genuine catch**. The `weak` tier —
which does *not* collapse onto the oracle (51% precision, so it is making
real mistakes) — costs **2.0**, at the same zero miss rate. On the 30-day
window it is 48.7 against 2.2. The information needed to cut false alarms by
an order of magnitude was in git data the harness was already reading; the
signal was throwing it away by asking "did this file change?" instead of
"did the thing this memory cites change?".

**This is why nothing here has been shipped into `verify.py`.** A corpus
where the target is diff-decidable cannot distinguish a genuine detector
from a well-dressed oracle replay, so J = 1.000 is not evidence a user would
feel. Building the detector measured the size of the prize; it did not earn
it.

> **Superseded in part.** This paragraph then named the multi-repo corpus
> as the thing that would make shipping safe. It was built, and it did not:
> on 30 repositories and 37,635 claims the strict tier is *still*
> arithmetically identical to `oracle_replica`. The corpus was never the
> binding constraint — see [P5](#p5-fired-and-the-pre-committed-reading-is-a-retraction).

### Two smaller findings that only the continuous score could state

**The commit COUNT carries signal the `> 0` threshold discards.** For symbol
claims the count scores **AUROC 0.725 (permutation p = 0.030)** while the
boolean decision built from the same number scores J = 0.023 at p = 0.871.
That combination — near-zero J, AUROC well above a coin — means the signal
is real but the *operating point* is wrong, which is a different repair from
"the signal isn't there". It is also not significant at this project's own
p<0.01 bar, on six positives.

**And for literal claims the same count is very slightly ANTI-informative:
AUROC 0.484.** Ranking by how hard a file was hit puts genuinely-rotten
literal claims *below* fresh ones marginally more often than chance. The old
boolean model could not express either number: with every score pinned to 0
or 1, almost every pair was a tie and AUROC was degenerate by construction.

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

**~~At its shipped default the product is a constant function.~~ FIXED
IN 3.30.0 — and this was the most actionable finding the benchmark
produced.** As measured here, `shipped_default` flagged 100% of claims
in every class and both windows: J = 0.000, Fisher p = 1.000,
arithmetically identical to `always_flag`. The cause was in
`compute_staleness_verdict`, not in the anchor: a `never`/`stale`
verification status pre-empted **both** drift inputs outright, so past
the 30-day window the calendar leg fired on everything and the drift
legs could not contribute. The fix lets a measured-zero commit leg —
"no commit touched anything this memory cites since its own last
verification" — stand the calendar leg down, while `None` ("the leg
could not ask") deliberately still does not. Re-measured on this exact
pinned window, `shipped_default` moves to **J = 0.034 at a 96.7% flag
rate (60d)** and **J = 0.111 at 89.2% (30d)**, converging exactly onto
`drift_only_relative_cite`.

Read that honestly: the default operating point is no longer a constant
function, but J = 0.034 is the *same weak signal the informative arm
always had*. The defect that has been removed is the calendar leg
erasing the measurement — not the mediocrity of the measurement, which
is a separate and still-open problem (item 4 below).

**The signal that exists lives entirely in one small class.** Symbol
claims are 72% of the corpus and carry none of it — J = 0.023 at
**p = 0.871**, which is 79 alerts per genuine catch. Literals are 17% of
the corpus and hold what little discrimination there is (5.5 alerts per
catch; p = 0.056 in the 30-day window, the only cell approaching
significance). Aggregates hide this completely.

**What a user actually experiences: 25 alerts per real catch.**
`always_flag` on the same corpus costs 25.9. The differentiator, as
shipped, costs the user essentially what flagging everything would cost.
**On the 30-repository corpus this is 3.4** — much better, and still four
fifths of all claims flagged, against 1.0 for the claim-level tier.

**Every single flag came from `commit_drift`.** `path_drift` fired
exactly zero times across all 675 claims, in every arm
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

~~**The detectable class is the one that rots least.**~~ **REVERSED BY THE
CORPUS.** On bettermemory, path claims had a 0% base rate (nothing is ever
deleted here) against literals at 18%, which read as "the one axis with a
purpose-built detector is the axis that barely drifts". Across 30
repositories the ordering flips: **path claims rot MOST — a 30.8% base
rate**, ahead of literals (30.3%) and symbols (21.4%). The purpose-built
detector is aimed at the right class after all; the finding was an artifact
of measuring a project that only adds. What stands is the narrower point
that `path_drift` cannot see it in the default citation style (P2).

## Results — 30 repositories nobody chose, 2026-07-26

The multi-repo corpus called for above. Frame, draw and seven falsifiable
predictions were committed in `PREREGISTRATION.md` **before** any
repository was screened; `scorecard.py` grades each one mechanically off
the results JSON, so hit/MISSED is computed rather than narrated.

180-day windows, PyPI-download frame walked to rank 767, **30 repositories,
37,635 claims, 8,627 false** (22.9% base rate against bettermemory's 3.9%).
Zero repositories failed to run.

```sh
venv/bin/python bench/rot/corpus.py     # clone, run, pool
venv/bin/python bench/rot/scorecard.py  # grade P1-P7
```

### The scorecard: 4 of 7 hit

| | prediction | result | |
| --- | --- | --- | --- |
| **P1** | `path_drift` (absolute arm) TPR ≥ 0.90 | TPR **0.957**, false-alarm rate **0.000** | hit |
| **P2** | relative arm flags **exactly zero** | **0.000** across all 37,635 claims | hit |
| **P3** | pooled macro-J < 0.15 | **0.2875** | **MISSED** |
| **P4** | symbol AUROC in [0.50, 0.65] | **0.5446** (p = 5e-05, 6,705 positives) | hit |
| **P5** | `claim_level_strict` symbol precision ≤ 0.97 | **0.9999** — one false positive in 6,705 | **MISSED** |
| **P6** | claims per `.py` file in [4.0, 9.0] | **8.96** | hit |
| **P7** | ≥ 8 stratum-D qualifiers | **7** | **MISSED** |

**The shipped signal (`commit_drift`, file-level), pooled:**

| class | n | false | flagged | precision | **J** | **alerts/catch** | AUROC |
| --- | --- | --- | --- | --- | --- | --- | --- |
| path | 5,272 | 1,625 | 68% | 45% | 0.457 | 2.2 | 0.674 |
| symbol | 31,382 | 6,705 | 80% | 27% | 0.260 | 3.7 | 0.545 |
| literal | 981 | 297 | 75% | 40% | 0.360 | 2.5 | 0.669 |
| **ALL** | **37,635** | **8,627** | **78%** | **29%** | **0.2875** | **3.4** | 0.555 |

**The claim-level detectors, same claims:**

| detector | class | flagged | precision | **J** | **alerts/catch** |
| --- | --- | --- | --- | --- | --- |
| strict | symbol | 21% | **99.99%** | **1.000** | 1.0 |
| strict | **ALL** | 23% | 99.93% | **0.991** | **1.0** |
| weak | symbol | 23% | 92.5% | 0.978 | 1.1 |
| weak | **ALL** | 24% | 94.0% | **0.973** | **1.1** |
| `oracle_replica` *(peeks)* | ALL | — | — | **1.000** | — |

Repo-level paired comparison, the unit no single large repository can
carry: `claim_weak` beats the incumbent on alerts-per-catch in **19
repositories, loses in 0**, ties in 7 (sign test p < 0.001).

### P5 fired, and the pre-committed reading is a retraction

`claim_level_strict` scores **J = 1.000 on symbol claims — precision
0.9999, exactly one false positive in 6,705 — arithmetically identical to
`oracle_replica`**, which peeks at the label. That is the same tie the
single-repo run produced, reproduced at **55x the claim count on thirty
repositories neither party chose.**

The pre-registration committed to this reading in advance, and it is
honoured here rather than renegotiated:

> it would mean the claim classes are diff-decidable *in general*, that
> the corpus was never the problem, and that the roadmap's "get
> statistical power" item is answered in the **negative** and must be
> retracted rather than celebrated.

**So it is retracted.** More repositories were the wrong instrument. The
circularity is not a small-n artifact and not a property of this project's
history — it is inherent to grading *structural* claims against
*structural* ground truth, because the diff between two trees is very
nearly the answer to "did this symbol survive". No corpus of git
repositories can separate the two. The next evidence has to be a different
**kind**: real memory bodies rather than synthesised claims, or
user-visible outcomes rather than structural labels.

What survives is what survived before, now on a real population: the
**contrast**. 3.4 alerts per catch becomes 1.0, and the weak tier — which
does *not* collapse onto the oracle, at 94% precision — costs 1.1, winning
in 19 repositories and losing in none.

### P3 fired: the published 0.034 was a property of one repository

Pooled macro-J is **0.2875**, an order of magnitude above bettermemory's
own 0.034, so the aggregate conclusion is softened in place above rather
than left standing. The direction is worth stating plainly: **the shipped
signal looks considerably better on a normal population than on this
project.** Alerts-per-catch is 3.4 against 25.1 here — below the predicted
[4, 15] band, i.e. better than predicted. bettermemory took 189 commits in
two weeks and is unusually churny; `commit_drift` fires on any touched
file, so this repository was close to a worst case.

That does not rescue the mechanism. J = 0.2875 with 78% of all claims
flagged is still a detector that alarms on four fifths of the corpus, and
the claim-level tier gets J = 0.99 at a quarter of the flag rate.

### P1 and P2: the path leg finally got a real test, and the default still gets nothing

675 zero-deletion claims could never test `path_drift`. Against 8,627 real
deletions it flags **95.7% of claims whose file is gone with a 0.0%
false-alarm rate and 100% precision** — an existence check scoring well at
existence checking, reported as such.

P2 is the informative half, and it is now demonstrated rather than argued
from a unit test: in the relative-citation arm `path_drift` fired
**exactly zero times across all 37,635 claims**, with thousands of genuine
deletions in front of it. `detect_path_drift` excludes relative paths by
design, so **the citation style a developer naturally writes gets no path
protection at all, even when there is finally something to catch.**

### The anchored-relative arm, 2026-07-30: the gap above, measured shut

P2 stands as graded — it describes the three arms that existed when it was
written, and `drift_only_relative_cite` still reads exactly 0.0 in the run
below. What follows is a **fourth arm appended after the fact**, not a
regrade: `drift_only_relative_cite_anchored` resolves each relative citation
against the memory's own recorded `origin.worktree_root` before checking
whether the file exists. Artifact:
`results/multirepo-anchored-2026-07-30.json`. The frozen `multirepo.json`
and `scorecard.json` are untouched, so every published prediction keeps the
grade it was given.

Pooled over the same 37,635 claims:

| arm | flag rate | precision | false alarms | J | alerts/catch |
|---|---|---|---|---|---|
| `drift_only_relative_cite` | 0.0% | — | — | 0.000 | — |
| `drift_only_relative_cite_anchored` | 0.73% | **1.000** | **0.0%** | 0.032 | **1.0** |

On path-shaped claims alone (n=5,272, base rate 30.8%) the anchored arm
reaches J=0.0505 at precision 1.000, Fisher p=0.0. Repo-level paired across
the corpus: **19 wins, 0 losses, 7 ties**.

Read it honestly in both directions. **Zero false alarms and one alert per
catch** is the number that matters, because the failure mode this whole
directory exists to avoid is a detector that cries wolf — the shipped
file-level incumbent sits at 3.4 alerts per catch with 0.2945 precision, and
this leg is strictly better on both. But **the recall is small**: 0.73% of
claims, against a 22.9% base rate. This is not a detector that finds most
rot. It closes a leg that was firing on *nothing at all* and makes it fire,
precisely, on the subset it can actually prove. The absolute-citation arm's
J=1.0 on the same path claims is the ceiling, and the distance between them
is simply how few claims are written as checkable relative citations.

The cross-host case is the one that would have made this dangerous: a store
synced from another machine carries a `worktree_root` that does not exist
locally, and checking against it would mark every citation missing at once —
`always_flag` wearing a new hat. The check fails open when the recorded root
is absent or unstattable, which is a deliberate reversal of the bias
`origin.py` records for files *underneath* a live worktree, scoped to the
root's own liveness and argued at the code.

### P7 fired: stratum D is underpowered, and it is published that way

Only **7** repositories cleared the deletion gate against a floor of 8, so
per the pre-registration this corpus is published as **underpowered on the
class the deletion gate existed to create** — with no re-draw, no widened
frame, and no extension of the walk. Re-drawing after seeing a property of
the sample is how a pre-registration becomes decoration.

The addendum's D-split turned out to be moot, in a way worth recording.
All **seven** repositories it named as wholesale package relocations
(`griffe`, `narwhals`, `dbt-core`, `fastmcp`, `modal-client`, `chardet`,
`httpx2`) were screened into D and then finalised into **R** by the
deletion-spread gate — a wholesale move lands in one or two commits, below
`MIN_DELETION_COMMITS`. So `D-relocated` is empty, the split never had to
be applied by hand, and an existing gate had already been doing the work
the addendum proposed to do manually. That reclassification is also *why*
D fell from 15 to 7, which is what made P7 miss.

### One place the scorecard is weaker than the prediction it grades

P6 predicted two things: claims per file in [4.0, 9.0], **and** that the
symbol share would fall below 72%. Density landed at 8.96 and the
prediction is graded **hit** — but the symbol share is **83.4%**, well
above the 72% it predicted to undercut. Only the density clause carries a
`MISSED if` threshold, so `scorecard.py` cannot see the half it got wrong.
That is a defect in how P6 was written, recorded here rather than quietly
enjoyed, and the graded-hit stands because changing a threshold after
seeing the number is the exact move this document exists to prevent.

### The commit-escalation gate fired, 2026-07-31, and the reading is a retraction

One more pre-registration lived outside `PREREGISTRATION.md`, in a source
comment above `_COMMIT_DRIFT_ESCALATES` in `verify.py`: once the path-drift
provenance split and the anchored-relative arm shipped, re-run these arms
pooled, and **if alerts-per-catch for the escalating tier is still ≥ 1.5,
the commit leg is carrying residual noise and the switch flips to False.**
Both preconditions shipped; the condition is live and is graded here.

**Which number it means, since this artifact carries two that read opposite
ways.** The trigger is `pooled.file_level_incumbent.ALL.alerts_per_catch` =
**3.4** in `results/multirepo-anchored-2026-07-30.json` — over the line.
The other candidate, `path_drift_anchored_relative_arm.ALL.alerts_per_catch`
= 1.0, is under the line and would read "stays True", but it grades the
**path** leg, whose flags are not the commit term at all.
`file_level_incumbent` is scored on `_MODES[0]` rows, where the calendar leg
is stood down and `path_drift` fires exactly zero times, so **100% of its
flags are the escalating commit term.** It is the only column here that
measures what the flip would remove.

So the condition fired. **The flip is refused anyway, and the gate is
retracted** — on a measurement, not on reluctance. The switch was
monkeypatched to `False` and this harness re-run over the pinned 60-day
window of `results/bettermemory-60d-2026-07-26.json` (t0 `053ab9de`, t1
`388b5be7`). The control run — same driver, switch untouched — reproduces
that published artifact bit for bit on all three arms it carries, on
`detectors` and on `baselines`, so the instrument is the published one.
Artifact: `results/escalation-off-60d-2026-07-31.json`.

| arm | flag rate | J | unflagged stale | alerts/catch |
|---|---|---|---|---|
| any drift arm, switch on | 96.74% | 0.034 | 0.0% | 25.1 |
| any drift arm, switch off | **0.00%** | **0.000** | **100%** | — |
| `shipped_default`, either way | 96.74% | 0.034 | 0.0% | 25.1 |

Every decision column in the off row equals the `never_flag` baseline. That
is the **mirror image of the `always_flag` constant function** 3.30.0 fixed
and postmortemed — the same failure with the sign reversed, and the reason
the flip is not a tuning step. `shipped_default` is bit-identical between
the runs because the demotion branch reads `commit_drift_count` directly and
bypasses the switch, which is exactly the separation `58a4fa4` built and
also the reason the flip would look harmless from the shipped arm alone.

**The gate's premise is falsified on disk.** It assumed the anchored path
leg would substitute for what the commit leg does. That leg reaches
`flag_rate` 0.0073 at `unflagged_stale_rate` 0.968 pooled — precise where it
fires and silent nearly everywhere (the section above says so in its own
words: "not a detector that finds most rot"). Subtracting the commit term
does not trade noise for a cleaner signal; it removes the only escalating
term the verdict has. J = 0.2875 at 3.4 alerts per catch, against
`always_flag`'s J = 0.000 at 4.4, is a **weak** signal — and nothing is what
the flip measures.

What the gate got right is that 3.4 is too expensive to keep forever. The
answer is a **replacement measured first**, not a subtraction: the
claim-level `weak` tier costs 1.1 alerts per catch at 94% precision on the
same corpus, and needs write-time claims before it can ship. That is the
`docs/ROADMAP.md` ordering, not a benchmark result.

Two things this retraction does not claim. It is one window of one
repository, chosen because the switch is what it grades and that window is
the pinned one; the pooled 30-repository numbers above are what carry the
premise half. And the off-run artifact deliberately sits outside the
`bettermemory-*d-*.json` glob that `tests/test_bench_rot.py` uses to assert
arm convergence on every published run — it separates those arms by
construction, and loosening a guard to admit a counterfactual would cost
more than the artifact is worth.

## Caveats, including one that softens the headline

- **The oracle undercounts false claims, so real precision is better than
  4%.** It catches structural change only: a file removed, a symbol gone,
  a literal altered. A function whose *behaviour* changed while its name
  and signature persisted is labelled `still_true`, and a memory
  describing that behaviour would in fact be false. Semantic-only drift
  is invisible to this harness — the same class the product itself cannot
  see. The true base rate is higher than measured and the true precision
  correspondingly better; by how much is unknown.
- ~~**n = 1 repository, and an unusually churny one.**~~ **ANSWERED, AND
  THE SUSPICION WAS RIGHT.** The 30-repository corpus puts pooled J at
  0.2875 against this project's 0.034, and alerts-per-catch at 3.4 against
  25.1 — bettermemory (189 commits in two weeks) was close to a worst case,
  and the shipped signal looks materially better on a normal population.
  The single-repo aggregate above should be read as one churny project, not
  as the mechanism.
- ~~**The path leg is untested here, not vindicated.**~~ **TESTED.**
  Against 8,627 real deletions it flags 95.7% of gone-file claims at a 0.0%
  false-alarm rate — and fires **exactly zero times** in the relative-
  citation arm, which is the default style. See P1/P2 above.
- **The corpus is widely-depended-on Python packages, not Python code.**
  Heavily-downloaded packages are mature, well-staffed and conservative
  about deletion; the frame says so up front. Private, under-maintained
  code — what this product most often runs against — is unrepresented by
  construction, and the ≥20-deletions gate on stratum D selects on a
  quantity correlated with the outcome, so every prevalence figure here is
  higher than the wild.
- **Stratum D is underpowered: 7 qualifiers against a pre-registered floor
  of 8.** Published as such, with no re-draw.
- ~~**`commit_drift` is modelled as a boolean.** The harness records 1 for
  any file changed in the window rather than a true commit count.~~
  **RETRACTED** — `commit_counts_touching` now emits real per-path commit
  counts from one `git log --name-only` pass, which is what made the AUROC
  column above computable. The verdict is unchanged, since
  `compute_staleness_verdict` still tests only `> 0`; what changed is that
  the benchmark can now ask whether a *better* threshold exists. It found
  that for symbol claims one might (AUROC 0.725) and for literal claims one
  does not (0.484).
- **Claims are synthesised, not written by anyone.** They are the shapes a
  real memory *contains*, not real memories. A real body mixes a checkable
  claim with judgement the verdict can never speak to — `bench/claims.py`
  measures that split at roughly 64/36.

## What would actually improve the verdict

The target is **J**, not recall. Recall is already 1.0 and worthless at
this flag rate; the work is raising discrimination without giving it back.

1. ~~**Fix the default operating point.**~~ **DONE IN 3.30.0, AND IT WAS
   A ONE-BRANCH BUG RATHER THAN A TUNING PROBLEM.** The diagnosis blamed
   the 400-day anchor; the actual cause was that
   `compute_staleness_verdict` let a `never`/`stale` status pre-empt both
   drift inputs, so no anchor value could have reached the drift legs.
   Fixed by letting a measured-zero commit leg stand the calendar leg
   down (and only that leg: `None` means "could not ask", and path
   existence alone is too weak — of 15 missing-path alerts raised from
   body prose on the live store, ~0 were real drift, against 3 of 3 for
   anchored attestations). `shipped_default` now scores identically to
   `drift_only_relative_cite` on both windows. **What this did NOT do is
   raise the ceiling**: J goes 0.000 -> 0.034 (60d) and 0.000 -> 0.111
   (30d) because the default now *reaches* the existing signal, not
   because the signal improved. Item 4 remains the real work.
2. ~~**Make `commit_drift` ask the right question.**~~ **DONE, AND THE
   ANSWER WAS NOT A NUMBER TO SHIP.** The claim-level detector is built
   (`build_binding_index`, `claim_level_drift`) and measured above: 25.1
   alerts per catch becomes 2.0 at the same zero miss rate. But its strict
   tier is arithmetically identical to `oracle_replica`, so this corpus
   cannot certify it. The finding is the *size of the gap*, not the score.
3. ~~**Get statistical power.**~~ **RETRACTED — DONE, AND IT ANSWERED IN
   THE NEGATIVE.** The corpus was built (30 repositories, 37,635 claims,
   8,627 positives — 330x the target) and `claim_level_strict` is *still*
   arithmetically identical to `oracle_replica` on symbol claims: J = 1.000,
   one false positive in 6,705. Power was never the binding constraint. A
   corpus of git repositories cannot certify this detector at any size,
   because grading structural claims against structural ground truth makes
   the tree-diff nearly a sufficient statistic for the oracle's own
   question. This item is closed as a dead end, exactly as
   `PREREGISTRATION.md` committed to closing it if P5 missed.
4. **Get evidence of a different KIND.** The only remaining route. Either
   grade against **real memory bodies** rather than machine-generated ones
   — `citation_resolved_rate` is 100% here *by construction*, while
   `bench/claims.py` measures the real checkable/judgement split at roughly
   64/36, so real-world performance is bounded by J_resolved x
   resolution_rate and only the first factor has ever been measured — or
   grade against **user-visible outcomes** rather than structural labels.
   Both are harder than another corpus. That is the point: the cheap axis
   is exhausted.
5. ~~**Resolve body- or attestation-cited commit SHAs read-side.**~~
   **MEASURED AND REJECTED 2026-07-26.** The repair `1a2d88e` promised
   when it retired the write-side commit-SHA marker: ask whether a cited
   commit still exists, is still an ancestor of HEAD, and how far HEAD has
   moved since. All three rules fail on arithmetic, not on judgement. The
   *distance* rule fires on **34 of 34** SHA-carrying in-repo memories in
   the live store (min 3 commits, median 188, max 685 — nothing at zero,
   so no threshold quiets it): **J = 0.000**, arithmetically `always_flag`,
   and the memories it would flip are exactly the SHA carriers already
   reading fresh, so a zero-git predictor reproduces its whole output.
   *Existence* changes zero verdicts and both its live fires are on
   permanently-true history. *Ancestry* fires zero times, and its answer is
   a property of local `git gc` rather than of the project. The corpus is
   what condemns it: across 4,647 merged pull requests in 29 of these 30
   repositories, **3,573 head SHAs end up unreachable from the default
   branch — and all 3,573 belong to work that MERGED**, so under squash and
   rebase merge the signal's dominant firing mode is "the change you
   described shipped". J = 0.231 pooled, 0.053 median, and exactly 0.000 in
   11 of 28 repositories. Note the instrument's shape: this is **a first
   instance of** the real-memory-bodies evidence item 4 asks for, not that
   instrument — item 4 stays fully open, because closing one candidate
   signal against one store is not closing the axis. `CLAIM_CLASSES` is
   pre-registered as `('path', 'symbol', 'literal')` with no commit class
   and no oracle, and adding one lands straight on the `oracle_replica`
   objection that closed item 3: "does this commit exist in this repo" is
   decided by the same git data any label would come from. Full record and
   the honest cost of the class left uncovered: the `SHA_MARKER` tombstone
   in `src/bettermemory/durability.py`.

## What the claim-level detector is, and what stops it cheating

One `git log -p -U0` pass builds an inverted index of *column-0 binding
tokens* — `def`/`class`/`async def` and module-level assignments — found on
changed lines. Claims then resolve by dict lookup. Two rules keep it from
quietly becoming a second copy of the oracle:

1. **`build_binding_index` takes the diff text and nothing else.** It cannot
   see a claim, so it cannot look one up. Pinned by a signature test.
2. **The claim side enters only as the rendered memory body**, parsed by
   `parse_claim_citation` — the same material a production implementation
   reads off a real memory. Passing the `Claim` dataclass would hand the
   detector structured truth the product never has, and would make the
   value comparison privileged rather than fair.

Deliberately unused: git's `@@ … @@` section headings. This repo sets no
Python diff driver, so git falls back to its default funcname heuristic, which
labels hunks `class Store:` (136×), `__all__ = [` (61×) and
`def add_subparser(` (84×). Every method-body edit inside `Store` would
report `class Store:`, making a heading-keyed detector a body-churn
amplifier wearing a def-shaped label.

**A body-only edit is deliberately not drift.** `label_claim` matches a
definition by name and never reads its contents, so a body edit leaves the
claim true *by construction*. Counting body churn could only manufacture
false alarms — today's failure, restored under a new name.

**One bug worth recording, because it cost 12 of 20 literal catches.** The
obvious implementation splits the claimed value into lines and looks for
those lines in the diff. It finds almost nothing: Python's implicit
concatenation means a long constant's *logical* lines and its *physical*
source lines are different objects, and a value with no newline at all still
occupies a dozen lines of source. The fix is to invert the test — decode
each changed physical line back to the text it contributes and ask whether
that text appears anywhere in the claimed value.

Each is now gradeable: change it, re-run, and read J and p. That is the
first time this project has been able to say that about its headline
feature, and it is worth more than the number itself.
