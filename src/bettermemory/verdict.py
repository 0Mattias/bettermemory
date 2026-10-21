"""One staleness verdict per memory, for `memory_show` and the session brief.

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
- `path_drift`: the claim-anchored path report (`verify.detect_path_
  drift`), stats only;
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

`memoised_verdicts` is the same computation for a caller that must not
fork git (the session brief, built on a hook's deadline): a row whose
commit leg the memo answers, or that needs no git at all, gets its full
verdict; every other row comes back an `UnresolvedVerdict`, with no
process run. `resolve_verdicts` over those rows answers them in the
caller's thread; `fill_verdicts` fills the memo with their commit legs
from a worker thread, storing row by row so that one disturbance in the
repository loses one row, not the pass. The memo is lock-guarded and
each call opens its own pass.

The caller's origin is an argument, never captured here: `memory_show`
passes `_handlers.capture_origin()`, the hook the origin it builds from
its payload's directory.

So is the claims' anchoring (design 3.6): `anchoring` reads each homed
record's path claims at its home checkout's live view, translated across
its recorded views, or in a stand-in that confirms by presence and never
convicts (`verify.Anchoring`). A record with no home row, and every record
when no anchoring is passed, is read at its recorded root as before.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ._response import _DriftPass
from .models import Memory
from .origin import Origin
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
    "FillReport",
    "MemoryVerdict",
    "UnresolvedVerdict",
    "fill_verdicts",
    "memoised_verdicts",
    "memory_verdict",
    "resolve_verdicts",
]


@dataclass(frozen=True)
class MemoryVerdict:
    """One memory's staleness parts, as `memory_show` emits them.

    `commit_drift` is None where the leg does not apply (never verified,
    no repository, the caller elsewhere, no anchor, or every anchor
    escaping the repository or a phantom); `staleness_verdict` is one of
    ``fresh`` / ``spot_check_recommended`` / ``spot_check_required``,
    before the trust rule."""

    id: str
    verification: VerificationStatus
    path_drift: PathDriftReport
    commit_drift: CommitDriftStatus | None
    staleness_verdict: str


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

    `admission` is what the commit legs are read under
    (`admission.DriftScope`: the surface's admission for the caller, or
    the records one decided); a record it does not count, and every
    record when it is None, has no commit leg (`_open_pass`).

    One pass over the batch (`_response._DriftPass`), so the rows share
    what a search's hits share: the head's history read once, a walk
    forked once per anchor, the files beside the history stamped once.
    The memo is filled at the end, while the head and every keyed file
    read as they did when the rows were keyed; a pass during which a
    commit landed keeps nothing and the next call resolves again.

    `memory_show` calls it with one memory; the session brief with the
    rows it resolves synchronously. The brief's background fill, which
    resolves the rest for the next call, runs `fill_verdicts`, which
    stores row by row."""
    drift = _open_pass(caller_origin, admission)
    if anchoring is not None:
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


@dataclass(frozen=True)
class FillReport:
    """What one `fill_verdicts` call did with the rows handed to it.

    `rows` is how many it was given; `resolved` how many commit legs its
    first attempt resolved with git and could key (a row the memo already
    answered, one that needs no git, one whose files settle no key and
    one resolved while a git process failed count for none); `kept` how
    many resolutions reached `_DRIFT_MEMO`, first attempt and retry
    together; `retried` the rows whose first resolution the hold check
    discarded, each resolved once more; `lost` those whose retry was
    discarded too."""

    rows: int
    resolved: int = 0
    kept: int = 0
    retried: int = 0
    lost: int = 0


def fill_verdicts(
    memories: Sequence[Memory],
    *,
    caller_origin: Origin | None,
    admission: DriftScope | None = None,
) -> FillReport:
    """Resolve each memory's commit leg the memo does not hold into
    `_DRIFT_MEMO`, for the next `memoised_verdicts` to read: the session
    brief's background fill. Returns what it kept (`FillReport`). No
    verdict is assembled: the memo holds the commit leg alone, and the
    other parts are computed on every read.

    What keeps a resolution is `resolve_verdicts`'s rule, the pass's
    `store` check: the head, its signature, every keyed file and every
    held directory (the repository root, the index, the attribute files'
    directories) read as they did when the pass opened. What changes is
    the window. `resolve_verdicts` stores once, at the end, so one
    unrelated disturbance anywhere in a pass over a hundred rows (a file
    an editor creates and removes in the root, a ``git status`` that
    rewrites the index) discards every row. Here the pass stores after
    each row it resolved: a store that holds commits that row, its check
    covering the whole span since the pass opened and so every walk the
    row shared with earlier rows; a store that fails discards only the
    row resolved since the last one that held, and the fill goes on in a
    new pass opened on what the files read then. The rows a failed store
    discarded are resolved once more, in a pass of their own, after the
    rest; a row lost twice stays out of the memo and the next brief
    resolves it. A disturbance that never stops (files created in the
    root steadily) still keeps nothing, as before.

    No looser than one store at the end: each store compares with the
    pass's opening read, as the single store does, and a pass whose check
    has failed once stores nothing more (`_DriftPass.store`). The cost is
    one check per row resolved with git: stats of the held files and
    directories and of the files that name the head, beside the git
    process the row ran.

    `admission` as for `resolve_verdicts`; the fill runs on a worker
    thread, so it is a `CountedRecords` decided on the request's
    (`Admission.counted`)."""
    rows = list(memories)
    if caller_origin is None or caller_origin.cwd is None or admission is None:
        return FillReport(rows=len(rows))
    if caller_origin.repo is None:
        return FillReport(rows=len(rows))
    cwd = Path(caller_origin.cwd)
    repo = caller_origin.repo
    scope = admission

    def attempt(batch: list[Memory]) -> tuple[int, int, list[Memory]]:
        drift = _DriftPass(cwd, repo, scope)
        resolved = kept = 0
        discarded: list[Memory] = []
        for memory in batch:
            drift.lookup(memory, memory.last_verified_at)
            count = drift.unstored
            if not count:
                continue
            resolved += count
            if drift.store():
                kept += count
            else:
                discarded.append(memory)
                drift = _DriftPass(cwd, repo, scope)
        return resolved, kept, discarded

    resolved, kept, discarded = attempt(rows)
    if not discarded:
        return FillReport(rows=len(rows), resolved=resolved, kept=kept)
    _, again, still = attempt(discarded)
    return FillReport(
        rows=len(rows),
        resolved=resolved,
        kept=kept + again,
        retried=len(discarded),
        lost=len(still),
    )


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
    (never verified, no repository, the caller outside the memory's
    repository or in none, no anchor and no declared claim) or the memo
    holds its key; the result is then equal to what `resolve_verdicts`
    returns for it. Every other row is an `UnresolvedVerdict`: after a
    commit (every key moves with the head), for a row never resolved, and
    wherever the repository's files do not settle a key at all (off
    POSIX, a reftable repository, a `config` that moves the working
    tree), where only `resolve_verdicts` answers. Building a key stats
    files only (the head, the attribute files a governed claim reads, the
    files beside the history), so the call is safe on a hook's deadline.
    `admission` as for `resolve_verdicts`."""
    drift = _open_pass(caller_origin, admission)
    if anchoring is not None:
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


def _open_pass(
    caller_origin: Origin | None, admission: DriftScope | None
) -> _DriftPass | None:
    """The pass a call's commit legs run through, under `admission`, or
    None when there is none to read under (a pass never counts a record
    no admission admitted) or the caller is in no repository (no
    directory, or no remote), where no memory's leg applies, as
    `verify.compute_commit_drift` returns None there."""
    if caller_origin is None or admission is None:
        return None
    if caller_origin.cwd is None or caller_origin.repo is None:
        return None
    return _DriftPass(Path(caller_origin.cwd), caller_origin.repo, admission)


def _verdict(
    memory: Memory,
    drift: _DriftPass | None,
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
    anchor = anchoring.for_record(memory.id) if anchoring is not None else None
    path_drift = detect_path_drift(
        memory.body,
        verified_paths=memory.verified_paths,
        absent_paths=memory.verified_absent_paths,
        worktree_root=(
            memory.origin.worktree_root
            if anchor is None and memory.origin is not None
            else None
        ),
        anchor=anchor,
    )
    commit_drift: CommitDriftStatus | None = None
    if drift is not None:
        # The cwd-aware commit leg, claim-anchored and memoised exactly as
        # the search's per-hit count is (`_DriftPass.lookup`), counted from
        # the record's own stamp.
        answered, resolved = drift.lookup(
            memory, memory.last_verified_at, resolve=resolve
        )
        if not answered:
            return UnresolvedVerdict(
                id=memory.id, verification=verification, path_drift=path_drift
            )
        if resolved is not None:
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
        staleness_verdict=staleness_verdict,
    )
