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

from datetime import datetime
from pathlib import Path

from . import _git as _git_runner
from . import origin, origin_history


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
) -> list[datetime] | None:
    """All author timestamps from the HEAD history of `cwd`'s repo.

    Returns a list of timezone-aware datetimes, or None on any failure
    (cwd is None, git not on PATH, not a repo, no commits, parse error
    on every line). An empty list is theoretically possible but almost
    never happens — `git log` on a repo with no commits exits non-zero
    and we surface that as None. Lines that fail to parse are skipped
    individually rather than poisoning the whole result.

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
    minute: a call meanwhile returns None without a process and counts the
    failure on the calling thread (`origin.failed_git_calls`), as the read did, so
    a caller that keeps a value only when no git call failed keeps none
    from it. Where the files git's discovery reads do not settle the pair
    and the caller passed none (off POSIX, in another account's
    repository), the root and the head are those `githead.locate` reads
    (`origin_history.located_key`), kept and extended alike; only where
    those do not settle it either does the log run from HEAD, uncached. A
    history longer than the memo's budget (`origin_history.remember`) is
    returned, and remembered as a failed read is, so the calls of the next
    minute answer None without reading it again. A hit returns the stored
    list itself: every caller only bisects it. Replace refs and grafts
    move nothing (`origin_history.AS_STORED_ENV`). A shallow repository's
    history, cut where the dates behind the cut are not read
    (`origin_history.cuts`), answers None, as a failed read does; a
    history kept before the cut was written is still returned until the
    head moves.
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
        if origin_history.cuts(cwd, walk=True) != frozenset():
            _git_runner.note_failed()
            return None
        return _read_author_timestamps(cwd, "HEAD")
    guard = origin_history.history_guard(Path(key[0]))
    stored, failed = origin_history.recall(key, guard)
    if stored is not None:
        return stored
    if not failed and origin_history.cuts(Path(key[0])) != frozenset():
        # A shallow file's cut: the commits behind it, whose dates git
        # does not read, may be the window's (ENG-220's review).
        origin_history.remember_failure(key, guard)
        failed = True
    if failed:
        _git_runner.note_failed()
        return None
    base = None if guard is None else origin_history.base_for(key[0], guard)
    dates = None
    if base is not None:
        dates = _extended_author_timestamps(cwd, base[0], base[1], key[1])
    if dates is None:
        dates = _read_author_timestamps(cwd, key[1])
    if dates is None or not origin_history.remember(key, dates, guard):
        origin_history.remember_failure(key, guard)
    return dates


def _read_author_timestamps(cwd: Path, revision: str) -> list[datetime] | None:
    """`commit_author_timestamps`' read: the author dates of every commit
    reachable from `revision`, ascending, or None."""
    # timeout=5.0, not the 1.0 default: the default is calibrated for
    # write-path origin capture, where origin is nice-to-have and a
    # hanging git must never stall a memory_write. This log and its two
    # path-filtered siblings below are READ legs — the drift verdicts
    # search/show/health surface ride on them, and at 1.0s a cold git
    # on a slow host (observed: the windows-latest CI runner) times out,
    # collapsing a real count into the omitted/conservative branch. Same
    # ceiling `commit_patch_stream` already runs at.
    raw = origin._git(cwd, "log", "--format=%at", revision, timeout=5.0)
    values = (
        None if raw is None else origin_history.read_lines(raw, origin_history.seconds)
    )
    if not values:
        if raw is not None:
            # A line that reads as no date declines the read.
            _git_runner.note_failed()
        return None
    return origin_history.author_dates(values)


def _extended_author_timestamps(
    cwd: Path, base_head: str, base: list[datetime], head: str
) -> list[datetime] | None:
    """`base`, the ascending author dates of `base_head`'s history, with the
    dates of the commits `head` reaches and `base_head` does not, where
    `base_head` is an ancestor of `head`: ``git log --boundary
    <base_head>..<head>``, whose boundary holds `base_head` exactly then
    (`origin._walk_reachable` reads the same mark). The list is new and shares
    `base`'s dates. None, and the caller reads the history whole, where
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
    added = [value for listed, _, value in lines if listed]
    return sorted(base + origin_history.author_dates(added))


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


def _touching_log(toplevel: Path, form: str, pathspecs: list[str]) -> str | None:
    """``git log --format=<form> HEAD -- <pathspecs>``, the head and the
    pathspecs on git's standard input (`origin_history.log_stdin`), so no
    count of them passes a command line's bound; None where git did not
    answer or a pathspec cannot be put on a line."""
    stdin = origin_history.log_stdin(["HEAD"], pathspecs)
    if stdin is None:
        return None
    return origin._git(
        toplevel,
        *origin_history.AS_STORED_PATHS,
        "log",
        f"--format={form}",
        "--stdin",
        timeout=5.0,
        empty_ok=True,
        stdin=stdin,
    )


def _dated_sha(line: str) -> tuple[datetime, str] | None:
    stamp, _, sha = line.strip().partition(" ")
    when = origin_history.instant(stamp)
    return None if when is None or not origin.is_full_commit_sha(sha) else (when, sha)


__all__ = [
    "commit_author_sha_pairs_touching_pathspecs",
    "commit_author_timestamps",
    "commit_author_timestamps_touching_pathspecs",
]
