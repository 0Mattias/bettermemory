"""Fixtures: the pin records and the follow of a ``.git`` entry, as
`gitfile_rule` reads them (``gitfile_pin_text.jsonl``,
``gitfile_follow.jsonl``, ``gitfile_follow_python.jsonl``).

Unit 4 (``native/.../gitfile_rule/follow.rs``) ports `gitfile_rule.gitfile_refusal`,
`_refusal`, `_recorded_reading`, `_spells`, `_read_recorded`,
`earliest_naming`, `_view`, `names_git_dir` and `_entry_names`, with the
record store as data (`gitdir_pin.recorded` and `_read` once the record's
file is found).

``gitfile_pin_text.jsonl`` (ungated, every platform): a record file's
bytes (``"b"``, hex, or a long one by shape: ``{"pad": n}``,
``{"digits": n}``, `long_text`) and what `gitdir_pin._read` makes of them: ``null``
(not a record) or ``[git_dir, tree]``, each the `str` encoded UTF-8 with
``surrogatepass`` (hex), so a lone surrogate is kept. It is a function of
the bytes alone.

``gitfile_follow.jsonl`` and ``gitfile_follow_python.jsonl`` (POSIX):
each case is a tree written in a fresh directory of the scratch world and
entered (``chdir``; its real path is the root), with the record store a
folder of the scratch world (`gitdir_pin.pins_dir` patched), the git on
PATH as data (release 2.54, `_reads_compat` True), ``GIT_COMMON_DIR``
unset and the read budget raised so a slow disk cannot time a read out.
Lines: the tree (``"f"`` files, a text with ``<root>``, or ``{"hex":
bytes}``; ``"d"``; ``"l"``; ``"m"`` modes; ``"r"`` records (a text, ``{"hex": bytes}`` or
``{"nest": n}``, ``n`` brackets opened and closed): git
directory to the record file's text, written where `gitdir_pin.put`
writes it), and ``"q"``: one answer a query ``op:args`` (split at ``|``):

- ``g:directory|target`` `_refusal(directory, target)`: ``[why, provisional]``,
  a refusal ``{"why": text, "u": FormatUnknown}``;
- ``e:entry|git_dir`` `_entry_names`;
- ``E:tree|git_dir|views`` `earliest_naming(views, tree, git_dir)`, the
  views split at ``;``, a tree of ``-`` None;
- ``p:git_dir`` `gitdir_pin.recorded`: ``[git_dir, tree]`` or null;
- ``s:recorded|directory`` `_spells`.

A path or text answered is spelled with the case's directory as
``<scratch>``. An exception is ``{"e": class}`` (``"n"`` the errno's
name for an OSError).

A query goes in the Python-gated file by its input: a ``g`` query of a
tree whose links, query paths or ``commondir`` texts have an empty, ``.``
or ``..`` part (`_git_common`'s strict walk; a commondir of ``..`` parts
alone climbs only through folders and is not gated), any query of a tree holding
a loop, and any query of a tree holding a record nested deeper than 500
(the JSON decoder's recursion limit differs between 3.11 and 3.13).
Gates: POSIX only (links, modes). No FIFO or socket. The ``{"digits":
n}`` row is read with the interpreter's default integer digit limit
(``sys.int_info.default_max_str_digits``) whatever the environment sets.
Nothing depends on root, the umask, the host's users, home, the environment, case folding,
``listdir``'s order or the scratch path.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import gitdir_pin
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
DEEP = 10_000


def _record(git_dir: str, tree: str) -> str:
    return f'{{"format": 1, "git_dir": "{git_dir}", "tree": "{tree}"}}\n'


# --- the record's own text --------------------------------------------------


def _texts() -> Iterator[bytes]:
    ok = _record("/g", "/t").encode()
    yield ok
    yield b"  \n" + ok + b"\n\t "
    yield b"\xef\xbb\xbf" + ok
    yield b"\xff\xfe" + ok.decode().encode("utf-16-le")
    yield b"\xfe\xff" + ok.decode().encode("utf-16-be")
    yield ok.decode().encode("utf-16-le")
    yield ok.decode().encode("utf-16-be")
    yield ok.decode().encode("utf-32")
    yield ok.decode().encode("utf-32-le")
    yield ok.decode().encode("utf-32-be")
    yield b'{"format": 1.0, "git_dir": "/g", "tree": "/t"}'
    yield b'{"format": true, "git_dir": "/g", "tree": "/t"}'
    yield b'{"format": "1", "git_dir": "/g", "tree": "/t"}'
    yield b'{"format": 2, "git_dir": "/g", "tree": "/t"}'
    yield b'{"format": 0, "git_dir": "/g", "tree": "/t"}'
    yield b'{"format": -1, "git_dir": "/g", "tree": "/t"}'
    yield b'{"format": 1e0, "git_dir": "/g", "tree": "/t"}'
    yield b'{"format": 10e-1, "git_dir": "/g", "tree": "/t"}'
    yield b'{"format": NaN, "git_dir": "/g", "tree": "/t"}'
    yield b'{"git_dir": "/g", "tree": "/t"}'
    yield b'{"format": 1, "git_dir": "g", "tree": "/t"}'
    yield b'{"format": 1, "git_dir": "/g", "tree": "t"}'
    yield b'{"format": 1, "git_dir": "/g", "tree": ""}'
    yield b'{"format": 1, "git_dir": "/g", "tree": null}'
    yield b'{"format": 1, "git_dir": "/g", "tree": ["/t"]}'
    yield b'{"format": 1, "git_dir": "/g"}'
    yield b'{"format": 1, "git_dir": "/g", "tree": "/t", "tree": "/u"}'
    yield b'{"format": 2, "format": 1, "git_dir": "/g", "tree": "/t"}'
    yield b'{"format": 1, "git_dir": "/g", "tree": "/t\\ud800"}'
    yield b'{"format": 1, "git_dir": "/g", "tree": "/t\\udcff"}'
    yield b'{"format": 1, "git_dir": "/g", "tree": "/t\\udc7f"}'
    yield b'{"format": 1, "git_dir": "/g\\u0000", "tree": "/t"}'
    yield b'{"format": 1, "git_dir": "/g", "tree": "/\\u00e9\\ud83d\\ude00"}'
    yield b'{"format": 1, "git_dir": "/g", "tree": "/\xc3\xa9"}'
    yield b'{"format": 1, "git_dir": "/g", "tree": "/\xff"}'
    yield b'{"format": 1, "git_dir": "/g", "tree": "/\xed\xa0\x80"}'
    yield b'["format", 1]'
    yield b"1"
    yield b"null"
    yield b""
    yield b"{"
    yield ok + b"x"
    yield b'{"format": 1, "git_dir": "/g", "tree": "/t", "x": [[[[[[[[[[1]]]]]]]]]]}'
    yield b"\x00\x00"
    yield b"\x00{"


#: Long texts, spelled by shape: ``{"pad": n}`` is the plain record with
#: ``n`` spaces before its last newline (65,536 bytes at most is read as
#: a record), ``{"digits": n}`` one with a member holding an ``n``-digit
#: integer.
LONG = [{"pad": 65536 - 45}, {"pad": 65536 - 44}, {"digits": 5000}]


def long_text(spec: dict[str, int]) -> bytes:
    ok = _record("/g", "/t").encode()
    if "pad" in spec:
        return ok[:-1] + b" " * spec["pad"] + b"\n"
    head = b'{"format": 1, "git_dir": "/g", "tree": "/t", "n": '
    return head + b"9" * spec["digits"] + b"}"


def _read_text(data: bytes, scratch: Path) -> Any:
    path = scratch / "record.json"
    path.write_bytes(data)
    try:
        pin = gitdir_pin._read(path)
    except (OSError, ValueError, RecursionError) as exc:
        return {"e": type(exc).__name__}
    if pin is None:
        return None
    return [
        pin.git_dir.encode("utf-8", "surrogatepass").hex(),
        pin.tree.encode("utf-8", "surrogatepass").hex(),
    ]


def generate_text() -> dict[str, bytes]:
    lines: list[dict[str, Any]] = []
    # `json.loads` refuses an integer past the digit limit, which
    # PYTHONINTMAXSTRDIGITS or -X int_max_str_digits move: the default
    # is the reference's limit.
    saved = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(sys.int_info.default_max_str_digits)
    try:
        lines = _text_lines()
    finally:
        sys.set_int_max_str_digits(saved)
    header = {"fixture": "gitfile_pin_text", "cases": len(lines)}
    return {"gitfile_pin_text.jsonl": jsonl([header, *lines])}


def _text_lines() -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        scratch = world.root / "pin-text"
        scratch.mkdir()
        for data in _texts():
            lines.append({"b": data.hex(), "v": _read_text(data, scratch)})
        for spec in LONG:
            lines.append({**spec, "v": _read_text(long_text(spec), scratch)})
    return lines


# --- trees ------------------------------------------------------------------


class Case(Tree):
    def __init__(
        self, queries: list[str], *, records: dict[str, Any] | None = None, **kw: Any
    ) -> None:
        super().__init__(queries, **kw)
        self.records = records or {}

    def record(self) -> dict[str, Any]:
        out = super().record()
        if self.records:
            out["r"] = dict(self.records)
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


V0 = "[core]\n\trepositoryformatversion = 0\n"


def _trees() -> Iterator[Case]:
    # A git directory kept apart (`--separate-git-dir`), and its records.
    files: dict[str, Any] = {}
    for name in (
        "sep",
        "sep2",
        "sep3",
        "sep4",
        "sep5",
        "sep6",
        "sep7",
        "sep8",
        "sep9",
        "sepu",
    ):
        files |= _repo(f"{name}.git", V0)
    files |= {
        "a/.git": "gitdir: <root>/sep.git\n",
        "a/f": "",
        "b/.git": "gitdir: <root>/sep.git\n",
        "c/.git": "gitdir: <root>/sep2.git\n",
        "moved/.git": "gitdir: <root>/sep2.git\n",
        "locked/inner/.git": "gitdir: <root>/sep3.git\n",
        "x3/.git": "gitdir: <root>/sep3.git\n",
        "x4/.git": "gitdir: <root>/sep4.git\n",
        "x5/.git": "gitdir: <root>/sep5.git\n",
        "x6/.git": "gitdir: <root>/sep6.git\n",
        "x7/.git": "gitdir: <root>/sep7.git\n",
        "x8/.git": "gitdir: <root>/sep8.git\n",
        "rewritten/.git": "gitdir: <root>/sep4.git\n",
        "x9/.git": "gitdir: <root>/sep9.git\n",
        "xu/.git": "gitdir: <root>/sepu.git\n",
    }
    records = {
        "sep.git": _record("<root>/sep.git", "<root>/a"),
        # The recorded tree's .git names another git directory now.
        "sep2.git": _record("<root>/sep2.git", "<root>/rewritten"),
        # The recorded tree sits in a folder that cannot be read.
        "sep3.git": _record("<root>/sep3.git", "<root>/locked/inner"),
        # A record whose git directory is another object.
        "sep4.git": _record("<root>/sep.git", "<root>/a"),
        # Damaged, another format, empty, not UTF-8.
        "sep5.git": "{not json",
        "sep6.git": '{"format": 2, "git_dir": "<root>/sep6.git", "tree": "<root>/b"}',
        "sep7.git": "",
        "sep8.git": {"hex": "7bff7d"},
        # The recorded tree spelled another way than the folder resolves.
        "sep9.git": _record("<root>/sep9.git", "<root>/x9/"),
        # A git directory the OS cannot be asked for (a lone surrogate).
        "sepu.git": _record("<root>/sepu.git\\ud800", "<root>/xu\\ud800"),
    }
    queries = [
        "g:<root>/a|<root>/sep.git",
        "g:<root>/la|<root>/sep.git",
        "g:<root>/b|<root>/sep.git",
        "g:a|sep.git",
        "g:<root>/moved|<root>/sep2.git",
        "g:<root>/c|<root>/sep2.git",
        "g:<root>/x3|<root>/sep3.git",
        "g:<root>/x4|<root>/sep4.git",
        "g:<root>/x5|<root>/sep5.git",
        "g:<root>/x6|<root>/sep6.git",
        "g:<root>/x7|<root>/sep7.git",
        "g:<root>/x8|<root>/sep8.git",
        "g:<root>/b|<root>/lsep",
        "g:<root>/x9|<root>/sep9.git",
        "g:<root>/b|<root>/sep9.git",
        "g:<root>/xu|<root>/sepu.git",
        "p:<root>/sepu.git",
        "s:<root>/la|<root>/la",
        "p:<root>/sep.git",
        "p:<root>/lsep",
        "p:<root>/sep2.git",
        "p:<root>/sep4.git",
        "p:<root>/sep5.git",
        "p:<root>/nothing",
        "s:<root>/a|<root>/a",
        "s:<root>/a|<root>/la",
        "s:<root>/a|a",
        "s:<root>/a/|<root>/a",
        "s:<root>/la|<root>/a",
        "E:<root>/b|<root>/sep.git|<root>/a;<root>/b",
        "E:<root>/b|<root>/sep.git|<root>/c;<root>/la;<root>/b",
        "E:<root>/b|<root>/sep.git|<root>/b;<root>/a",
        "E:-|<root>/sep.git|<root>/nothing;<root>/x3;<root>/a",
        "E:<root>/b|<root>/sep3.git|<root>/locked/inner;<root>/x3",
        "E:<root>/b|<root>/sep.git|",
    ]
    yield Case(
        queries,
        files=files,
        links={"la": "<root>/a", "lsep": "<root>/sep.git"},
        modes={"locked": 0o000},
        records=records,
    )
    # What a `.git` entry names: a file with a relative, absolute, `..`,
    # linked, looping-free, missing, unreadable or over-bound target; a
    # folder; a link to either; another working tree's own .git.
    files = _repo("g", V0) | _repo("other", V0)
    files |= {
        "abs/.git": "gitdir: <root>/g\n",
        "rel/.git": "gitdir: ../g\n",
        "dots/.git": "gitdir: <root>/x/../g\n",
        "viaLink/.git": "gitdir: <root>/lg\n",
        "missing/.git": "gitdir: <root>/nothing\n",
        "otherf/.git": "gitdir: <root>/other\n",
        "crlf/.git": "gitdir: <root>/g\r\n",
        "empty/.git": "",
        "junk/.git": "not a gitdir line\n",
        "big/.git": {"p": "gitdir: <root>/g", "x": " ", "n": (1 << 20) + 1},
        "edge/.git": {"p": "gitdir: /", "x": "/", "n": (1 << 20) - 9},
        "unread/.git": "gitdir: <root>/g\n",
        "lockdir/in/.git": "gitdir: <root>/g\n",
        "x/y": "",
        "tree/f": "",
        "tree2/f": "",
        "own/.git/HEAD": HEAD,
        "own/f": "",
    }
    queries = []
    for e in (
        "abs/.git",
        "rel/.git",
        "dots/.git",
        "viaLink/.git",
        "missing/.git",
        "otherf/.git",
        "crlf/.git",
        "empty/.git",
        "junk/.git",
        "big/.git",
        "edge/.git",
        "unread/.git",
        "lockdir/in/.git",
        "dirlink/.git",
        "dangle/.git",
        "nothing/.git",
        "g",
        "tree/.git",
        "tree2/.git",
        "fifo-free/.git",
        "w2/.git",
    ):
        queries.append(f"e:<root>/{e}|<root>/g")
    queries += [
        "e:rel/.git|g",
        "e:<root>/abs/.git|<root>/nothing",
        "e:<root>/abs/.git|g",
        "e:<root>/w2/.git|<root>/own/.git",
        "e:<root>/own/.git|<root>/own/.git",
    ]
    yield Case(
        queries,
        files=files,
        links={
            "lg": "<root>/g",
            "dirlink/.git": "<root>/g",
            "dangle/.git": "<root>/nothing",
            "tree/.git": "<root>/g",
            "tree2/.git": "<root>/tree/.git",
            "w2/.git": "<root>/own/.git",
        },
        modes={"unread/.git": 0o000, "lockdir": 0o000},
    )
    # A linked worktree (absolute commondir) and a submodule shape.
    files = _repo("main/.git", V0)
    files |= {
        "main/f": "",
        "main/.git/worktrees/wt/HEAD": HEAD,
        "main/.git/worktrees/wt/commondir": "<root>/main/.git\n",
        "main/.git/worktrees/wt/gitdir": "<root>/wt/.git\n",
        "main/.git/worktrees/gone/HEAD": HEAD,
        "main/.git/worktrees/gone/commondir": "<root>/main/.git\n",
        "main/.git/worktrees/gone/gitdir": "<root>/away/.git\n",
        "main/.git/worktrees/blank/HEAD": HEAD,
        "main/.git/worktrees/blank/commondir": "<root>/main/.git\n",
        "main/.git/worktrees/blank/gitdir": " \n",
        "main/.git/worktrees/nogitdir/HEAD": HEAD,
        "main/.git/worktrees/nogitdir/commondir": "<root>/main/.git\n",
        "wt/.git": "gitdir: <root>/main/.git/worktrees/wt\n",
        "wt/f": "",
        "elsewhere/f": "",
        "forged/HEAD": HEAD,
        "forged/commondir": "<root>/main/.git\n",
        "bare.git/HEAD": HEAD,
        "bare.git/objects/.keep": "",
        "bare.git/refs/.keep": "",
        "bare.git/config": "[core]\n\trepositoryformatversion = 0\n\tbare = true\n",
        "fmt/HEAD": HEAD,
        "fmt/objects/.keep": "",
        "fmt/refs/.keep": "",
        "fmt/config": "[core]\n\trepositoryformatversion = 2\n",
    }
    files |= _repo("super/.git/modules/sub", V0 + "\tworktree = <root>/super/sub\n")
    files |= {
        "super/sub/.git": "gitdir: <root>/super/.git/modules/sub\n",
        "super/sub/f": "",
    }
    files |= _repo("super/.git", V0)
    files |= _repo("super/.git/modules/moved", V0 + "\tworktree = <root>/super/gone\n")
    files |= _repo(
        "main2/.git",
        "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tworktreeconfig = true\n",
    )
    files |= {
        "main2/f": "",
        "main2/.git/worktrees/w3/HEAD": HEAD,
        "main2/.git/worktrees/w3/commondir": "<root>/main2/.git\n",
        "main2/.git/worktrees/w3/gitdir": "<root>/w3/.git\n",
        "main2/.git/worktrees/w3/config.worktree": "[core]\n\tworktree = <root>/elsewhere\n",
        "w3/.git": "gitdir: <root>/main2/.git/worktrees/w3\n",
        "w3/f": "",
    }
    target = "<root>/main/.git/worktrees"
    queries = [
        f"g:<root>/wt|{target}/wt",
        f"g:<root>/elsewhere|{target}/wt",
        f"g:<root>/elsewhere|{target}/gone",
        f"g:<root>/elsewhere|{target}/blank",
        f"g:<root>/elsewhere|{target}/nogitdir",
        "g:<root>/elsewhere|<root>/forged",
        "g:<root>/elsewhere|<root>/bare.git",
        "g:<root>/elsewhere|<root>/fmt",
        "g:<root>/elsewhere|<root>/main/.git",
        "g:<root>/main|<root>/main/.git",
        "g:<root>/super/sub|<root>/super/.git/modules/sub",
        "g:<root>/elsewhere|<root>/super/.git/modules/sub",
        "g:<root>/elsewhere|<root>/nothing",
        "g:<root>/elsewhere|<root>/super/.git/modules/moved",
        "g:<root>/w3|<root>/main2/.git/worktrees/w3",
        "g:<root>/elsewhere|<root>/main2/.git/worktrees/w3",
        "e:<root>/wt/.git|<root>/main/.git/worktrees/wt",
        "e:<root>/super/sub/.git|<root>/super/.git/modules/sub",
    ]
    yield Case(queries, files=files)
    # A linked worktree as git writes it by default: a relative `../..`
    # commondir and a relative gitdir.
    files = _repo("main/.git", V0)
    files |= {
        "main/f": "",
        "main/.git/worktrees/wt/HEAD": HEAD,
        "main/.git/worktrees/wt/commondir": "../..\n",
        "main/.git/worktrees/wt/gitdir": "<root>/wt/.git\n",
        "wt/.git": "gitdir: ../main/.git/worktrees/wt\n",
        "wt/f": "",
        "elsewhere/f": "",
    }
    yield Case(
        [
            "g:<root>/wt|<root>/main/.git/worktrees/wt",
            "g:<root>/elsewhere|<root>/main/.git/worktrees/wt",
            "e:<root>/wt/.git|<root>/main/.git/worktrees/wt",
        ],
        files=files,
    )
    # Records whose paths hold surrogates: escaped bytes that encode to a
    # real UTF-8 name ("\udcc3\udca9" is the bytes of "\u00e9"), which
    # spell no decoded folder, in a folder that cannot be read and in one
    # that is absent; a tree alone that cannot be encoded; the lower edge
    # of the escaped range.
    files = _repo("se.git", V0) | _repo("sf.git", V0) | _repo("sx.git", V0)
    files |= _repo("sy.git", V0)
    files |= {
        "lk/\u00e9/.git": "gitdir: <root>/se.git\n",
        "lk/\u00e9/f": "",
        "xx/.git": "gitdir: <root>/sx.git\n",
        "xy/.git": "gitdir: <root>/sy.git\n",
    }
    yield Case(
        [
            "g:<root>/lk/\u00e9|<root>/se.git",
            "g:<root>/\u00e9|<root>/sf.git",
            "g:<root>/xx|<root>/sx.git",
            "g:<root>/xy|<root>/sy.git",
        ],
        files=files,
        modes={"lk": 0o000},
        records={
            "se.git": _record("<root>/se.git", "<root>/lk/\\udcc3\\udca9"),
            "sf.git": _record("<root>/sf.git", "<root>/\\udcc3\\udca9"),
            "sx.git": _record("<root>/sx.git", "<root>/xx\\ud800"),
            "sy.git": _record("<root>/sy.git", "<root>/xy\\udc7f"),
        },
    )
    # Gated by input: a loop, a record nested past the JSON decoder's
    # limit.
    files = _repo("main/.git", V0)
    files |= {
        "deep/.git": "gitdir: <root>/sepd.git\n",
        "loopy/.git": "gitdir: <root>/loop\n",
    }
    files |= _repo("sepd.git", V0)
    yield Case(
        [
            "g:<root>/deep|<root>/sepd.git",
            "p:<root>/sepd.git",
            "e:<root>/loopy/.git|<root>/main/.git",
            "e:<root>/l1|<root>/main/.git",
            "s:<root>/x|<root>/l1",
        ],
        files=files,
        links={"l1": "l2", "l2": "l1", "loop": "loop"},
        loop=True,
        records={"sepd.git": {"nest": DEEP}},
    )


def _text(value: Any) -> bytes:
    if isinstance(value, dict):
        if "nest" in value:
            return b"[" * value["nest"] + b"]" * value["nest"]
        if "hex" in value:
            return bytes.fromhex(value["hex"])
        return (value.get("p", "") + str(value["x"]) * int(value["n"])).encode()
    return str(value).encode("utf-8", "surrogateescape")


def _spell(text: str, real: str) -> Any:
    data = text.replace(real, "<scratch>").encode("utf-8", "surrogateescape")
    return _b(data)


def _refusal(why: str | None, real: str) -> Any:
    if why is None:
        return None
    return {"why": _spell(why, real), "u": isinstance(why, g.FormatUnknown)}


def _call(fn: Any) -> Any:
    try:
        return fn()
    except (OSError, ValueError, RecursionError) as exc:
        return _error(exc)


def _query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    op, _, raw = query.partition(":")
    args = [a.replace("<root>", real) for a in raw.split("|")]
    for a in args:
        for part in a.split(";"):
            check_length(part, links, real)
            check_parent_of_file(part)
    out: dict[str, Any] = {"p": _b(query.encode("utf-8", "surrogateescape"))}
    if op == "g":

        def refuse() -> Any:
            why, provisional = g._refusal(args[0], args[1])
            return [_refusal(why, real), provisional]

        out["v"] = _call(refuse)
    elif op == "e":
        out["v"] = _call(lambda: g._entry_names(args[0], args[1]))
    elif op == "E":
        views = [v for v in args[2].split(";") if v]
        tree = None if args[0] == "-" else args[0]

        def earliest() -> Any:
            found = g.earliest_naming(views, tree, args[1])
            return None if found is None else _spell(found, real)

        out["v"] = _call(earliest)
    elif op == "p":

        def pin() -> Any:
            found = gitdir_pin.recorded(args[0])
            if found is None:
                return None
            return [_spell(found.git_dir, real), _spell(found.tree, real)]

        out["v"] = _call(pin)
    elif op == "s":
        out["v"] = _call(lambda: g._spells(args[0], args[1]))
    else:
        raise ValueError(op)
    return out


def _answer(tree: Case, root: Path, pins: Path) -> list[dict[str, Any]]:
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
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target.replace("<root>", real), base / rel)
        pins.mkdir(parents=True)
        for rel, value in tree.records.items():
            name = gitdir_pin._name(os.stat(base / rel))
            (pins / name).write_bytes(
                _text(value).replace(b"<root>", os.fsencode(real))
            )
        for rel in sorted(tree.modes, key=lambda r: -r.count("/")):
            os.chmod(base / rel, tree.modes[rel])

        def at_least(want: tuple[int, int]) -> bool:
            return (2, 54) >= want

        try:
            with (
                patch.dict(os.environ),
                patch.object(g, "_git_is_at_least", at_least),
                patch.object(g, "_reads_compat", lambda: True),
                patch.object(g, "_RECORDED_BUDGET", 60.0),
                patch.object(gitdir_pin, "pins_dir", lambda: pins),
            ):
                os.environ.pop("GIT_COMMON_DIR", None)
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


def gated(query: str, tree: Case) -> bool:
    """Whether a query goes in the Python-gated file, by its input."""
    if tree.loop:
        return True
    if any(
        isinstance(v, dict) and v.get("nest", 0) > 500 for v in tree.records.values()
    ):
        return True
    op, _, raw = query.partition(":")
    if op != "g":
        return False
    for path in raw.split("|"):
        if strict_gated(path, Tree([path], links=tree.links)):
            return True
    return any(
        strict_gated(t, Tree([t]))
        for t in _commondir_texts(tree)
        if t and t[0] != "<" and not _ascends(t)
    )


def _ascends(text: str) -> bool:
    """Whether a commondir text is ``..`` parts alone (git's ``../..``).
    It is read from a file inside the git directory, so each ``..``
    climbs from a folder to its parent: no non-directory is met before a
    part, where 3.11 and 3.13 would differ, and it needs no gate."""
    return all(p == ".." for p in text.split("/"))


def _lines(want_gated: bool) -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "gitfile-follow"
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
                loop=tree.loop,
                records=tree.records,
            )
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(asked, root, world.root / f"pins-{i}-{int(want_gated)}")
            shutil.rmtree(root)
            lines.append({**asked.record(), "q": answers})
    return lines


def generate() -> dict[str, bytes]:
    lines = _lines(False)
    header = {"fixture": "gitfile_follow", "cases": len(lines)}
    return {"gitfile_follow.jsonl": jsonl([header, *lines])}


def generate_python() -> dict[str, bytes]:
    lines = _lines(True)
    header = {"fixture": "gitfile_follow_python", "cases": len(lines)}
    return {"gitfile_follow_python.jsonl": jsonl([header, *lines])}
