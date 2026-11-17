"""Fixture: `os.access` and the open that does not follow a final link,
of paths in real trees (``fs_access.jsonl``).

The native file-system trait (``fsdata.rs``) answers ``access`` (what
`gitfile_rule._git_found` asks with ``X_OK`` and `gitconfig._accessible`
with ``R_OK``) and ``open_no_follow`` (the open of `gitfile_rule._read`
with ``follow=False``). This pins both to the reference's own calls.

A separate file from ``fs_stat.jsonl``: other calls, and its bytes stay
as they are. The trees are written as `gen_fs_stat` writes them (every
entry given its mode first, so nothing depends on the umask); the header
and the case lines are as there, each query answered with:

- ``a``: `os.access(p, m)` for ``R_OK``, ``W_OK`` and ``X_OK`` in turn,
  as ``"rwx"`` with ``-`` where it answers False; ``{"e": "ValueError"}``
  where it raises;
- ``o``: `os.open(p, O_RDONLY | O_NONBLOCK | O_NOFOLLOW)` (with
  ``O_CLOEXEC``), then `os.fstat`'s kind as ``{"k": kind}``, and for a
  regular file ``"d"``: what `_read` reads, up to ``LIMIT + 1`` bytes
  (spelled as a path is); an error is ``{"e": class, "n": errno name}``.
  A FIFO is never read: the open does not wait for a writer, and the
  caller refuses it by its kind.

Why Linux gives the same bytes as macOS, outcome by outcome:

- a final link (to a file, to a folder, dangling) under ``O_NOFOLLOW`` is
  ELOOP on both (open(2) on Linux and macOS: "the last component is a
  symbolic link"); a link in the middle is followed on both;
- a trailing separator after a final link makes the kernel follow it
  under ``O_NOFOLLOW`` too (``ld/`` opens the folder; ``lf/`` is ENOTDIR;
  ``dangling/`` ENOENT), on both;
- a folder opens for reading on both (``O_RDONLY``), and its kind is
  ``dir``; a FIFO opens at once under ``O_NONBLOCK`` for reading on both;
- EACCES for a file or folder without its read bit, or a folder on the
  way without its search bit; ENOENT and ENOTDIR as POSIX says; a NUL is
  ValueError before any call;
- `access(2)` answers by the owner's bits for the owner (no root: the
  generator refuses it), search asked of each folder on the way, links
  followed; no ACL is set on a scratch folder made here.

Nothing depends on the Python release (``os.access``, ``os.open`` and
``os.fstat`` answer alike from 3.11 to 3.14, and no exception text is
recorded), ``..`` after a file (`check_parent_of_file` refuses it:
macOS resolves it, Linux does not), the link-hop limit (no loop is queried), root, the umask, or
a path's length (`check_length`). Gate: POSIX only
(`generate.POSIX_ONLY`): links, FIFOs, modes and ``O_NOFOLLOW``.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from typing import Any

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import (
    Tree,
    _b,
    check_length,
    check_parent_of_file,
    refuse_root,
)
from .gen_fs_stat import _answer, _error, _kind

#: The caller's bound in the fixture: `_read` reads ``LIMIT + 1`` bytes at
#: most, so a longer file is told apart.
LIMIT = 8

_BITS = (("r", os.R_OK), ("w", os.W_OK), ("x", os.X_OK))


def _trees() -> Iterator[Tree]:
    # access: each permission bit alone and none, on files and folders,
    # through folders without search, links to each, dangling, a FIFO.
    yield Tree(
        [
            "<root>",
            "<root>/f",
            "<root>/r",
            "<root>/w",
            "<root>/x",
            "<root>/none",
            "<root>/all",
            "<root>/d",
            "<root>/dr",
            "<root>/dw",
            "<root>/dx",
            "<root>/d0",
            "<root>/dr/f",
            "<root>/dw/f",
            "<root>/dx/f",
            "<root>/d0/f",
            "<root>/lf",
            "<root>/lx",
            "<root>/ld",
            "<root>/ldx",
            "<root>/dangling",
            "<root>/p",
            "<root>/f/",
            "<root>/missing",
            "<root>/a\x00b",
        ],
        files={
            "f": "f",
            "r": "r",
            "w": "w",
            "x": "x",
            "none": "n",
            "all": "a",
            "dr/f": "1",
            "dw/f": "2",
            "dx/f": "3",
            "d0/f": "4",
        },
        dirs=("d", "dr", "dw", "dx", "d0"),
        links={
            "lf": "f",
            "lx": "x",
            "ld": "d",
            "ldx": "dx",
            "dangling": "nothing",
        },
        fifos=("p",),
        modes={
            "r": 0o400,
            "w": 0o200,
            "x": 0o100,
            "none": 0o000,
            "all": 0o700,
            "dr": 0o400,
            "dw": 0o200,
            "dx": 0o100,
            "d0": 0o000,
        },
    )
    # The open that does not follow a final link.
    yield Tree(
        [
            "<root>/f",
            "<root>/empty",
            "<root>/big",
            "<root>/exact",
            "<root>/d",
            "<root>/lf",
            "<root>/ld",
            "<root>/dangling",
            "<root>/mid/g",
            "<root>/mid/lg",
            "<root>/ld/",
            "<root>/lf/",
            "<root>/dangling/",
            "<root>/p",
            "<root>/zero",
            "<root>/locked/f",
            "<root>/missing",
            "<root>/f/x",
            "<root>/a\x00b",
        ],
        files={
            "f": "abc",
            "empty": "",
            "big": "b" * (4 * LIMIT),
            "exact": "e" * (LIMIT + 1),
            "d/g": "g",
            "zero": "0",
            "locked/f": "l",
        },
        dirs=("d", "locked"),
        links={
            "lf": "f",
            "ld": "d",
            "dangling": "nothing",
            "mid": "d",
            "d/lg": "g",
        },
        fifos=("p",),
        modes={"zero": 0o000, "locked": 0o000},
    )


def _access(path: str) -> Any:
    try:
        return "".join(c if os.access(path, m) else "-" for c, m in _BITS)
    except ValueError as exc:
        return _error(exc)


def _open(path: str) -> dict[str, Any]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError) as exc:
        return _error(exc)
    try:
        mode = os.fstat(fd).st_mode
        out: dict[str, Any] = {"k": _kind(mode)}
        if stat.S_ISREG(mode):
            chunks: list[bytes] = []
            size = 0
            while size <= LIMIT:
                chunk = os.read(fd, LIMIT + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            out["d"] = _b(b"".join(chunks))
        return out
    finally:
        os.close(fd)


def _query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    path = query.replace("<root>", real)
    check_length(path, links, real)
    check_parent_of_file(path)
    return {
        "p": _b(query.encode("utf-8", "surrogateescape")),
        "a": _access(path),
        "o": _open(path),
    }


def generate() -> dict[str, bytes]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "fs-access"
        for i, tree in enumerate(_trees()):
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(tree, root, _query)
            shutil.rmtree(root)
            lines.append({**tree.record(), "q": answers})
    header = {"fixture": "fs_access", "cases": len(lines), "limit": LIMIT}
    return {"fs_access.jsonl": jsonl([header, *lines])}
