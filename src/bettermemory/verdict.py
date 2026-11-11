"""One staleness verdict per memory, for `memory_show`.

`memory_show` used to compute its commit-drift leg with
`verify.compute_commit_drift`, which keeps no per-memory result: every
show of a row the repository had not moved for forked git again, while
`memory_search` read the same row's leg from the per-hit memo
(`_response._DRIFT_MEMO`). This module is the one verdict both read now.
`resolve_verdicts` computes each memory's parts the way `memory_show`
emits them, its commit leg through the search's own pass
(`_response._DriftPass`): the same gates, the same key, the same memo,
the same resolution and the same store-at-the-end checks. So a show of a
row a search (or an earlier show) resolved, nothing moved, runs no git
process, and the show and the search cannot read one memory two ways.

The parts, per memory:

- `verification`: the calendar status (`verify.compute_verification_
  status`) at the caller's `now` and `stale_after_days`;
- `path_drift`: the claim-anchored path report
  (`verify_anchoring.detect_path_drift`), stats only;
- `commit_drift`: the commit leg as `memory_show` emits it
  (`verify.commit_drift_status` over the memoised `ResolvedCommitDrift`),
  None where the leg does not apply;
- `staleness_verdict`: the rollup (`verify.compute_staleness_verdict`)
  over the three.

The first two are computed on every call and never memoised: the clock
moves and a file can vanish without anything the memo keys on changing.
Only the commit leg is memoised, and only as the search memoises it,
with the coverage and the blind spots `ResponseBuilder.attach_commit_
drift_counts` sets out.

The trust rule (`ResponseBuilder.apply_trust` / `apply_trust_
unavailable`) is NOT part of the verdict here: it reads the index, not
the record, and `memory_show` applies it to the assembled response after
this verdict, as the search applies it after its own. A caller selecting
rows by verdict outside those surfaces applies it the same way.

`memoised_verdicts` is the same computation from the memo alone, with
no git process: a row whose commit leg the memo answers, or that needs
no git at all, gets its full verdict; every other row comes back an
`UnresolvedVerdict`. The rc6 session brief read it on a hook's deadline
and filled the memo from a worker thread; since rs-v6 the brief reads no
verdict (RS-F7 part 4), and this is how the tests read what the memo
holds. The memo is lock-guarded and each call opens its own pass.

The caller's origin is an argument, never captured here: `memory_show`
passes `_handlers.capture_origin()`, the hook the origin it builds from
its payload's directory.

So is the claims' anchoring (design 3.6): `anchoring` reads each homed
record's path claims at its home checkout's live view, translated across
its recorded views, or in a stand-in that confirms by presence and never
convicts (`verify_anchoring.Anchoring`). A record with no home row, and every
record when no anchoring is passed, is read at its recorded root as before.

The anchoring also reads the commit leg of every record the caller's
admission does not count in the caller's checkout (`commit_legs.
CommitLegs`, ENG-229): at its home checkout's live view, else a caller or
project stand-in that holds its cited commit. A record with no home row
is not checked until it is homed; its recorded worktree supplies no
commit fallback. A leg that applies and that nothing here reads is not
checked, and the verdict is never ``fresh``. Without anchoring such a
record has no commit leg, as before.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .attestation_paths import record_paths
from .commit_legs import CommitLegs, CommitLegUnchecked, unchecked_verdict
from .models import Memory
from .origin import Origin
from .verify_basis import CommitDriftUnread
from .verify import (
    Anchoring,
    CommitDriftStatus,
    PathDriftReport,
    VerificationStatus,
    commit_drift_status,
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
)

if TYPE_CHECKING:
    from .admission import DriftScope

__all__ = [
    "MemoryVerdict",
    "UnresolvedVerdict",
    "memoised_verdicts",
    "memory_verdict",
    "resolve_verdicts",
]


@dataclass(frozen=True)
class MemoryVerdict:
    """One memory's staleness parts, as `memory_show` emits them.

    `commit_drift` is None where the leg does not apply (never verified,
    no repository named, no anchoring to read it away from the caller,
    no anchor, or every anchor escaping the repository or a phantom);
    `staleness_verdict` is one of ``fresh`` / ``spot_check_recommended``
    / ``spot_check_required``, before the trust rule. `commit_drift_unread` is why the history was
    not read where the leg applies (`verify_basis.CommitDriftUnread`), and
    None elsewhere: `commit_drift` is None there too, the verdict is
    ``spot_check_required``, and the surface flags the row
    (`ResponseBuilder.apply_commit_drift_unread`). `commit_drift_unchecked`
    is why nothing here read a leg that applies (`commit_legs.
    CommitLegUnchecked`, ENG-229), and None elsewhere: `commit_drift` is
    None there too, the verdict is never ``fresh``, and the surface flags
    the row (`ResponseBuilder.apply_commit_drift_unchecked`)."""

    id: str
    verification: VerificationStatus
    path_drift: PathDriftReport
    commit_drift: CommitDriftStatus | None
    staleness_verdict: str
    commit_drift_unread: CommitDriftUnread | None = None
    commit_drift_unchecked: CommitLegUnchecked | None = None


@dataclass(frozen=True)
class UnresolvedVerdict:
    """A row `memoised_verdicts` could not settle without git: its commit
    leg applies and the memo does not hold its key. The two parts that
    need no git are here; the verdict is not, since the commit leg can
    move it either way (escalate a fresh row, stand a stale one down)."""

    id: str
    verification: VerificationStatus
    path_drift: PathDriftReport


def resolve_verdicts(
    memories: Sequence[Memory],
    *,
    caller_origin: Origin | None,
    now: datetime,
    stale_after_days: int,
    anchoring: Anchoring | None = None,
    admission: DriftScope | None = None,
) -> list[MemoryVerdict]:
    """Every memory's verdict, in order, resolving each commit leg the memo
    does not hold and keeping what the pass resolved in `_DRIFT_MEMO`.

    `admission` is what the commit legs are read under in the caller's
    checkout (`admission.DriftScope`: the surface's admission for the
    caller, or the records one decided); a record it does not count, and
    every record when it is None, is read where `anchoring` reads it
    (`commit_legs.CommitLegs`), and has no commit leg with neither.

    One pass over the batch at each root (`_response._DriftPass`), so the
    rows share
    what a search's hits share: the head's history read once, a walk
    forked once per anchor, the files beside the history stamped once.
    The memo is filled at the end, while the head and every keyed file
    read as they did when the rows were keyed; a pass during which a
    commit landed keeps nothing and the next call resolves again.

    `memory_show` calls it with one memory (`memory_verdict`)."""
    drift = _open_legs(caller_origin, admission, anchoring)
    if drift is not None:
        drift.prime([memory.id for memory in memories])
    elif anchoring is not None:
        anchoring.prime([memory.id for memory in memories])
    out: list[MemoryVerdict] = []
    for memory in memories:
        verdict = _verdict(
            memory,
            drift,
            now=now,
            stale_after_days=stale_after_days,
            anchoring=anchoring,
        )
        assert isinstance(verdict, MemoryVerdict)  # resolve=True answers every row
        out.append(verdict)
    if drift is not None:
        drift.store()
    return out


def memoised_verdicts(
    memories: Sequence[Memory],
    *,
    caller_origin: Origin | None,
    now: datetime,
    stale_after_days: int,
    anchoring: Anchoring | None = None,
    admission: DriftScope | None = None,
) -> list[MemoryVerdict | UnresolvedVerdict]:
    """Every memory's verdict, in order, from the memo alone: no git
    process runs.

    A row reads a full `MemoryVerdict` when its commit leg needs no git
    (never verified, no repository named, no anchor and no declared
    claim, no home row, a cited commit no live checkout here holds) or
    the memo holds its key; the result is then equal to what
    `resolve_verdicts` returns for it. Every other row is an `UnresolvedVerdict`: after a
    commit (every key moves with the head), for a row never resolved, and
    wherever the repository's files do not settle a key at all (off
    POSIX, a reftable repository, a `config` that moves the working
    tree), where only `resolve_verdicts` answers. Building a key stats
    files only (the head, the attribute files a governed claim reads, the
    files beside the history), so the call is safe on a hook's deadline.
    `admission` as for `resolve_verdicts`."""
    drift = _open_legs(caller_origin, admission, anchoring)
    if drift is not None:
        drift.prime([memory.id for memory in memories])
    elif anchoring is not None:
        anchoring.prime([memory.id for memory in memories])
    return [
        _verdict(
            memory,
            drift,
            now=now,
            stale_after_days=stale_after_days,
            resolve=False,
            anchoring=anchoring,
        )
        for memory in memories
    ]


def memory_verdict(
    memory: Memory,
    *,
    caller_origin: Origin | None,
    now: datetime,
    stale_after_days: int,
    anchoring: Anchoring | None = None,
    admission: DriftScope | None = None,
) -> MemoryVerdict:
    """`resolve_verdicts` for one memory: `memory_show`'s verdict."""
    (verdict,) = resolve_verdicts(
        [memory],
        caller_origin=caller_origin,
        now=now,
        stale_after_days=stale_after_days,
        anchoring=anchoring,
        admission=admission,
    )
    return verdict


def _open_legs(
    caller_origin: Origin | None,
    admission: DriftScope | None,
    anchoring: Anchoring | None,
) -> CommitLegs | None:
    """Where a call's commit legs are read (`commit_legs.CommitLegs`): in
    the caller's checkout under `admission`, and with `anchoring` where
    the estate check reads every other record's; None when neither reads
    any (no admission or no repository at the caller, and no anchoring),
    where no memory's leg applies."""
    legs = CommitLegs(caller_origin, admission, anchoring)
    return legs if legs.active else None


def _verdict(
    memory: Memory,
    drift: CommitLegs | None,
    *,
    now: datetime,
    stale_after_days: int,
    resolve: bool = True,
    anchoring: Anchoring | None = None,
) -> MemoryVerdict | UnresolvedVerdict:
    """One row's verdict through `drift`, or unresolved where `resolve` is
    False and the commit leg needs git the memo does not spare."""
    # Verification staleness is structurally always present — emitted
    # even for "fresh" memories — because consistent shape means the
    # consumer can branch on `verification.status` without an existence
    # check. The clock moves under it, so it is never memoised.
    verification = compute_verification_status(
        memory.last_verified_at, now=now, stale_after_days=stale_after_days
    )
    # Path drift runs against the full body; `verified_paths` is threaded
    # in so a path the user has previously attested lands in
    # `path_drift.verified`. A homed record's claims are read where design
    # 3.6 reads them (its anchor); a record with no home row at its
    # recorded worktree. Stats only, and never memoised: a file can vanish
    # without anything the commit memo keys on moving.
    anchor = (
        anchoring.for_record(
            memory.id,
            origin_root=memory.origin.worktree_root if memory.origin else None,
        )
        if anchoring is not None
        else None
    )
    path_drift = detect_path_drift(
        memory.body,
        verified_paths=record_paths(memory),
        absent_paths=record_paths(memory, absent=True),
        worktree_root=(
            memory.origin.worktree_root
            if anchor is None and memory.origin is not None
            else None
        ),
        anchor=anchor,
    )
    commit_drift: CommitDriftStatus | None = None
    unread: CommitDriftUnread | None = None
    unchecked: CommitLegUnchecked | None = None
    if drift is not None:
        # The commit leg, claim-anchored and memoised exactly as the
        # search's per-hit count is (`_DriftPass.lookup`), counted from the
        # record's own stamp where `CommitLegs` reads it: in the caller's
        # checkout, or at the record's home or recorded worktree.
        answered, resolved = drift.lookup(
            memory, memory.last_verified_at, resolve=resolve
        )
        if not answered:
            return UnresolvedVerdict(
                id=memory.id, verification=verification, path_drift=path_drift
            )
        if isinstance(resolved, CommitDriftUnread):
            unread = resolved
        elif isinstance(resolved, CommitLegUnchecked):
            unchecked = resolved
        elif resolved is not None:
            commit_drift = commit_drift_status(resolved)
    staleness_verdict = compute_staleness_verdict(
        verification=verification,
        # Claim-anchored subset only: attested paths that vanished, and
        # citations resolved against this memory's own recorded worktree.
        # The full `missing` list still travels in `path_drift` — prose
        # absences stay visible as evidence, they just no longer raise the
        # tier. Rationale and the 15-vs-3 measurement live on
        # `verdict_from_signals`.
        path_drift_missing=len(path_drift.claim_anchored_missing),
        commit_drift_count=(
            commit_drift.commits_since_verify if commit_drift is not None else None
        ),
    )
    return MemoryVerdict(
        id=memory.id,
        verification=verification,
        path_drift=path_drift,
        commit_drift=commit_drift,
        # A history not read never reads fresh: the row's standing could
        # not be established, as under `apply_trust_unavailable`. Nor does
        # a leg nothing here read (ENG-229).
        staleness_verdict=(
            "spot_check_required"
            if unread is not None
            else unchecked_verdict(staleness_verdict)
            if unchecked is not None
            else staleness_verdict
        ),
        commit_drift_unread=unread,
        commit_drift_unchecked=unchecked,
    )
