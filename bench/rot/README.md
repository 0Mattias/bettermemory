# Memory-rot benchmark

The accuracy measurement for `staleness_verdict` — the mechanism the
project README leads with. Ground truth comes from git, not from a
model; every verdict-shaped quantity is computed by the SHIPPED
functions, never a reimplementation.

Four reads are published and all four stand: a single-repository
pilot on bettermemory's own history, a **pre-registered 30-repository
corpus** (37,635 claims, 8,627 positives) that supersedes the pilot's
aggregates, the **live-store read** (T1) that grades the shipped
chain on real, author-declared claims, and the **T2 acceptance read**
that confirmed the absence shape's inverted polarity on T1's live
reappearance cases.

```sh
.venv/bin/python bench/rot/run.py --days 60
.venv/bin/python bench/rot/run.py --t0 053ab9de4520   # reproduce a published run
.venv/bin/python bench/rot/corpus.py                  # clone, run, pool the 30 repos
.venv/bin/python bench/rot/scorecard.py               # grade P1-P7 mechanically
.venv/bin/python bench/rot/live_census.py --out bench/rot/results/live-store-YYYY-MM-DD.json
.venv/bin/python bench/rot/t2_validation.py --out bench/rot/results/t2-validation-YYYY-MM-DD.json
```

**Pin `--t0` for anything you intend to compare.** Without it t0 is
resolved from `--days` against the wall clock and slides between runs;
every result file records its full t0/t1 shas and whether t0 was pinned.

## Method

Pick a repository and two commits (t0, t1). Extract fact-shaped claims
from the tree at t0 purely mechanically — a path exists, a top-level
symbol is defined in a named file, a module constant holds a literal.
Re-evaluate each against the tree at t1 with a checker, not a judge.
Each claim becomes a memory body citing it, anchored at t0; at t1 the
three signals production uses — calendar age, path drift, commit
drift — feed the **shipped** `compute_staleness_verdict`.

Three arms, because they answer different questions:

- **`drift_only_relative_cite`** — calendar leg disabled, paths cited
  the way a developer naturally writes them. The informative arm.
- **`drift_only_absolute_cite`** — identical but absolute citations,
  because `detect_path_drift` excludes relative paths by design.
- **`shipped_default`** — anchored 400 days back, past the freshness
  window: what a user who has not re-verified in a year gets. Since
  3.30.0 it scores **identically to `drift_only_relative_cite`** —
  the calendar leg no longer erases the drift legs — and the arm is
  kept as the regression guard for exactly that convergence
  (`test_the_shipped_default_is_not_a_constant_function`; the defect
  it guards is written up in
  `docs/incidents/2026-07-26-staleness-verdict-constant-function.md`).

## Results — bettermemory's own history, 2026-07-26

60-day window, t0 `053ab9de4520` → t1 `388b5be75472`, 675 claims.
Artifact: `results/bettermemory-60d-2026-07-26.json` (and the 30-day
sibling). Read the glob `results/bettermemory-*d-*.json`, not the
directory — `results/escalation-off-60d-2026-07-31.json` is a
counterfactual whose drift arms describe no shipped build, and it says
so in its own `counterfactual` block.

**The shipped signal (`commit_drift`, file-level):**

| class | n | false | flagged | unflagged-stale | precision | **J** | **Fisher p** | **alerts/catch** | AUROC |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| path | 77 | 0 | 91% | n/a | 0% | n/a | n/a | n/a | n/a |
| symbol | 485 | 6 | 98% | 0% | 1% | 0.023 | 0.871 | 79.0 | 0.725 |
| literal | 113 | 20 | 96% | 0% | 18% | 0.043 | 0.594 | 5.5 | 0.484 |
| **ALL** | 675 | 26 | 97% | 0% | 4% | **0.034** | **0.415** | 25.1 | 0.550 |

**The claim-level detector, same claims, beside its references:**

| detector | flagged | unflagged-stale | precision | **J** | **alerts/catch** |
| --- | --- | --- | --- | --- | --- |
| claim strict | 4% | 0% | 100% | 1.000 | 1.0 |
| claim weak | 8% | 0% | 51% | 0.962 | 2.0 |
| `always_flag` | 100% | 0% | — | 0.000 | 25.9 |
| `never_flag` | 0% | 100% | — | 0.000 | — |
| `oracle_replica` *(peeks at the label)* | 3.85% | 0% | 100% | 1.000 | 1.0 |

Youden's J (`TPR − FPR`) is exactly 0.0 for every constant classifier,
which is why it and the Fisher test sit beside every rate: a 0%
unflagged-stale figure alone is what `always_flag` scores, so a miss
rate reported without those counterweights is meaningless. On this
repository the shipped verdict is not statistically distinguishable
from rate-matched random flagging (Fisher p = 0.415 at 60d), and the
strict claim tier is arithmetically identical to `oracle_replica` —
a fact about the benchmark's construction, not a win (see the corpus
read below, which settled it).

## Results — 30 repositories nobody chose, 2026-07-26

Frame, draw and seven falsifiable predictions committed in
`PREREGISTRATION.md` **before** any repository was screened;
`scorecard.py` grades each mechanically off `results/multirepo.json`
into `results/scorecard.json`. 180-day windows, PyPI-download frame
walked to rank 767, 30 repositories, 37,635 claims, 8,627 false
(22.9% base rate).

### The scorecard: 4 of 7 hit

| | prediction | result | |
| --- | --- | --- | --- |
| **P1** | `path_drift` (absolute arm) TPR ≥ 0.90 | TPR 0.957, false-alarm rate 0.000 | hit |
| **P2** | relative arm flags exactly zero | 0.000 across all 37,635 claims | hit |
| **P3** | pooled macro-J < 0.15 | 0.2875 | **MISSED** |
| **P4** | symbol AUROC in [0.50, 0.65] | 0.5446 (p = 5e-05) | hit |
| **P5** | `claim_level_strict` symbol precision ≤ 0.97 | 0.9999 — one false positive in 6,705 | **MISSED** |
| **P6** | claims per `.py` file in [4.0, 9.0] | 8.96 | hit |
| **P7** | ≥ 8 stratum-D qualifiers | 7 | **MISSED** |

Each MISS was pre-committed to a reading and the reading is honoured,
not renegotiated. **P5 is the one that matters**: strict-tier
precision at 1.000 on thirty repositories neither party chose means
structural claims graded against structural ground truth collapse onto
the oracle at any corpus size — the tree-diff is nearly a sufficient
statistic for the oracle's own question — so the "get statistical
power" roadmap item is answered in the negative and retracted. No
corpus of git repositories can certify the strict tier. **P3** missed
in the flattering direction: the shipped signal reads materially
better on a normal population than on this unusually churny
repository. **P7** publishes stratum D as underpowered (7 qualifiers
against a floor of 8), with no re-draw. One defect in P6's own wording
is recorded in `PREREGISTRATION.md`'s terms: its symbol-share clause
(predicted < 72%, landed 83.4%) carries no `MISSED if` threshold, so
the scorecard cannot see the half it got wrong; the density clause it
does grade landed inside its band.

**Pooled results, the numbers that carry the finding:**

| detector | flagged | precision | **J** | **alerts/catch** |
| --- | --- | --- | --- | --- |
| file-level incumbent (ALL) | 78% | 29% | 0.2875 | 3.4 |
| claim strict (ALL) | 23% | 99.93% | 0.991 | 1.0 |
| claim weak (ALL) | 24% | 94.0% | 0.973 | 1.1 |
| `always_flag` | 100% | 22.9% | 0.000 | 4.4 |

**The contrast is the finding.** The weak tier — which does not
collapse onto the oracle (94% precision, real mistakes) — costs 1.1
alerts per catch against the incumbent's 3.4, winning the repo-level
paired comparison in 19 repositories, losing in 0, tying in 7 (sign
test p < 0.001). The information needed to cut false alarms by that
margin was in git data the harness already reads; the incumbent
discards it by asking "did this file change?" instead of "did the
thing this memory cites change?".

### The anchored-relative arm, 2026-07-30

A fourth arm appended after the scorecard (the frozen grades are
untouched): resolve each relative citation against the memory's own
recorded `origin.worktree_root` before checking existence. Artifact:
`results/multirepo-anchored-2026-07-30.json`.

| arm | flag rate | precision | J | alerts/catch |
|---|---|---|---|---|
| `drift_only_relative_cite` | 0.0% | — | 0.000 | — |
| `drift_only_relative_cite_anchored` | 0.73% | 1.000 | 0.032 | 1.0 |

Zero false alarms, one alert per catch, and **small recall by
construction** (0.73% flagged against a 22.9% base rate): the leg is
precise where it fires and silent nearly everywhere. It closes a leg
that fired on nothing at all; it is not a detector that finds most
rot. The check fails open when the recorded root is absent locally —
a synced store must not read as `always_flag` in a new home.

## Results — the live store, 2026-08-14 (T1)

The corpus retraction left one evidence route: real memory bodies and
user-visible outcomes. T1 is its first instrument — declared first
(`T1_LIVE_STORE_DECLARATION.md`, five predictions, sha ordering
declaration → implementation → run), then run once against the
dogfood store. Artifact: `results/live-store-2026-08-14.json`,
aggregates only.

| | prediction | result | |
| --- | --- | --- | --- |
| **T-P1** | zero stored-claim parse failures | 0 of 585 | hit |
| **T-P2** | false-while-fresh ≤ 1% of classifiable claims | 0.0% (0 of 501) | hit |
| **T-P3** | repair-follow multiplier ≥ 2.0, floors 10+10 | claim-carrying cohort holds 3 resolved deliveries | **underpowered** |
| **T-P4** | historical share ≥ 25% of classifiable absent attestations | 89.5% (17 of 19) | hit |
| **T-P5** | notes at length ≥ 450 are ≥ 10% of notes | 11.5% (144 of 1,257) | hit |

Read T-P2 with the flattery its declaration named (the store was
curated the day before; later censuses read natural drift) and T-P3 as
the floors doing their job (claims-at-write is days old; the contrast
stays ungraded rather than under-evidenced). The declaration's
decision rules executed on these grades: T-P4 scopes **the negative
claim shape** (17 of 19 absent attestations mean "deleted on purpose",
for which `expected_absent`'s presence-never-flags polarity is
inverted), and T-P5 scopes **the note-cap unit**, carrying an
unpredicted finding — 20 notes exceed the 500 cap, so the gate is
younger than the log or a path bypasses it. The proposed Lane T
criterion v1 reads **open_floors_unmet** (claims floor met, 501
against 200; resolved claim-carrying deliveries 3 against 20).

## The absence shape, 2026-08-14 (T2)

T-P4's decision rule executed as the grammar's fourth kind: `!path` —
refused at declaration while anything occupies the path, drifting when
it reappears (weak = touched in the window, strict = net-reappeared),
and the verify stamp refused over a reappeared path with no new gate
code, since both gates already route through `check_claim`. Declared
first (`T2_ABSENCE_CLAIM_DECLARATION.md`, five acceptance
predictions, sha ordering declaration → implementation → sealed read →
run), then read once against the live store
(`results/t2-validation-2026-08-14.json`). All five predictions hit:

| | prediction | result | |
| --- | --- | --- | --- |
| **A-P1** | full gate green, no assertion weakened | 4,760 passed; no pinned table moved | hit |
| **A-P2** | every stored claim still parses; zero `!`-reinterpretations | 595 of 595; 0 | hit |
| **A-P3** | descriptions ≤ 26,000 chars; schema remainder unmoved | 25,993; 7,438 | hit |
| **A-P4** | T1's reappeared cases refuse at declaration and fire strict | 2 of 2; 2 of 2 | hit |
| **A-P5** | corpus detector pins pass unmodified | additive branch only | hit |

The three measured kinds' code paths are untouched — the corpus
numbers keep describing the code they measured — and the absent kind's
tier semantics are owned by `tests/test_claims.py` on handcrafted
`-U0` streams, since no git corpus contains the kind.

## What stands, in one place

1. **The claim tier is the replacement, measured first.** 1.1 alerts
   per catch at 94% precision against the incumbent's 3.4
   (`results/multirepo-anchored-2026-07-30.json`). It shipped as
   claims-at-write in 3.40.0 — declared claims, gate-checked at write
   and verify, watched by drift (`src/bettermemory/claims.py`); the
   bench imports the shipped copies. The grammar's absence kind
   (`!path`, 5.6.0) inverts the polarity for deleted-on-purpose
   paths — T1's cohort D measured that pattern at 89.5% of classifiable
   absent attestations, carried off-label until T2 gave it a shape.
2. **The strict tier cannot be certified by any git corpus** (P5,
   honoured). Evidence of a different kind is the only route, and T1
   is its first instrument.
3. **The incumbent stays until the timeline can grade its
   replacement on the live store** — flipping
   `_COMMIT_DRIFT_ESCALATES` off was pre-registered, its condition
   fired, and the flip was refused on a measurement: with the switch
   off every drift arm scores exactly `never_flag`
   (`results/escalation-off-60d-2026-07-31.json`), the constant
   function with the sign reversed. Full rationale under "Not
   planned" in `docs/ROADMAP.md`.
4. **Read-side commit-SHA rules were measured and rejected** — all
   three candidate rules fail on arithmetic (the distance rule is
   `always_flag` on the live store; under squash/rebase merge, 3,573
   of 4,647 merged-PR head SHAs go unreachable, so the dominant
   firing mode is "the change you described shipped"). Record: the
   `SHA_MARKER` tombstone in `src/bettermemory/durability.py`.

## Caveats

- **The oracle sees structural change only.** A function whose
  behaviour changed under a stable name reads `still_true`, so the
  true base rate is higher than measured and precision better; by how
  much is unknown.
- **The corpus is widely-depended-on Python packages, not Python
  code.** Mature projects delete conservatively; private
  under-maintained code is unrepresented by construction, and the
  deletion gate selects on a quantity correlated with the outcome.
- **Corpus claims are synthesised.** `citation_resolved_rate` is 100%
  by construction there; `bench/claims.py` measures the real
  checkable/judgement split at roughly 64/36. T1 exists because of
  this gap, and its declared-claims population is the cleaner-by-
  construction complement.
- **Stratum D is underpowered** (7 of a pre-registered 8), published
  as such.

## The claim-level detector, and what stops it cheating

One `git log -p -U0` pass builds an inverted index of column-0
binding tokens found on changed lines; claims resolve by dict lookup.
Two rules keep it honest: `build_binding_index` takes the diff text
and nothing else (it cannot see a claim, pinned by a signature test),
and the claim side enters only as the rendered memory body — the same
material production reads. Git's `@@ … @@` funcname headings are
deliberately unused (this repo's default heuristic labels method-body
edits `class Store:`, which would make a heading-keyed detector a
body-churn amplifier), and **a body-only edit is deliberately not
drift** — `label_claim` matches a definition by name and never reads
its contents. One implementation lesson is kept because it cost 12 of
20 literal catches: match direction matters — decode each changed
physical line back to the text it contributes and look for that text
in the claimed value, never the claimed value's logical lines in the
diff (implicit concatenation makes them different objects).

## Record and receipts

Current truth lives above; the full grading narratives, superseded
readings, and correction-by-correction history live in this file's
own git history and in the campaign record in the memory store —
that is what the memory system is for. The dated record:

- **2026-07-26** — single-repo pilot, both windows
  (`results/bettermemory-60d-2026-07-26.json`, `-30d-`); the
  constant-function defect it surfaced, fixed in 3.30.0
  (`docs/incidents/2026-07-26-staleness-verdict-constant-function.md`).
- **2026-07-26** — the 30-repository corpus, pre-registered
  (`PREREGISTRATION.md`; `results/multirepo.json`,
  `results/scorecard.json`).
- **2026-07-30** — the anchored-relative arm
  (`results/multirepo-anchored-2026-07-30.json`).
- **2026-07-31** — the escalation-flip gate fired and was retracted
  on measurement (`results/escalation-off-60d-2026-07-31.json`;
  `docs/ROADMAP.md`, "Not planned").
- **3.40.0 (2026-08-04)** — the detector promoted to the product as
  claims-at-write (`docs/ROADMAP.md`, "Claims-at-write";
  `src/bettermemory/claims.py`).
- **2026-08-14** — T1, the live-store read
  (`T1_LIVE_STORE_DECLARATION.md`;
  `results/live-store-2026-08-14.json`); next units scoped: the
  negative claim shape (T2), the note-cap unit (T3).
- **2026-08-14** — T2, the absence claim shape, shipped in 5.6.0
  (`T2_ABSENCE_CLAIM_DECLARATION.md`;
  `results/t2-validation-2026-08-14.json`). Remaining scoped unit:
  the note-cap (T3).
