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
which is counted. The trees show more deletions than the patch prints (an
empty or binary file's deletion prints no ``/dev/null`` header), so a claim
can fire here that a full clone leaves quiet and implicate a local commit;
the caller therefore counts the whole window in a split whenever any fired
claim implicates no commit, not only when none implicates one
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
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import origin
from .claims import COMMIT_MARK
from .origin_gitfiles import _unquote_c_path

#: The mode of a gitlink: its object is a commit of the submodule, never a
#: blob of this repository, and the patch prints it without reading it.
_GITLINK = "160000"


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
    changes = _changes(toplevel, shas, pathspecs)
    if changes is None:
        return None
    blobs = {
        blob for found in changes.values() for change in found for blob in change.blobs
    }
    held = _held(toplevel, blobs) if blobs else set()
    if held is None:
        return None
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
        "log",
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
            current = changes.setdefault(line[1:].strip().lower(), [])
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


def _held(toplevel: Path, blobs: Collection[str]) -> set[str] | None:
    """The blobs among `blobs` the clone holds: ``cat-file --batch-check``
    names each with its type where it is present and as ``missing`` where
    it is not, which a partial clone's missing blob reads as under the
    no-fetch pins. A blob it does not report present counts as missing.
    None where git did not answer."""
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
    held: set[str] = set()
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[1] == "blob":
            held.add(fields[0].lower())
    return held


__all__ = ["Change", "PatchWindow", "local_patch_window"]
