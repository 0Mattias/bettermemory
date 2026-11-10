"""Where each record's commit leg is read for one caller (ENG-229).

A record's commit leg is counted in the caller's checkout where the
caller's admission counts its home there (`counted_at_caller`: the
caller stands in a repository with a remote, the record names one, and
`DriftScope.drift_counts` holds) and a history applies there. Everywhere
else it is read where health's estate check (`health.cross_repo_drift`)
reads a homed record, so `memory_show`, `memory_search` and health give
one memory one verdict for one caller: at `ClaimAnchor.commit_root` of
the commit its stamp names (design 3.6), its home checkout's live view
here, else the caller's checkout when it holds that commit.

Where the leg applies and nothing reads it, the row is not checked
(`CommitLegUnchecked`): no live checkout here holds the cited commit, no
history applies at the root it resolves to, or the record has no home
row. A row not checked never reads ``fresh``: its verdict is at least
``spot_check_recommended`` (`unchecked_verdict`), as health lists it
skipped rather than clean.

A record with no home row anchors nowhere until the migration homes it
(decision B23: the verification path compares no remotes), and health
lists it skipped, no home, as these surfaces read it not checked; the
estate check once read one at its recorded worktree and counted what
these surfaces left not checked (ENG-229's review).

The leg applies to a verified record with a path anchor or a declared
claim that names a repository (`names_a_repository`): one written in a
git working tree (a remote or a toplevel read) or stamped at a commit. A
record written outside any repository and stamped at no commit has no
repository whose commits could speak for it, wherever it is homed; nor
has a record homed in no project (``global``, `GLOBAL_RECORD`), as
`Admission.drift_counts` already rules where the caller stands, wherever
it was written. Its leg is not "not checked": a record moved to global
and verified again stamps no commit, so a leg that applied there would
read not checked until it moved back, and no verify could clear it
(ENG-229's review).

Before ENG-229, a record the caller's admission did not count had no
commit leg on `memory_show` and `memory_search`: a memory homed in one
repository and read from another, a plain folder, a bare clone or inside
``.git`` read ``fresh`` with no commit counted, while the estate check
counted it at its home and listed it drifted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from ._response import _Drift, _DriftPass
from .claims import load_claims
from .verify import commit_drift_anchor_paths
from .verify_anchoring import GLOBAL_RECORD

if TYPE_CHECKING:
    from .admission import DriftScope
    from .origin import Origin
    from .verify_anchoring import Anchoring

__all__ = [
    "NO_HISTORY",
    "NO_HOME",
    "UNCHECKED_RECOMMENDATIONS",
    "UNCONFIRMED",
    "CommitLegUnchecked",
    "CommitLegs",
    "counted_at_caller",
    "names_a_repository",
    "unchecked_recommendation",
    "unchecked_verdict",
]

#: The home checkout has no live view here, and the checkout the caller
#: stands in, where it stands in one, does not hold the commit the stamp
#: names (`ClaimAnchor.commit_root`). A bare clone, a ``.git`` directory
#: and a folder outside any checkout are no checkout the membership model
#: reads, so nothing there is searched for the commit (ENG-229's review:
#: the cause said no checkout here held it, where a bare clone the caller
#: stood in did).
UNCONFIRMED = "unconfirmed"
#: The root the leg resolves to reads no history that applies: git reads
#: no repository there, or one membership refuses (ENG-158), or its
#: branch has no commit (`origin_dates.commit_author_timestamps` None).
NO_HISTORY = "no_history"
#: A record with no home row: it anchors nowhere until the migration
#: homes it (decision B23).
NO_HOME = "no_home"

_UNCHECKED_RECOMMENDATIONS: dict[str, str] = {
    UNCONFIRMED: (
        "This memory's home checkout has no live view on this machine, and "
        "the checkout you are in does not hold the commit it was verified at "
        "(a bare clone or a folder outside any checkout is not searched), so "
        "the commits since were not counted. Spot-check a claim before "
        "relying on the memory, or read it from a checkout of its project."
    ),
    NO_HISTORY: (
        "git reads no commit history at this memory's home checkout, so the "
        "commits since it was verified were not counted. Spot-check a claim "
        "before relying on the memory."
    ),
    NO_HOME: (
        "This memory has no home yet, so no checkout's commits were counted "
        "since it was verified. Spot-check a claim before relying on the "
        "memory."
    ),
}
#: Every recommendation a row not checked carries.
UNCHECKED_RECOMMENDATIONS = frozenset(_UNCHECKED_RECOMMENDATIONS.values())

_FRESH = "fresh"
_RECOMMENDED = "spot_check_recommended"


@dataclass(frozen=True)
class CommitLegUnchecked:
    """A commit leg that applies and that nothing read: `why` is one of
    `UNCONFIRMED`, `NO_HISTORY`, `NO_HOME`; `rule` the anchor's rule, as
    health's estate check lists it skipped."""

    why: str
    rule: str = ""


def unchecked_recommendation(unchecked: CommitLegUnchecked) -> str:
    """The recommendation a row not checked carries."""
    return _UNCHECKED_RECOMMENDATIONS[unchecked.why]


def unchecked_verdict(verdict: str) -> str:
    """A not-checked row's verdict: never ``fresh``, at least
    ``spot_check_recommended``; a ``spot_check_required`` stands."""
    return _RECOMMENDED if verdict == _FRESH else verdict


def names_a_repository(
    origin_repo: str | None, worktree_root: str | None, verified_head: str | None
) -> bool:
    """Whether a repository's commits can speak for the record: it was
    written in a git working tree (`origin.repo`, or `origin.worktree_root`,
    which only git's toplevel sets) or its stamp names a commit."""
    return bool(origin_repo or worktree_root or verified_head)


def counted_at_caller(
    record_id: str,
    origin_repo: str | None,
    *,
    caller_origin: Origin | None,
    admission: DriftScope,
) -> bool:
    """Whether the record's commit leg is counted in the caller's checkout:
    the caller stands in a repository with a remote, the record names one,
    and the caller's admission counts its home there (`drift_counts`: a
    project's home the surface admits). Where a history applies there, the
    caller's pass counts it (`_response._DriftPass`); every other record's
    leg is read where `CommitLegs` reads it."""
    if caller_origin is None or not caller_origin.cwd or not caller_origin.repo:
        return False
    if not origin_repo:
        return False
    return admission.drift_counts(record_id)


class _AtHome:
    """The scope a pass at a record's home reads under: every record it is
    asked about counts there, since `CommitLegs` decided the root."""

    def prime(self, record_ids: Iterable[str]) -> None:
        """Nothing to read."""

    def drift_counts(self, record_id: str) -> bool:
        return True


_AT_HOME = _AtHome()

#: What one row's lookup answers: the leg, or why it was not checked.
Leg = _Drift | CommitLegUnchecked


class CommitLegs:
    """The commit legs of one call for one caller: the caller's pass
    (`_response._DriftPass`), opened where the caller stands in a
    repository with a remote and under an admission, and one pass at each
    other root a record's leg resolves to. Each pass keys, recalls,
    resolves and stores through `_DRIFT_MEMO` as the caller's does, so a
    row read at its home is memoised as one read in the caller's checkout.

    `eager` reads the caller's history as the pass opens and drops the
    pass where none applies, as `memory_search` always has; otherwise it
    is read where a row that counts there resolved no leg. Either way a
    record counted at the caller where no history applies is read at its
    home. Not shared between threads, as the passes are not."""

    def __init__(
        self,
        caller_origin: Origin | None,
        admission: DriftScope | None,
        anchoring: Anchoring | None,
        *,
        eager: bool = False,
    ) -> None:
        self.caller_origin = caller_origin
        self.admission = admission
        self.anchoring = anchoring
        self.caller: _DriftPass | None = None
        self._caller_applies: bool | None = None
        if (
            admission is not None
            and caller_origin is not None
            and caller_origin.cwd is not None
            and caller_origin.repo is not None
        ):
            self.caller = _DriftPass(
                Path(caller_origin.cwd), caller_origin.repo, admission
            )
            if eager:
                self._caller_applies = self.caller.timestamps() is not None
        self._roots: dict[str, _DriftPass] = {}
        self._root_applies: dict[str, bool] = {}

    @property
    def active(self) -> bool:
        """Whether any record's leg can be read: a caller's pass, or the
        anchoring the other roots are read through."""
        return self.anchoring is not None or (
            self.caller is not None and self._caller_applies is not False
        )

    def prime(self, record_ids: Iterable[str]) -> None:
        """Read the homes of `record_ids` once, for the admission and the
        anchoring."""
        ids = list(record_ids)
        if self.admission is not None:
            self.admission.prime(ids)
        if self.anchoring is not None:
            self.anchoring.prime(ids)

    def lookup(
        self, record: Any, last_verified_at: datetime | None, *, resolve: bool = True
    ) -> tuple[bool, Leg]:
        """One row's commit leg, as `_DriftPass.lookup` answers it: whether
        it is answered, and the leg (None where it does not apply,
        `CommitDriftUnread` where the history was not read), or why it was
        not checked (`CommitLegUnchecked`). With `resolve` False no git
        runs: a row the memo does not hold is left unanswered."""
        if last_verified_at is None:
            return True, None
        # The claim-anchored gate every pass applies (`_DriftPass.lookup`),
        # read first so a record it exempts reads no history anywhere.
        if not commit_drift_anchor_paths(record.body, record.verified_paths) and not (
            record.claims and load_claims(record.claims)
        ):
            return True, None
        origin = record.origin
        origin_repo = origin.repo if origin is not None else None
        if (
            self.caller is not None
            and self.admission is not None
            and self._caller_applies is not False
            and counted_at_caller(
                record.id,
                origin_repo,
                caller_origin=self.caller_origin,
                admission=self.admission,
            )
        ):
            answered, leg = self.caller.lookup(
                record, last_verified_at, resolve=resolve
            )
            if not answered or leg is not None or not resolve:
                return answered, leg
            if self._caller_history_applies():
                return True, None
        if self.anchoring is None:
            return True, None
        if not names_a_repository(
            origin_repo,
            origin.worktree_root if origin is not None else None,
            record.verified_head,
        ):
            return True, None
        root = self._root_for(record)
        if root is None:
            return True, None
        if isinstance(root, CommitLegUnchecked):
            return True, root
        return self._at_root(root, record, last_verified_at, resolve)

    def _caller_history_applies(self) -> bool:
        """Whether a history applies in the caller's checkout, read once
        (`_DriftPass.history_applies`: the whole history only where the
        root and the head do not settle it)."""
        if self._caller_applies is None:
            assert self.caller is not None
            self._caller_applies = self.caller.history_applies()
        return self._caller_applies

    def _root_for(self, record: Any) -> str | CommitLegUnchecked | None:
        """The root the record's leg is read at away from the caller, or why
        it is not checked; None for a record homed in no project, which has
        no leg (`GLOBAL_RECORD`). Reads the object store a cited commit is
        looked up in, and runs no git."""
        assert self.anchoring is not None
        anchor = self.anchoring.for_record(record.id)
        if anchor is None:
            return CommitLegUnchecked(NO_HOME)
        if anchor.rule == GLOBAL_RECORD:
            return None
        root, _rule = anchor.commit_root(record.verified_head)
        if root is None:
            return CommitLegUnchecked(UNCONFIRMED, anchor.rule)
        return root

    def _at_root(
        self, root: str, record: Any, last_verified_at: datetime, resolve: bool
    ) -> tuple[bool, Leg]:
        """The row's leg through the pass at `root`, opened once per call;
        not checked where no history applies there (`_DriftPass.
        history_applies`, which reads the whole history only where the
        root's head did not resolve)."""
        found = self._roots.get(root)
        if found is None:
            found = _DriftPass(Path(root), "", _AT_HOME, at_home=True)
            self._roots[root] = found
        answered, leg = found.lookup(record, last_verified_at, resolve=resolve)
        if not answered or leg is not None or not resolve:
            return answered, leg
        if root not in self._root_applies:
            self._root_applies[root] = found.history_applies()
        if not self._root_applies[root]:
            return True, CommitLegUnchecked(NO_HISTORY)
        return True, None

    def store(self) -> None:
        """Merge every pass's resolutions into `_DRIFT_MEMO`
        (`_DriftPass.store`)."""
        if self.caller is not None:
            self.caller.store()
        for found in self._roots.values():
            found.store()
