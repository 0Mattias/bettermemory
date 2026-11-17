"""Fixtures: a git directory's format from its files, and the trust of a
``commondir``, as `gitfile_rule` reads them (``gitfile_trust.jsonl``,
``gitfile_trust_python.jsonl``).

Unit 3 (``native/.../gitfile_rule/trust.rs``) ports `gitfile_rule.tree_format`,
`_common_format`, `object_format_at`, `reads_worktree_config`,
`_tree_refusal`, `own_refusal`, `inside_reading`, `_common_of`,
`_commondir`, `_foreign_commondir`, `_registered` and `_is_git_directory`;
this pins each to the reference's own function on real trees.

Each case is a tree written in a fresh directory of the scratch world and
entered (``chdir``; its real path is the root), then each query answered
with the git on PATH replaced by data: ``"gv"`` the release
`_git_is_at_least` reads (default 2.54) and `_reads_compat` True. Lines:
a header, then one per case: the tree (``"f"`` files, a text with
``<root>`` for the case's directory, or ``{"x": char, "n": count}`` after
an optional ``"p"`` prefix, or ``{"hex": bytes}``; ``"d"``; ``"l"``;
``"m"`` modes), and ``"q"``: one answer a query ``op:args`` (split at
``|``, a path relative to the case's directory or under ``<root>``):

- ``f`` `tree_format(path)`: ``{"why": text}`` or ``{"wt": path, "bare": b}``;
- ``o`` `own_refusal(dotgit, directory)` and ``I`` `inside_reading(path)`:
  a refusal is ``{"why": text, "u": FormatUnknown}``;
- ``w`` `reads_worktree_config(common)`, ``a`` `object_format_at(common)`;
- ``c`` `_common_of(path)` with ``GIT_COMMON_DIR`` the second argument
  (absent: unset); ``n`` `_commondir(path)`; ``F``
  `_foreign_commondir(path)`; ``r`` `_registered(path, common)`; ``i``
  `_is_git_directory(path)` with ``GIT_COMMON_DIR`` likewise.

A path or text answered is spelled with the case's directory as
``<scratch>`` (one over 256 bytes as ``{"n": length, "sha256": digest}``). An exception is ``{"e": class}`` with ``"n"`` the errno's
name for an OSError.

Which file a query goes in is decided by its input: an ``f``, ``o`` or
``I`` query (whose `_git_common` resolves strictly) of a tree whose
links, query paths or ``commondir`` texts have an empty, ``.`` or ``..``
part goes in the Python-gated file (`gen_fs_realpath.strict_gated`).
Gates: POSIX only (links, modes). No FIFO or socket. Nothing depends on
root (`refuse_root`), the umask, the host's users, home, the
environment (``GIT_COMMON_DIR`` is set per query and cleared
otherwise), case folding, ``listdir``'s order or the scratch path.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import gitfile_rule as g

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

HEAD = "ref: refs/heads/main\n"


class Case(Tree):
    def __init__(self, queries: list[str], *, gv: str = "2.54", **kw: Any) -> None:
        super().__init__(queries, **kw)
        self.gv = gv

    def record(self) -> dict[str, Any]:
        out = super().record()
        if self.gv != "2.54":
            out["gv"] = self.gv
        return out


def _repo(name: str, config: str | None = None) -> dict[str, Any]:
    files: dict[str, Any] = {
        f"{name}/HEAD": HEAD,
        f"{name}/objects/.keep": "",
        f"{name}/refs/.keep": "",
    }
    if config is not None:
        files[f"{name}/config"] = config
    return files


def _fmt(name: str) -> list[str]:
    return [
        f"f:<root>/{name}",
        f"o:<root>/{name}|<root>",
        f"I:<root>/{name}",
        f"w:<root>/{name}",
        f"a:<root>/{name}",
    ]


FORMATS = {
    "none": None,
    "nover": "[core]\n\tworktree = <root>/elsewhere\n\tbare = true\n",
    "v0": "[core]\n\trepositoryformatversion = 0\n\tworktree = <root>/elsewhere\n",
    "v0here": "[core]\n\trepositoryformatversion = 0\n\tworktree = <root>\n",
    "v0rel": "[core]\n\trepositoryformatversion = 0\n\tworktree = ..\n",
    "v0bare": "[core]\n\trepositoryformatversion = 0\n\tbare = yes\n\tworktree = <root>\n",
    "v0badbare": "[core]\n\trepositoryformatversion = 0\n\tbare = maybe\n",
    "v0emptywt": "[core]\n\trepositoryformatversion = 0\n\tworktree =\n",
    "v0nowt": "[core]\n\trepositoryformatversion = 0\n\tworktree\n",
    "v0nul": "[core]\n\trepositoryformatversion = 0\n\tworktree = <root>\x00x\n",
    "v1": "[core]\n\trepositoryformatversion = 1\n\tworktree = <root>/elsewhere\n",
    "v2": "[core]\n\trepositoryformatversion = 2\n",
    "vneg": "[core]\n\trepositoryformatversion = -5\n\tworktree = <root>/elsewhere\n",
    "vhuge": "[core]\n\trepositoryformatversion = 99999999999999999999\n",
    "vk": "[core]\n\trepositoryformatversion = 1k\n",
    "vnan": "[core]\n\trepositoryformatversion = one\n",
    "vdup": "[core]\n\trepositoryformatversion = 2\n\trepositoryformatversion = 0\n",
    "vdup2": "[core]\n\trepositoryformatversion = 0\n\trepositoryformatversion = 2\n",
    "v0noop": "[core]\n\trepositoryformatversion = 0\n[extensions]\n\tnoop = x\n",
    "v0ofmt": "[core]\n\trepositoryformatversion = 0\n[extensions]\n\tobjectformat = sha256\n",
    "v1ofmt": "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n",
    "v1badofmt": "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = md5\n",
    "v1unknown": "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tfrobnicate = 1\n",
    "v0unknown": "[core]\n\trepositoryformatversion = 0\n[extensions]\n\tfrobnicate = 1\n",
    "noverext": "[extensions]\n\tobjectformat = sha256\n\tfrobnicate = 1\n",
    "v1wtc": "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tworktreeConfig = true\n",
    "v0wtc": "[core]\n\trepositoryformatversion = 0\n[extensions]\n\tworktreeconfig\n",
    "v0wtcbad": "[core]\n\trepositoryformatversion = 0\n[extensions]\n\tworktreeconfig = sure\n",
    "vnegwtc": "[core]\n\trepositoryformatversion = -2\n[extensions]\n\tworktreeconfig = true\n",
    "include": "[include]\n\tpath = more\n[core]\n\trepositoryformatversion = 0\n",
    "crlf": "[core]\r\n\trepositoryformatversion = 0\r\n\tworktree = <root>/elsewhere\r\n",
    "spaced": "[core]\n\trepositoryformatversion = 0\n\tworktree = <root>/a\tb\n",
    "unparsed": "[core\n\trepositoryformatversion = 0\n",
    "locked": "[core]\n\trepositoryformatversion = 0\n",
    "wtcbare": "[core]\n\trepositoryformatversion = 0\n[extensions]\n\tworktreeconfig\n",
    "wtcparse": "[core]\n\trepositoryformatversion = 0\n[extensions]\n\tworktreeconfig\n",
    "wtcnowt": "[core]\n\trepositoryformatversion = 0\n[extensions]\n\tworktreeconfig\n",
    "noverwtc": "[extensions]\n\tworktreeconfig = true\n",
    "v0nothing": "[core]\n\trepositoryformatversion = 0\n\tworktree = <root>/nothing\n",
    "ucd": "[core]\n\trepositoryformatversion = 0\n",
    "vneg2ofmt": "[core]\n\trepositoryformatversion = -2\n[extensions]\n\tobjectformat = sha256\n",
    "vneg3ofmt": "[core]\n\trepositoryformatversion = -3\n[extensions]\n\tobjectformat = sha256\n",
    "vneg1ofmt": "[core]\n\trepositoryformatversion = -1\n[extensions]\n\tobjectformat = sha256\n",
    "bare/.git": "[core]\n\trepositoryformatversion = 0\n\tbare = true\n",
    "x.git": "[core]\n\trepositoryformatversion = 0\n",
    "bare.git": "[core]\n\trepositoryformatversion = 0\n\tbare = true\n",
}


def _trees() -> Iterator[Case]:
    files: dict[str, Any] = {}
    queries: list[str] = []
    for name, config in FORMATS.items():
        files |= _repo(name, config)
        queries += _fmt(name)
    files |= {
        "include/more": "[core]\n\trepositoryformatversion = 2\n\tworktree = /x\n",
        "v1wtc/config.worktree": "[core]\n\tworktree = <root>\n\tbare = false\n",
        "v0wtc/config.worktree": "[core]\n\tbare = true\n",
        "v0wtcbad/config.worktree": "[core]\n\tbare = true\n",
        "vnegwtc/config.worktree": "[core]\n\tbare = true\n",
        "wtcbare/config.worktree": "[core]\n\tbare = maybe\n",
        "wtcparse/config.worktree": "[core\n",
        "wtcnowt/config.worktree": "[core]\n\tworktree\n",
        "ucd/commondir": "",
        "elsewhere/x": "",
        "dirconf/HEAD": HEAD,
        "dirconf/config/x": "",
        "a\tb/x": "",
    }
    queries += _fmt("dirconf") + [
        "f:none",
        "o:none|.",
        "I:<root>/.git",
        "f:unparsed",
        "f:locked",
    ]
    files |= _repo(".git", "[core]\n\trepositoryformatversion = 0\n")
    # A linked worktree's git directory: its own config.worktree under
    # worktreeConfig in the common config, and a commondir by an absolute
    # path with no dot part (the strict walk's ungated shape).
    files |= _repo(
        "main",
        "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tworktreeconfig = true\n",
    )
    files |= {
        "main/worktrees/wt/HEAD": HEAD,
        "main/worktrees/wt/commondir": "<root>/main\n",
        "main/worktrees/wt/config.worktree": "[core]\n\tworktree = <root>/elsewhere\n",
        "main/worktrees/wt2/HEAD": HEAD,
        "main/worktrees/wt2/commondir": "<root>/main",
    }
    files |= _repo(
        "plain", "[core]\n\trepositoryformatversion = 0\n\tworktree = <root>\n"
    )
    files |= {
        "plain/worktrees/p/HEAD": HEAD,
        "plain/worktrees/p/commondir": "<root>/plain",
        "plain/worktrees/p/config.worktree": "[core]\n\tworktree = <root>\n",
    }
    queries += [
        "f:<root>/main/worktrees/wt",
        "o:<root>/main/worktrees/wt|<root>/elsewhere",
        "f:<root>/main/worktrees/wt2",
        "f:<root>/plain/worktrees/p",
        "I:<root>/main/worktrees/wt",
    ]
    yield Case(queries, files=files, modes={"locked/config": 0o000})
    # A git before 2.45 reads a tab in core.worktree as a space; one before
    # 2.42 skips config.worktree below format -1.
    files = (
        _repo("spaced", FORMATS["spaced"])
        | _repo("vnegwtc", FORMATS["vnegwtc"])
        | {"vnegwtc/config.worktree": "[core]\n\tbare = true\n"}
    )
    yield Case(
        ["f:<root>/spaced", "w:<root>/vnegwtc", "f:<root>/vnegwtc"],
        files=files,
        gv="2.41",
    )
    yield Case(
        ["f:<root>/spaced", "w:<root>/vnegwtc", "a:<root>/spaced"],
        files=files,
        gv="none",
    )
    # The trust of a commondir.
    files = _repo("main") | _repo("other")
    files |= {
        "main/worktrees/wt/HEAD": HEAD,
        "main/worktrees/wt/commondir": "../..\n",
        "main/worktrees/abs/HEAD": HEAD,
        "main/worktrees/abs/commondir": "<root>/main",
        "main/worktrees/ws/HEAD": HEAD,
        "main/worktrees/ws/commondir": "  ../..  \t\n\n",
        "main/worktrees/crlf/HEAD": HEAD,
        "main/worktrees/crlf/commondir": "../..\r\n",
        "main/worktrees/link/HEAD": HEAD,
        "main/worktrees/link/commondir": "<root>/lmain",
        "main/worktrees/dots/HEAD": HEAD,
        "main/worktrees/dots/commondir": "../../../main/./",
        "main/worktrees/self/HEAD": HEAD,
        "main/worktrees/self/commondir": ".",
        "main/worktrees/empty/HEAD": HEAD,
        "main/worktrees/empty/commondir": " \n",
        "main/worktrees/file/HEAD": HEAD,
        "main/worktrees/file/commondir": "../../HEAD",
        "main/worktrees/none/HEAD": HEAD,
        "main/worktrees/none/commondir": "../../nothing",
        "main/worktrees/big/HEAD": HEAD,
        "main/worktrees/big/commondir": {"p": "../..", "x": "/", "n": (1 << 20) + 10},
        "main/worktrees/bigx/HEAD": HEAD,
        "main/worktrees/bigx/commondir": {"p": "../../", "x": "a", "n": (1 << 20) + 10},
        "main/worktrees/utf/HEAD": HEAD,
        "main/worktrees/utf/commondir": {"hex": "2e2e2fff2e2e"},
        "main/worktrees/dir/HEAD": HEAD,
        "main/worktrees/dir/commondir/x": "",
        "main/worktrees/locked/HEAD": HEAD,
        "main/worktrees/locked/commondir": "../..",
        "forged/HEAD": HEAD,
        "forged/commondir": "<root>/main",
        "forgedrel/HEAD": HEAD,
        "forgedrel/commondir": "../main",
        "forgedbare/HEAD": HEAD,
        "forgedbare/commondir": "<root>/elsewhere",
        "elsewhere/x": "",
        "nohead/commondir": "<root>/main",
        "headlink/objects/x": "",
        "headlink/refs/x": "",
        "headdangle/objects/x": "",
        "headdangle/refs/x": "",
        "objonly/objects/x": "",
        "toobj/HEAD": HEAD,
        "toobj/commondir": "<root>/objonly",
        "nohead/objects/x": "",
    }
    names = [
        "main",
        "main/worktrees/wt",
        "main/worktrees/abs",
        "main/worktrees/ws",
        "main/worktrees/crlf",
        "main/worktrees/link",
        "main/worktrees/dots",
        "main/worktrees/self",
        "main/worktrees/empty",
        "main/worktrees/file",
        "main/worktrees/none",
        "main/worktrees/big",
        "main/worktrees/bigx",
        "main/worktrees/utf",
        "main/worktrees/dir",
        "main/worktrees/locked",
        "forged",
        "forgedrel",
        "forgedbare",
        "nohead",
        "toobj",
        "headlink",
        "headdangle",
        "missing",
    ]
    queries = []
    for n in names:
        queries += [f"n:<root>/{n}", f"c:<root>/{n}", f"F:<root>/{n}", f"i:<root>/{n}"]
    queries += [
        "n:main/worktrees/wt",
        "c:main/worktrees/wt",
        "i:main/worktrees/wt",
        "F:forgedrel",
        "r:<root>/main/worktrees/wt|<root>/main",
        "r:<root>/main/worktrees/wt|<root>/lmain",
        "r:<root>/lwt|<root>/main",
        "r:<root>/forged|<root>/main",
        "r:<root>/main/worktrees/wt|<root>/other",
        "r:<root>/main/worktrees/wt|<root>/missing",
        "r:<root>/a\x00|<root>/main",
        "c:<root>/main/worktrees/wt|../..",
        "c:<root>/main/worktrees/wt|<root>/other",
        "c:<root>/forged|<root>/other",
        "c:<root>/forged|",
        "c:<root>/forged|rel",
        "c:main/worktrees/wt|../..",
        "i:<root>/forged|<root>/main",
        "i:<root>/forged|<root>/elsewhere",
        "i:<root>/forged|<root>/objonly",
        "i:<root>/main/worktrees/wt|",
        "n:<root>/a\x00b",
        "c:<root>/a\x00b",
        "F:<root>/a\x00b",
        "i:<root>/a\x00b",
    ]
    yield Case(
        queries,
        files=files,
        links={
            "lmain": "main",
            "lwt": "main/worktrees/wt",
            "headlink/HEAD": "../main/HEAD",
            "headdangle/HEAD": "nothing",
        },
        modes={"main/worktrees/locked/commondir": 0o000},
    )
    # The strict walk of `_git_common` over a commondir with dot parts
    # (gated by input), and the format it then reads.
    files = _repo(
        "main",
        "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tworktreeconfig = true\n",
    )
    files |= {
        "main/worktrees/wt/HEAD": HEAD,
        "main/worktrees/wt/commondir": "../..\n",
        "main/worktrees/wt/config.worktree": "[core]\n\tworktree = <root>/elsewhere\n",
        "elsewhere/x": "",
    }
    yield Case(
        [
            "f:<root>/main/worktrees/wt",
            "o:<root>/main/worktrees/wt|<root>/elsewhere",
            "I:<root>/main/worktrees/wt",
        ],
        files=files,
    )


def _text(value: Any) -> bytes:
    if isinstance(value, dict):
        if "hex" in value:
            return bytes.fromhex(value["hex"])
        return (value.get("p", "") + str(value["x"]) * int(value["n"])).encode()
    return str(value).encode("utf-8", "surrogateescape")


def _spell(text: str, real: str) -> Any:
    data = text.replace(real, "<scratch>").encode("utf-8", "surrogateescape")
    if len(data) > 256:
        return {"n": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return _b(data)


def _call(fn: Any, real: str) -> Any:
    try:
        out = fn()
    except (OSError, ValueError) as exc:
        return _error(exc)
    return _shown(out, real)


def _shown(out: Any, real: str) -> Any:
    if isinstance(out, g.TreeFormat):
        if out.why is not None:
            return {"why": _spell(out.why, real)}
        return {
            "wt": None if out.worktree is None else _spell(out.worktree, real),
            "bare": out.bare,
        }
    if isinstance(out, str):
        return _spell(out, real)
    if isinstance(out, bytes):
        return _b(out)
    if isinstance(out, tuple):
        return [_shown(x, real) for x in out]
    return out


def _refusal(out: str | None, real: str) -> Any:
    if out is None:
        return None
    return {"why": _spell(out, real), "u": isinstance(out, g.FormatUnknown)}


def _query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    op, _, raw = query.partition(":")
    args = [a.replace("<root>", real) for a in raw.split("|")]
    for a in args:
        check_length(a, links, real)
        check_parent_of_file(a)
    env = args[1] if len(args) > 1 and op in "ci" else None
    out: dict[str, Any] = {"p": _b(query.encode("utf-8", "surrogateescape"))}
    with patch.dict(os.environ):
        os.environ.pop("GIT_COMMON_DIR", None)
        if env is not None:
            os.environ["GIT_COMMON_DIR"] = env
        p = args[0]
        if op == "f":
            out["v"] = _call(lambda: g.tree_format(p), real)
        elif op == "o":
            out["v"] = _call(lambda: _refusal(g.own_refusal(p, args[1]), real), real)
        elif op == "I":

            def inside() -> Any:
                why, root = g.inside_reading(p)
                return [
                    _refusal(why, real),
                    None if root is None else _spell(root, real),
                ]

            out["v"] = _call(inside, real)
        elif op == "w":
            out["v"] = _call(lambda: g.reads_worktree_config(p), real)
        elif op == "a":
            out["v"] = _call(lambda: g.object_format_at(p), real)
        elif op == "c":
            out["v"] = _call(lambda: g._common_of(p), real)
        elif op == "n":
            out["v"] = _call(lambda: g._commondir(p), real)
        elif op == "F":
            out["v"] = _call(lambda: g._foreign_commondir(p), real)
        elif op == "r":
            out["v"] = _call(lambda: g._registered(p, args[1]), real)
        elif op == "i":
            out["v"] = _call(lambda: g._is_git_directory(p), real)
        else:
            raise ValueError(op)
    return out


def _release(gv: str) -> tuple[int, int] | None:
    if gv == "none":
        return None
    major, minor = gv.split(".")
    return int(major), int(minor)


def _answer(tree: Case, root: Path) -> list[dict[str, Any]]:
    os.chmod(root, 0o755)
    saved = os.getcwd()
    os.chdir(root)
    try:
        real = os.getcwd()
        base = Path(real)
        for d in tree.dirs:
            (base / d).mkdir(parents=True, exist_ok=True)
        for rel, value in tree.files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_text(value).replace(b"<root>", os.fsencode(real)))
            os.chmod(path, 0o644)
        for rel, target in tree.links.items():
            os.symlink(target.replace("<root>", real), base / rel)
        for rel in sorted(tree.modes, key=lambda r: -r.count("/")):
            os.chmod(base / rel, tree.modes[rel])
        release = _release(tree.gv)

        def at_least(want: tuple[int, int]) -> bool | None:
            return None if release is None else release >= want

        try:
            with (
                patch.object(g, "_git_is_at_least", at_least),
                patch.object(g, "_reads_compat", lambda: True),
            ):
                return [_query(q, real, tree.links) for q in tree.queries]
        finally:
            for rel in tree.modes:
                os.chmod(base / rel, 0o700)
    finally:
        os.chdir(saved)


def _commondir_texts(tree: Tree) -> list[str]:
    return [
        _text(v).decode("utf-8", "replace").strip()
        for k, v in tree.files.items()
        if k.endswith("commondir") and not isinstance(v, dict)
    ]


def gated(query: str, tree: Tree) -> bool:
    """Whether a query goes in the Python-gated file, by its input."""
    op, _, raw = query.partition(":")
    if op not in "foI":
        return False
    probe = Tree(raw.split("|")[:1], links=tree.links, loop=tree.loop)
    if strict_gated(raw.split("|")[0], probe):
        return True
    return any(
        strict_gated(t, Tree([t])) for t in _commondir_texts(tree) if t and t[0] != "<"
    )


def _lines(want_gated: bool) -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "gitfile-trust"
        for i, tree in enumerate(_trees()):
            queries = [q for q in tree.queries if gated(q, tree) == want_gated]
            if not queries:
                continue
            asked = Case(
                queries,
                files=tree.files,
                dirs=tree.dirs,
                links=tree.links,
                modes=tree.modes,
                gv=tree.gv,
            )
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(asked, root)
            shutil.rmtree(root)
            lines.append({**asked.record(), "q": answers})
    return lines


def generate() -> dict[str, bytes]:
    lines = _lines(False)
    header = {"fixture": "gitfile_trust", "cases": len(lines)}
    return {"gitfile_trust.jsonl": jsonl([header, *lines])}


def generate_python() -> dict[str, bytes]:
    lines = _lines(True)
    header = {"fixture": "gitfile_trust_python", "cases": len(lines)}
    return {"gitfile_trust_python.jsonl": jsonl([header, *lines])}
