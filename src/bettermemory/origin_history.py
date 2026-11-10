"""How `origin` reads a repository's history, and the whole-history author
dates `origin_dates.commit_author_timestamps` keeps.

Every git `origin` runs reads the history as its objects hold it, whatever
the repository sets (`AS_STORED_ENV`, `AS_STORED_CONFIG`, `AS_STORED_DIFF`,
`AS_STORED_PATHS`), and no count is read behind a shallow file's cut
(`cuts`). A history that applies and was not read says why
(`HistoryUnread`), apart from the None that says none applies.

`origin_dates` reads from git the author date of every commit a head reaches
(``git log --format=%at <head>``), or of the commits a head reaches that a
head already read does not (``<old>..<new>``); this module keeps what was
read, per repository root and head, with the stamps of the files that
change which commits a head reaches without moving it (`history_guard`),
and remembers for a while each read that failed. The reachable walk
`origin.commits_since_anchor` returns is a `ReachableWalk`, defined here.

This module lives apart from `origin.py` to keep each module under the
package's 2,000-line bound (ENG-96). It imports the standard library,
`_caches`, `_git` and `githead`; `origin` and `origin_dates` import it, never the
reverse.
"""

from __future__ import annotations

import enum
from bisect import bisect_left
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from typing import TypeAlias, TypeVar

from . import _caches, gitfile_rule, githead
from . import _git as _git_runner

# What every git `origin` runs is set to, so that it reads the history as
# its objects hold it and nothing the repository sets can make a changed
# claim read lower or fresh (ENG-220's review). The repository's config,
# config.worktree, the files they include, its .gitmodules and its
# attribute files are its own, and a hostile `.git` copied in an archive
# sets them. Each pin is git's default, or the reading the walk and the
# split already made:
#
# - Replace refs and a grafts file rewrite which commits a head reaches and
#   what each changed, under a head and stamps that stay (a replace ref
#   under a reftable ref store). The variables set them aside; the ``-c``
#   setting is read after the repository's config, so it holds where a git
#   before 2.42 lets ``core.useReplaceRefs=true`` override the variable,
#   and the variable on a git before 2.19, which has no such setting.
# - A commit-graph file names each commit's parents, root tree and
#   changed-path filter in place of the object's, and git believes it:
#   forged, it hid the commit that changed a claim from the windows.
# - ``log.showRoot=false`` hides a root commit's creation diff: an orphan
#   root merged into the window changed a claim and the walk read 0.
# - ``log.follow=true`` makes a log over one path follow it across
#   renames, which moves the window off the paths as stored.
# - ``diff.renames``, on by default in a patch, pairs a claimed path's
#   deletion with a claimed path's creation as a rename that prints no
#   ``/dev/null`` header, so neither claim fired; the walk and the split
#   read renames as a deletion and a creation already (``--no-renames``).
# - A diff of a file its attributes (``-diff``, ``binary``, a driver's
#   ``binary``) or ``core.bigFileThreshold`` mark binary prints "Binary
#   files differ", no header and no hunk, so beside an indexed file its
#   claims read untouched: ``--text`` on every diff.
# - ``diff.ignoreSubmodules`` and ``submodule.<name>.ignore`` (in the
#   config or the .gitmodules) drop a gitlink's change from a diff, its
#   deletion included: ``--ignore-submodules=none`` on every diff, which
#   only the command line puts above the .gitmodules.
# - ``i18n.logOutputEncoding`` (or ``i18n.commitEncoding``, which stands
#   in for it) re-encodes every line a format prints: in UTF-7 the walk's
#   and the patch's ``\x01`` mark and the ``+`` of an author date east of
#   UTC are spelled otherwise, so the walk declined and the dates that
#   carried one were dropped, and a claim counted 0 where it changed once.
#   Pinned to UTF-8, a format prints what the package reads; a commit's
#   own ``encoding`` header re-encodes only its message, which no format
#   prints.
# - ``diff.orderFile`` naming no file fails every diff, so the walk, the
#   listing and the patch declined: ``-O`` with the null device, an empty
#   order, outranks it. A file it names only orders a commit's files,
#   which the readers take in any order.
#
# The settings the review found to move no count, or only up (the hit
# then counts more, declines or is omitted), are listed in the rc19
# CHANGELOG entry for ENG-220.
AS_STORED_ENV = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull}
AS_STORED_CONFIG = (
    "-c",
    "core.useReplaceRefs=false",
    "-c",
    "core.commitGraph=false",
    "-c",
    "log.showRoot=true",
    "-c",
    "log.follow=false",
    "-c",
    "diff.renames=false",
    "-c",
    "i18n.logOutputEncoding=UTF-8",
)
#: On every log that prints a diff (the walk's ``--name-only``, the split's
#: ``--raw``, the patch stream's ``-p``), after the subcommand.
AS_STORED_DIFF = ("--text", "--ignore-submodules=none", f"-O{os.devnull}")
#: On every log given a claim's or an anchor's paths, before the
#: subcommand. On macOS ``core.precomposeUnicode=true`` rewrites each
#: argument git is given in decomposed Unicode to its composed form, so a
#: pathspec spelled as a tree stores a decomposed path (``cafe`` and a
#: combining accent) matched nothing: the windows and the patch missed the
#: file, and a deleted claimed file read fresh. Pinned off, a pathspec
#: matches the path its bytes spell. It is pinned on these logs alone:
#: the setting also spells the working tree's paths, which the capture
#: reads as the user's git does.
AS_STORED_PATHS = ("-c", "core.precomposeUnicode=false")

#: ``(root, head)``: the repository root as the caller resolved it and the
#: full hash of the head the dates were read for.
Key: TypeAlias = tuple[str, str]

#: The stamps `history_guard` reads, and the variables it carries.
Guard: TypeAlias = tuple[object, ...]

# The most (root, head) entries kept, and the most failures remembered. A
# daemon reads the heads its callers stand at, and an entry for a head that
# moved on is the base the next head extends (`base_for`), so a few roots
# with a few heads each fit. The memory is bounded by `_TIMESTAMPS_MEMO_SLOTS`,
# not by this count.
_TIMESTAMPS_MEMO_CAP = 16

# The most dates the entries hold together, counted per list slot. Measured
# on a synthetic linear history of 500,000 commits (Python 3.11, macOS): a
# list read whole holds 56 bytes per commit (a datetime and its slot, all of
# one tzinfo), and a list extended from another holds 8 bytes per commit it
# shares. Two million slots bound the memo at about 112 MB where no list
# shares a date, and admit any one history the read's 5 s ceiling admits
# there (about 1.0 to 1.2 million commits at the 4.2 to 5.0 us a commit
# measured); a list longer than the bound is returned and not kept, and its
# key is remembered as a failed read's is (`remember`).
_TIMESTAMPS_MEMO_SLOTS = 2_000_000


class HistoryUnread(enum.Enum):
    """Why a repository's history supplies no usable verification count:
    the row it would count reads neither fresh nor
    not applicable. The per-memory surfaces flag it and stop it reading
    fresh, and the health rollups count it under `unread`
    (`verify_basis.CommitDriftUnread` is this enum). None, apart from
    these, says no history applies: no repository, no git on PATH, no
    commit.

    - ``CALL_BUDGET``: this call exhausted its shared Git-read deadline.
      No clean count is supplied, and no failure memo retains the cutoff.
    - ``TIMEOUT``: git met its 5 s bound, on the dates' log, or on the walk
      from a stamp's head and the listing of its commits both
      (`origin.commits_since_anchor`, `unwalked`).
    - ``OVER_BUDGET``: the history is longer than the memo keeps
      (`_TIMESTAMPS_MEMO_SLOTS` dates), so it is not counted, the read that
      met it as much as the calls of the next minute (`remember`).
    - ``SHALLOW``: the clone is shallow, its ``shallow`` file listing a
      commit the head's history reaches (or unreadable, `cuts`), and git
      reads no commit behind one; a cut the history never reaches, as a
      branch fetched with ``--depth`` beside a full one, reads nothing,
      and a head that moves on from one whose history reached none, under
      the same file, reaches one only where a commit it adds is one
      (`base_for`). No window on the dates can be shown to stop short of
      the cut: a commit there authored before the stamp hides parents
      authored after it. A walk from a stamp's head whose range holds no
      cut is read in full, and counts.
    - ``ERROR``: git ran and failed on a head named by its hash (a non-zero
      exit), printed a line its format does not print (`read_lines`), or
      could not be spawned for another reason than a binary or a directory
      that is not there; or it failed on the walk from a stamp's head and
      the listing of its commits both, where the repository holds that
      head (`origin.commits_since_anchor`, `unwalked`).
    - ``UNCONFIRMED_CUT``: no usable verification commit establishes the
      range, and an author-date zero cannot exclude commits authored
      before verification but integrated afterwards."""

    TIMEOUT = "timeout"
    OVER_BUDGET = "over_budget"
    SHALLOW = "shallow"
    ERROR = "error"
    UNCONFIRMED_CUT = "unconfirmed_cut"
    CALL_BUDGET = "call_budget"


def git_counts() -> tuple[int, int]:
    """The calling thread's git timeouts and the calls that found no binary
    or no directory (`_git.timed_out_calls`, `_git.missing_calls`), which
    `unanswered` reads a read's failure off."""
    return _git_runner.timed_out_calls(), _git_runner.missing_calls()


def unanswered(before: tuple[int, int], *, named: bool) -> HistoryUnread | None:
    """Why a history read that came back with no answer since `before`
    (`git_counts`) was not read: ``TIMEOUT`` where git met its bound; None
    where the binary or the directory is not there, so no history applies;
    otherwise ``ERROR`` for a head `named` by its hash, which a repository
    holds, and None for ``HEAD``, where git's refusal also answers a folder
    that is no repository and a branch with no commit."""
    timed_out, missing = git_counts()
    if timed_out != before[0]:
        return HistoryUnread.TIMEOUT
    if missing != before[1] or not named:
        return None
    return HistoryUnread.ERROR


def note_unread(why: HistoryUnread) -> None:
    """Count, on the calling thread, an unread history served without a
    process (`recall`, a pass's kept read), as the read that met it was
    counted: a timeout as a failure and a timeout (`_git.note_timed_out`),
    any other cause as a failure, so a caller that keeps a value only when
    no git call failed keeps none from it, save a timeout's for a while."""
    if why is HistoryUnread.TIMEOUT:
        _git_runner.note_timed_out()
    else:
        _git_runner.note_failed()


# How long a failed read is remembered, in seconds of `time.monotonic`. A
# read fails at its 5 s ceiling on a history too long for it, and fails
# again for the same head. Remembered for a minute, it runs at most once a
# minute for a caller that asks on every row of every search, which then
# spends at most 5 s a minute on it (a twelfth of its time) where it spent
# up to 5 s per row; a failure that clears on its own (a lock, an object
# briefly unreadable) is read again within the minute. A failure is kept with
# the `history_guard` read before it and answers only under an equal one, so
# a shallow clone deepened (``fetch --unshallow``, ``--deepen``), which
# rewrites or removes the ``shallow`` file, is read at once (ENG-220's
# review).
_FAILURE_SECONDS = 60.0


@dataclass(frozen=True)
class _Entry:
    """The ascending author dates of a head's history, the `history_guard`
    read before they were, None where it could not be, and the commits the
    ``shallow`` file listed then (`cuts`), none of which the history
    reaches: the read named each commit it listed against them, or listed
    none where the file is not there."""

    dates: list[datetime]
    guard: Guard | None
    cut: frozenset[str]


# The entries, oldest first; the failures, by when each was met. The lock
# makes each look-up-and-touch and each insert-and-evict atomic; git runs
# outside it. Registered with `_caches`, which empties both before each
# test.
_TIMESTAMPS_MEMO: OrderedDict[Key, _Entry] = OrderedDict()
_TIMESTAMPS_FAILED: OrderedDict[Key, tuple[float, Guard | None, HistoryUnread]] = (
    OrderedDict()
)
_TIMESTAMPS_MEMO_LOCK = threading.Lock()


@_caches.register
def _clear_timestamps_memo() -> None:
    with _TIMESTAMPS_MEMO_LOCK:
        _TIMESTAMPS_MEMO.clear()
        _TIMESTAMPS_FAILED.clear()


def recall(
    key: Key, guard: Guard | None, cut: frozenset[str] | None
) -> tuple[list[datetime] | None, HistoryUnread | None]:
    """``(dates, failure)`` for `key`: the dates kept for it (the stored
    list itself), or None; and why a read for it failed less than
    `_FAILURE_SECONDS` ago under `guard`, where none is kept, or None.

    The dates are returned only while `cut`, the commits the ``shallow``
    file lists now (`cuts`), are among those they were checked against:
    their history reaches none of them, so git reads it whole under them.
    A shallow file that lists another commit (a ``fetch --depth`` since
    they were kept), or that cannot be read, may cut the history, and the
    caller reads it again under the file as it is (ENG-233's review: the
    path-filtered log a row narrows on is read under it, and narrowed a
    count against a history the cut would have hidden). A clone
    unshallowed since lists none, and keeps them."""
    with _TIMESTAMPS_MEMO_LOCK:
        entry = _TIMESTAMPS_MEMO.get(key)
        if entry is not None and cut is not None and cut <= entry.cut:
            _TIMESTAMPS_MEMO.move_to_end(key)
            return entry.dates, None
        failed = _TIMESTAMPS_FAILED.get(key)
        if failed is not None:
            failed_at, failed_under, why = failed
            if failed_under == guard and (
                0.0 <= time.monotonic() - failed_at < _FAILURE_SECONDS
            ):
                return None, why
            del _TIMESTAMPS_FAILED[key]
    return None, None


def checked_cut(key: Key) -> frozenset[str] | None:
    """The commits the ``shallow`` file listed when the history kept for
    `key` was checked against it, none of which that history reaches; None
    where none is kept. Read without touching the entry's place."""
    with _TIMESTAMPS_MEMO_LOCK:
        entry = _TIMESTAMPS_MEMO.get(key)
    return None if entry is None else entry.cut


def base_for(
    root: str, guard: Guard, cut: frozenset[str]
) -> tuple[str, list[datetime], frozenset[str]] | None:
    """The most recently used entry under `root` read under `guard` and
    checked against `cut`, as ``(head, dates, cut)``: the history a head of
    the same repository extends when that head is its ancestor. Its
    history reaches none of `cut`, so the head's reaches one only where
    the commits it adds list one; an entry checked against other cuts, a
    shallow file that changed since, is none (ENG-220's review, round F).
    The cut returned is the entry's own, equal to `cut`, which the caller
    keeps in its place so the entries of one shallow file hold it once."""
    with _TIMESTAMPS_MEMO_LOCK:
        for (entry_root, head), entry in reversed(_TIMESTAMPS_MEMO.items()):
            if entry_root == root and entry.guard == guard and entry.cut == cut:
                return head, entry.dates, entry.cut
    return None


def remember(
    key: Key, dates: list[datetime], guard: Guard | None, cut: frozenset[str]
) -> bool:
    """Keep `dates` for `key`, read under `guard` and checked against `cut`
    (`_Entry`), and evict the oldest entries past `_TIMESTAMPS_MEMO_CAP` or
    `_TIMESTAMPS_MEMO_SLOTS`; True. A list longer than the slots is not
    kept, and False says so: the caller remembers its key as a failed
    read's (`remember_failure`, with `HistoryUnread.OVER_BUDGET`), so the
    calls of the next minute answer that where each would read the history
    whole again (review Ob, D3)."""
    if len(dates) > _TIMESTAMPS_MEMO_SLOTS:
        return False
    with _TIMESTAMPS_MEMO_LOCK:
        _TIMESTAMPS_FAILED.pop(key, None)
        _TIMESTAMPS_MEMO[key] = _Entry(dates, guard, cut)
        _TIMESTAMPS_MEMO.move_to_end(key)
        held = sum(len(entry.dates) for entry in _TIMESTAMPS_MEMO.values())
        while len(_TIMESTAMPS_MEMO) > _TIMESTAMPS_MEMO_CAP or (
            held > _TIMESTAMPS_MEMO_SLOTS
        ):
            _, gone = _TIMESTAMPS_MEMO.popitem(last=False)
            held -= len(gone.dates)
    return True


def remember_failure(key: Key, guard: Guard | None, why: HistoryUnread) -> None:
    """Remember that a read for `key` failed, now, under `guard`, and why."""
    if why is HistoryUnread.CALL_BUDGET:
        return
    with _TIMESTAMPS_MEMO_LOCK:
        _TIMESTAMPS_FAILED[key] = (time.monotonic(), guard, why)
        _TIMESTAMPS_FAILED.move_to_end(key)
        while len(_TIMESTAMPS_FAILED) > _TIMESTAMPS_MEMO_CAP:
            _TIMESTAMPS_FAILED.popitem(last=False)


# The instant a ``%at`` author date counts its seconds from: a date is built
# from it by arithmetic, which holds for every value a datetime holds on every
# platform (``datetime.fromtimestamp`` refuses a negative one on Windows), and
# every date of a history shares its one tzinfo.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_LATEST = datetime.max.replace(tzinfo=timezone.utc)
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)
# One second beyond datetime's range is the existing latest-date stand-in.
# Compare decimal text against this natural bound before any int conversion.
_LAST_SECOND = (_LATEST - _EPOCH) // timedelta(seconds=1)
_LAST_SECOND_TEXT = str(_LAST_SECOND)


def seconds(text: str) -> int | None:
    """Decimal ``%at`` seconds, saturated past datetime's range, or None.

    The date reader already clamps an overflowing instant to the latest
    date. Apply its bound before converting arbitrary-length digits to
    int: Python's digit limit must neither raise nor change the count.
    """
    text = text.strip()
    if not text.isascii() or not text.isdigit():
        return None
    digits = text.lstrip("0") or "0"
    if (len(digits), digits) > (len(_LAST_SECOND_TEXT), _LAST_SECOND_TEXT):
        return _LAST_SECOND + 1
    return int(digits)


def instant(text: str) -> datetime | None:
    """The instant a ``%aI`` date spells, in UTC where it names no offset,
    or None for any other text."""
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


_Read = TypeVar("_Read")


def read_lines(raw: str, read: Callable[[str], _Read | None]) -> list[_Read] | None:
    """`read` of each record of `raw`, or None where any
    reads as None. Git prints every line of a log's format, so a line that
    reads as none (a date a re-encoding spelled otherwise, a mark it
    garbled) declines the whole read, never drops out of the count
    (ENG-220's review). The lines are split on line feeds alone."""
    out: list[_Read] = []
    lines = raw.split("\n")
    if lines[-1] == "":
        lines.pop()  # the final line feed terminates, rather than adds, a record
    for line in lines:
        value = read(line)
        if value is None:
            return None
        out.append(value)
    return out


def author_dates(values: list[int]) -> list[datetime]:
    """`values`, author dates in ``%at`` seconds, as ascending UTC
    datetimes; a value past what a datetime holds reads as the latest
    (or the earliest) one, so a commit is never dropped from a count."""
    return [author_instant(value) for value in sorted(values)]


def author_instant(value: int) -> datetime:
    """One UTC author instant, clamped as a whole-history read clamps it."""
    try:
        return _EPOCH + timedelta(seconds=value)
    except OverflowError:
        return _LATEST if value > 0 else _EARLIEST


def located_key(cwd: Path) -> Key | None:
    """The root and head `githead.locate` reads for `cwd`, the key where
    the files git's discovery reads do not settle the pair (off POSIX, in
    another account's repository): the log is run for that head, so what
    is kept under the key is that head's history, as where they do
    (review Ob, G2). None where it reads none, and under a variable that
    moves git's discovery (`githead.discovery_moved`), where git may read
    another repository than the one `locate` names."""
    if githead.discovery_moved():
        return None
    found = githead.locate(cwd)
    head = None if found is None else githead.head_sha(found)
    if found is None or head is None:
        return None
    return str(found.worktree_root), head


# The variables that change which commits a head reaches in git's eyes: the
# replacement refs and their namespace, the graft and shallow files. Since
# ENG-218 `_git` removes GIT_SHALLOW_FILE from every git it runs, and since
# ENG-220 `origin` sets the replace and graft ones on every call
# (`AS_STORED_ENV`), so none moves git's answer; they stay listed, so a
# change in one reads the history whole again where an extension would serve.
_HISTORY_ENV = (
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_GRAFT_FILE",
    "GIT_SHALLOW_FILE",
)


def history_guard(root: Path) -> Guard | None:
    """The files that change which commits a head reaches without moving
    the head, for the repository whose working tree `root` holds (`githead.
    gitdir_at`): the stamps of ``shallow``, ``info/grafts``, the
    ``refs/replace`` directory, ``packed-refs`` and ``config``
    (``core.useReplaceRefs``) in the common directory, and the variables
    that move them. A head's dates are extended from an earlier head's only
    where the two guards are equal, so a shallow clone deepened reads the
    history whole again. Replace refs and grafts move no answer since
    `origin` reads past them (`AS_STORED_ENV`); their stamps stay, and a
    change in one reads the history whole where an extension would serve.
    Where `githead.gitdir_at` declines (off POSIX, another
    account's repository) the files are those `githead.locate` reads at
    `root`, which are only stamped here. None, and no extension, where
    neither reads a repository at `root` or a stat fails for any reason
    but absence."""
    gd = githead.gitdir_at(root) or githead.locate(root, walk=False)
    if gd is None:
        return None
    common = gd.commondir
    try:
        stamps = tuple(
            githead.stamp(path)
            for path in (
                common / "shallow",
                common / "info" / "grafts",
                common / "refs" / "replace",
                common / "packed-refs",
                common / "config",
            )
        )
    except (OSError, ValueError):
        return None
    return (*stamps, *(os.environ.get(name) for name in _HISTORY_ENV))


#: The longest ``shallow`` file `cuts` reads: 400,000 commits.
_CUTS_MAX = 1 << 24


def cuts(start: Path, *, walk: bool = False) -> frozenset[str] | None:
    """The commits the ``shallow`` file of the repository at `start` lists,
    in its common directory, as `history_guard` finds it: git reads none of
    their parents, so the history behind them is not read, whether a clone
    left them there or the file was written by hand (ENG-220's review: a
    cut hid the commit that changed a claim, and the count read 0). Empty
    where there is no such file; None where the repository's files or the
    file cannot be read, or the file is longer than `_CUTS_MAX` bytes."""
    path = shallow_file(start, walk=walk)
    return None if path is None else cuts_in(path)


def shallow_file(start: Path, *, walk: bool = False) -> Path | None:
    """The ``shallow`` file of the repository at `start`, in its common
    directory, found as `cuts` finds it; None where the files read no
    repository there."""
    gd = githead.gitdir_at(start) or githead.locate(start, walk=walk)
    return None if gd is None else gd.commondir / "shallow"


def cuts_in(path: Path) -> frozenset[str] | None:
    """The commits the ``shallow`` file at `path` lists (`cuts`): empty
    where it is not there, None where it cannot be read or is longer than
    `_CUTS_MAX` bytes. Git reads the first full object name of each LF
    record and ignores its suffix, even more hex digits. Its width comes
    from the common config's object format, never from that suffix.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_CUTS_MAX + 1)
    except FileNotFoundError:
        return frozenset()
    except OSError:
        return None
    if len(raw) > _CUTS_MAX:
        return None
    if not raw:
        return frozenset()
    name = gitfile_rule.object_format_at(str(path.parent))
    if name is None:
        return None
    width = 64 if name == b"sha256" else 40
    records = raw.split(b"\n")
    if records[-1] == b"":
        records.pop()
    out: set[str] = set()
    for record in records:
        oid = record[:width].lower()
        if len(oid) != width or any(ch not in b"0123456789abcdef" for ch in oid):
            return None
        out.add(oid.decode("ascii"))
    return frozenset(out)


def log_stdin(revisions: Sequence[str], pathspecs: Sequence[str]) -> bytes | None:
    """What ``git log --stdin`` reads in place of its revisions and
    pathspecs on the command line: each revision on a line, a ``--`` line,
    then each pathspec on a line (git's ``read_revisions_from_stdin``).

    Windows holds a command line to 32,767 characters, which a patch
    stream's 256 hashes and a memory's claims in each spelling of their
    paths (64 of up to 1,024 characters, each in up to 64 spellings) pass,
    and process creation then fails; standard input has no such bound
    (ENG-220's review, round C). None where a line cannot carry an element
    as git reads it, and the caller reads that as git not answering: one
    holding a line break (git also takes a CR before the LF off a line), an
    empty one, which ends git's read, or a revision that starts with a
    dash, which git takes for an option there and dies on. Encoded as
    ``os.fsencode`` encodes an argument on POSIX; None for an element that
    encoding cannot carry, a lone surrogate outside the escaped bytes."""
    lines = (*revisions, "--", *pathspecs)
    if any(not line or "\n" in line or "\r" in line for line in lines):
        return None
    if any(revision.startswith("-") for revision in revisions):
        return None
    data = "".join(f"{line}\n" for line in lines)
    try:
        return data.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return None


@dataclass(frozen=True)
class ReachableWalk:
    """The commits reachable from `head` but not from `anchor` — the
    range ``anchor..HEAD`` — with the paths each one changed
    (`origin.commits_since_anchor`).

    Built only when `anchor` is an ancestor of `head` (or is `head`
    itself, an empty walk), so `commits` is exactly what has landed on
    top of the verified tree state: a branch authored before the stamp
    and merged after it is in here, which is what the author-date count
    cannot say. `touched` maps a repo-relative path to the commits in
    the range whose diff carried it. A combined merge diff contributes
    paths the merge changed against every parent, including edits made
    while committing the merge; changes carried unchanged from one
    parent remain represented by that branch's own commits (ENG-260).

    `listed` is False for a walk whose names were not read and whose
    commits were listed alone: it names no path (`touched` is empty), every
    commit reads as touching each (`shas_touching`), and claim drift keeps
    its whole length, never the author-date count (ENG-220's review, round
    C; round F for a names read git failed on, a tree it could not read).
    `timed_out` is True where the names met their timeout, False where git
    failed on them.
    """

    anchor: str
    head: str
    commits: tuple[str, ...]
    touched: Mapping[str, frozenset[str]]
    listed: bool = True
    timed_out: bool = False

    @cached_property
    def _sorted_paths(self) -> tuple[str, ...]:
        """One range index, shared by all of its file and directory questions."""
        return tuple(sorted(self.touched))

    def shas_touching(self, pathspecs: Sequence[str]) -> set[str]:
        """The commits in the range that changed a file matching any of
        `pathspecs` — repo-root-relative forward-slash specs, the output
        of `resolve_repo_pathspecs`, each a file or a directory prefix.
        A path can change between a file and a directory across the range,
        so both exact names and descendants contribute. The sorted path index
        finds descendants without scanning unrelated paths for each file.
        Every commit, for a walk that did not list its names."""
        if not self.listed:
            return set(self.commits)
        out: set[str] = set()
        for spec in pathspecs:
            exact = self.touched.get(spec)
            if exact is not None:
                out |= exact
            prefix = spec.rstrip("/") + "/"
            paths = self._sorted_paths
            for index in range(bisect_left(paths, prefix), len(paths)):
                path = paths[index]
                if not path.startswith(prefix):
                    break
                out |= self.touched[path]
        return out


#: What one pass over many memories remembers of each walk no memo keeps,
#: by (root, anchor, head) (`origin.commits_since_anchor`): the walk; for one
#: that came back None, whether a git process failed for it; why, for one
#: given up on (`unwalked`).
Walked: TypeAlias = dict[tuple[str, str, str], "ReachableWalk | bool | HistoryUnread"]


class _Unwalked(threading.local):
    """The walks on the current thread that `origin.commits_since_anchor`
    gave up on, and why the last was. Read through `unwalked` and
    `given_up_since`."""

    count = 0
    why = HistoryUnread.TIMEOUT


_UNWALKED = _Unwalked()


def unwalked() -> int:
    """How many reachable walks on the current thread were given up on
    (`origin.commits_since_anchor`), served from a pass's `Walked` too:
    their names and the listing of their commits alone met their timeout,
    or git failed on both from an anchor the repository holds. A caller
    that reads this before a walk and finds it moved after
    (`given_up_since`) omits the count and reads the row unread, and never
    reads the author dates in its place: a rebased or back-dated commit
    moves them (ENG-220's review, rounds C and G)."""
    return _UNWALKED.count


def given_up_since(before: int) -> HistoryUnread | None:
    """Why the walk run since `before` (`unwalked`) was given up on:
    ``TIMEOUT`` where its listing met its timeout, ``ERROR`` where git
    failed on it from an anchor the repository holds; None where none
    was."""
    return _UNWALKED.why if _UNWALKED.count != before else None


def note_unwalked(why: HistoryUnread) -> None:
    """Count, on the calling thread, a walk given up on, and why
    (`unwalked`)."""
    _UNWALKED.count += 1
    _UNWALKED.why = why


__all__ = [
    "AS_STORED_CONFIG",
    "AS_STORED_DIFF",
    "AS_STORED_ENV",
    "AS_STORED_PATHS",
    "Guard",
    "HistoryUnread",
    "Key",
    "ReachableWalk",
    "Walked",
    "author_dates",
    "author_instant",
    "base_for",
    "checked_cut",
    "cuts",
    "cuts_in",
    "git_counts",
    "given_up_since",
    "history_guard",
    "instant",
    "located_key",
    "log_stdin",
    "note_unread",
    "note_unwalked",
    "read_lines",
    "recall",
    "remember",
    "remember_failure",
    "shallow_file",
    "seconds",
    "unanswered",
    "unwalked",
]
