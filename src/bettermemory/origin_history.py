"""How `origin` reads a repository's history, and the whole-history author
dates `origin.commit_author_timestamps` keeps.

Every git `origin` runs reads the history as its objects hold it, whatever
the repository sets (`AS_STORED_ENV`, `AS_STORED_CONFIG`, `AS_STORED_DIFF`),
and no count is read behind a shallow file's cut (`cuts`).

`origin` reads from git the author date of every commit a head reaches
(``git log --format=%at <head>``), or of the commits a head reaches that a
head already read does not (``<old>..<new>``); this module keeps what was
read, per repository root and head, with the stamps of the files that
change which commits a head reaches without moving it (`history_guard`),
and remembers for a while each read that failed.

This module lives apart from `origin.py` to keep each module under the
package's 2,000-line bound (ENG-96). It imports the standard library,
`_caches` and `githead`; `origin` imports it, never the reverse.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeAlias

from . import _caches, githead

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
)
#: On every log that prints a diff (the walk's ``--name-only``, the split's
#: ``--raw``, the patch stream's ``-p``), after the subcommand.
AS_STORED_DIFF = ("--text", "--ignore-submodules=none")

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

# How long a failed read is remembered, in seconds of `time.monotonic`. A
# read fails at its 5 s ceiling on a history too long for it, and fails
# again for the same head. Remembered for a minute, it runs at most once a
# minute for a caller that asks on every row of every search, which then
# spends at most 5 s a minute on it (a twelfth of its time) where it spent
# up to 5 s per row; a failure that clears on its own (a lock, an object
# briefly unreadable) is read again within the minute.
_FAILURE_SECONDS = 60.0


@dataclass(frozen=True)
class _Entry:
    """The ascending author dates of a head's history, and the
    `history_guard` read before they were, None where it could not be."""

    dates: list[datetime]
    guard: Guard | None


# The entries, oldest first; the failures, by when each was met. The lock
# makes each look-up-and-touch and each insert-and-evict atomic; git runs
# outside it. Registered with `_caches`, which empties both before each
# test.
_TIMESTAMPS_MEMO: OrderedDict[Key, _Entry] = OrderedDict()
_TIMESTAMPS_FAILED: OrderedDict[Key, float] = OrderedDict()
_TIMESTAMPS_MEMO_LOCK = threading.Lock()


@_caches.register
def _clear_timestamps_memo() -> None:
    with _TIMESTAMPS_MEMO_LOCK:
        _TIMESTAMPS_MEMO.clear()
        _TIMESTAMPS_FAILED.clear()


def recall(key: Key) -> tuple[list[datetime] | None, bool]:
    """``(dates, failed)`` for `key`: the dates kept for it (the stored list
    itself), or None; and whether a read for it failed less than
    `_FAILURE_SECONDS` ago, where none is kept."""
    with _TIMESTAMPS_MEMO_LOCK:
        entry = _TIMESTAMPS_MEMO.get(key)
        if entry is not None:
            _TIMESTAMPS_MEMO.move_to_end(key)
            return entry.dates, False
        failed_at = _TIMESTAMPS_FAILED.get(key)
        if failed_at is not None:
            if 0.0 <= time.monotonic() - failed_at < _FAILURE_SECONDS:
                return None, True
            del _TIMESTAMPS_FAILED[key]
    return None, False


def base_for(root: str, guard: Guard) -> tuple[str, list[datetime]] | None:
    """The most recently used entry under `root` read under `guard`, as
    ``(head, dates)``: the history a head of the same repository extends
    when that head is its ancestor."""
    with _TIMESTAMPS_MEMO_LOCK:
        for (entry_root, head), entry in reversed(_TIMESTAMPS_MEMO.items()):
            if entry_root == root and entry.guard == guard:
                return head, entry.dates
    return None


def remember(key: Key, dates: list[datetime], guard: Guard | None) -> bool:
    """Keep `dates` for `key`, read under `guard`, and evict the oldest
    entries past `_TIMESTAMPS_MEMO_CAP` or `_TIMESTAMPS_MEMO_SLOTS`; True.
    A list longer than the slots is not kept, and False says so: the caller
    remembers its key as a failed read's (`remember_failure`), so the calls
    of the next minute answer None where each would read the history whole
    again (review Ob, D3)."""
    if len(dates) > _TIMESTAMPS_MEMO_SLOTS:
        return False
    with _TIMESTAMPS_MEMO_LOCK:
        _TIMESTAMPS_FAILED.pop(key, None)
        _TIMESTAMPS_MEMO[key] = _Entry(dates, guard)
        _TIMESTAMPS_MEMO.move_to_end(key)
        held = sum(len(entry.dates) for entry in _TIMESTAMPS_MEMO.values())
        while len(_TIMESTAMPS_MEMO) > _TIMESTAMPS_MEMO_CAP or (
            held > _TIMESTAMPS_MEMO_SLOTS
        ):
            _, gone = _TIMESTAMPS_MEMO.popitem(last=False)
            held -= len(gone.dates)
    return True


def remember_failure(key: Key) -> None:
    """Remember that a read for `key` failed, now."""
    with _TIMESTAMPS_MEMO_LOCK:
        _TIMESTAMPS_FAILED[key] = time.monotonic()
        _TIMESTAMPS_FAILED.move_to_end(key)
        while len(_TIMESTAMPS_FAILED) > _TIMESTAMPS_MEMO_CAP:
            _TIMESTAMPS_FAILED.popitem(last=False)


# The instant a ``%at`` author date counts its seconds from: a date is built
# from it by arithmetic, which holds for every value a datetime holds on every
# platform (``datetime.fromtimestamp`` refuses a negative one on Windows), and
# every date of a history shares its one tzinfo.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def seconds(text: str) -> int | None:
    """The decimal seconds ``%at`` prints, or None for any other line."""
    text = text.strip()
    return int(text) if text.isascii() and text.isdigit() else None


def author_dates(values: list[int]) -> list[datetime]:
    """`values`, author dates in ``%at`` seconds, as ascending UTC
    datetimes; a value past what a datetime holds is skipped."""
    out: list[datetime] = []
    for value in sorted(values):
        try:
            out.append(_EPOCH + timedelta(seconds=value))
        except OverflowError:
            continue
    return out


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
    gd = githead.gitdir_at(start) or githead.locate(start, walk=walk)
    if gd is None:
        return None
    try:
        with open(gd.commondir / "shallow", "rb") as handle:
            raw = handle.read(_CUTS_MAX + 1)
    except FileNotFoundError:
        return frozenset()
    except OSError:
        return None
    if len(raw) > _CUTS_MAX:
        return None
    return frozenset(
        line.strip().decode("ascii", "replace").lower()
        for line in raw.splitlines()
        if line.strip()
    )


__all__ = [
    "AS_STORED_CONFIG",
    "AS_STORED_DIFF",
    "AS_STORED_ENV",
    "Guard",
    "Key",
    "author_dates",
    "base_for",
    "cuts",
    "history_guard",
    "located_key",
    "recall",
    "remember",
    "remember_failure",
    "seconds",
]
