"""Fixtures: git's own reading of a git directory, as `gitfile_rule`
reads it (``gitdir_read.jsonl``, ``gitdir_read_python.jsonl``).

Unit 2's first part (``native/.../gitfile_rule/gitdir.rs``) ports
`gitfile_rule._read`, `_real`, `_real_to_last`, `_valid_headref`,
`_git_common` and `_git_read` (1083-1287); this pins each to the
reference's own function on real trees.

Each case is a tree written in a fresh directory of the scratch world and
entered (``chdir``; its real path is the root), then each query answered.
Lines: a header, then one per case:

- ``"f"``: files, path to text, where ``<root>`` in a text stands for the
  case's directory, or ``{"x": char, "n": count}`` for a long one;
  ``"d"``: directories; ``"l"``: links (``<root>`` likewise); ``"p"``:
  FIFOs; ``"m"``: modes, set last (every entry is given its mode first, so
  nothing depends on the umask);
- ``"q"``: one answer a query ``op:path`` (``path`` relative to the case's
  directory, or under ``<root>``): ``r`` `_read(path, 8)`, ``n``
  `_read(path, 8, follow=False)` (the data spelled as a path is);
  ``R`` `_real(path)`, ``t`` `_real_to_last(path)`; ``h``
  `_valid_headref(path)` and with ``read=False``; ``g`` `_git_common(path)`
  and `_git_read(path)` (``[common, named]``, ``false`` where git dies,
  ``null`` where it is no git directory). A path answered is spelled with
  the case's directory as ``<scratch>``. An exception is ``{"e": class}``
  with ``"n"`` the errno's name for an OSError (``_Unsettled`` is the
  reference's own class).

Which file a query goes in is decided by its input: a resolving query
(``R``, ``t``, ``g``) whose path, a link target of its tree or a
``commondir`` text has an empty, ``.`` or ``..`` part after its first, or
whose tree holds a loop, goes in the Python-gated file (3.11's strict
``realpath`` asks the OS where 3.13 raises ENOTDIR itself;
`gen_fs_realpath.strict_gated`). Gates: POSIX only (links, FIFOs, modes,
``O_NOFOLLOW``). A placeholder (``SF_DATALESS``) cannot be made here: the
``fetch`` branch is held by reading only. Nothing depends on root
(`refuse_root`), the umask, the host's users, case folding, ``listdir``'s
order or the scratch path; the shared generator checks apply.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from bettermemory.gitfile_rule import (
    _git_common,
    _git_read,
    _read,
    _real,
    _real_to_last,
    _Unsettled,
    _valid_headref,
)

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import (
    Tree,
    _b,
    check_length,
    check_parent_of_file,
    refuse_root,
    strict_gated,
)
from .gen_fs_stat import _error

LIMIT = 8
HEAD = "ref: refs/heads/main\n"
HEX = "0123456789abcdefABCDEF0123456789abcdef01\n"


def _repo(
    name: str, *, head: str = HEAD, objects: bool = True, refs: bool = True
) -> tuple[dict[str, Any], list[str]]:
    files: dict[str, Any] = {f"{name}/HEAD": head}
    dirs = []
    if objects:
        dirs.append(f"{name}/objects")
    if refs:
        dirs.append(f"{name}/refs")
    return files, dirs


def _merge(
    *parts: tuple[dict[str, Any], list[str]],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    files: dict[str, Any] = {}
    dirs: list[str] = []
    for f, d in parts:
        files |= f
        dirs += d
    return files, tuple(dirs)


def _trees() -> Iterator[Tree]:
    # HEAD and the folders a git directory needs.
    files, dirs = _merge(
        _repo("ok"),
        _repo("hex", head=HEX),
        _repo("bad", head="garbage\n"),
        _repo("empty", head=""),
        _repo("spaced", head="ref:  \trefs/x\n"),
        _repo("refsonly", objects=False),
        _repo("objonly", refs=False),
        _repo("lockedobj"),
        _repo("noxobj"),
        _repo("lockedhead"),
        (
            {
                "dirhead/HEAD/x": "x",
                "linkhead/x": "",
                "badlink/x": "",
                "fifohead/x": "",
            },
            [
                "dirhead/objects",
                "dirhead/refs",
                "linkhead/objects",
                "linkhead/refs",
                "badlink/objects",
                "badlink/refs",
                "fifohead/objects",
                "fifohead/refs",
            ],
        ),
    )
    yield Tree(
        [
            "g:<root>/ok",
            "g:ok",
            "g:<root>/hex",
            "g:<root>/bad",
            "g:<root>/empty",
            "g:<root>/spaced",
            "g:<root>/refsonly",
            "g:<root>/objonly",
            "g:<root>/lockedobj",
            "g:<root>/noxobj",
            "g:<root>/lockedhead",
            "g:<root>/dirhead",
            "g:<root>/linkhead",
            "g:<root>/badlink",
            "g:<root>/fifohead",
            "g:<root>/lok",
            "g:<root>/missing",
            "g:<root>/ok/HEAD",
            "g:<root>/a\x00b",
            "h:<root>/ok/HEAD",
            "h:<root>/bad/HEAD",
            "h:<root>/linkhead/HEAD",
            "h:<root>/badlink/HEAD",
            "h:<root>/dirhead/HEAD",
            "h:<root>/fifohead/HEAD",
            "h:<root>/lockedhead/HEAD",
            "h:<root>/missing/HEAD",
            "h:<root>/lhead",
        ],
        files=files,
        dirs=dirs,
        links={
            "linkhead/HEAD": "refs/heads/main",
            "badlink/HEAD": "HEAD.lock",
            "lok": "ok",
            "lhead": "ok/HEAD",
        },
        fifos=("fifohead/HEAD",),
        modes={
            "lockedobj/objects": 0o000,
            "noxobj/objects": 0o600,
            "lockedhead/HEAD": 0o000,
        },
    )
    # `commondir`: absent, absolute, relative, missing at the last or a
    # middle name, empty, a NUL, over the bound, a folder, a FIFO, mode
    # 000, a dangling link, line ends.
    files, dirs = _merge(
        _repo("main"),
        _repo("wabs"),
        _repo("wrel"),
        _repo("wlast"),
        _repo("wmid"),
        _repo("wempty"),
        _repo("wnul"),
        _repo("wbig"),
        _repo("wdir"),
        _repo("wfifo"),
        _repo("wlocked"),
        _repo("wdangle"),
        _repo("wcrlf"),
        _repo("wlink"),
        _repo("wfile"),
    )
    files |= {
        "wabs/commondir": "<root>/main",
        "wrel/commondir": "sub",
        "wlast/commondir": "<root>/nothere",
        "wmid/commondir": "<root>/gone/x",
        "wempty/commondir": "",
        "wnul/commondir": "<root>/ma\x00in",
        "wbig/commondir": {"x": "a", "n": 65537},
        "wdir/commondir/x": "x",
        "wlocked/commondir": "<root>/main",
        "wcrlf/commondir": "<root>/main\r\n\n",
        "wlink/commondir": "<root>/lmain",
        "wfile/commondir": "<root>/main/HEAD/x",
    }
    dirs += ("wrel/sub/objects", "wrel/sub/refs")
    yield Tree(
        [
            f"g:<root>/{n}"
            for n in (
                "main",
                "wabs",
                "wrel",
                "wlast",
                "wmid",
                "wempty",
                "wnul",
                "wbig",
                "wdir",
                "wfifo",
                "wlocked",
                "wdangle",
                "wcrlf",
                "wlink",
                "wfile",
            )
        ]
        + ["g:wabs"],
        files=files,
        dirs=dirs,
        links={"wdangle/commondir": "nothing", "lmain": "main"},
        fifos=("wfifo/commondir",),
        modes={"wlocked/commondir": 0o000},
    )
    # `_read` and the resolutions on every kind.
    yield Tree(
        [
            "r:<root>/f",
            "r:f",
            "r:<root>/empty",
            "r:<root>/exact",
            "r:<root>/big",
            "r:<root>/d",
            "r:<root>/lf",
            "n:<root>/lf",
            "n:<root>/f",
            "n:<root>/ld",
            "n:<root>/d",
            "n:<root>/dangling",
            "r:<root>/dangling",
            "r:<root>/p",
            "n:<root>/p",
            "r:<root>/zero",
            "r:<root>/locked",
            "n:<root>/locked",
            "r:<root>/locked/f",
            "r:<root>/missing",
            "r:<root>/f/x",
            "r:",
            "r:<root>/a\x00b",
            "R:<root>/lf",
            "R:<root>/missing",
            "R:<root>/dangling",
            "R:<root>/locked/f",
            "R:<root>/f/x",
            "R:lf",
            "t:<root>/lf",
            "t:<root>/missing",
            "t:<root>/gone/x",
            "t:<root>/dangling",
            "t:<root>/f/x",
            "t:<root>/locked/f",
            "t:<root>/d/new",
            "t:<root>/ld/new",
            "t:new",
            "t:<root>/a\x00b",
        ],
        files={
            "f": "abc",
            "empty": "",
            "exact": "e" * (LIMIT + 1),
            "big": "b" * (4 * LIMIT),
            "zero": "0",
            "locked/f": "l",
            "d/g": "g",
        },
        dirs=("d", "locked"),
        links={"lf": "f", "ld": "d", "dangling": "nothing"},
        fifos=("p",),
        modes={"zero": 0o000, "locked": 0o000},
    )
    # Resolutions by `.`, `..` and trailing separators (gated by shape),
    # and a loop.
    files, dirs = _merge(_repo("main"), _repo("wt"))
    files |= {"wt/commondir": "../main", "f": "x"}
    yield Tree(
        [
            "g:<root>/wt",
            "g:<root>/./main",
            "t:<root>/d/../missing",
            "t:<root>/missing/",
            "t:<root>/missing/.",
            "t:<root>/f/.",
            "R:<root>/d/../f",
            "R:<root>/f/",
        ],
        files=files,
        dirs=(*dirs, "d"),
    )
    yield Tree(
        ["R:<root>/s", "t:<root>/s", "t:<root>/s/x", "g:<root>/s"],
        links={"s": "s"},
        loop=True,
    )


def _spell(answer: str, real: str) -> str:
    return _b(answer.replace(real, "<scratch>").encode("utf-8", "surrogateescape"))


def _raised(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, _Unsettled):
        return {"e": "_Unsettled"}
    return _error(exc)


def _call(fn: Any, real: str) -> Any:
    try:
        out = fn()
    except (OSError, ValueError, _Unsettled) as exc:
        return _raised(exc)
    if isinstance(out, bytes):
        return _b(out)
    if isinstance(out, str):
        return _spell(out, real)
    if isinstance(out, tuple):
        return [_spell(out[0], real), out[1]]
    return out


def _query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    op, _, raw = query.partition(":")
    path = raw.replace("<root>", real)
    check_length(path, links, real)
    check_parent_of_file(path)
    out: dict[str, Any] = {"p": _b(query.encode("utf-8", "surrogateescape"))}
    if op == "r":
        out["v"] = _call(lambda: _read(path, LIMIT), real)
    elif op == "n":
        out["v"] = _call(lambda: _read(path, LIMIT, follow=False), real)
    elif op == "R":
        out["v"] = _call(lambda: _real(path), real)
    elif op == "t":
        out["v"] = _call(lambda: _real_to_last(path), real)
    elif op == "h":
        out["v"] = _call(lambda: _valid_headref(path), real)
        out["u"] = _call(lambda: _valid_headref(path, read=False), real)
    elif op == "g":
        out["c"] = _call(lambda: _git_common(path), real)
        out["v"] = _call(lambda: _git_read(path), real)
    else:
        raise ValueError(op)
    return out


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value["x"]) * int(value["n"])
    return str(value)


def _answer(tree: Tree, root: Path) -> list[dict[str, Any]]:
    os.chmod(root, 0o755)
    saved = os.getcwd()
    os.chdir(root)
    try:
        real = os.getcwd()
        base = Path(real)
        for d in tree.dirs:
            (base / d).mkdir(parents=True, exist_ok=True)
            os.chmod(base / d, 0o755)
        for rel, value in tree.files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                _text(value).replace("<root>", real).encode("utf-8", "surrogateescape")
            )
            os.chmod(path, 0o644)
        for rel, target in tree.links.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target.replace("<root>", real), base / rel)
        for rel in tree.fifos:
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(base / rel)  # type: ignore[attr-defined,unused-ignore]
            os.chmod(base / rel, 0o644)
        by_depth = sorted(tree.modes, key=lambda rel: rel.count("/"))
        for rel in reversed(by_depth):
            os.chmod(base / rel, tree.modes[rel])
        try:
            return [_query(q, real, tree.links) for q in tree.queries]
        finally:
            for rel in by_depth:
                os.chmod(base / rel, 0o700)
    finally:
        os.chdir(saved)


def gated(query: str, tree: Tree) -> bool:
    """Whether a query goes in the Python-gated file, by its input."""
    op, _, path = query.partition(":")
    if op not in ("R", "t", "g"):
        return False
    texts = [_text(v) for k, v in tree.files.items() if k.endswith("commondir")]
    probe = Tree([path], links=tree.links, loop=tree.loop)
    if strict_gated(path, probe):
        return True
    return any(strict_gated(t, Tree([t])) for t in texts if t and t[0] != "<")


def _lines(want_gated: bool) -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "gitdir-read"
        for i, tree in enumerate(_trees()):
            queries = [q for q in tree.queries if gated(q, tree) == want_gated]
            if not queries:
                continue
            asked = Tree(
                queries,
                files=tree.files,
                dirs=tree.dirs,
                links=tree.links,
                fifos=tree.fifos,
                modes=tree.modes,
                loop=tree.loop,
            )
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(asked, root)
            shutil.rmtree(root)
            lines.append({**asked.record(), "q": answers})
    return lines


def generate() -> dict[str, bytes]:
    lines = _lines(False)
    header = {"fixture": "gitdir_read", "cases": len(lines), "limit": LIMIT}
    return {"gitdir_read.jsonl": jsonl([header, *lines])}


def generate_python() -> dict[str, bytes]:
    lines = _lines(True)
    header = {"fixture": "gitdir_read_python", "cases": len(lines), "limit": LIMIT}
    return {"gitdir_read_python.jsonl": jsonl([header, *lines])}
