"""Fixture: `os.stat` and `os.listdir` of paths in real trees
(``fs_stat.jsonl``).

The native build's file-system trait (``fsdata.rs``) answers ``stat``
(the fields `gitconfig.file_stamp` reads) and ``listdir`` (what
`gitconfig._legacy_names` lists), with each error's class and errno.
This pins both to the reference's own calls on a real tree.

Each case is a tree written in a fresh directory of the scratch world,
entered by its real path (``os.getcwd()`` after a ``chdir``), then each
query answered. The first line is a header (the count); then one line a
case:

- ``"f"``: files, path (relative to the case's directory) to text;
  ``"d"``: directories; ``"l"``: symbolic links, path to target
  (``<root>`` stands for the case's directory); ``"p"``: FIFOs; ``"m"``:
  modes (octal) set last. Every entry made is given its mode first
  (files and FIFOs 644, directories 755), so no answer depends on the
  umask;
- ``"q"``: one answer a query (every query is absolute but the empty
  one: the native trait takes no working directory): ``p`` the query (``=`` and the text where
  printable ASCII without a backslash, else hex; ``<root>`` the case's
  directory); ``s`` `os.stat(p)` as ``{"k": kind, "m": st_mode in octal}``
  with ``"z"`` the size for a regular file, and ``l`` `os.listdir(p)`,
  its names sorted (the OS's order is not the reference's to keep;
  `_legacy_names` sorts them), each spelled as ``p``. An error is
  ``{"e": class}`` with ``"n"`` the errno's name (`errno.errorcode`, never
  its number, which differs between macOS and Linux) for an OSError.

What is left out, as the host's own: the inode number and the times (no
two runs agree; the on-disk native test compares them with the
reference Python's `os.stat` of the same tree instead), a directory's
size (a count of entries on macOS, blocks on Linux).

Gate: POSIX only (`generate.POSIX_ONLY`): links, FIFOs and mode-000
folders. Nothing depends on the Python release (``os.stat`` and
``os.listdir`` answer alike from 3.11 to 3.14, and no text of an
exception is recorded), case folding, the link-hop limit (a loop fails
at either; no chain is longer than 30), a name only Linux stores (none
is made), a path of 1,024 to 4,095 bytes as given or as its links are
followed (`check_length` refuses one), or root (`refuse_root` stops the
generator there).
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
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

NAME_MAX = 255


def _trees() -> Iterator[Tree]:
    long = "n" * NAME_MAX
    yield Tree(
        [
            "<root>",
            "<root>/",
            "<root>/f",
            "<root>/f/",
            "<root>/f/x",
            "<root>/empty",
            "<root>/d",
            "<root>/d/",
            "<root>/d/e",
            "<root>/d/../f",
            "<root>/missing",
            "<root>/missing/x",
            "",
            "<root>/a\x00b",
            "<root>/\udcff",
            f"<root>/{long}",
            f"<root>/{long}n",
            "<root>/big",
        ],
        files={
            "f": "x",
            "empty": "",
            "d/g": "y",
            "d/e/h": "z",
            long: "l",
            "big": "b" * 5000,
        },
        dirs=("d", "d/e", "d/empty"),
    )
    # Links: to each kind, dangling, loops, a chain; listed through.
    yield Tree(
        [
            "<root>/lf",
            "<root>/ld",
            "<root>/ld/",
            "<root>/lp",
            "<root>/dangling",
            "<root>/self",
            "<root>/a",
            "<root>/c0",
            "<root>/abs",
            "<root>/dot",
            "<root>/ld/lf2",
        ],
        files={"f": "x", "d/g": "y"},
        dirs=("d",),
        links={
            "lf": "f",
            "ld": "d",
            "lp": "fifo",
            "dangling": "nothing",
            "self": "self",
            "a": "b",
            "b": "a",
            "abs": "<root>/d",
            "dot": ".",
            "d/lf2": "../f",
        }
        | {f"c{i}": f"c{i + 1}" for i in range(20)}
        | {"c20": "d"},
        fifos=("fifo",),
    )
    # A link to a deep folder: followed, the path grows well past what is
    # typed, and stays short of 1,024 bytes (the cases past it are native
    # tests on macOS alone: Linux's limit is 4,096).
    deep = "/".join(["w" * 100] * 3)
    yield Tree(
        [
            "<root>/L",
            "<root>/L/",
            "<root>/L/g",
            "<root>/L/" + "x" * 100,
            "<root>/L/../" + "w" * 100,
        ],
        files={deep + "/g": "g"},
        dirs=(deep,),
        links={"L": "<root>/" + deep},
    )
    # A FIFO, stated and listed, never opened.
    yield Tree(["<root>/fifo", "<root>/fifo/", "<root>/fifo/x"], fifos=("fifo",))
    # Modes: a folder of mode 000 at the end and along the path, one that
    # can be read but not searched, one that can be searched but not read,
    # and a file of mode 000.
    yield Tree(
        [
            "<root>/locked",
            "<root>/locked/f",
            "<root>/readonly",
            "<root>/readonly/f",
            "<root>/searchonly",
            "<root>/searchonly/f",
            "<root>/zero",
            "<root>/lz",
        ],
        files={"locked/f": "x", "readonly/f": "y", "searchonly/f": "z", "zero": "0"},
        dirs=("locked", "readonly", "searchonly"),
        links={"lz": "zero"},
        modes={"locked": 0o000, "readonly": 0o600, "searchonly": 0o300, "zero": 0o000},
    )


def check_unshared(number: int) -> str:
    """The one name `errno` has for `number`; refuse a number that another
    name shares on this platform (EAGAIN and EWOULDBLOCK, ENOTSUP and
    EOPNOTSUPP), whose recorded name would depend on which one
    `errno.errorcode` happens to keep."""
    names = sorted(
        n for n in dir(errno) if n.startswith("E") and getattr(errno, n) == number
    )
    if len(names) != 1:
        raise ValueError(f"errno {number} has more than one name: {names}")
    return names[0]


def _error(exc: BaseException) -> dict[str, Any]:
    out: dict[str, Any] = {"e": type(exc).__name__}
    if isinstance(exc, OSError) and exc.errno is not None:
        if exc.errno not in errno.errorcode:
            raise ValueError(f"errno {exc.errno} has no name")
        out["n"] = check_unshared(exc.errno)
    return out


#: Every errno name `bettermemory_sys::errno_name` answers (a native test
#: holds the two lists equal).
ERRNO_NAMES = (
    "ENOENT",
    "ENOTDIR",
    "EISDIR",
    "EACCES",
    "EPERM",
    "EEXIST",
    "ELOOP",
    "ENAMETOOLONG",
    "EIO",
    "ENXIO",
    "EINVAL",
    "ENOTCONN",
    "ESTALE",
    "ETIMEDOUT",
    "EINTR",
    "EBADF",
    "EILSEQ",
)


def generate_errno_class() -> dict[str, bytes]:
    """The class CPython raises for each errno name the native side knows:
    `type(OSError(n, ...)).__name__`, the class alone."""
    lines: list[dict[str, Any]] = []
    for name in ERRNO_NAMES:
        number = getattr(errno, name)
        if check_unshared(number) != name:
            raise ValueError(f"{name} is not errno's name for {number}")
        lines.append({"n": name, "e": type(OSError(number, "x")).__name__})
    header = {"fixture": "errno_class", "cases": len(lines)}
    return {"errno_class.jsonl": jsonl([header, *lines])}


def _kind(mode: int) -> str:
    for test, name in (
        (stat.S_ISREG, "reg"),
        (stat.S_ISDIR, "dir"),
        (stat.S_ISFIFO, "fifo"),
        (stat.S_ISSOCK, "sock"),
        (stat.S_ISLNK, "link"),
    ):
        if test(mode):
            return name
    return "other"


def _query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    path = query.replace("<root>", real)
    check_length(path, links, real)
    check_parent_of_file(path)
    out: dict[str, Any] = {"p": _b(query.encode("utf-8", "surrogateescape"))}
    try:
        st = os.stat(path)
    except (OSError, ValueError) as exc:
        out["s"] = _error(exc)
    else:
        out["s"] = {"k": _kind(st.st_mode), "m": f"{st.st_mode:o}"}
        if stat.S_ISREG(st.st_mode):
            out["s"]["z"] = st.st_size
    try:
        names = os.listdir(path)
    except (OSError, ValueError) as exc:
        out["l"] = _error(exc)
    else:
        out["l"] = [_b(os.fsencode(n)) for n in sorted(names)]
    return out


def _answer(
    tree: Tree,
    root: Path,
    query: Callable[[str, str, dict[str, str]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Write `tree` under `root` and answer each query by `query` (by
    default `os.stat` and `os.listdir`)."""
    ask = _query if query is None else query
    os.chmod(root, 0o755)
    for d in tree.dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
        os.chmod(root / d, 0o755)
    for rel, text in tree.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.chmod(path, 0o644)
    saved = os.getcwd()
    os.chdir(root)
    try:
        real = os.getcwd()
        for rel, target in tree.links.items():
            os.symlink(target.replace("<root>", real), Path(real) / rel)
        for rel in tree.fifos:
            os.mkfifo(Path(real) / rel)  # type: ignore[attr-defined,unused-ignore]
            os.chmod(Path(real) / rel, 0o644)
        by_depth = sorted(tree.modes, key=lambda rel: rel.count("/"))
        for rel in reversed(by_depth):
            os.chmod(Path(real) / rel, tree.modes[rel])
        try:
            return [ask(q, real, tree.links) for q in tree.queries]
        finally:
            for rel in by_depth:
                os.chmod(Path(real) / rel, 0o700)
    finally:
        os.chdir(saved)


def generate() -> dict[str, bytes]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "fs-stat"
        for i, tree in enumerate(_trees()):
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(tree, root)
            shutil.rmtree(root)
            lines.append({**tree.record(), "q": answers})
    header = {"fixture": "fs_stat", "cases": len(lines)}
    return {"fs_stat.jsonl": jsonl([header, *lines])}
