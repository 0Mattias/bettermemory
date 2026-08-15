# T2 — the absence claim shape: polarity mirrored from `check_claim`, 2026-08-14

The second unit of Lane T, and the product unit T1's decision rule 1
scoped mechanically: T-P4 hit at 89.5% historical share (17 of 19
classifiable absent-path attestations name paths this repository's own
history once tracked — deleted on purpose, documented as deleted;
`bench/rot/results/live-store-2026-08-14.json`, `d_absent_cohort`).
The store is already using `verified_absent_paths` — a machine-locality
lever whose contract says *presence never raises a flag* — to make
historical claims whose polarity is the opposite: if a deliberately
deleted path REAPPEARS, the memory documenting the deletion is what
drifted. Two of the nineteen have already reappeared. The claims
grammar has no shape for any of this; all three kinds assert existence.

This is a product declaration, not a measurement declaration: it pins
design decisions and acceptance predictions before the implementation
exists, in R2's commit mold — this commit, then the implementation,
then the validation read. A prediction graded below is a compatibility
or behavior guarantee, checkable mechanically after the change lands;
nothing may be added to the list after the implementation commit.

## The wire shape

`!path` — a leading bang on the path claim, kind `absent`, path-only.

- `!src/old/module.py` asserts: nothing exists at that path in the
  origin worktree. Refused at declaration while anything is there;
  drifts when something returns.
- `!path::symbol` and `!path::NAME=value` are REFUSED with a teaching
  error. Symbol- and literal-absence have no measured evidence base —
  T-P4's cohort is paths — and the module's standing rule applies:
  loosening the oracle without re-running the bench ships an unmeasured
  detector wearing measured numbers.

Rejected alternatives: `absent:path` opens a second grammar axis
(prefix keywords) for one kind; `path::absent` collides with the symbol
grammar outright — `absent` is a valid Python identifier and therefore
already a legal symbol claim. The bang is one character, reads as
negation to every coding agent, and cannot begin a Python identifier,
so it collides with nothing in the symbol/literal shapes.

Reinterpretation risk, measured at declaration time: **0 of 595 stored
claims across 120 claim-carrying memories begin with `!`** (live-store
frontmatter read, 2026-08-14, post-T1 organic growth from 585/118).
The old grammar would only ever have admitted such a claim if a file
literally named `!…` existed at declaration; none did. If this count
had been non-zero the marker would have been redesigned, not the store
migrated.

## Oracle semantics (`check_claim`)

Resolution first, unchanged: the same `_resolve_claim_path` containment
walk, so `!/etc/passwd`, `!~/x`, traversal and drive-letter escapes are
refused exactly as they are for presence claims — absence claims are
anchored to the origin worktree like everything else.

Then the inversion: the claim holds when nothing exists at the resolved
path, and is refused when anything does — a directory occupying the
path defeats absence just as it fails a presence claim's `is_file()`.
The refusal names the polarity ("the path exists — an absence claim
asserts it stays deleted") because refusal messages are where callers
learn this grammar; the tool description carries only the shape.

The oracle stays dull on purpose: no git-history requirement at the
gate. A path that never existed here (T-P4's LOCALITY minority) is a
weaker but not a false absence claim, and demanding `git log` inside
`check_claim` would put git plumbing into a pure filesystem/AST oracle.
History enters on the read side, where it already lives.

## What needs no new code

Both gates route through `check_claim` already:

- `handlers/_shared.py::_validate_declared_claims` — memory_write and
  memory_verify refuse a false claim at declaration. With the inverted
  oracle, declaring `!p` while `p` exists refuses with the polarity
  message.
- `handlers/verify.py::_refuse_stale_stored_claims` — a verify that
  does not re-declare re-runs the oracle over STORED claims and refuses
  to stamp `last_verified_at` over a false one. A reappeared path
  therefore BLOCKS the freshness stamp with no verify-side change:
  escalation-on-reappearance at the strongest surface is free.

`_resolve_with_claims` is kind-agnostic through `claim_paths`, so an
absent claim's path joins the governed half of the commit-drift split
unchanged, and `git log HEAD -- <path>` on a deleted path still returns
its history — the governed leg is not phantom for a path whose deletion
is itself in the log.

## Diff-tier semantics (`claim_level_drift`)

One additive branch, polarity mirrored from the `path` kind:

- **weak** — the path was touched at all in the window (`files`).
- **strict** — touched AND not in `deleted`: the window net-reappeared
  it.

The set-based index cannot order events, but the declaration-time
invariant closes the gap: the window STARTS absent (the claim was
gated on absence), so a window showing both a touch and a deletion can
only be add-then-delete — it ends absent, and weak-only is the correct
verdict. The one degradation: an add-delete-add cycle inside a single
window ends present but reads weak-only. Accepted as the dull answer —
the verify gate catches the survivor on the next stamp attempt.

Attribution mirrors the path kind: a weak-fired absent claim implicates
the commits whose diffs carried lines for that path
(`_weak_tier_evaluation`'s `changed_text` rule), never silently zero.

## Old-reader behavior

A store carrying `!p` read by a pre-T2 server parses it as a PATH claim
named `!p` (the old grammar has no bang rule): `check_claim` finds no
such file and the verify gate refuses to stamp — conservative, loud,
correct in direction — while the diff tier stays quiet on a binding
that never existed. No crash, no false-fresh. `load_claims` leniency is
untouched in both directions.

## Acceptance predictions

Graded in the T2 record after the implementation and validation land.
Each is mechanical; a MISS is published, not renegotiated.

- **A-P1 — the gate.** Full local suite green including the format
  check (ops lesson 17). No existing assertion is weakened; pinned
  budget/count tables that move are enumerated in the record commit.
- **A-P2 — wire compatibility.** Every stored claim in the live store
  still parses after the change, and the `!`-prefix reinterpretation
  count is exactly the 0 measured above. **MISSED if** any stored
  claim changes meaning or fails to parse.
- **A-P3 — resident budgets.** The tool-description surface stays
  ≤ 26,000 chars (the absence shape is documented in memory_write's
  claims bullet; memory_verify already delegates to it), and the
  schema-side remainder does not move from 7,438 — the wire change
  adds no parameter. **MISSED if** either ceiling is crossed or the
  remainder grows.
- **A-P4 — the live cases.** T1's two REAPPEARED attested-absent paths,
  located by the same cohort-D rule on the live store: expressed as
  absence claims, both refuse at declaration (they exist again), and
  each one's re-add window fires the absent kind's strict tier.
  Published as counts (n/2, n/2) in an aggregates-only artifact —
  no paths, no ids. **MISSED if** either count is below 2.
- **A-P5 — detector identity.** The bench's pinned detector tests pass
  unmodified and every committed corpus artifact stays byte-identical —
  the absent branch is additive on a kind the corpus never contains,
  so the measured numbers keep describing the measured code paths.
  **MISSED if** any detector pin needs editing to pass.

## What is not claimed

- **No symbol/literal absence.** Path-only, stated above.
- **No path-drift-leg integration.** Claims keep feeding exactly the
  commit leg and the two gates. An UNCOMMITTED reappearance is
  invisible to memory_search — the next verify attempt catches it.
  Wiring claims into the disk-stat leg is a separate unit if evidence
  demands one.
- **No store migration.** Existing `verified_absent_paths` attestations
  keep their machine-locality contract; nothing is rewritten. Whether
  the 17 historical attestations should be re-expressed as `!path`
  claims is the operator's call per memory, not a sweep.
- **One store, one user.** A-P2/A-P4 read the dogfood store; nothing
  generalizes past it.

## Owner doors, surfaced and not taken

The memory_write description edit is governed by the description-budget
ceiling — a mechanical tripwire, not an owner door; this unit stays
under it or does not ship. Lane T criterion v1 ratification remains the
charter's (unratified; unchanged by this unit). T3 (the note-cap unit)
stays queued behind this one, untouched.
