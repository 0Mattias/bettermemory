"""Fixtures: `os.path.realpath` and `os.path.abspath` of paths in real trees
(``fs_realpath.jsonl``, ``fs_realpath_python.jsonl``), and
`os.path.realpath(p, strict=True)` of the same trees
(``fs_realpath_strict.jsonl``, ``fs_realpath_strict_python.jsonl``: ``s``
the answer, spelled as ``r``, or the error's class and errno name; a query
goes in the Python-gated file by its input, `strict_gated`).

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
about), or on a path of 1,024 to 4,095 bytes as given or as its links
are followed (macOS refuses one, Linux reads it: `check_length` refuses
to write one). Run as root a
mode-000 folder is searchable, so the ``locked`` tree would answer
otherwise: `refuse_root` stops the generator there (no other POSIX-only
generator has a convention for root; CI runs none as root). No path reaches the scratch directory's parents:
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
        hard: dict[str, str] | None = None,
    ) -> None:
        self.queries = queries
        self.files = files or {}
        self.dirs = dirs
        self.links = links or {}
        self.fifos = fifos
        self.modes = modes or {}
        #: Hard links: a new name to an existing file (`gen_fs_samestat`).
        self.hard = hard or {}
        #: The tree holds a link loop (declared, not observed).
        self.loop = loop

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"f": dict(self.files)}
        for key, value in (
            ("d", list(self.dirs)),
            ("l", dict(self.links)),
            ("p", list(self.fifos)),
            ("m", {k: f"{v:03o}" for k, v in self.modes.items()}),
            ("h", dict(self.hard)),
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


def _answer(tree: Tree, root: Path, *, strict: bool = False) -> list[dict[str, Any]]:
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
            if strict:
                return [_strict_query(q, real, tree.links) for q in tree.queries]
            return [_query(q, real, tree.links) for q in tree.queries]
        finally:
            for rel in by_depth:
                os.chmod(Path(real) / rel, 0o700)
    finally:
        os.chdir(saved)


def _strict_query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    path = query.replace("<root>", real)
    check_length(path, links, real)
    return {"p": _b(query.encode("utf-8", "surrogateescape")), "s": _strict(path, real)}


def refuse_root() -> None:
    """Stop where the process runs as root, whose answers about a mode-000
    folder are not a user's."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError(
            "the POSIX file-system fixtures read mode-000 folders as a user does; "
            "do not generate them as root"
        )


#: macOS's PATH_MAX; Linux's is 4,096.
PATH_MAX = 1024


def check_length(
    path: str, links: dict[str, str] | None = None, real: str = ""
) -> None:
    """Refuse a query that is, or could become as its tree's links are
    followed, 1,024 bytes or more: macOS answers ENAMETOOLONG there (the
    path as given, or a link's target and the bytes after it, at 1,024
    bytes or more), Linux reads it up to 4,096, and the native model
    answers as macOS. Following a link at ``<root>/name`` puts its target
    in place of ``<root>/name``, so the path can grow by at most the
    target less ``len(real) + 1 + len(name)``; the bound adds that growth,
    where positive, for every link the query could reach (one whose name
    is a component of the query or of a target already counted), each
    once."""
    size = len(path.encode("utf-8", "surrogateescape"))
    root = len(real.encode("utf-8", "surrogateescape"))
    pending = [path]
    counted: set[str] = set()
    while pending:
        parts = set(pending.pop().split("/"))
        for name, target in (links or {}).items():
            if name in counted or name.rsplit("/", 1)[-1] not in parts:
                continue
            counted.add(name)
            spelled = target.replace("<root>", real)
            grows = len(spelled.encode("utf-8", "surrogateescape")) - root - 1
            size += max(0, grows - len(name.encode("utf-8", "surrogateescape")))
            pending.append(spelled)
    if size >= PATH_MAX:
        raise RuntimeError(
            f"a query of {size} bytes with its tree's links is at or past {PATH_MAX}: "
            "shorten it (cases past the limit are native tests on macOS alone)"
        )


def check_parent_of_file(path: str) -> None:
    """Refuse a query with ``..`` directly after a component that exists
    and is not a directory nor a link to one: macOS resolves ``x/..``
    after a file, FIFO or socket where Linux answers ENOTDIR (and the
    native model follows Linux), so a fixture made on macOS with such a
    path would differ on Linux. For the generators whose queries reach the
    kernel (`gen_fs_stat`, `gen_fs_access`); `realpath`'s own walk is
    Python's."""
    if "\x00" in path:
        return
    parts = path.split("/")
    for i, part in enumerate(parts):
        if part != ".." or i == 0:
            continue
        before = "/".join(parts[:i]) or "/"
        if os.path.lexists(before) and not os.path.isdir(before):
            raise RuntimeError(
                f"a query with '..' after {before!r}, which is not a directory: "
                "macOS resolves it and Linux answers ENOTDIR"
            )


def _query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    path = query.replace("<root>", real)
    check_length(path, links, real)

    def spelled(answer: str) -> str:
        return _b(answer.replace(real, "<scratch>").encode("utf-8", "surrogateescape"))

    out: dict[str, Any] = {"p": _b(query.encode("utf-8", "surrogateescape"))}
    for key, call in (("r", os.path.realpath), ("a", os.path.abspath)):
        try:
            out[key] = spelled(call(path))
        except (OSError, ValueError) as exc:
            out[key] = {"raised": type(exc).__name__}
    return out


def strict_gated(query: str, tree: Tree) -> bool:
    """Whether a strict query goes in the Python-gated file, by its input:
    a tree holding a loop, or a query or link target with an empty, ``.``
    or ``..`` part after its first. 3.13's strict walk raises ENOTDIR at
    a non-directory with any part after it, those included; 3.11's asks
    the OS, which never sees such a part (``f/..`` resolves there)."""
    if tree.loop:
        return True
    for text in [query, *tree.links.values()]:
        parts = text.split("/")
        if any(p in ("", ".", "..") for p in parts[1:]) or parts[0] in (".", ".."):
            return True
        if text in ("", "."):
            return True
    return False


def _strict(path: str, real: str) -> Any:
    from .gen_fs_stat import _error

    try:
        answer = os.path.realpath(path, strict=True)
    except (OSError, ValueError) as exc:
        return _error(exc)
    return _b(answer.replace(real, "<scratch>").encode("utf-8", "surrogateescape"))


def _strict_trees() -> Iterator[Tree]:
    """Every tree of the non-strict fixture, then cases asked strictly
    alone: a file, and a link to one, followed by ``/.`` (3.13 raises
    ENOTDIR there itself; 3.11 asks the OS, which never sees the ``.``)."""
    yield from _trees()
    yield Tree(
        ["<root>/f/.", "<root>/lf/.", "<root>/f/./."],
        files={"f": "x"},
        links={"lf": "f"},
    )


def _strict_lines(gated: bool) -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "fs-realpath-strict"
        for i, tree in enumerate(_strict_trees()):
            queries = [q for q in tree.queries if strict_gated(q, tree) == gated]
            if not queries:
                continue
            root = base / str(i)
            root.mkdir(parents=True)
            asked = Tree(
                queries,
                files=tree.files,
                dirs=tree.dirs,
                links=tree.links,
                fifos=tree.fifos,
                modes=tree.modes,
                loop=tree.loop,
            )
            answers = _answer(asked, root, strict=True)
            shutil.rmtree(root)
            lines.append({**asked.record(), "q": answers})
    return lines


def generate_strict() -> dict[str, bytes]:
    lines = _strict_lines(gated=False)
    header = {"fixture": "fs_realpath_strict", "cases": len(lines)}
    return {"fs_realpath_strict.jsonl": jsonl([header, *lines])}


def generate_strict_python() -> dict[str, bytes]:
    lines = _strict_lines(gated=True)
    header = {"fixture": "fs_realpath_strict_python", "cases": len(lines)}
    return {"fs_realpath_strict_python.jsonl": jsonl([header, *lines])}


def _lines(loop: bool) -> list[dict[str, Any]]:
    refuse_root()
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
