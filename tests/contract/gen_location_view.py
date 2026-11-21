"""Fixtures: `location.py`'s view continuity (``location_view.jsonl`` and
``location_view_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_view.rs``. This pins
`continuity`, `_same_number`, `_carries_project_file`'s path,
`_git_dir_at`, `_head_at` and `view_state`.

``location_view.jsonl`` (ungated) holds pure rows. ``["c", args, answer]``
is `continuity`: args are ``[token, last_head, stamps, token_here,
head_here, stamps_here, holds, confirms, superseded, reminted]`` where
``holds`` is what `holds` answers (truthy or not), and ``confirms`` and
``reminted`` are null (not given) or what they answer; the answer is
``[continues, witness, calls]``, ``calls`` the letters of the callables
asked in order (``r`` reminted, ``h`` holds, ``c`` confirms), or
``["err", "TypeError", calls]`` where a stamp is unhashable. A stamp is a
list of JSON values (a nested list stays a list). The digest row
``["cx", count, sha256]`` covers the whole cross product of `_AXES`
(`_cross`), each answer as `_line` writes it. ``["same", a, b, answer]``
is `_same_number`; ``["pf", flavour, folder, path]`` is the path
`_carries_project_file` parses.

``location_view_posix.jsonl`` (POSIX only: links, a mode-000 folder,
device and inode numbers) holds `view_state` over trees built in scratch
(items as `gen_location_git_readers._build` reads them, plus ``["tok",
git_dir, which]`` writing a token file), with `object_key` stubbed as
``(str(st_dev), str(st_ino), birth)`` of `os.stat` (``birth`` the case's
``hb``), `_at_dotgit` stubbed to follow a regular ``.git`` file's
``gitdir:`` line relative to the view's path when it names a folder,
`project_file.parse` stubbed to answer a project file whose bytes are
``ok\\n``, and the history's `holds` answering every commit asked where
the case's ``held`` is set. Keys are named, not written: ``kd`` is the
folder whose stubbed key is recorded, with ``rb`` as its birth; ``ck``
names the common directory's key (``"common"``) or another (``"other"``).
Recorded stamps are ``"same"`` (`_init_stamps` of ``kd``), ``"other"`` or
absent. Not run as root.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import stat
import tempfile
import types
from collections.abc import Iterator
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from bettermemory import checkout_token, location, project_file

from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_location_git_readers import _build, _record

T = "0123456789ABCDEFGHJKMNPQRS"
U = "ABCDEFGHJKMNPQRSTVWXYZ0123"
NONCE = "0000000000000"
A = "a" * 40
B = "b" * 40

_STAMPS: list[Any] = [[], [["d", 1, 2]], [["d", 1.0, True + 1]], [["e", 9]]]
_AXES: list[list[Any]] = [
    [None, "", T, U],
    [None, "h", "g"],
    _STAMPS,
    [None, "", T, U],
    [None, "h"],
    [[], [["d", True, 2]]],
    [False, True],
    [None, False, True],
    [False, True],
    [None, False, True],
]


def _continuity(args: list[Any]) -> list[Any]:
    token, last, stamps, there, head, here_st, held, conf, sup, rem = args
    calls: list[str] = []

    def holds(commits: Any) -> set[str]:
        calls.append("h")
        return set(commits) if held else set()

    def confirms() -> bool:
        calls.append("c")
        return bool(conf)

    def reminted() -> bool:
        calls.append("r")
        return bool(rem)

    try:
        ok, how = location.continuity(
            token,
            last,
            stamps,
            token_here=there,
            head_here=head,
            stamps_here=here_st,
            holds=holds,
            confirms=None if conf is None else confirms,
            superseded=sup,
            reminted=None if rem is None else reminted,
        )
    except TypeError:
        return ["err", "TypeError", "".join(calls)]
    return [ok, how, "".join(calls)]


def _line(args: list[Any]) -> str:
    return json.dumps(_continuity(args), separators=(",", ":"))


def _cross() -> list[Any]:
    digest = hashlib.sha256()
    count = 0
    for args in itertools.product(*_AXES):
        digest.update(_line(list(args)).encode() + b"\n")
        count += 1
    return ["cx", count, digest.hexdigest()]


def _picked() -> Iterator[list[Any]]:
    every = list(itertools.product(*_AXES))
    for i in range(0, len(every), 139):
        yield list(every[i])
    nested = [["d", [1]]]
    yield [None, None, nested, None, None, [["d"]], False, None, False, None]
    yield [None, None, [["d"]], None, None, nested, False, None, False, None]
    yield [None, None, [], None, None, nested, False, True, False, None]
    yield [None, "h", nested, None, None, nested, True, None, False, None]
    yield [None, None, [["d", 0.5]], None, None, [["d", 0.5]], False, None, False, None]
    yield [
        None,
        None,
        [["d", 1e300]],
        None,
        None,
        [["d", 10**300]],
        False,
        None,
        False,
        None,
    ]
    yield [
        None,
        None,
        [["d", 2**53 + 1]],
        None,
        None,
        [["d", 2.0**53]],
        False,
        None,
        False,
        None,
    ]
    yield [None, None, [["d", -0.0]], None, None, [["d", 0]], False, None, False, None]
    yield [None, None, [["d", "1"]], None, None, [["d", 1]], False, None, False, None]
    yield [
        None,
        None,
        [["d", None]],
        None,
        None,
        [["d", None]],
        False,
        None,
        False,
        None,
    ]
    yield [
        None,
        None,
        [["d", 1], ["d", 1]],
        None,
        None,
        [["x"], ["d", 1]],
        False,
        None,
        False,
        None,
    ]
    yield [None, None, [["d", 1, 2]], None, None, [["d", 1]], False, None, False, None]
    yield [T, None, [], T, None, [], False, False, True, True]
    yield [T, None, [], "", None, [], False, None, True, True]
    yield [T, None, [], "", None, [], False, None, False, True]


def _same_rows() -> Iterator[list[Any]]:
    keys: list[Any] = [
        None,
        ["1", "2", None],
        ["1", "2", "9"],
        ["1", "3", None],
        ["0", "2", None],
    ]
    for a in keys:
        for b in keys:
            got = location._same_number(
                None if a is None else tuple(a), None if b is None else tuple(b)
            )
            yield ["same", a, b, got]


def _pf_rows() -> Iterator[list[Any]]:
    for folder in ["", "/", "a", "a/", "a/./b/", "//x", "/x//y/.", ".", ".."]:
        yield [
            "pf",
            "posix",
            folder,
            str(PurePosixPath(folder) / project_file.PROJECT_FILE_NAME),
        ]
    for folder in ["", "C:", "C:\\", "a/b", "a\\.\\b\\"]:
        yield [
            "pf",
            "windows",
            folder,
            str(PureWindowsPath(folder) / project_file.PROJECT_FILE_NAME),
        ]


def _pure_file() -> bytes:
    rows: list[Any] = [["c", args, _continuity(args)] for args in _picked()]
    rows.append(_cross())
    rows.extend(_same_rows())
    rows.extend(_pf_rows())
    return jsonl([{"fixture": "location_view", "cases": len(rows)}, *rows])


def _repo(
    rel: str, *, bare: bool = False, head: bytes = b"ref: refs/heads/main\n"
) -> list[list[Any]]:
    g = rel if bare else rel + "/.git"
    items: list[list[Any]] = [] if bare else [["d", rel.encode()]]
    return [
        *items,
        ["f", (g + "/HEAD").encode(), head],
        ["f", (g + "/refs/heads/main").encode(), A.encode() + b"\n"],
        ["d", (g + "/objects").encode()],
        ["f", (g + "/description").encode(), b"d\n"],
        ["d", (g + "/info").encode()],
    ]


def _linked() -> list[list[Any]]:
    w = "main/.git/worktrees/w"
    return [
        *_repo("main"),
        ["f", (w + "/HEAD").encode(), b"ref: refs/heads/main\n"],
        ["f", (w + "/commondir").encode(), b"../..\n"],
        ["f", (w + "/gitdir").encode(), b"x\n"],
        ["d", (w + "/info").encode()],
        ["f", b"wt/.git", b"gitdir: ../main/.git/worktrees/w\n"],
    ]


def _cases() -> Iterator[dict[str, Any]]:
    P = _repo("p")
    pk = {"t": P, "k": "primary", "p": "p", "kd": "p/.git"}
    yield {"note": "gone", "t": [], "k": "primary", "p": "nope"}
    yield {
        "note": "unreachable",
        "t": [["d", b"lk"], ["m", b"lk"]],
        "k": "primary",
        "p": "lk/x",
    }
    yield {"note": "legacy", **pk, "kd": None}
    yield {"note": "folder legacy", "t": [["d", b"f"]], "k": "folder", "p": "f"}
    yield {"note": "other inode", **pk, "kd": "p/.git/info"}
    yield {"note": "other inode same device", **pk, "kd": "p/.git/info", "lh": A}
    yield {"note": "birth same", **pk, "hb": "1", "rb": "1"}
    yield {"note": "birth differs", **pk, "hb": "1", "rb": "2"}
    yield {"note": "birth recorded only", **pk, "rb": "2", "lh": A}
    yield {"note": "token", **pk, "t": [*P, ["tok", "p/.git", "T"]], "tk": T}
    yield {"note": "token other", **pk, "t": [*P, ["tok", "p/.git", "U"]], "tk": T}
    yield {"note": "token none", **pk, "tk": T, "lh": A}
    yield {
        "note": "token malformed",
        **pk,
        "t": [*P, ["f", b"p/.git/info/bettermemory", b"junk\n"]],
        "tk": T,
    }
    yield {"note": "remint", **pk, "t": [*P, ["tok", "p/.git", "succ"]], "tk": T}
    yield {
        "note": "remint other dir",
        **pk,
        "t": [*P, ["tok", "p/.git", "succx"]],
        "tk": T,
    }
    yield {
        "note": "superseded other",
        **pk,
        "t": [*P, ["tok", "p/.git", "U"]],
        "tk": T,
        "sup": 1,
        "lh": A,
    }
    yield {
        "note": "superseded other moved",
        **pk,
        "t": [*P, ["tok", "p/.git", "U"]],
        "tk": T,
        "sup": 1,
        "lh": B,
    }
    yield {"note": "superseded none", **pk, "tk": T, "sup": 1, "lh": A}
    yield {
        "note": "superseded same",
        **pk,
        "t": [*P, ["tok", "p/.git", "T"]],
        "tk": T,
        "sup": 1,
    }
    yield {"note": "last head", **pk, "lh": A}
    yield {"note": "last head held", **pk, "lh": B, "held": 1}
    yield {"note": "last head gone", **pk, "lh": B}
    yield {"note": "detached", **pk, "t": _repo("p", head=A.encode() + b"\n"), "lh": A}
    yield {
        "note": "head missing ref",
        **pk,
        "t": _repo("p", head=b"ref: refs/heads/x\n"),
        "lh": A,
    }
    yield {"note": "stamps same", **pk, "st": "same"}
    yield {"note": "stamps other", **pk, "st": "other"}
    yield {"note": "nothing", **pk}
    yield {
        "note": "nothing token",
        **pk,
        "t": [*P, ["tok", "p/.git", "T"]],
        "tk": T,
        "sup": 1,
    }
    L = {"t": _linked(), "k": "linked", "p": "wt", "kd": "main/.git/worktrees/w"}
    yield {"note": "linked legacy", **L, "kd": None}
    yield {"note": "linked common", **L, "ck": "common"}
    yield {"note": "linked common other", **L, "ck": "other"}
    yield {"note": "linked common none", **L}
    yield {"note": "linked stamps", **L, "st": "same"}
    yield {"note": "linked last head", **L, "lh": A}
    yield {"note": "primary common", **pk, "ck": "common"}
    Bt = {"t": _repo("b.git", bare=True), "k": "bare", "p": "b.git", "kd": "b.git"}
    yield {"note": "bare last head", **Bt, "lh": A}
    yield {
        "note": "bare token",
        **Bt,
        "t": [*_repo("b.git", bare=True), ["tok", "b.git", "T"]],
        "tk": T,
    }
    yield {"note": "bare stamps", **Bt, "st": "same"}
    F = {"t": [["d", b"f"]], "k": "folder", "p": "f", "kd": "f"}
    yield {"note": "folder no file", **F}
    yield {
        "note": "folder file",
        **F,
        "t": [["d", b"f"], ["f", b"f/bettermemory.toml", b"ok\n"]],
    }
    yield {
        "note": "folder bad file",
        **F,
        "t": [["d", b"f"], ["f", b"f/bettermemory.toml", b"no\n"]],
    }
    yield {"note": "folder birth", **F, "hb": "1", "rb": "1"}
    yield {
        "note": "moved no git",
        "t": [["d", b"p"]],
        "k": "primary",
        "p": "p",
        "kd": "p",
    }
    yield {
        "note": "git link",
        "t": [*_repo("q"), ["d", b"p"], ["l", b"p/.git", b"../q/.git"]],
        "k": "primary",
        "p": "p",
        "kd": "q/.git",
        "lh": A,
    }
    yield {
        "note": "git file",
        "t": [*_repo("q"), ["d", b"p"], ["f", b"p/.git", b"gitdir: ../q/.git\n"]],
        "k": "primary",
        "p": "p",
        "kd": "q/.git",
        "lh": A,
    }
    yield {
        "note": "git file nowhere",
        "t": [["d", b"p"], ["f", b"p/.git", b"gitdir: ../q/.git\n"]],
        "k": "primary",
        "p": "p",
        "kd": "p",
    }
    yield {"note": "kind folder at repo", **pk, "k": "folder", "kd": "p"}


def _token_bytes(which: str, git_dir: str) -> bytes:
    if which in ("T", "U"):
        return checkout_token.render(T if which == "T" else U)
    st = os.stat(git_dir)
    ino = st.st_ino + (1 if which == "succx" else 0)
    lin = checkout_token.Lineage(T, str(st.st_dev), str(ino))
    return checkout_token.render(NONCE + checkout_token._link(lin, NONCE))


def _stub_key(birth: str | None) -> Any:
    def key(path: str) -> Any:
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            return None
        return (str(st.st_dev), str(st.st_ino), birth)

    return key


def _stub_at_dotgit(path: str, dotgit: str, st: os.stat_result) -> Any:
    if not stat.S_ISREG(st.st_mode):
        return None
    with open(dotgit, "rb") as fh:
        line = fh.read().decode().strip()
    if not line.startswith("gitdir: "):
        return None
    found = os.path.normpath(os.path.join(path, line[len("gitdir: ") :]))
    return types.SimpleNamespace(git_dir=found) if os.path.isdir(found) else None


class _ProjectFile:
    pass


def _parse(path: Any) -> Any:
    try:
        with open(path, "rb") as fh:
            return _ProjectFile() if fh.read() == b"ok\n" else None
    except OSError:
        return None


def _view(base: str, case: dict[str, Any]) -> Any:
    at = lambda rel: os.path.join(base, rel)  # noqa: E731
    items = [i for i in case["t"] if i[0] != "tok"]
    _build(os.fsencode(base), items)
    for item in case["t"]:
        if item[0] == "tok":
            with open(at(item[1] + "/info/bettermemory"), "wb") as fh:
                fh.write(_token_bytes(item[2], at(item[1])))
    held = bool(case.get("held"))

    class History:
        def __init__(self, common: str) -> None:
            pass

        def holds(self, commits: Any) -> set[str]:
            return set(commits) if held else set()

    key = None
    if case.get("kd") is not None:
        st = os.stat(at(case["kd"]))
        key = (str(st.st_dev), str(st.st_ino), case.get("rb"))
    stamps: list[Any] = []
    if case.get("st") == "same":
        stamps = [
            list(s)
            for s in location._init_stamps(at(case["kd"]), linked=case["k"] == "linked")
        ]
    elif case.get("st") == "other":
        stamps = [["description", 1, 2, 3, 4]]
    common_key = None
    if case.get("ck") == "common":
        st = os.stat(at("main/.git" if case["k"] == "linked" else "p/.git"))
        common_key = (str(st.st_dev), str(st.st_ino), None)
    elif case.get("ck") == "other":
        common_key = ("0", "0", None)
    names = {
        "object_key": _stub_key(case.get("hb")),
        "_at_dotgit": _stub_at_dotgit,
        "RepositoryHistory": History,
        "project_file": types.SimpleNamespace(parse=_parse, ProjectFile=_ProjectFile),
    }
    saved = {n: getattr(location, n) for n in names}
    try:
        for n, v in names.items():
            setattr(location, n, v)
        return location.view_state(
            at(case["p"]),
            case["k"],
            key,
            case.get("tk"),
            last_head=case.get("lh"),
            stamps=stamps,
            common_key=common_key,
            superseded=bool(case.get("sup")),
        )
    finally:
        for n, v in saved.items():
            setattr(location, n, v)


def _posix_file() -> bytes:
    refuse_root()
    env = os.environ.pop("GIT_COMMON_DIR", None)
    rows: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory() as top:
            for n, case in enumerate(_cases()):
                base = os.path.join(os.path.realpath(top), str(n))
                os.makedirs(base)
                answer = _view(base, case)
                for dirpath, dirs, files in os.walk(base):
                    for name in dirs + files:
                        p = os.path.join(dirpath, name)
                        if not os.path.islink(p):
                            os.chmod(p, 0o700)
                rows.append({**case, "t": _record(case["t"]), "a": answer})
    finally:
        if env is not None:
            os.environ["GIT_COMMON_DIR"] = env
    return jsonl([{"fixture": "location_view_posix", "cases": len(rows)}, *rows])


def generate() -> dict[str, bytes]:
    return {"location_view.jsonl": _pure_file()}


def generate_posix() -> dict[str, bytes]:
    return {"location_view_posix.jsonl": _posix_file()}
