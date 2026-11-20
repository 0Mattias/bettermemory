"""Fixtures: the leaves of `origin_gitfiles` that read the machine
(``origin_gitfiles_machine.jsonl``, ``origin_gitfiles_darwin.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_gitfiles/machine.rs``. This
pins it to `_read_prefix`, `relative_to_root` and `_spelled_as_git` on
real trees.

Each case is a tree written in a fresh directory of the scratch world
(`gen_fs_stat._answer`: ``"f"`` files, ``"d"`` folders, ``"l"`` links,
``"p"`` FIFOs, ``"m"`` modes), entered by its real path, then each query
answered. A path is spelled as `gen_fs_realpath._b` spells it (``=`` and
the text where printable ASCII without a backslash, else hex), with
``<root>`` for the case's directory, in a query and in an answer alike.
The first line is a header (the count). Each case's ``"q"`` holds one
answer a query:

- ``{"rel": [candidate, root], ...}``: `relative_to_root(Path(c),
  Path(r))`, ``"a"`` the answer's `str`, or ``"e"`` the class and ``"m"``
  the message.
- ``{"pre": [path, skip, length], ...}``: the path opened as the native
  `Fs::open` opens (``O_RDONLY | O_NONBLOCK``), `skip` bytes read, then
  `_read_prefix(fd, length)`: ``"a"`` the bytes (``"z"`` the length and
  ``"h"`` their SHA-256 past 64 bytes), or ``"e"`` the class.
- ``{"sp": path, ...}`` (darwin file only): `_spelled_as_git(Path(p))`,
  ``"a"`` the answer's `str`, ``"n"`` true for None, or ``"e"`` the class.

Gates. Both files are POSIX only (`generate.POSIX_ONLY`: links, FIFOs,
modes). ``origin_gitfiles_machine.jsonl`` holds nothing that differs
between macOS and Linux: no name differs from another by case or Unicode
form, no ``..`` follows a non-folder (`check_parent_of_file`), no path
reaches 1,024 bytes. ``origin_gitfiles_darwin.jsonl`` is gated on the
platform (`generate.PLATFORM_GATED`): `_spelled_as_git` reads ``F_GETPATH``
on macOS alone and answers the root itself elsewhere, and its trees are
asked by names that differ from the stored ones by case and by Unicode
form, which macOS's default file system folds and Linux's does not. No
answer depends on the Python release (`pathlib`'s parsing and
`relative_to`, `os.lseek` and `os.read` answer alike from 3.11 to 3.14).
No FIFO is opened by `_spelled_as_git`, whose blocking open would wait.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from bettermemory.origin_gitfiles import (
    _read_prefix,
    _spelled_as_git,
    relative_to_root,
)

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import Tree, _b, check_length, check_parent_of_file, refuse_root
from .gen_fs_stat import _answer as _write_and_answer

_BIG = 300
_NFC = "café"
_NFD = "café"


def _spell(path: str, real: str) -> str:
    return _b(os.fsencode(path.replace(real, "<root>")))


def _path(query: str, real: str) -> str:
    path = os.fsdecode(_unspell(query)).replace("<root>", real)
    check_length(path, None, real)
    check_parent_of_file(path)
    return path


def _unspell(text: str) -> bytes:
    if text.startswith("="):
        return text[1:].encode("ascii")
    return bytes.fromhex(text)


def _bytes_answer(data: bytes) -> dict[str, Any]:
    if len(data) <= 64:
        return {"a": _b(data)}
    return {"z": len(data), "h": hashlib.sha256(data).hexdigest()}


def _query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    del links
    kind, _, rest = query.partition(":")
    if kind == "rel":
        c, t = rest.split("|")
        out: dict[str, Any] = {"rel": [c, t]}
        try:
            got = relative_to_root(Path(_path(c, real)), Path(_path(t, real)))
        except ValueError as exc:
            out |= {"e": type(exc).__name__, "m": _spell(str(exc), real)}
        else:
            out["a"] = _spell(str(got), real)
        return out
    if kind == "pre":
        p, skip, length = rest.split("|")
        out = {"pre": [p, int(skip), int(length)]}
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK  # type: ignore[attr-defined,unused-ignore]
            fd = os.open(_path(p, real), flags)
        except OSError as exc:
            return out | {"e": type(exc).__name__}
        try:
            if int(skip):
                os.read(fd, int(skip))
            data = _read_prefix(fd, int(length))
        except OSError as exc:
            return out | {"e": type(exc).__name__}
        finally:
            os.close(fd)
        return out | _bytes_answer(data)
    if kind == "sp":
        out = {"sp": rest}
        try:
            spelled = _spelled_as_git(Path(_path(rest, real)))
        except ValueError as exc:
            return out | {"e": type(exc).__name__}
        if spelled is None:
            return out | {"n": True}
        return out | {"a": _spell(str(spelled), real)}
    raise ValueError(query)


def _rel(candidate: str, root: str) -> str:
    return f"rel:{_b(os.fsencode(candidate))}|{_b(os.fsencode(root))}"


def _pre(path: str, skip: int, length: int) -> str:
    return f"pre:{_b(os.fsencode(path))}|{skip}|{length}"


def _sp(path: str) -> str:
    return f"sp:{_b(os.fsencode(path))}"


def _machine_trees() -> list[Tree]:
    rel = [
        # By spelling: no stat.
        ("<root>/d/x", "<root>/d"),
        ("<root>/d", "<root>/d"),
        ("<root>/d/", "<root>/d/."),
        ("<root>//d//x/./y", "<root>/d"),
        ("<root>/d/../d/x", "<root>/d/.."),
        ("<root>/d/x", "<root>"),
        ("a/b", "a"),
        ("a/b", ""),
        ("a/b", "."),
        ("./a/./b/", "a"),
        ("", ""),
        ("/", "/"),
        ("/x", "/"),
        ("//x/y", "//x"),
        ("<root>/a\x00b/c", "<root>/a\x00b"),
        ("<root>/\udcff/x", "<root>/\udcff"),
        # By the disk: a link names the root's folder.
        ("<root>/ld/x/y", "<root>/d"),
        ("<root>/ld", "<root>/d"),
        ("<root>/ld/", "<root>/d"),
        ("<root>/ld/ld2/z", "<root>/d"),
        ("<root>/ld/ld2/z", "<root>/d/e"),
        ("<root>/d/e/../x", "<root>/d"),
        ("<root>/d/x", "<root>/ld"),
        ("<root>/d/x", "<root>/ld/"),
        ("<root>/d/missing/deeper", "<root>/ld"),
        ("<root>/d/x", "<root>/lf"),
        ("<root>/f/x", "<root>/f"),
        # Two parents are the root's folder: the nearest anchors.
        ("<root>/d/l/x", "<root>/ld"),
        # The last parent, `/`, anchors under another spelling.
        ("/nothing/x", "<root>/lr"),
        # A file as the root: its parent is stated without a separator.
        ("<root>/f/x", "<root>/lf"),
        # The root cannot be read, or holds a NUL.
        ("<root>/d/x", "<root>/missing"),
        ("<root>/d/x", "<root>/dangling"),
        ("<root>/d/x", "<root>/locked/in"),
        ("<root>/d/x", "<root>/a\x00b"),
        ("<root>/a\x00b/c", "<root>/d"),
        ("<root>/d/a\x00b", "<root>/ld"),
        # No ancestor is the root.
        ("<root>/e/x", "<root>/d"),
        ("<root>/f", "<root>/d"),
        ("<root>/locked/in/x", "<root>/d"),
        ("/", "<root>/d"),
        ("<root>/d/\udcff", "<root>/e"),
    ]
    pre = [
        ("<root>/f", 0, 0),
        ("<root>/f", 0, 1),
        ("<root>/f", 0, 5),
        ("<root>/f", 0, 6),
        ("<root>/f", 3, 2),
        ("<root>/f", 5, 6),
        ("<root>/big", 0, 64),
        ("<root>/big", 0, 65),
        ("<root>/big", 100, _BIG - 1),
        ("<root>/big", _BIG, _BIG),
        ("<root>/big", 0, _BIG + 1),
        ("<root>/big", 0, 1 << 20),
        ("<root>/empty", 0, 0),
        ("<root>/empty", 0, 10),
        ("<root>/lf", 1, 3),
        ("<root>/d", 0, 0),
        ("<root>/d", 0, 1),
        ("<root>/fifo", 0, 0),
        ("<root>/fifo", 0, 4),
    ]
    return [
        Tree(
            [_rel(c, t) for c, t in rel] + [_pre(p, s, n) for p, s, n in pre],
            files={
                "f": "hello",
                "big": "".join(chr(0x61 + i % 26) for i in range(_BIG)),
                "empty": "",
                "d/e/z": "z",
                "e/x": "x",
                "locked/in/x": "x",
            },
            dirs=("d", "d/e", "e", "locked", "locked/in"),
            links={
                "ld": "<root>/d",
                "d/ld2": "e",
                "lf": "f",
                "dangling": "nothing",
                "d/l": ".",
                "lr": "/",
            },
            fifos=("fifo",),
            modes={"locked": 0o000},
        )
    ]


def _darwin_trees() -> list[Tree]:
    queries = [
        _sp("<root>"),
        _sp("<root>/"),
        _sp("<root>/Docs/Repo"),
        _sp("<root>/docs/repo"),
        _sp("<root>/DOCS/REPO/"),
        _sp("<root>/docs/./repo/../repo"),
        _sp(f"<root>/{_NFD}"),
        _sp(f"<root>/{_NFC.upper()}"),
        _sp("<root>/docs/f"),
        _sp("<root>/lr"),
        _sp("<root>/LR"),
        _sp("<root>/missing"),
        _sp("<root>/dangling"),
        _sp("<root>/locked/in"),
        _sp("<root>/a\x00b"),
        _sp("<root>/\udcff"),
        _rel("<root>/docs/repo/x", "<root>/Docs/Repo"),
        _rel("<root>/DOCS/repo/x/y", "<root>/Docs/Repo"),
        _rel(f"<root>/{_NFD}/x", f"<root>/{_NFC}"),
        _rel("<root>/docs/repo", "<root>/Docs/Repo"),
        _rel("<root>/docs/other/x", "<root>/Docs/Repo"),
    ]
    return [
        Tree(
            queries,
            files={"Docs/f": "f", "Docs/Repo/x": "x", "locked/in/x": "x"},
            dirs=("Docs", "Docs/Repo", "Docs/other", _NFC, "locked", "locked/in"),
            links={"lr": "Docs/Repo", "dangling": "nothing"},
            modes={"locked": 0o000},
        )
    ]


def _run(trees: list[Tree], stem: str, check_folding: bool) -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / stem
        for i, tree in enumerate(trees):
            root = base / str(i)
            root.mkdir(parents=True)
            if check_folding and sys.platform == "darwin":
                (root / "Fold").mkdir()
                if not (root / "fold").is_dir():
                    raise RuntimeError("the scratch file system is case-sensitive")
                (root / "Fold").rmdir()
            answers = _write_and_answer(tree, root, _query)
            shutil.rmtree(root)
            lines.append({**tree.record(), "q": answers})
    return lines


def generate() -> dict[str, bytes]:
    lines = _run(_machine_trees(), "ogf-machine", False)
    header = {"fixture": "origin_gitfiles_machine", "cases": len(lines)}
    return {"origin_gitfiles_machine.jsonl": jsonl([header, *lines])}


def generate_darwin() -> dict[str, bytes]:
    lines = _run(_darwin_trees(), "ogf-darwin", True)
    header = {"fixture": "origin_gitfiles_darwin", "cases": len(lines)}
    return {"origin_gitfiles_darwin.jsonl": jsonl([header, *lines])}
