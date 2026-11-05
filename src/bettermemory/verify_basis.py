"""The bases a commit-drift count is measured on, and the count since a verify.

`BASIS_REACHABILITY` and `BASIS_AUTHOR_DATE` name the axis a commit-drift
count was measured on; `verify` re-exports both, where callers import
them. `_count_since_verify` is the first step of
`verify.compute_commit_drift`: the commits since a verify, counted in
reachability space from the stamp's `verified_head` while that commit is
still an ancestor of HEAD, and by author date otherwise. `CommitDriftUnread`
is the third outcome, where the history the basis reads was not read,
and says why; `unread_recommendation` is what such a row tells the
reader to do.

This module lives apart from `verify.py` to keep it well under the
package's 1,900-line watch line (ENG-97). It is a leaf of the verify
family: from the package it imports `origin`, `origin_dates` and
`origin_history` alone;
`verify` imports it, and the per-hit surfaces (`_response`, `verdict`)
import `CommitDriftUnread` from it. A patch on `commit_author_timestamps`,
`commits_since_anchor` or `repo_toplevel_and_head` that must reach the
count targets this module.
"""

from __future__ import annotations

import bisect
from datetime import datetime
from pathlib import Path

from .origin import (
    ReachableWalk,
    commits_since_anchor,
    repo_toplevel_and_head,
)
from .origin_dates import commit_author_timestamps
from .origin_history import HistoryUnread, unwalked

# The axis a commit-drift count was measured on, carried as `basis` on
# every block the four surfaces emit, so they cannot disagree about which
# one they counted. `compute_commit_drift` picks it per memory: the
# reachable range from the stamp's `verified_head` when that anchor is
# an ancestor of HEAD, the author-date count otherwise.
BASIS_REACHABILITY = "reachability"
BASIS_AUTHOR_DATE = "author-date"


#: A commit leg whose history was not read, and why
#: (`origin_history.HistoryUnread`): the walk from the stamp's head and the
#: listing of its commits both met their timeout (`origin_history.unwalked`),
#: whose dates are not read in their place since a rebased or back-dated
#: commit moves them, or the dates themselves were not read. Not None,
#: which says the leg does not apply: the per-memory surfaces omit the
#: count but flag the row and stop it reading fresh
#: (`_response.ResponseBuilder.apply_commit_drift_unread`), and the health
#: rollups count it under `unread`.
CommitDriftUnread = HistoryUnread

# The recommendation such a row carries, on the search hit and memory_show,
# by cause.
_UNREAD_RECOMMENDATIONS: dict[HistoryUnread, str] = {
    HistoryUnread.TIMEOUT: (
        "The repository's commit history could not be read in time, so the "
        "commits since this memory was verified were not counted. Spot-check "
        "a claim before relying on the memory."
    ),
    HistoryUnread.OVER_BUDGET: (
        "The repository's commit history is longer than bettermemory reads, "
        "so the commits since this memory was verified were not counted. "
        "Spot-check a claim before relying on the memory."
    ),
    HistoryUnread.SHALLOW: (
        "The checkout is a shallow clone, whose history is cut, so the "
        "commits since this memory was verified were not counted. Fetch the "
        "missing history (git fetch --unshallow), or spot-check a claim "
        "before relying on the memory."
    ),
    HistoryUnread.ERROR: (
        "git failed reading the repository's commit history, so the commits "
        "since this memory was verified were not counted. Spot-check a "
        "claim before relying on the memory."
    ),
}
#: Every recommendation an unread row carries.
UNREAD_RECOMMENDATIONS = frozenset(_UNREAD_RECOMMENDATIONS.values())


def unread_recommendation(why: HistoryUnread) -> str:
    """The recommendation a row whose history was not read carries."""
    return _UNREAD_RECOMMENDATIONS[why]


def dates_since(
    history: list[datetime] | HistoryUnread | None, since: datetime
) -> int | HistoryUnread | None:
    """The commits authored strictly after `since` on `history`, ascending
    author dates (`origin_dates.commit_author_timestamps`), by
    ``bisect_right``: an equal instant falls before the cut, so a verify in
    the same instant as a commit is no drift. Why the history was not
    read, or None where none applies, passed through."""
    if history is None or isinstance(history, HistoryUnread):
        return history
    return len(history) - bisect.bisect_right(history, since)


def _count_since_verify(
    cwd_path: Path, last_verified_at: datetime, verified_head: str | None
) -> tuple[int, str, ReachableWalk | None, Path | None] | HistoryUnread | None:
    """Take the stamp; return (count, basis, walk, toplevel) since it, why
    the history was not read, or None where none applies."""
    # The basis. A stamp that recorded the checkout's HEAD is counted in
    # reachability space — the commits in `verified_head..HEAD`, which
    # hold a branch authored before the stamp and merged after it — when
    # that anchor is still an ancestor of HEAD; `commits_since_anchor`
    # answers None otherwise (a rewritten history, a checkout that moved
    # backwards, an anchor this repository does not have) and the
    # author-date count below stands. The root and the head together,
    # read from the repository's files (one `rev-parse` where they do not
    # settle them), then the walk, memoised per (root, anchor, head) so a
    # search over many hits verified at one anchor forks it once. The
    # whole-history log below is memoised per (root, head), shared with
    # the per-hit search surface.
    walk: ReachableWalk | None = None
    toplevel: Path | None = None
    if verified_head is not None:
        located = repo_toplevel_and_head(cwd_path)
        if located is not None:
            toplevel, head = located
            given_up = unwalked()
            walk = commits_since_anchor(
                cwd_path, verified_head, toplevel=toplevel, head=head
            )
            if walk is None and unwalked() != given_up:
                # The walk and its listing met their timeout: the count is
                # omitted, never read off the author dates
                # (`origin_history.unwalked`), and the row is unread.
                return HistoryUnread.TIMEOUT
    if walk is not None:
        count = len(walk.commits)
        basis = BASIS_REACHABILITY
    else:
        # Count via author timestamps + bisect_right — the SAME date
        # source and boundary rule memory_search
        # (`_response.attach_commit_drift_counts`) and memory_health
        # (`_compute_commit_drift_debt`) use. Two prior divergences from
        # those surfaces are both closed here:
        #   1. date source — the old `commits_since` shelled out `git
        #      rev-list --since`, which filters on COMMITTER date; a
        #      rebase rewrites committer date while preserving author
        #      date, so the same memory could read drifted via
        #      memory_show yet clean via memory_search.
        #   2. boundary — `git rev-list --since` is INCLUSIVE and
        #      whole-second, so a commit landing in the same UTC second
        #      as `last_verified_at` counted as drift on memory_show but
        #      not on the bisect_right (strictly-greater, microsecond)
        #      path the other two use.
        # `commit_author_timestamps` returns timezone-aware datetimes
        # already ascending, so `bisect_right` yields the first index
        # strictly after the verify instant. Equal-instant commits fall
        # before the cut (no drift), matching the health rollup and
        # per-hit search count exactly. The sort lives at the source
        # because THIS function runs once per memory: doing it here
        # re-sorted the repo's whole history per row.
        counted = dates_since(commit_author_timestamps(cwd_path), last_verified_at)
        if counted is None or isinstance(counted, HistoryUnread):
            return counted
        count = counted
        basis = BASIS_AUTHOR_DATE
    return count, basis, walk, toplevel
