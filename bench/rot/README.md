# Memory-rot benchmark

The first accuracy measurement of `staleness_verdict` — the mechanism the
README leads with, and the one that had none.

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
  `DEFAULT_VERIFICATION_STALE_DAYS`. A deliberate worst case showing what
  the calendar leg alone does; not a claim about typical usage, since
  real memories get re-verified.

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
| `shipped_default` | 100% | **0%** | **0.000** |
| `oracle_replica` *(peeks at the label)* | 3.85% | 0% | **1.000** |

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
feel. The multi-repo corpus in item 3 below — repositories neither party
chose, with enough positives to resolve a small effect — is the thing that
would make that call safe. Building the detector measured the size of the
prize; it did not earn it.

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

1. **Fix the default operating point.** `shipped_default` being a
   constant function is a defect. A calendar leg that fires on everything
   older than 30 days makes the drift legs unreachable in exactly the
   configuration most users run.
2. ~~**Make `commit_drift` ask the right question.**~~ **DONE, AND THE
   ANSWER WAS NOT A NUMBER TO SHIP.** The claim-level detector is built
   (`build_binding_index`, `claim_level_drift`) and measured above: 25.1
   alerts per catch becomes 2.0 at the same zero miss rate. But its strict
   tier is arithmetically identical to `oracle_replica`, so this corpus
   cannot certify it. The finding is the *size of the gap*, not the score.
3. **Get statistical power.** 26 positives cannot resolve a small effect,
   and — now the sharper reason — a corpus whose claims are diff-decidable
   cannot tell a real detector from an oracle replay. A multi-repo corpus
   targeting ≥150 positives, including repositories that actually delete
   files so `path_drift` gets its first real test after 0 flags in 675
   claims, is what would let the claim-level detector be shipped on
   evidence rather than on a ceiling.

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

Deliberately unused: git's `@@ … @@` section headings. This repo ships no
`.gitattributes`, so git falls back to its default funcname heuristic, which
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
