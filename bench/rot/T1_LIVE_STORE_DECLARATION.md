# T1 — the live-store read: the shipped verdict graded on declared claims, 2026-08-14

The first unit of Lane T (trust/rot widening), and the first instrument
built against `bench/rot/README.md` item 4 since that item became the
only route left: *"Get evidence of a different KIND. Either grade
against real memory bodies rather than machine-generated ones … or
grade against user-visible outcomes rather than structural labels."*
This declaration commits to both halves at once, because the dogfood
store now contains both: author-declared claims on real bodies
(claims-at-write, 3.40.0), and an event log that records the verdict
each read actually delivered and what the operator did next.

The enforcement record is the sha ordering, R2's mold: this commit,
then the census implementation commit, then the run commit. Predictions
are graded mechanically by the census itself and land in the artifact;
nothing may be added after the run.

## Why this population is the one the roadmap named

`docs/ROADMAP.md`'s claims-at-write entry closes with the open half:
the corpus figures grade the DETECTOR on extracted corpus claims, while
the shipped surface runs it on author-declared, oracle-gated claims —
"a cleaner population by construction, and an unmeasured one until the
dogfood store carries enough declared claims to read." The denominator
it said a backfill pass would mint has partially minted itself through
organic writes; this instrument is the first read of it.

What this is NOT: a detector-vs-oracle certification. The corpus
retraction (`bench/rot/PREREGISTRATION.md` P5) stands — structural
claims graded against structural ground truth collapse onto the oracle,
at any corpus size. Here `check_claim` is the same function that GATED
every stored claim at declaration, so a claim false today went false
through the repository's own later history, not through a label the
detector could replay. The quantities below are operating costs and
miss rates of the shipped chain on its real population — the
"we verify and here is the measured accuracy" sentence, given its
number — not proof the detector beats an oracle.

## Scouted at declaration — observed, so excluded from prediction

Everything in this list was counted while scoping this document.
None of it is predicted; every prediction below targets a quantity
still unobserved at commit time.

- 268 memory files at the top level of the live store; tombstones live
  apart in `.tombstones/` (`src/bettermemory/store.py::TOMBSTONE_DIR`).
- 118 of those files carry a `claims:` frontmatter list.
- 46 files matched a loose grep for absent-path attestations; the loose
  match overcounts and the census recounts it strictly.
- Event-log kind counts, including: 585 show, 541 verify, 501
  turn_audited, 313 update, 223 use, 180 write, 16 search_miss,
  11 miss_ack, 3 remove, 1 silent_miss_cutoff.
- `show` events record the delivered `staleness_verdict`,
  `verification_status`, `path_drift_checked` / `path_drift_missing`
  counts, `commit_drift_status` and `commits_since_verify` —
  which is what makes the outcome join replayable at all.

Not scouted, and deliberately left unobserved until the run: every
claim's truth value now, every verdict the chain computes now, the
note-length distribution, the strict absent-attestation census and its
historical split, the post-cutoff calibration counts, and every join
between a delivered verdict and what followed it.

## The census, precisely

One committed script, `bench/rot/live_census.py`, run once against the
live store, READ-ONLY: it opens memory files and event shards directly,
takes no locks, writes no events, and mutates nothing. It reuses the
shipped machinery — `claims.load_claims` / `claims.check_claim`, and
the same verification / path-drift / commit-drift / verdict chain the
`memory_show` handler composes — never a reimplementation
(`bench/rot/README.md`'s standing rule for this directory).

**A — population.** Active memories; how many carry declared claims;
claims by kind (`path` / `symbol` / `literal`); parse failures under
`load_claims`. Of claim-less memories, how many are checkable-but-
undeclared under `bench/claims.py`'s mechanical checkability census —
the true remaining backfill.

**B — claim truth against the delivered verdict, now.** For every
stored claim whose origin worktree is live on this machine:
`check_claim` against that worktree today, joined with the staleness
verdict the shipped chain computes for its memory today. The cell that
matters is **false-while-fresh**: a claim the tree refutes on a memory
the chain still calls `fresh` — the silent-rot miss rate of the
shipped surface on its real population. Claims whose worktree is dead
or absent land in an `unclassifiable` bucket, counted, never graded.

**C — the outcome timeline.** Every `show` event whose recorded
verdict was `spot_check_required` is an escalated delivery. For each,
the join: an `update` on the same id within 7 days is a REPAIR; a
`verify` within 7 days with no update is a HOLD; neither is
UNRESOLVED. Repair-follow rate = repairs / (repairs + holds),
unresolved reported beside it. Split into two cohorts by whether the
memory carried declared claims at delivery time, reconstructed from
the write/update/verify event sequence — the claim tier only governs
claim-carrying memories, so the cohort split is the policy split.
Named confound, declared now: claim-carrying memories skew toward this
repository's curated campaign records and claim-less memories skew
personal and out-of-repo, so the cohorts differ in more than policy;
the census reports the split per scope family so the confound is
visible rather than averaged away.

**D — the absent-attestation cohort.** Strict recount of memories
carrying `verified_absent_paths`. For each attested-absent path whose
origin worktree is live and a git repository: if the repository's
history ever tracked the path (`git log --oneline -1 -- <path>`
non-empty), the attestation is HISTORICAL — the path existed here and
was deleted; otherwise it is LOCALITY — the path never existed in this
tree (remote host, other platform, not-the-location). Dead-worktree
attestations land unclassifiable. This is the mechanical size of the
"path legitimately historical" gap: `expected_absent` semantics say
absence is the expected state and presence never flags
(`src/bettermemory/verify.py`, PathDriftReport docstring), which is
the wrong polarity for a historical claim — if a deliberately-deleted
path REAPPEARS, the memory documenting the deletion is what drifted.
The claims grammar has no negative shape; all three kinds assert
existence (`src/bettermemory/claims.py::parse_claim`).

**E — note-cap pressure.** Length distribution of every `note`
recorded on `verify` and `use` events, and the fraction within 50
characters of the 500-character cap
(`src/bettermemory/handlers/_shared.py::_NOTE_MAX_LEN=500`). The cap
refuses rather than truncates, so the log cannot show overruns; what
it can show is squeezing — mass piled against the ceiling.

**F — calibration accumulation.** Post-cutoff audited-turn and
un-acknowledged-miss counts, read through the shipped
`eval.compute_threshold_sweep` invalidation semantics (cutoff event,
per-event acks, tombstoned-hit exclusion).

## Predictions

Graded mechanically by `live_census.py` into the artifact's
`predictions` block, hit or MISSED, thresholds encoded in the script
before the run and identical to these.

**T-P1 — compatibility.** Every stored claim parses under
`load_claims`: zero parse failures. Every stored claim passed this
parser at declaration or at a later verify, so any failure now is a
wire-format compatibility break between releases, not user error.
**MISSED if** ≥ 1 failure.

**T-P2 — silent rot.** False-while-fresh ≤ 1% of classifiable claims.
The machinery argument for a low number: a stored claim was true at
its last gate check, so a claim false now implies later commits
touched its binding, which the claim tier escalates on — the leak
channels are the declared narrownesses (merge-only touches never
escalate; a weak-tier index miss excludes the file's commits from
escalation; a window past `MAX_PATCH_STREAM_COMMITS` falls back to
any-touch). A miss above 1% means one of those channels is carrying
real rot and names the next repair. **MISSED if** > 1%.

**T-P3 — the live contrast.** The repair-follow rate on escalated
deliveries to claim-carrying memories is ≥ 2.0× the rate on escalated
deliveries to claim-less memories. This is the live-store analogue of
the corpus contrast (claim tier 1.1 alerts per catch against the
incumbent's 3.4 — `bench/rot/results/multirepo-anchored-2026-07-30.json`):
an escalation computed over declared claims should be acted on as a
repair materially more often than an any-touch escalation. Graded only
if BOTH cohorts hold ≥ 10 resolved escalated deliveries; below either
floor the cell publishes as underpowered, ungraded, with no widening
of the join horizon to rescue it. **MISSED if** the multiplier is
< 2.0 with both floors met.

**T-P4 — the historical share.** Of classifiable absent-path
attestations, ≥ 25% are HISTORICAL. Hit means the store is already
using a machine-locality lever to say "deleted on purpose" — off-label,
with reappearance polarity inverted — and the negative claim shape is
scoped as the next Lane T product unit. Graded only if ≥ 8
attestations classify; fewer publishes as underpowered and the lever
question stays open. **MISSED if** < 25% with the floor met — in which
case the gap is smaller than the plan believed, the lever is
deprioritized, and that closure is recorded as the finding.

**T-P5 — note-cap pressure.** ≥ 10% of recorded notes have length
≥ 450. Hit confirms the ergonomics gap: operators are compressing
evidence to fit the ceiling. **MISSED if** < 10% — the cap is not
binding in practice, and the queued gap closes as a documentation line
rather than a code change.

## Decision rules this census executes

Declared here so the follow-up is mechanical, not a mood:

1. **T-P4 hit → the negative claim shape is the next product unit**
   (a claim kind asserting absence, refused at declaration if the path
   EXISTS, escalating when it reappears — polarity mirrored from
   `check_claim`). T-P4 MISSED → parked with the census as evidence.
2. **T-P5 hit → the note-cap change is scoped** (raise vs
   truncate-with-acknowledge decided then, against the measured
   distribution). MISSED → closed.
3. **Calibration unlock (F):** the successor-rule labeling unit
   (`docs/eval.md#silent_miss_rate`'s conjunctive candidate) unlocks
   at ≥ 300 post-cutoff audited turns AND ≥ 10 post-cutoff un-acked
   misses — the pre-registered sample floor the widening lane's
   postmortem demanded, set before any labeling. Below the floors it
   stays parked regardless of appetite.

## The Lane T success criterion — proposed, not self-ratified

Lane T has had no numeric criterion; the forward plan assigns this
document the proposal. Proposed:

> **Lane T criterion v1.** At any census run of this committed
> instrument where the floors are met — ≥ 200 classifiable declared
> claims AND ≥ 20 resolved escalated deliveries on claim-carrying
> memories — the shipped surface reads BOTH:
> (1) false-while-fresh ≤ 1% of classifiable claims, and
> (2) repair-follow multiplier ≥ 2.0 against the claim-less cohort.

Until the floors are met the criterion is OPEN — unread, neither met
nor failed — and each census publishes progress toward the floors. At
the floors, a read below either bar is a MISS to publish, never a
renegotiation. The bars are carried with their n floors because the
widening lane's closure taught exactly this: a point estimate that
clears a bar on a small n invites an after-the-fact interval argument;
declaring the floor first is what makes the eventual read final.
Ratification is the owner's: this document proposes, the charter
ratifies. Until then the criterion binds this lane's instruments (they
must report it) and nothing else.

## What is not claimed

- **One store, one user, one machine.** Every rate here describes the
  dogfood store. `docs/ROADMAP.md`'s contributing note stands: more
  distributions is the open question, and nothing below generalizes
  past this population until someone else runs the census on theirs.
- **A curated store flatters T-P2.** Repo-local freshness debt was
  driven to zero the day before this declaration (store curation,
  2026-08-14). The instrument is repeatable; later censuses read a
  store that has drifted naturally since.
- **The cohort split in C is policy plus population.** Named above;
  reported per scope family; not adjusted away.
- **Aggregates only.** The committed artifact carries counts, rates,
  buckets and grades — no memory ids, no bodies, no claim strings, no
  scope names beyond coarse families (`this-repo` / `other-repo` /
  `no-repo`), no worktree paths or repo URLs except this repository's
  own. Reproducibility pin: per-shard event counts and sha256s, a
  sha256 over the sorted (filename, file-sha256) list of the store,
  and HEAD of this repository — enough to re-run bit-identically on
  the same snapshot without publishing a byte of the store.
- **The 7-day join horizon is a choice.** Declared before the run,
  encoded in the script, and not tunable afterwards; UNRESOLVED is
  reported so the horizon's cost is visible.

## Owner doors, surfaced and not taken

The negative-claim grammar's wire syntax (if T-P4 scopes it) touches
`memory_write`'s documented claim surface — the resident-footprint
tripwire governs before any tool-surface edit. The Lane T criterion's
ratification is charter surgery. Both stay open doors; neither is
exercised by this unit.
