"""Fixtures: `os.path.realpath` and `os.path.abspath` of paths in real trees
(``fs_realpath.jsonl``, ``fs_realpath_python.jsonl``).

The native build reads paths through a file-system trait
(``native/crates/bettermemory-core/src/fsdata.rs``) and resolves them with
one walk over its ``lstat`` and ``readlink`` (``fsdata/realpath.rs``);
this pins that walk to the reference's own calls on a real tree.

Each case is a tree written in a fresh directory of the scratch world
(`_sandbox.world`), resolved from its real path (``os.getcwd()`` after a
``chdir`` there, so no link above it is in any answer), then each query
answered. The first line is a header (the count); then one line per case:

- ``"f"``: files, path (relative to the case's directory) to text;
  ``"d"``: directories; ``"l"``: symbolic links, path to target, where
  ``<root>`` in a target stands for the case's directory; ``"p"``: FIFOs;
  ``"m"``: owner modes (octal), set after everything is made;
- ``"q"``: the answers, one per query: ``p`` the query, its bytes as
  ``=`` and the text where printable ASCII without a backslash, else hex,
  with ``<root>`` standing for the case's directory; ``r``
  `os.path.realpath(p)` and ``a`` `os.path.abspath(p)`, run with the
  case's directory as the working directory, each spelled as ``p`` is
  with the case's directory written ``<scratch>``, or ``{"raised":
  class}``.

Which file a case goes in is decided by its tree, never by its answers: a
tree holding a link loop goes in ``fs_realpath_python.jsonl``, since
Python 3.11's ``realpath`` (a recursive walk) answers a loop otherwise
than 3.13's (an iterative one, the port's). Every other case answers
alike from 3.11 to 3.14 (checked under 3.11 and 3.13), and its chains
are short of 3.11's recursion limit.

Gates. Both files are POSIX only (`generate.POSIX_ONLY`): the trees hold
links and FIFOs, and the answers are ``posixpath``'s. The loop file is
gated on the Python release (`generate.PYTHON_GATED`). Nothing depends on
the link-hop limit (``realpath`` reads each link with ``readlink`` and
follows none through the OS, so a chain of 45 answers alike on macOS and
Linux), on case folding (no two names differ in case), on a name only
Linux stores (no name is made that is not UTF-8; one is only asked
about), or on root. No path reaches the scratch directory's parents:
every ``..`` stays below the case's directory. No FIFO is opened.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import _sandbox
from ._common import jsonl

NAME_MAX = 255


def _b(data: bytes) -> str:
    if all(0x20 <= b < 0x7F and b != 0x5C for b in data):
        return "=" + data.decode("ascii")
    return data.hex()


class Tree:
    def __init__(
        self,
        queries: list[str],
        *,
        files: dict[str, str] | None = None,
        dirs: tuple[str, ...] = (),
        links: dict[str, str] | None = None,
        fifos: tuple[str, ...] = (),
        modes: dict[str, int] | None = None,
        loop: bool = False,
    ) -> None:
        self.queries = queries
        self.files = files or {}
        self.dirs = dirs
        self.links = links or {}
        self.fifos = fifos
        self.modes = modes or {}
        #: The tree holds a link loop (declared, not observed).
        self.loop = loop

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"f": dict(self.files)}
        for key, value in (
            ("d", list(self.dirs)),
            ("l", dict(self.links)),
            ("p", list(self.fifos)),
            ("m", {k: f"{v:03o}" for k, v in self.modes.items()}),
        ):
            if value:
                out[key] = value
        return out


def _trees() -> Iterator[Tree]:
    yield Tree(
        [
            "<root>",
            "<root>/",
            "<root>//f",
            "<root>/./f",
            "<root>/d/../f",
            "<root>/d/e/../../d",
            "<root>/f/",
            "<root>/f/x",
            "<root>/f/..",
            "<root>/missing",
            "<root>/missing/a/../b",
            "<root>/d/g",
            "",
            ".",
            "f",
            "d/e/..",
            "./d//e/",
            "missing/x",
            "<root>/a\x00b",
            "<root>/\udcff\udcfe",
            "<root>/café",
            "/",
            "//",
            "///",
            "/dev/null",
            "/dev/null/x",
        ],
        files={"f": "x", "d/g": "y"},
        dirs=("d", "d/e"),
    )
    # Links to each kind, dangling, and through `.` and `..`.
    yield Tree(
        [
            "<root>/lf",
            "<root>/ld",
            "<root>/ld/g",
            "<root>/ld/..",
            "<root>/ld/../f",
            "<root>/le/..",
            "<root>/le/../g",
            "<root>/dangling",
            "<root>/dangling/x",
            "<root>/abs",
            "<root>/abs/g",
            "<root>/dot",
            "<root>/dot/f",
            "<root>/up/d",
            "<root>/lnull",
            "<root>/lslash/f",
            "<root>/d/rel",
            "<root>/d/rel/../f",
            "lf",
            "ld/e",
            "<root>/d/lp",
        ],
        files={"f": "x", "d/g": "y"},
        dirs=("d", "d/e", "sub"),
        links={
            "lf": "f",
            "ld": "d",
            "le": "d/e",
            "dangling": "nothing/here",
            "abs": "<root>/d",
            "dot": ".",
            "up": "sub/..",
            "lnull": "/dev/null",
            "lslash": "d/e/../../",
            "d/rel": "../ld",
            "d/lp": "../fifo",
        },
        fifos=("fifo",),
    )
    # A FIFO along the path and at its end: lstat alone, never opened.
    yield Tree(
        ["<root>/fifo", "<root>/fifo/", "<root>/fifo/x", "<root>/lfifo/x"],
        fifos=("fifo",),
        links={"lfifo": "fifo"},
    )
    # Chains: 10 relative links, 45 (past Linux's 40 and macOS's 32), and
    # absolute ones.
    chain = {f"c{i}": f"c{i + 1}" for i in range(45)}
    chain["c45"] = "end"
    yield Tree(
        ["<root>/c0", "<root>/c35", "<root>/c0/x", "c0"],
        files={"end": "e"},
        links=chain,
    )
    yield Tree(
        ["<root>/a0", "<root>/a0/g"],
        files={"d/g": "y"},
        dirs=("d",),
        links={f"a{i}": f"<root>/a{i + 1}" for i in range(10)} | {"a10": "<root>/d"},
    )
    # Permissions: a folder that cannot be searched along the path, at its
    # end, and a file of mode 000.
    yield Tree(
        [
            "<root>/locked",
            "<root>/locked/l",
            "<root>/locked/l/x",
            "<root>/open/zero",
            "<root>/lz",
        ],
        files={"locked/f": "x", "open/zero": "z"},
        dirs=("locked", "open"),
        links={"locked/l": "../open", "lz": "open/zero"},
        modes={"locked": 0o000, "open/zero": 0o000},
    )
    yield Tree(
        ["<root>/half/l", "<root>/half/l/zero"],
        files={"open/zero": "z"},
        dirs=("half", "open"),
        links={"half/l": "../open"},
        modes={"half": 0o600},
    )
    # A name at the length limit, and one past it.
    long = "n" * NAME_MAX
    yield Tree(
        [f"<root>/{long}", f"<root>/{long}n", f"<root>/l/{long}"],
        files={long: "x"},
        links={"l": "."},
    )
    # Loops of length 1 to 3, and a link into a loop.
    yield Tree(
        ["<root>/s", "<root>/s/x", "<root>/s/.."],
        links={"s": "s"},
        loop=True,
    )
    yield Tree(
        ["<root>/a", "<root>/b/x", "<root>/into/y"],
        links={"a": "b", "b": "a", "into": "a"},
        loop=True,
    )
    yield Tree(
        ["<root>/p", "<root>/q/../x", "<root>/r/"],
        links={"p": "q", "q": "r", "r": "<root>/p"},
        loop=True,
    )
    yield Tree(
        ["<root>/d/up/d/up/x"],
        dirs=("d",),
        links={"d/up": ".."},
        loop=True,
    )


def _answer(tree: Tree, root: Path) -> list[dict[str, Any]]:
    for d in tree.dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, text in tree.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    saved = os.getcwd()
    os.chdir(root)
    try:
        real = os.getcwd()
        for rel, target in tree.links.items():
            path = Path(real) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target.replace("<root>", real), path)
        for rel in tree.fifos:
            os.mkfifo(Path(real) / rel)  # type: ignore[attr-defined,unused-ignore]
        by_depth = sorted(tree.modes, key=lambda rel: rel.count("/"))
        for rel in reversed(by_depth):
            os.chmod(Path(real) / rel, tree.modes[rel])
        try:
            return [_query(q, real) for q in tree.queries]
        finally:
            for rel in by_depth:
                os.chmod(Path(real) / rel, 0o700)
    finally:
        os.chdir(saved)


def _query(query: str, real: str) -> dict[str, Any]:
    path = query.replace("<root>", real)

    def spelled(answer: str) -> str:
        return _b(answer.replace(real, "<scratch>").encode("utf-8", "surrogateescape"))

    out: dict[str, Any] = {"p": _b(query.encode("utf-8", "surrogateescape"))}
    for key, call in (("r", os.path.realpath), ("a", os.path.abspath)):
        try:
            out[key] = spelled(call(path))
        except (OSError, ValueError) as exc:
            out[key] = {"raised": type(exc).__name__}
    return out


def _lines(loop: bool) -> list[dict[str, Any]]:
    trees = [t for t in _trees() if t.loop == loop]
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "fs-realpath"
        for i, tree in enumerate(trees):
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(tree, root)
            shutil.rmtree(root)
            lines.append({**tree.record(), "q": answers})
    return lines


def generate() -> dict[str, bytes]:
    lines = _lines(loop=False)
    header = {"fixture": "fs_realpath", "cases": len(lines)}
    return {"fs_realpath.jsonl": jsonl([header, *lines])}


def generate_python() -> dict[str, bytes]:
    lines = _lines(loop=True)
    header = {"fixture": "fs_realpath_python", "cases": len(lines)}
    return {"fs_realpath_python.jsonl": jsonl([header, *lines])}
