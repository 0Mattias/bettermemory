"""Fixtures: `symbols.check_symbol_citations` over parsed citations
(``symbols.jsonl``, ``symbols_platform.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/symbols.rs``. This pins it to
`check_symbol_citations` with `extract_symbol_citations` answering the
case's citations, and through it `_tier`, `_live_root`, `_resolve_module`
and `_module_bindings`.

Each case is a tree written in a fresh scratch directory ``@`` (resolved,
and the working directory while the case runs):

- ``f``: files, path to text, or ``{"x": hex}`` for bytes, or
  ``{"rep": text, "n": count, "head": text}`` for ``head`` and then
  ``text`` repeated ``count`` times; ``d``: directories; ``l``: symbolic
  links, path to target (``@`` at the start of a target stands for the
  case's directory); ``m``: owner modes (octal), set last;
- ``c``: the citations, each ``[symbol, module]``;
- ``w``: ``worktree_root`` (absent: None), with ``@`` at its start
  standing for the case's directory; ``a``: an anchor, ``[root,
  convicting]`` (absent: none), its root spelled as ``w``;
- ``n``: for each file that `Path.read_text` reads (with
  ``errors="replace"``), ``h`` the SHA-256 of that text in UTF-8 and
  ``v`` the names `_top_level_names` and `_all_names` answer of
  `ast.parse` of it, sorted, or null where it raises (null where the file
  cannot be read); a native test answers its own `ast.parse` by these,
  looking the text it is given up by its digest;
- ``r``: the report's `to_dict()`.

A text holding a lone surrogate is written as a list of code points.

Gates. ``symbols.jsonl`` holds no link, mode, FIFO or name that differs
only in case, and every module or root it names is either inside the
case's directory or refused before the file system is asked, so it
answers alike on every host and release. ``symbols_platform.jsonl`` holds
the trees with links, a FIFO, a mode-000 file and names that differ only
in case: it follows the host's file system (macOS folds case) and is POSIX
only (links and modes); it is made as a user, never as root. It is gated
on the Python release too: 3.11 and 3.12's `Path.resolve` raises
RuntimeError on a link loop, which leaves the checker (written
``{"raised": "RuntimeError"}``), where 3.13 resolves the loop as far as
it goes and the citation is unresolved.
"""

from __future__ import annotations

import ast
import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import symbols

from ._common import jsonl
from .gen_fs_realpath import refuse_root

MIB2 = 2 * 1024 * 1024


def _s(text: str) -> Any:
    if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        return [ord(c) for c in text]
    return text


def _content(spec: Any) -> bytes:
    if isinstance(spec, str):
        return spec.encode("utf-8")
    if "x" in spec:
        return bytes.fromhex(spec["x"])
    return (spec.get("head", "") + spec["rep"] * spec["n"]).encode("utf-8")


def _root(spelled: str | None, here: str) -> str | None:
    if spelled is None:
        return None
    return here + spelled[1:] if spelled.startswith("@") else spelled


def _names(path: Path) -> Any:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    digest = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return {"h": digest, "v": None}
    top = sorted(symbols._top_level_names(tree))
    every = sorted(symbols._all_names(tree))
    return {"h": digest, "v": [[_s(t) for t in top], [_s(t) for t in every]]}


Case = dict[str, Any]

BASE: dict[str, Any] = {
    "pkg/a.py": (
        "import os.path as osp\nfrom x import y as z\n"
        "class Store:\n    def mark(self, arg):\n        self.attr = 1\n"
        "def top():\n    local = 1\n    f(key=1)\n"
        "try:\n    import fast\nexcept ImportError as err:\n    fast = None\n"
        "if True:\n    COND = 1\n"
        "lam = lambda inner: inner\n"
    ),
    "pkg/b.py": "VALUE = 2\n",
    "pkg/sub/c.py": "def deep():\n    pass\n",
}


def _plain() -> Iterator[Case]:
    cite = [
        ["top", "pkg/a.py"],
        ["Store", "pkg/a.py"],
        ["mark", "pkg/a.py"],
        ["Store.mark", "pkg/a.py"],
        ["pkg.a.top", "pkg/a.py"],
        ["Store.nothing.mark", "pkg/a.py"],
        ["a.Store.b", "pkg/a.py"],
        ["nothing", "pkg/a.py"],
        ["self.attr", "pkg/a.py"],
        ["attr", "pkg/a.py"],
        ["key", "pkg/a.py"],
        ["arg", "pkg/a.py"],
        ["err", "pkg/a.py"],
        ["local", "pkg/a.py"],
        ["inner", "pkg/a.py"],
        ["os", "pkg/a.py"],
        ["osp", "pkg/a.py"],
        ["z", "pkg/a.py"],
        ["y", "pkg/a.py"],
        ["fast", "pkg/a.py"],
        ["COND", "pkg/a.py"],
        ["lam", "pkg/a.py"],
        ["", "pkg/a.py"],
        [".", "pkg/a.py"],
        ["top.", "pkg/a.py"],
        [".top", "pkg/a.py"],
        ["VALUE", "pkg/b.py"],
        ["deep", "pkg/sub/c.py"],
    ]
    yield {"f": BASE, "c": cite, "w": "@"}
    # No citation: the empty report whatever the root.
    yield {"f": BASE, "c": [], "w": "@"}
    yield {"f": BASE, "c": [], "w": "@/missing"}
    # No root, a missing root, a root that is a file, a NUL, a surrogate.
    one = [["top", "pkg/a.py"]]
    for root in (None, "@/missing", "@/pkg/a.py", "@/pk\0g", "@/\ud800", "@/\udcff"):
        case: Case = {"f": BASE, "c": one}
        if root is not None:
            case["w"] = root
        yield case
    # Roots spelled relative to the working directory, and with dots.
    for root in ("", ".", "pkg", "@/", "@/./", "@//", "@/pkg/..", "pkg/sub/../.."):
        yield {"f": BASE, "c": [["top", "pkg/a.py"], ["deep", "sub/c.py"]], "w": root}
    # A root holding the cited directory one level down.
    yield {"f": BASE, "c": [["deep", "sub/c.py"], ["top", "a.py"]], "w": "@/pkg"}
    # The module's spelling.
    modules = [
        "a.py",
        "pkg",
        "pkg/",
        "/pkg/a.py",
        "//pkg/a.py",
        "pkg/a.py/",
        "~/pkg/a.py",
        "~pkg/a.py",
        "\\pkg/a.py",
        "pkg\\a.py",
        "c:/pkg/a.py",
        "p:kg/a.py",
        ":/pkg/a.py",
        "pkg//a.py",
        "pkg/./a.py",
        "./pkg/a.py",
        "pkg/../pkg/a.py",
        "pkg/sub/..",
        "pkg/missing.py",
        "pkg/sub",
        "pkg/a.py\0",
        "pkg/\udcff.py",
        "pkg/\ud800.py",
        "pkg/.a.py",
    ]
    yield {"f": BASE, "c": [["top", m] for m in modules], "w": "@"}
    # One read per module: repeated modules and a repeated pair.
    yield {
        "f": BASE,
        "c": [
            ["top", "pkg/a.py"],
            ["nothing", "pkg/a.py"],
            ["top", "pkg/a.py"],
            ["x", "pkg/b.py"],
        ],
        "w": "@",
    }
    # More than the extraction's cap: the checker caps nothing.
    yield {"f": BASE, "c": [[f"n{i}", "pkg/a.py"] for i in range(12)], "w": "@"}
    # The anchor: its root first, its conviction.
    for anchor in (
        ["@", True],
        ["@", False],
        [None, True],
        [None, False],
        ["@/missing", True],
    ):
        for root in (None, "@", "@/missing"):
            case = {
                "f": BASE,
                "c": [
                    ["top", "pkg/a.py"],
                    ["nothing", "pkg/a.py"],
                    ["mark", "pkg/a.py"],
                    ["x", "q.py"],
                ],
                "a": anchor,
            }
            if root is not None:
                case["w"] = root
            yield case
    # Sizes: at the bound and one past it.
    yield {
        "f": {
            "pkg/at.py": {"head": "AT = 1\n", "rep": "#", "n": MIB2 - 7},
            "pkg/past.py": {"head": "PAST = 1\n", "rep": "#", "n": MIB2 - 8},
            "pkg/empty.py": "",
        },
        "c": [["AT", "pkg/at.py"], ["PAST", "pkg/past.py"], ["x", "pkg/empty.py"]],
        "w": "@",
    }
    # Bytes that are not UTF-8, newlines, a NUL, a BOM, a syntax error.
    yield {
        "f": {
            "pkg/bad.py": {"x": "64656620668128293a2070617373"},
            "pkg/badc.py": {"x": "78203d20310a2320ff0a"},
            "pkg/badn.py": {"x": "ff203d20310a"},
            "pkg/crlf.py": {"x": "78203d20310d0a79203d20320d"},
            "pkg/cr.py": {"x": "78203d20310d79203d20320d"},
            "pkg/nul.py": {"x": "78203d20310a00"},
            "pkg/bom.py": {"x": "efbbbf42203d20310a"},
            "pkg/syn.py": "def (:\n",
            "pkg/uni.py": "\ufb01 = 1\nclass \u00e9t\u00e9: pass\n",
            "pkg/nest.py": "x = " + "(" * 250 + ")" * 250 + "\n",
        },
        "c": [
            ["f", "pkg/bad.py"],
            ["x", "pkg/badc.py"],
            ["x", "pkg/badn.py"],
            ["\ufffd", "pkg/badn.py"],
            ["x", "pkg/crlf.py"],
            ["y", "pkg/crlf.py"],
            ["x", "pkg/cr.py"],
            ["x", "pkg/nul.py"],
            ["B", "pkg/bom.py"],
            ["x", "pkg/syn.py"],
            ["fi", "pkg/uni.py"],
            ["\ufb01", "pkg/uni.py"],
            ["\u00e9t\u00e9", "pkg/uni.py"],
            ["x", "pkg/nest.py"],
        ],
        "w": "@",
    }


def _platform() -> Iterator[Case]:
    tree = {
        "f": {**BASE, "out/o.py": "OUT = 1\n"},
        "d": ["root"],
        "l": {
            "root/pkg": "@/pkg",
            "pkg/in.py": "b.py",
            "pkg/out.py": "@/out/o.py",
            "pkg/outdir": "@/out",
            "pkg/indir": "sub",
            "pkg/dangling.py": "nowhere.py",
            "pkg/loop.py": "loop.py",
            "rootlink": "@/pkg/..",
            "filelink": "@/pkg/a.py",
        },
    }
    cite = [
        ["VALUE", "pkg/in.py"],
        ["OUT", "pkg/out.py"],
        ["OUT", "pkg/outdir/o.py"],
        ["deep", "pkg/indir/c.py"],
        ["x", "pkg/dangling.py"],
        ["x", "pkg/loop.py"],
        ["top", "PKG/A.py"],
        ["top", "pkg/A.PY"],
        ["VALUE", "Pkg/In.py"],
        ["top", "pkg/a.py."],
        ["top", "pkg/a.py "],
        ["top", "pkg/..."],
    ]
    for root in ("@", "@/rootlink", "@/root", "@/ROOT", "@/filelink"):
        yield {**tree, "c": cite, "w": root}
    # The resolved root is above the link: `root/pkg` leads out of `root`.
    yield {**tree, "c": [["top", "pkg/a.py"]], "w": "@/root"}
    # A FIFO is no regular file; a mode-000 file is one that cannot be read.
    yield {
        "f": {**BASE, "pkg/locked.py": "L = 1\n"},
        "p": ["pkg/fifo.py"],
        "m": {"pkg/locked.py": 0},
        "c": [["x", "pkg/fifo.py"], ["L", "pkg/locked.py"]],
        "w": "@",
    }


def _make(case: Case, here: Path) -> None:
    for d in case.get("d", ()):
        (here / d).mkdir(parents=True, exist_ok=True)
    for rel, spec in case["f"].items():
        path = here / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_content(spec))
    for rel, target in case.get("l", {}).items():
        path = here / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(here) + target[1:] if target.startswith("@") else target, path)
    for rel in case.get("p", ()):
        os.mkfifo(here / rel)  # type: ignore[attr-defined,unused-ignore]
    for rel, mode in case.get("m", {}).items():
        os.chmod(here / rel, mode)


def _record(case: Case) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("d", "l", "p", "w"):
        if key in case:
            out[key] = case[key] if key != "w" else _s(case[key])
    files: dict[str, Any] = {}
    for rel, spec in case["f"].items():
        if isinstance(spec, dict) and "rep" in spec and spec["n"] < 64:
            spec = spec["head"] + spec["rep"] * spec["n"]
        files[rel] = spec
    out["f"] = files
    if "m" in case:
        out["m"] = {k: f"{v:03o}" for k, v in case["m"].items()}
    out["c"] = [[_s(s), _s(m)] for s, m in case["c"]]
    if "a" in case:
        root, convicting = case["a"]
        out["a"] = [None if root is None else _s(root), convicting]
    return out


def _answer(case: Case, here: Path) -> dict[str, Any]:
    _make(case, here)
    names = {rel: _names(here / rel) for rel in case["f"]}
    saved = os.getcwd()
    os.chdir(here)
    try:
        real = os.getcwd()
        anchor = None
        if "a" in case:
            root, convicting = case["a"]
            anchor = SimpleNamespace(root=_root(root, real), convicting=convicting)
        cites = tuple((s, m) for s, m in case["c"])
        answer: Any
        with mock.patch.object(symbols, "extract_symbol_citations", return_value=cites):
            try:
                report = symbols.check_symbol_citations(
                    "body",
                    worktree_root=_root(case.get("w"), real),
                    anchor=anchor,  # type: ignore[arg-type]
                )
                answer = {k: [_s(t) for t in v] for k, v in report.to_dict().items()}
            except RuntimeError as exc:
                # 3.11 and 3.12's `Path.resolve` raises on a link loop,
                # out of the checker (3.13 resolves it as far as it goes).
                answer = {"raised": type(exc).__name__}
    finally:
        os.chdir(saved)
        for rel in case.get("m", {}):
            os.chmod(here / rel, 0o600)
    record = _record(case)
    record["n"] = names
    record["r"] = answer
    return record


def _lines(cases: list[Case]) -> list[dict[str, Any]]:
    out = []
    base = Path(os.path.realpath(tempfile.mkdtemp(prefix="bm-symbols-")))
    try:
        for i, case in enumerate(cases):
            here = base / str(i)
            here.mkdir()
            out.append(_answer(case, here))
    finally:
        shutil.rmtree(base)
    return out


def generate() -> dict[str, bytes]:
    lines = _lines(list(_plain()))
    header = {"fixture": "symbols", "cases": len(lines)}
    return {"symbols.jsonl": jsonl([header, *lines])}


def generate_platform() -> dict[str, bytes]:
    refuse_root()
    lines = _lines(list(_platform()))
    header = {"fixture": "symbols_platform", "cases": len(lines)}
    return {"symbols_platform.jsonl": jsonl([header, *lines])}
