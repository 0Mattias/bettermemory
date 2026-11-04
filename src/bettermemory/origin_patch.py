"""A claim-drift window's patch over the commits whose blobs are local
(ENG-220).

No git the package runs fetches (ENG-218), so in a partial clone an object
the clone lacks reads as absent, and `origin.commit_patch_stream`, one ``git
log -p`` over the window's commits, fails whole on the first blob it lacks.
The governed half of claim drift then fell back to the claimed files'
per-file count and named no claim, for nearly every window of a blobless
clone (``--filter=blob:none``), whose trees are all local and whose blobs are
those its checkout and the user's own git have read.

`local_patch_window` is what claim drift reads when that stream fails. One
``git log --raw`` over the same commits and pathspecs lists the blobs each
commit's diff reads, from the trees alone; one ``git cat-file
--batch-check`` over those blobs says which the clone holds; and the stream
runs again over the commits whose blobs are all local. Every other commit
of the window is counted, as the per-file count counts it, and never
skipped: a blob the batch does not report present marks its commit so. Both
calls run under the no-fetch pins every call takes (`_git.NO_FETCH`).

A claim's verdict over the local commits alone is not always its verdict
over the window: a path claim fires on its file's deletion and then
implicates every commit carrying lines for the file, and the weak tier
counts the whole window when a fired claim implicates no commit. So the
paths each counted commit changed, and the ones it deleted or changed the
type of (a typechange's patch deletes the old file: a file made a symbolic
link or a gitlink), are marked in the index before the claims are read
(`PatchWindow.mark`): a claim the trees show fired fires, and what it
implicates among the local commits is counted. What the trees cannot show
(a binding line a counted commit touched) implicates only that commit,
which is counted. A claim the trees fire implicates no local commit where
the counted commit alone carried its change, which a full clone's patch
names, and the claims beside it can implicate local commits; the caller
therefore counts the whole window in a split whenever any fired claim
implicates no commit, not only when none implicates one
(`verify._weak_tier_evaluation`). The count is never below the full
clone's; a claim is named where a local commit, or a counted commit's
trees, show it fired.

Measured on a 1,000-commit window of one file (git 2.54, macOS, median of
5): in a full clone the one-call stream took 47 ms and a stream per commit
13.5 s; in a blobless clone with half the window's blobs local the split
took 101 ms (the listing 52, the check 34, the stream over the 500 local
commits 32) and a stream per commit 15.3 s, so the split it is. The check
looks for each blob the clone lacks in every pack: with the 503 packs that
fetching those blobs one at a time had left, it took 1.26 s until a repack.
Where git ignores ``GIT_NO_LAZY_FETCH`` (before 2.44), the empty
``GIT_ALLOW_PROTOCOL`` alone keeps the check from fetching, and git ends
it at the first blob the clone lacks: the split declines, and the window
keeps the per-file count.

A treeless clone (``--filter=tree:0``) is left to the unfiltered count: the
pathspec readers need the trees on each spec's path for every commit and
its parent, and such a clone holds the trees of the commits it checked out
and none of their parents', so a split by local trees would recover nearly
no commit, at a tree walk per path component.

The same listing and check bound what any patch reads (`too_large`): the
patch diffs every file as text (``--text``, ENG-220), a large binary file's
lines included, so the sizes the check reports are summed over the blobs
each commit's diff reads, and past `MAX_PATCH_STREAM_BYTES` no patch is
read and the window keeps the per-file count.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

from . import _caches, githead, origin, origin_history
from .claims import COMMIT_MARK
from .origin_gitfiles import _unquote_c_path

#: The mode of a gitlink: its object is a commit of the submodule, never a
#: blob of this repository, and the patch prints it without reading it.
_GITLINK = "160000"

#: The most bytes of blob one patch reads: the sizes ``cat-file`` reports,
#: summed over the blobs each commit's diff reads (both sides of a change,
#: once per commit). A patch costs what git reads and what it prints, and a
#: binary or rewritten file prints about what it read. Measured through
#: `origin.commit_patch_stream` and the parse (git 2.54, macOS, M3 Pro,
#: packed objects): a 5 MB binary file changed three times (29 MiB read) in
#: 140 ms, and rewritten three times in 520 ms; a 5 MB file of short
#: assignment lines rewritten three times in 2.8 s, nearly all of it the
#: parse; a 1 MB binary changed 64 times (122 MiB read, a 93 MB patch) in
#: 1.5 s. Past the bound no patch is read: a 20 MB binary changed three
#: times (114 MiB), and a window of 256 one-line edits to a 200 KB file
#: (98 MiB), whose patch is small, where the per-file count is loud
#: already; 64 such edits (24 MiB) are read.
MAX_PATCH_STREAM_BYTES = 32 * 1024 * 1024

# The listing and check, memoised per process: the window's changes and
# the blobs among them the clone held, with their sizes, which the size
# bound reads before any patch (`too_large`) and the split after a failed
# one, keyed on the common directory, the commits, the pathspecs and the
# stamps (`githead.stamp`) of
# what decides them: the pack directory, which every fetch of the missing
# objects writes a pack into, the alternates file, the config, and the
# shallow file, which changes which parent a commit is diffed against.
# Replace refs, a grafts file and the commit-graph decide nothing here:
# both calls read the objects as stored (`origin_history.AS_STORED_ENV`,
# `origin_history.AS_STORED_CONFIG`), as every origin call does, so a
# replace ref written under a ref store no stamp sees (reftable) moves no
# answer (ENG-220's review). The first stream still runs and fails on every
# search (and keeps the drift memo from keeping the value, so a blob
# fetched since is read at once where its pack is new); a search after the
# first pays it and the stream over the local commits alone, where it paid
# the listing and the check too. What the key does not see is sound to
# miss: a blob that arrives loose reads as missing and its commit is
# counted, and one that went reads as held and fails the local stream,
# which declines the split. The common directory is the one
# `githead.gitdir_at` settles from the files on POSIX, and where it
# declines (off POSIX, another account's repository) the one
# `githead.locate` reads at the root, as the history's guard reads it
# (`origin_history.history_guard`): git on Windows also reads ownership
# and names by rules the package does not replicate, but a repository git
# refuses fails the listing, which is not kept, and a directory named
# otherwise is the same files, whose stamps the key reads. Under a
# variable that moves git's discovery nothing is kept. Bounded LRU,
# emptied before each test (`_caches`).
_MEMO_CAP = 256
_MemoKey: TypeAlias = tuple[str, tuple[object, ...], tuple[str, ...], tuple[str, ...]]
#: A window's changes per commit, and the size of each blob the clone holds.
_Listed: TypeAlias = tuple[dict[str, list["Change"]], Mapping[str, int]]
_MEMO: OrderedDict[_MemoKey, _Listed] = OrderedDict()
_MEMO_LOCK = threading.Lock()

# The bytes a window's patch reads (`too_large`), memoised per process on
# every platform: every full-clone evaluation of a governed claim reads
# the bound, and off POSIX paid its two git processes on every search
# (ENG-220's re-review). Keyed as the history's dates are, on the root and head
# `githead.locate` reads (`origin_history.located_key`) and the files that
# change which commits a head reaches (`origin_history.history_guard`),
# both of which answer off POSIX, with the commits and pathspecs. A
# commit, a shallow clone deepened or a replace ref written moves the key.
# What it does not see is a blob fetched or dropped under the same head:
# the sizes summed are of the blobs the clone held when the bound was
# read, so a window read under the bound after its blobs arrive is read
# whole within the patch's own 5 s bound, and one past it keeps the
# per-file count, the two answers the bound chooses between and both
# sound. Bounded as `_MEMO` is, and emptied with it.
_ReadKey: TypeAlias = tuple[
    origin_history.Key, origin_history.Guard, tuple[str, ...], tuple[str, ...]
]
_READ: OrderedDict[_ReadKey, int] = OrderedDict()
_STAMPED = (
    ("objects", "pack"),
    ("objects", "info", "alternates"),
    ("config",),
    ("shallow",),
)


@_caches.register
def _clear_memo() -> None:
    with _MEMO_LOCK:
        _MEMO.clear()
        _READ.clear()


@dataclass(frozen=True)
class Change:
    """One path a commit's diff changed, as ``--raw`` lists it."""

    path: str
    status: str
    blobs: tuple[str, ...]


@dataclass(frozen=True)
class PatchWindow:
    """A window's patch over the commits whose blobs are local, and what
    the trees say of the rest.

    `stream` is `origin.commit_patch_stream`'s over the local commits, ""
    where none is; `counted` the commits a blob the clone lacks keeps out of
    it, each counted as the per-file count counts it; `touched` the paths
    those commits changed and `deleted` the ones they deleted or changed
    the type of."""

    stream: str
    counted: frozenset[str]
    touched: frozenset[str]
    deleted: frozenset[str]

    def mark(self, index: dict[str, Any]) -> None:
        """Mark the counted commits' paths in a `claims.build_binding_index`
        index of `stream`, so a claim the trees show fired fires."""
        index["files"] = set(index["files"]) | self.touched
        index["deleted"] = set(index["deleted"]) | self.deleted


def too_large(
    cwd: Path | None,
    shas: Sequence[str],
    pathspecs: list[str],
    *,
    toplevel: Path | None = None,
) -> bool:
    """Whether the blobs a patch of `shas` under `pathspecs` reads, as the
    clone holds them, sum past `MAX_PATCH_STREAM_BYTES`: the caller then
    keeps the per-file count without reading the patch. False where git
    could not list or check them, and the patch is read as before; a blob
    the clone lacks is not summed, since no patch reads it (the stream
    fails on it, and the split reads only the local commits). The sum is
    kept under the head and the history's guard (`_READ`), on every
    platform."""
    if cwd is None or not shas or not pathspecs:
        return False
    if len(shas) > origin.MAX_PATCH_STREAM_COMMITS:
        return False
    if toplevel is None:
        toplevel = origin.repo_toplevel(cwd)
        if toplevel is None:
            return False
    key = _read_key(toplevel, shas, pathspecs)
    if key is not None:
        with _MEMO_LOCK:
            kept = _READ.get(key)
            if kept is not None:
                _READ.move_to_end(key)
                return kept > MAX_PATCH_STREAM_BYTES
    listed = _listed(toplevel, shas, pathspecs)
    if listed is None:
        return False
    changes, held = listed
    read = sum(
        held.get(blob, 0)
        for found in changes.values()
        for change in found
        for blob in change.blobs
    )
    if key is not None:
        with _MEMO_LOCK:
            _READ[key] = read
            _READ.move_to_end(key)
            while len(_READ) > _MEMO_CAP:
                _READ.popitem(last=False)
    return read > MAX_PATCH_STREAM_BYTES


def _read_key(
    toplevel: Path, shas: Sequence[str], pathspecs: list[str]
) -> _ReadKey | None:
    """`_READ`'s key for a window, or None where the files settle no head
    or guard (a variable that moves git's discovery, a head the files do
    not name), and the bound is read uncached."""
    located = origin_history.located_key(toplevel)
    if located is None:
        return None
    guard = origin_history.history_guard(Path(located[0]))
    if guard is None:
        return None
    return (
        located,
        guard,
        tuple(sha.lower() for sha in shas),
        tuple(pathspecs),
    )


def local_patch_window(
    cwd: Path | None,
    shas: Sequence[str],
    pathspecs: list[str],
    *,
    toplevel: Path | None = None,
) -> PatchWindow | None:
    """The patch of `shas` under `pathspecs` over the commits whose blobs
    are local, or None where the split cannot help: git could not list or
    check the blobs, every blob is local (the stream failed for another
    reason), or the stream over the local commits failed. None is the
    caller's per-file count, as before."""
    if cwd is None or not shas or not pathspecs:
        return None
    if len(shas) > origin.MAX_PATCH_STREAM_COMMITS:
        return None
    if toplevel is None:
        toplevel = origin.repo_toplevel(cwd)
        if toplevel is None:
            return None
    listed = _listed(toplevel, shas, pathspecs)
    if listed is None:
        return None
    changes, held = listed
    counted = {
        sha
        for sha, found in changes.items()
        if any(blob not in held for change in found for blob in change.blobs)
    }
    if not counted:
        return None
    local = [sha for sha in shas if sha.lower() not in counted]
    stream = ""
    if local:
        streamed = origin.commit_patch_stream(cwd, local, pathspecs, toplevel=toplevel)
        if streamed is None:
            return None
        stream = streamed
    mine = [change for sha in counted for change in changes[sha]]
    return PatchWindow(
        stream=stream,
        counted=frozenset(counted),
        touched=frozenset(change.path for change in mine),
        deleted=frozenset(
            change.path for change in mine if change.status in ("D", "T")
        ),
    )


def _listed(
    toplevel: Path, shas: Sequence[str], pathspecs: list[str]
) -> _Listed | None:
    """The window's changes (`_changes`) and the blobs among them the clone
    holds, with their sizes (`_held`), from the memo where its key holds;
    None where git did not answer, which is not kept. The value is shared:
    no caller changes it."""
    key = _memo_key(toplevel, shas, pathspecs)
    if key is not None:
        with _MEMO_LOCK:
            kept = _MEMO.get(key)
            if kept is not None:
                _MEMO.move_to_end(key)
                return kept
    changes = _changes(toplevel, shas, pathspecs)
    if changes is None:
        return None
    blobs = {
        blob for found in changes.values() for change in found for blob in change.blobs
    }
    held = _held(toplevel, blobs) if blobs else {}
    if held is None:
        return None
    listed = (changes, MappingProxyType(held))
    if key is not None:
        with _MEMO_LOCK:
            _MEMO[key] = listed
            while len(_MEMO) > _MEMO_CAP:
                _MEMO.popitem(last=False)
    return listed


def _memo_key(
    toplevel: Path, shas: Sequence[str], pathspecs: list[str]
) -> _MemoKey | None:
    """The memo's key for a window (see `_MEMO`), or None where neither
    `githead.gitdir_at` nor `githead.locate` reads the repository at
    `toplevel`, or a variable moves git's discovery past what `locate`
    reads."""
    found = githead.gitdir_at(toplevel)
    if found is None and not githead.discovery_moved():
        found = githead.locate(toplevel, walk=False)
    if found is None:
        return None
    try:
        stamps = tuple(
            githead.stamp(os.path.join(found.commondir, *parts)) for parts in _STAMPED
        )
    except (OSError, ValueError):
        return None
    return (
        os.fspath(found.commondir),
        stamps,
        tuple(sha.lower() for sha in shas),
        tuple(pathspecs),
    )


def _changes(
    toplevel: Path, shas: Sequence[str], pathspecs: list[str]
) -> dict[str, list[Change]] | None:
    """The paths each of `shas` changed under `pathspecs`, with the blobs
    its diff reads, from the trees alone: the commits and pathspecs the
    stream takes, ``--raw`` in place of ``-p``. Renames are not looked for
    (``--no-renames``), since finding an inexact one reads the blobs, so a
    rename lists its source's deletion and its target's addition, both of
    whose blobs a stream that pairs them reads; nor is ``log.follow``
    obeyed, whose rename search reads blobs outside the pathspecs (a
    stream it reaches that far fails, and the window is counted per file).
    None where git did not answer."""
    raw = origin._git(
        toplevel,
        "-c",
        "core.quotePath=false",
        "-c",
        "log.follow=false",
        *origin_history.AS_STORED_PATHS,
        "log",
        *origin_history.AS_STORED_DIFF,
        "--no-walk=unsorted",
        "--raw",
        "--no-renames",
        "--no-abbrev",
        "--no-color",
        f"--format={COMMIT_MARK}%H",
        *shas,
        "--",
        *pathspecs,
        timeout=5.0,
        empty_ok=True,
    )
    if raw is None:
        return None
    changes: dict[str, list[Change]] = {}
    current: list[Change] | None = None
    for line in raw.split("\n"):
        if line.startswith(COMMIT_MARK):
            sha = line[1:].strip().lower()
            if not origin.is_full_commit_sha(sha):
                # A mark carries the hash ``%H`` prints, and no other.
                return None
            current = changes.setdefault(sha, [])
            continue
        if not line.startswith(":") or current is None:
            continue
        meta, _, path = line.partition("\t")
        fields = meta[1:].split()
        if len(fields) < 5 or not path:
            return None
        if path.startswith('"') and path.endswith('"'):
            path = _unquote_c_path(path) or path
        modes, oids = fields[0:2], fields[2:4]
        current.append(
            Change(
                path=path,
                status=fields[4][:1],
                blobs=tuple(
                    oid.lower()
                    for mode, oid in zip(modes, oids)
                    if mode != _GITLINK and oid.strip("0")
                ),
            )
        )
    return changes


def _held(toplevel: Path, blobs: Collection[str]) -> dict[str, int] | None:
    """The blobs among `blobs` the clone holds, with their sizes: ``cat-file
    --batch-check`` names each with its type and size where it is present
    and as ``missing`` where it is not, which a partial clone's missing blob
    reads as under the no-fetch pins. A blob it does not report present
    counts as missing. None where git did not answer."""
    raw = origin._git(
        toplevel,
        "cat-file",
        "--batch-check",
        timeout=5.0,
        empty_ok=True,
        stdin="".join(f"{blob}\n" for blob in sorted(blobs)).encode("ascii"),
    )
    if raw is None:
        return None
    held: dict[str, int] = {}
    for line in raw.split("\n"):
        fields = line.split()
        if len(fields) == 3 and fields[1] == "blob" and fields[2].isdigit():
            held[fields[0].lower()] = int(fields[2])
    return held


__all__ = [
    "MAX_PATCH_STREAM_BYTES",
    "Change",
    "PatchWindow",
    "local_patch_window",
    "too_large",
]
