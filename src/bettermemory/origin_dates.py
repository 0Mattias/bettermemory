"""The author dates a commit-drift count is read off.

`commit_author_timestamps` reads a head's whole history as ascending
author dates, memoised per (root, head) in `origin_history`;
`commit_author_timestamps_touching_pathspecs` and
`commit_author_sha_pairs_touching_pathspecs` read the commits that touch a
set of pathspecs, in the same date space. Every git process here is
`origin`'s (`origin._git`, `origin._git_result`), and the root and the
head are read through `origin` too, looked up on the module at call time,
so a patch on `origin` reaches the reads here.

This module lives apart from `origin.py` to keep that module under the
package's 2,000-line bound (ENG-96). It imports `origin`; `origin` never
imports it, and the drift readers (`verify`, `verify_basis`, `_response`,
`health`) import these names from here.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from . import _git as _git_runner
from . import gitfile_rule, githead, origin, origin_history
from .origin_history import HistoryUnread


def _instant(stamp: datetime) -> float:
    """Absolute-instant sort key for timezone-aware author timestamps.

    One `utcoffset()` per element instead of one per comparison: `%aI`
    keeps each author's own UTC offset, so a list parsed from it carries
    many distinct tzinfo objects and CPython's same-tzinfo comparison fast
    path never fires. Same ordering either way; both are the instant.
    """
    return stamp.timestamp()


def commit_author_timestamps(
    cwd: Path | None, *, located: tuple[Path, str] | None = None
) -> list[datetime] | HistoryUnread | None:
    """All author timestamps from the HEAD history of `cwd`'s repo.

    Returns a list of timezone-aware datetimes; None where no history
    applies (cwd is None, git not on PATH, a directory that is not there,
    no repository the files name and none git's walk from `cwd` passes
    unread but one membership refuses (`_unread_entry`: the folder is then
    one outside any repository, ENG-158), and, from ``HEAD`` where no pair
    names the head, a folder git finds no repository for and passes none
    unread or a branch with no commit, which `_refused_head` tells by the
    exit of plumbing); and
    where a history applies and was not read, why
    (`origin_history.HistoryUnread`): git met its timeout, failed on the
    head (named by its hash, or HEAD where the plumbing finds both the
    repository and its head, or a ref of HEAD's that exists and that git
    cannot read, which the files tell from an unborn branch), refused a
    repository its walk from `cwd` stops at, or passed one no reader reads
    (an ``objects`` directory no one may search: ``ERROR``, ENG-220's
    review, rounds F and G; `githead.entry_above`), or printed
    a line its format does not print (one such line declines the whole
    read), the history is longer than the memo's budget, or the clone is
    shallow. A caller reads the third outcome as neither a count nor a
    leg that does not apply: the row is flagged and never reads fresh.

    Returned ASCENDING, ready to `bisect_right`. Every caller wants that
    order and none wants git's; leaving the sort to them meant the
    per-memory `compute_commit_drift` re-sorted the repo's whole history
    on every row, which the rot benchmark measured at 38ms x 2,163 calls
    = 82s of a 90s scipy run. Sorting here happens once per git
    invocation, beside the fork+exec that already dominates the call.

    Read as ``%at``, the author date's seconds, each date built in UTC
    (`origin_history.author_dates`): the instant ``%aI`` names, with one tzinfo for the
    whole list, so the comparisons a sort and a bisect make take CPython's
    same-tzinfo fast path, and a date holds 56 bytes with its slot where
    one parsed with its author's own offset held about 106.

    Used by the health rollup to count commits-since for many memories
    from one git invocation; a per-memory ``git rev-list --count`` would
    otherwise pay a fork+exec for every row.

    MEMOISED per (root, head) (`origin_history`). `located` is the root
    and the head as the caller already resolved them for `cwd`; without
    it they are read from the repository's files
    (`origin.toplevel_and_head_from_files`). The log then names the head by its
    hash, so the list stored under a head is that head's history even if
    a commit lands while git runs. A head whose repository holds an entry
    for an earlier head reads only what it adds (`_extended_author_
    timestamps`, one ``<earlier>..<head>`` log) where the earlier head is
    its ancestor and the files that change which commits a head reaches
    read as they did (`origin_history.history_guard`); elsewhere it reads
    its whole history. A read that fails is remembered for the key for a
    minute, with why: a call meanwhile returns the cause without a process
    and counts the failure on the calling thread
    (`origin.failed_git_calls`, a timeout as a timeout too), as the read
    did, so a caller that keeps a value only when no git call failed keeps
    none from it. Where the files git's discovery reads do not settle the
    pair and the caller passed none (off POSIX, in another account's
    repository), the root and the head are those `githead.locate` reads
    (`origin_history.located_key`), kept and extended alike; only where
    those do not settle it either does the log run from HEAD, uncached. A
    history longer than the memo's budget (`origin_history.remember`) is
    not counted: it answers ``OVER_BUDGET``, counted as a failure, and is
    remembered as a failed read is, so the calls of the next minute answer
    the same without reading it again. A hit returns the stored list
    itself: every caller only bisects it. Replace refs and grafts move
    nothing (`origin_history.AS_STORED_ENV`). A ``shallow`` file that is
    not read (`origin_history.cuts`) answers ``SHALLOW`` with no process;
    one that lists commits has the log name each commit, and a history
    that reaches one of them answers ``SHALLOW`` (`_read_author_timestamps`),
    while one that reaches none counts. A head is extended from an earlier
    one only where that one's history was checked against the same cuts
    and the file has not changed since (`origin_history.base_for`), and
    then reaches a cut only where a commit it adds is one
    (`_extended_author_timestamps`). ``SHALLOW`` is counted as a failure
    and remembered under the file's stamp (`origin_history.history_guard`),
    so a fetch that deepens or unshallows the clone is read on the next
    call: git reads no commit behind a cut, and no window on the dates can
    be shown to stop short of it. A history kept is returned only while
    the file lists none but the cuts it was checked against
    (`origin_history.recall`): one kept before a ``fetch --depth`` wrote
    another cut is read again under it, so the path-filtered logs a row
    narrows on, which git reads under the file as it is, never narrow a
    count read past a cut they stop at (`_touching_log`, ENG-233's
    review); a clone unshallowed since keeps it. The cuts are read
    again after the log, and where the file lists others (a ``git fetch
    --depth`` or ``--unshallow`` while the log ran, which may have stopped
    it at a cut the first read did not list), the read answers
    ``SHALLOW``, counted as a failure, and nothing is kept: the next call
    reads the history under the cuts the file lists then (ENG-233). The
    shallow file, or its directory while absent, is held across the read
    too: a depth fetch undone before the log returns leaves the cut sets
    equal but still invalidates a truncated read, including extensions
    and the uncached HEAD entry path.
    """
    if cwd is None:
        return None
    if located is None:
        located = origin.toplevel_and_head_from_files(cwd)
    key = (
        origin_history.located_key(cwd)
        if located is None
        else (str(located[0]), located[1])
    )
    if key is None:
        cut = origin_history.cuts(cwd, walk=True)
        if cut is None:
            _git_runner.note_failed()
            if githead.gitdir_at(cwd) or githead.locate(cwd):
                # A shallow file that is not read may cut it anywhere.
                return HistoryUnread.SHALLOW
            # No repository the files read: none applies only where no
            # entry may hold one that nothing reads (`_unread_entry`).
            return _unread_entry(cwd)
        held = _hold_shallow(cwd, walk=True)
        return _cut_held(cwd, cut, _read_author_timestamps(cwd, "HEAD", cut), held)
    guard = origin_history.history_guard(Path(key[0]))
    cut = origin_history.cuts(Path(key[0]))
    stored, failure = origin_history.recall(key, guard, cut)
    if stored is not None:
        return stored
    if cut is None or failure is not None:
        if failure is None:
            # A shallow file that is not read may cut the history anywhere.
            failure = HistoryUnread.SHALLOW
            origin_history.remember_failure(key, guard, failure)
        origin_history.note_unread(failure)
        return failure
    held = _hold_shallow(Path(key[0]))
    base = None if guard is None else origin_history.base_for(key[0], guard, cut)
    dates: list[datetime] | HistoryUnread | None = None
    if base is not None:
        base_head, base_dates, cut = base
        dates = _extended_author_timestamps(cwd, base_head, base_dates, key[1], cut)
    if dates is None:
        dates = _read_author_timestamps(cwd, key[1], cut)
    if isinstance(dates, list) and not _shallow_held(Path(key[0]), cut, held):
        # A depth fetch undone during the log leaves the cut sets equal,
        # but changes the file or its containing directory (ENG-233).
        _git_runner.note_failed()
        return HistoryUnread.SHALLOW
    if isinstance(dates, HistoryUnread):
        origin_history.remember_failure(key, guard, dates)
    elif dates is not None and not origin_history.remember(key, dates, guard, cut):
        dates = HistoryUnread.OVER_BUDGET
        origin_history.remember_failure(key, guard, dates)
        _git_runner.note_failed()
    return dates


def _hold_shallow(
    start: Path, *, walk: bool = False
) -> dict[str, tuple[int, int]] | None:
    """Hold the shallow file, or its nearest existing directory, across
    a history read. None when its stamp cannot be read; an empty hold
    where the repository files name no shallow-file location."""
    shallow = origin_history.shallow_file(start, walk=walk)
    held: dict[str, tuple[int, int]] = {}
    if shallow is not None:
        try:
            origin._hold_directory(shallow, held)
        except (OSError, ValueError):
            return None
    return held


def _cut_held(
    cwd: Path,
    cut: frozenset[str],
    dates: list[datetime] | HistoryUnread | None,
    held: dict[str, tuple[int, int]] | None,
) -> list[datetime] | HistoryUnread | None:
    """`dates`, read from HEAD at `cwd` under `cut`, where the shallow file
    still lists `cut` after the log; ``SHALLOW``, counted as a failure,
    where it moved while the log ran (ENG-233)."""
    if isinstance(dates, list) and not _shallow_held(cwd, cut, held, walk=True):
        _git_runner.note_failed()
        return HistoryUnread.SHALLOW
    return dates


def _shallow_held(
    start: Path,
    cut: frozenset[str],
    held: dict[str, tuple[int, int]] | None,
    *,
    walk: bool = False,
) -> bool:
    """The same cuts and the same file/directory evidence after a log."""
    return (
        held is not None
        and origin_history.cuts(start, walk=walk) == cut
        and origin.directories_held(held)
    )


def _read_author_timestamps(
    cwd: Path, revision: str, cut: frozenset[str] = frozenset()
) -> list[datetime] | HistoryUnread | None:
    """`commit_author_timestamps`' read: the author dates of every commit
    reachable from `revision`, ascending; or why they were not read, or
    None where none applies (`origin_history.unanswered`: a `revision`
    named by its hash is a head the repository holds, so git's refusal of
    it is an error). Git's refusal of ``HEAD`` also answers a folder that
    is no repository and a branch with no commit, which `_refused_head`
    tells apart from an error by the exit of two plumbing calls and the
    files. Where a shallow file lists commits (`cut`), the log names each
    commit, as the walk's does (`origin._walk_reachable`): a history that
    lists one of them stops at it and reads ``SHALLOW``, counted as a
    failure; one that reaches none, as a full clone's main branch beside a
    branch fetched with ``--depth``, is read whole and counts (ENG-220's
    review, round F)."""
    # timeout=5.0, not the 1.0 default: the default is calibrated for
    # write-path origin capture, where origin is nice-to-have and a
    # hanging git must never stall a memory_write. This log and its two
    # path-filtered siblings below are READ legs — the drift verdicts
    # search/show/health surface ride on them, and at 1.0s a cold git
    # on a slow host (observed: the windows-latest CI runner) times out,
    # collapsing a real count into the omitted/conservative branch. Same
    # ceiling `commit_patch_stream` already runs at.
    before = origin_history.git_counts()
    form = "--format=%H %at" if cut else "--format=%at"
    raw = origin._git(cwd, "log", form, revision, timeout=5.0)
    if raw is None:
        named = origin.is_full_commit_sha(revision)
        if not named and origin_history.git_counts() == before:
            # Git ran and exited non-zero on HEAD, neither timed out nor
            # missing (ENG-220's review, round F).
            return _refused_head(cwd)
        return origin_history.unanswered(before, named=named)
    if not cut:
        values = origin_history.read_lines(raw, origin_history.seconds)
    else:
        pairs = origin_history.read_lines(raw, _hashed_seconds)
        if pairs and cut.intersection(sha for sha, _ in pairs):
            _git_runner.note_failed()
            return HistoryUnread.SHALLOW
        values = None if pairs is None else [value for _, value in pairs]
    if not values:
        # A line that reads as no date declines the read.
        _git_runner.note_failed()
        return HistoryUnread.ERROR
    return origin_history.author_dates(values)


def _hashed_seconds(line: str) -> tuple[str, int] | None:
    """A ``%H %at`` line as (hash, seconds); None for any other line."""
    sha, _, stamp = line.partition(" ")
    value = origin_history.seconds(stamp)
    if not origin.is_full_commit_sha(sha) or value is None:
        return None
    return sha, value


#: The plumbing `_refused_head` runs, in order: the first fails where git
#: finds no repository, the second where the repository's branch has no
#: commit.
_HEAD_PROBES = (("rev-parse", "--git-dir"), ("rev-parse", "--verify", "-q", "HEAD"))


def _refused_head(cwd: Path) -> HistoryUnread | None:
    """Why ``git log HEAD`` exited non-zero at `cwd`, told by the exit of
    plumbing run from the same directory under the same environment, so
    the variables that move git's discovery apply to it as they did to the
    log: None where ``git rev-parse --git-dir`` fails (no repository) and
    git's walk from `cwd` passes no repository it may not read, or stops
    at an entry membership refuses (`_unread_entry`), ``ERROR`` where it
    passes one or stops at a ``.git`` file otherwise, since git then
    refused a repository there, as the keyed read names its refusal
    (ENG-220's review, round F); where ``git rev-parse --verify -q HEAD``
    fails, None on a branch with no commit, no history that applies, and
    ``ERROR`` on one whose ref git cannot read (`_unborn_or_broken`);
    ``ERROR`` where both answer, since git then failed on a head the
    repository holds; and a probe that met its timeout, ``TIMEOUT``
    (`origin_history.unanswered`). Run only on that failure, so a read
    that answers runs neither; the log's failure is already counted on
    the calling thread."""
    for probe in _HEAD_PROBES:
        before = origin_history.git_counts()
        if origin._git(cwd, *probe, timeout=5.0) is not None:
            continue
        if origin_history.git_counts() != before:
            return origin_history.unanswered(before, named=False)
        if probe is _HEAD_PROBES[0]:
            return _unread_entry(cwd)
        return _unborn_or_broken(cwd)
    return HistoryUnread.ERROR


def _unborn_or_broken(cwd: Path) -> HistoryUnread | None:
    """Why ``git rev-parse --verify -q HEAD`` failed in a repository git
    reads: None where HEAD names a branch with no commit; ``ERROR`` where
    the ref HEAD names exists and git cannot resolve it (a loose file of
    garbage or NULs, a ``packed-refs`` line git dies on or its search
    steps past: ENG-220's review, round F, and final review, finding 1),
    or HEAD is detached at no hash git reads. The files settle it where
    they read the ref or find its line in a ``packed-refs`` they decline
    (`githead.head_unborn`); where they do not (a reftable repository, a
    declined ``packed-refs`` with no line for it) plumbing does: ``git
    symbolic-ref -q HEAD`` names the branch and ``git show-ref --exists``
    exits 2 where it does not exist, save where the files hold a
    ``packed-refs`` line naming it (`githead.packed_names`), for which
    git's search answers exit 2 as well. Any other answer is ``ERROR``, a
    call that met its timeout ``TIMEOUT`` and one that found no binary
    None (`origin_history.unanswered`)."""
    gd = githead.gitdir_at(cwd) or githead.locate(cwd)
    unborn = None if gd is None else githead.head_unborn(gd)
    if unborn is not None:
        return None if unborn else HistoryUnread.ERROR
    before = origin_history.git_counts()
    name = origin._git(cwd, "symbolic-ref", "-q", "HEAD", timeout=5.0)
    if name is None or not name.startswith("refs/"):
        return origin_history.unanswered(before, named=True)
    shown = origin._git_result(cwd, "show-ref", "--exists", name, timeout=5.0)
    if shown is None:
        return origin_history.unanswered(before, named=True)
    if shown.returncode != 2:
        return HistoryUnread.ERROR
    # git's search can step past the ref's own line in a ``packed-refs``
    # it reads otherwise, and exits 2 where the line stands for the ref.
    named = gd is not None and githead.packed_names(gd, name)
    return HistoryUnread.ERROR if named else None


def _unread_entry(cwd: Path) -> HistoryUnread | None:
    """Why no history was read at `cwd`, where no reader reads a repository
    there: ``ERROR`` where git's walk from `cwd` passes a directory it
    would have stopped at but for what it may not read, or stops at a
    ``.git`` file (`githead.entry_above`), which may hold a repository
    nothing reads, as behind an ``objects`` directory no one may search
    (ENG-220's review, rounds F and G); None where it passes none, a
    ``HEAD`` alone or an empty ``.git`` directory among them, and
    where git's walk from `cwd` stops at an entry membership refuses
    (`gitfile_rule.discovery_refusal`, ENG-158), which leaves the folder
    one outside any repository along every path the origin family reads."""
    if gitfile_rule.discovery_refusal(os.fspath(cwd)) is not None:
        return None
    return HistoryUnread.ERROR if githead.entry_above(cwd) else None


def _extended_author_timestamps(
    cwd: Path, base_head: str, base: list[datetime], head: str, cut: frozenset[str]
) -> list[datetime] | HistoryUnread | None:
    """`base`, the ascending author dates of `base_head`'s history, with the
    dates of the commits `head` reaches and `base_head` does not, where
    `base_head` is an ancestor of `head`: ``git log --boundary
    <base_head>..<head>``, whose boundary holds `base_head` exactly then
    (`origin._walk_reachable` reads the same mark). The list is new and shares
    `base`'s dates. `base_head`'s history reaches none of the shallow file's
    `cut` (`origin_history.base_for`), so `head`'s reaches one only where
    the range lists it, as it lists a cut a merge brings in: then
    ``SHALLOW``, counted as a failure, as the whole read answers, and a
    head that adds no cut is extended where it was read whole (ENG-220's
    review, round F). None, and the caller reads the history whole, where
    `base_head` is no ancestor (a switched or rewritten branch, a head that
    moved back, an object gone) or git did not answer; a non-zero exit is
    not counted as a failure, since the whole read follows it."""
    result = origin._git_result(
        cwd,
        "log",
        "--boundary",
        "--format=%m%H %at",
        f"{base_head}..{head}",
        timeout=5.0,
    )
    if result is None or result.returncode != 0:
        return None
    lines = origin_history.read_lines(result.stdout, _marked_seconds)
    if lines is None or (False, base_head, 0) not in lines:
        return None
    added = [(sha, value) for listed, sha, value in lines if listed]
    if not cut.isdisjoint(sha for sha, _ in added):
        _git_runner.note_failed()
        return HistoryUnread.SHALLOW
    return sorted(base + origin_history.author_dates([value for _, value in added]))


def _marked_seconds(line: str) -> tuple[bool, str, int] | None:
    """A ``%m%H %at`` line as (listed, hash, seconds): `listed` False for a
    boundary commit, whose seconds read 0. None for any other line."""
    sha, _, stamp = line[1:].partition(" ")
    value = origin_history.seconds(stamp)
    if line[:1] not in "-<>" or not origin.is_full_commit_sha(sha) or value is None:
        return None
    return line[0] != "-", sha, value if line[0] != "-" else 0


def commit_author_timestamps_touching_pathspecs(
    cwd: Path | None,
    pathspecs: list[str],
    *,
    toplevel: Path | None = None,
) -> list[datetime] | None:
    """Author timestamps of the commits touching any of `pathspecs`.

    `pathspecs` must already be repo-root-relative forward-slash specs (the
    output of `origin.resolve_repo_pathspecs`). The path-filtered analogue of
    `commit_author_timestamps`: the same author dates (``%aI`` here, ``%at``
    there: one instant either way), so a caller can `bisect_right` the
    result against a `since` instant and get a count that lives in the SAME
    date space as the unfiltered bisect — no
    committer-vs-author mismatch. That is the whole point: `commits_touching_
    pathspecs` counts on COMMITTER date (git's ``--since``), which a rebase
    can inflate past the author-date truth, forcing a downstream clamp;
    reading author dates here removes the mismatch at the source.

    Three-valued return — the distinction the claim-anchored drift policy
    (`verify.resolve_commit_drift_count`) needs:

    - ``None`` — git could not answer (cwd is None, empty pathspecs, not a
      repo, non-zero git exit). The caller keeps its conservative default
      (never under-count on infrastructure failure).
    - ``[]`` — git answered cleanly and NO commit reachable from HEAD ever
      touched any spec. Every pathspec is a PHANTOM: a citation
      `origin.resolve_repo_pathspecs` mapped LEXICALLY (no existence check) onto a
      repo-relative path no commit touched. No separate existence probe is
      needed: an empty author-date log IS the "no spec ever appeared in
      history" answer, so one git call answers both questions.
    - ``[ts, ...]`` — author timestamps (timezone-aware) of the touching
      commits, sorted ASCENDING and ready to `bisect_right` — same
      contract as `commit_author_timestamps`, for the same reason. A
      since-DELETED cited file still lands here: its removal is itself a
      commit that touched it, so it stays in the log — the real-not-phantom
      signal a drift anchor needs.

    The clean-empty (``[]``) vs failure (``None``) split rides on
    `origin._git(empty_ok=True)`: ``git log -- <specs>`` exits 0 with empty stdout
    for a phantom and non-zero when git itself can't run; the default
    ``out or None`` collapse would merge those two, so ``empty_ok`` keeps
    them apart. Non-empty stdout that parses to nothing (a git oddity, not a
    clean phantom) also degrades to ``None`` — stay conservative rather than
    mint a not-applicable exemption from garbage output.
    """
    if cwd is None or not pathspecs:
        return None
    if toplevel is None:
        toplevel = origin.repo_toplevel(cwd)
        if toplevel is None:
            return None
    raw = _touching_log(toplevel, "%aI", pathspecs)
    if raw is None:
        # Git could not answer (non-zero exit, missing binary, timeout).
        return None
    if not raw:
        # Clean exit, empty log — no commit touches any spec (phantom).
        return []
    out = origin_history.read_lines(raw, origin_history.instant)
    if not out:
        # A line that reads as no date declines the read (dropped, it
        # shrank the count, ENG-220's review), and so does non-empty stdout
        # that parsed to nothing: `None` (not `[]`) keeps it from minting
        # the phantom not-applicable exemption — the clean-empty case
        # already returned `[]` above, off `empty_ok`.
        _git_runner.note_failed()
        return None
    out.sort(key=_instant)
    return out


def commit_author_sha_pairs_touching_pathspecs(
    cwd: Path | None,
    pathspecs: list[str],
    *,
    toplevel: Path | None = None,
) -> list[tuple[datetime, str]] | None:
    """`(author_instant, sha)` pairs for the commits touching `pathspecs`.

    The sibling of `commit_author_timestamps_touching_pathspecs` that
    keeps the commit identity beside each timestamp — the claim-level
    drift narrowing needs the POST-`since` SHAs by name (to fetch their
    patches and to union implicated commits into an exact distinct
    count), not just how many there are. Same three-valued contract and
    the same author-date space as the timestamps sibling: ``None`` when
    git could not answer, ``[]`` for the clean phantom (no commit ever
    touched any spec), pairs sorted ASCENDING on the instant otherwise —
    so a `bisect` against a `since` instant lands on the same boundary
    the count surfaces use.

    A separate function rather than a flag on the sibling because the
    return types differ and every existing caller of the sibling wants
    bare timestamps; threading a mode flag through the three-valued
    contract is how the None/[]-collapse bug gets reintroduced.
    """
    if cwd is None or not pathspecs:
        return None
    if toplevel is None:
        toplevel = origin.repo_toplevel(cwd)
        if toplevel is None:
            return None
    raw = _touching_log(toplevel, "%aI %H", pathspecs)
    if raw is None:
        return None
    if not raw:
        return []
    out = origin_history.read_lines(raw, _dated_sha)
    if not out:
        # Mirrors the timestamps sibling: a line that reads as no date and
        # hash declines the read, and non-empty stdout that parsed to
        # nothing is a git oddity, not a clean phantom.
        _git_runner.note_failed()
        return None
    out.sort(key=lambda pair: _instant(pair[0]))
    return out


def paths_in_history(
    cwd: Path | None, pathspecs: list[str], *, toplevel: Path | None = None
) -> bool | None:
    """Whether HEAD's visible history touches a pathspec; None if unread.

    Classification needs no complete-history count. A shallow boundary
    lists every path in its tree, so a real, untouched anchor can still
    be distinguished from a phantom when the stamp's range is held.
    Dates are irrelevant to this question, including malformed dates.
    The shallow-file hold still declines a log whose cuts moved during
    the read. Counting readers retain the stricter complete-history gate.
    """
    if cwd is None or not pathspecs:
        return None
    if toplevel is None:
        toplevel = origin.repo_toplevel(cwd)
        if toplevel is None:
            return None
    raw = _touching_log(toplevel, "%H", pathspecs, require_complete=False)
    if raw is None:
        return None
    if not raw:
        return False
    if not all(origin.is_full_commit_sha(line) for line in raw.splitlines()):
        _git_runner.note_failed()
        return None
    return True


def _touching_log(
    toplevel: Path,
    form: str,
    pathspecs: list[str],
    *,
    require_complete: bool = True,
) -> str | None:
    """``git log --format=<form> HEAD -- <pathspecs>``, the head and the
    pathspecs on git's standard input (`origin_history.log_stdin`), so no
    count of them passes a command line's bound; None where git did not
    answer or a pathspec cannot be put on a line.

    Git stops the log at every commit the ``shallow`` file lists, without
    a word, so a log read under a cut the head's whole history was not
    checked against may list fewer commits than that history holds, and a
    count narrowed on it reads low: a ``fetch --depth`` landing after the
    whole history was read narrowed a drifted memory to 0, kept as fresh
    (ENG-233's review). The cuts are read before the log and after it, and
    the log is declined (None, counted as a failure, so nothing is kept
    and the caller keeps its unnarrowed count) where they moved meanwhile,
    where the file cannot be read, and where it lists a commit the head's
    whole history, kept for its root and head, was not checked against
    (`origin_history.checked_cut`), or none is kept for them. The file's
    stamp, or its directory's where it is not there, is held across the
    log: a ``fetch --depth`` and an ``--unshallow`` both inside it leave
    the cuts as they were and git may have stopped at the first, so the
    narrowing is declined too. A directory change can also come from an
    unrelated entry (a ``git status`` taking the index's lock), but it
    cannot prove that the log saw no temporary cut: the caller keeps its
    conservative unnarrowed count for this call and caches nothing.
    `paths_in_history` alone sets `require_complete=False`: it classifies
    paths without counting commits, so needs no cached complete history.
    Where the files read no repository at `toplevel` nothing is checked,
    as before."""
    stdin = origin_history.log_stdin(["HEAD"], pathspecs)
    if stdin is None:
        return None
    shallow = origin_history.shallow_file(toplevel)
    cut = None if shallow is None else origin_history.cuts_in(shallow)
    held = _hold_shallow(toplevel)
    out = origin._git(
        toplevel,
        *origin_history.AS_STORED_PATHS,
        "log",
        f"--format={form}",
        "--stdin",
        timeout=5.0,
        empty_ok=True,
        stdin=stdin,
    )
    if out is None or shallow is None:
        return out
    if (
        cut is None
        or origin_history.cuts_in(shallow) != cut
        or (
            require_complete
            and cut
            and not cut <= (_checked_cut(toplevel) or frozenset())
        )
    ):
        _git_runner.note_failed()
        return None
    if held is None or not origin.directories_held(held):
        _git_runner.note_failed()
        return None
    return out


def _checked_cut(toplevel: Path) -> frozenset[str] | None:
    """The cuts the whole history kept for the root and the head at
    `toplevel` was checked against (`origin_history.checked_cut`), the
    pair read as `commit_author_timestamps` reads it; None where none is
    kept."""
    located = origin.toplevel_and_head_from_files(toplevel)
    key = (
        origin_history.located_key(toplevel)
        if located is None
        else (str(located[0]), located[1])
    )
    return None if key is None else origin_history.checked_cut(key)


def _dated_sha(line: str) -> tuple[datetime, str] | None:
    stamp, _, sha = line.strip().partition(" ")
    when = origin_history.instant(stamp)
    return None if when is None or not origin.is_full_commit_sha(sha) else (when, sha)


__all__ = [
    "commit_author_sha_pairs_touching_pathspecs",
    "commit_author_timestamps",
    "commit_author_timestamps_touching_pathspecs",
    "paths_in_history",
]
