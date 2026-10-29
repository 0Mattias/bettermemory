"""The whole-history author dates `origin.commit_author_timestamps` keeps.

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
# replacement refs and their namespace, the graft and shallow files.
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
    where the two guards are equal, so a shallow clone deepened, a graft or
    a replacement written, or such a setting changed, reads the history
    whole again. Where `githead.gitdir_at` declines (off POSIX, another
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


__all__ = [
    "Guard",
    "Key",
    "author_dates",
    "base_for",
    "history_guard",
    "located_key",
    "recall",
    "remember",
    "remember_failure",
    "seconds",
]
