"""Fixture: `os.path.samefile` of pairs of paths, and the stat bits
`gitfile_rule` reads, in real trees (``fs_samestat.jsonl``).

`gitfile_rule._tree_of` and `_registered` (972-982, 1035-1046) compare
two paths as filesystem objects (`os.path.samestat` of two `os.stat`
results, `os.path.samefile`); `_placeholder` (1062-1070) reads
``st_flags`` and ``st_file_attributes`` of a `stat` or `lstat` result, and
`_is_link` (943-948) the kind and ``st_reparse_tag`` of an `lstat`. The
native trait answers ``stat``/``lstat_result`` with ``dev``, ``ino``,
``flags``, ``file_attributes`` and ``reparse_tag``; this pins what the
reference makes of them on a real tree. The reference's own functions
are called (`_placeholder`, `_is_link`).

Trees are written as `gen_fs_stat` writes them, with ``"h"``: hard links,
new name to an existing file. Each query is two paths joined by a tab
(``<root>`` the case's directory); each answer:

- ``p``, ``o``: the two paths, spelled as `gen_fs_stat` spells one;
- ``same``: `os.path.samefile(p, o)`, or the first error's class and
  errno name;
- ``a``, ``b``: for each path, ``s`` `_placeholder(os.stat(path))` and
  ``l`` `_placeholder(os.lstat(path))`, and ``k`` `_is_link(os.lstat(path))`,
  each a bool or the error.

No device or inode number, flag or attribute value is recorded: only
equality and the reference's own booleans. A dataless file (``SF_DATALESS``
is set by a file provider, never by a user) and the Windows recall
attributes and junction tag cannot be made here: the port of those is held
to `gitfile_rule.py` 75-76, 940-948 and 1062-1070 by reading only. Pairs
across a mount are not recorded (whether the scratch folder shares a
device with ``/`` is the host's); the native on-disk test asks the
reference live instead. Gate: POSIX only (links, hard links, modes).
Nothing depends on the Python release (``samefile``, ``stat`` and
``lstat`` answer alike from 3.11 to 3.14), root, the umask, case folding
or the scratch path.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from typing import Any

from bettermemory.gitfile_rule import _is_link, _placeholder

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import (
    Tree,
    _b,
    check_length,
    check_parent_of_file,
    refuse_root,
)
from .gen_fs_stat import _answer, _error


def _pairs() -> list[tuple[str, str]]:
    return [
        ("f", "f"),
        ("f", "./f"),
        ("f", "h"),
        ("f", "lf"),
        ("f", "llf"),
        ("f", "g"),
        ("f", "d/f"),
        ("f", ""),
        ("d", "ld"),
        ("d", "ld/"),
        ("d", "ld/../d"),
        ("d/f", "ld/f"),
        ("h", "d/hf"),
        ("f", "missing"),
        ("missing", "f"),
        ("dangling", "f"),
        ("f", "locked/f"),
        ("p", "lp"),
        ("p", "f"),
        ("f", "a\x00b"),
    ]


def _trees() -> Iterator[Tree]:
    yield Tree(
        [f"<root>/{a}\t<root>/{b}" for a, b in _pairs()],
        files={"f": "same", "g": "same", "d/f": "same", "locked/f": "x"},
        dirs=("d", "locked"),
        links={
            "lf": "f",
            "llf": "lf",
            "ld": "d",
            "dangling": "nothing",
            "lp": "p",
        },
        hard={"h": "f", "d/hf": "f"},
        fifos=("p",),
        modes={"locked": 0o000},
    )


def _bits(path: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, call in (("s", os.stat), ("l", os.lstat)):
        try:
            out[key] = _placeholder(call(path))
        except (OSError, ValueError) as exc:
            out[key] = _error(exc)
    try:
        out["k"] = _is_link(os.lstat(path))
    except (OSError, ValueError) as exc:
        out["k"] = _error(exc)
    return out


def _query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    first, second = (q.replace("<root>", real) for q in query.split("\t"))
    for path in (first, second):
        check_length(path, links, real)
        check_parent_of_file(path)
    try:
        same: Any = os.path.samefile(first, second)
    except (OSError, ValueError) as exc:
        same = _error(exc)
    p, o = query.split("\t")
    return {
        "p": _b(p.encode("utf-8", "surrogateescape")),
        "o": _b(o.encode("utf-8", "surrogateescape")),
        "same": same,
        "a": _bits(first),
        "b": _bits(second),
    }


def generate() -> dict[str, bytes]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "fs-samestat"
        for i, tree in enumerate(_trees()):
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(tree, root, _query)
            shutil.rmtree(root)
            lines.append({**tree.record(), "q": answers})
    header = {"fixture": "fs_samestat", "cases": len(lines)}
    return {"fs_samestat.jsonl": jsonl([header, *lines])}
