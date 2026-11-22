"""Fixtures: the attribute file signatures, the index's ``.gitmodules``
entry and `_gitdir_at_root` (``origin_gitfiles_attrs_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_gitfiles/attrs.rs``.

POSIX only (`generate.POSIX_ONLY`): the scenarios make links, a FIFO and
mode-000 entries, and discovery checks each path's owner. The git on PATH
is data: release 2.54, patched as `gen_origin_gitfiles_root` patches it.

Each row is ``["scenario", steps, answers]``: a scenario in a fresh
scratch folder (``<root>``, resolved), with empty memos, each step's
answer in order (null for a step that changes the tree):

- ``["dir", rel]``, ``["write", rel, text]``, ``["bytes", rel, runs]``
  (bytes spelled as `gen_origin_gitfiles._runs` spells them),
  ``["bigindex", rel, n, version]`` (`_big_index`), ``["link", rel,
  target]``, ``["fifo", rel]``, ``["chmod", rel, mode]``, ``["rm", rel]``:
  changes, each made `PAUSE` after the step before.
- ``["attrs", cwd, root, env, hold]``: `attribute_files_signature`.
- ``["gattr", root, pathspecs, hold]``: `gitattributes_signature`.
- ``["gar", root, env]``: `_gitdir_at_root`: null or ``[gitdir,
  commondir]``.
- ``["index", root, mid]``: `_index_gitmodules` of what `_gitdir_at_root`
  finds (``"no repository"`` where none): null, ``"unmerged"``, ``[mode,
  oid]``, or the exception's class name; with ``mid`` a byte is appended
  to the index after each prefix read, so it is rewritten mid-read.
- ``["imemo"]``: the paths the index memo holds, oldest first.

``env`` maps XDG_CONFIG_HOME, HOME and GIT_DIR to a value or null
(unset) for the call; the other variables stay as the sandbox sets them.
A signature is spelled with each stamp as ``"s"`` (null where None), the
global file's path, and the position of the first earlier step of the
scenario whose signature of the same kind was equal (-1 where none), so
no time is written; ``hold`` adds the held paths, sorted. Paths spell the
scratch folder as ``<root>``.
"""

from __future__ import annotations

import os
import shutil
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import gitfile_rule, gitsystem, origin_gitfiles
from bettermemory.origin_gitfiles import (
    _gitdir_at_root,
    _index_gitmodules,
    _IndexUnsettled,
    attribute_files_signature,
    gitattributes_signature,
)

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_origin_gitfiles import _entry, _index, _runs

#: The pause before each change, as `gen_origin_gitfiles_root.PAUSE`.
PAUSE = 0.05

#: The release the git on PATH is read as.
RELEASE = (2, 54)

_INIT = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
_ENV = ("XDG_CONFIG_HOME", "HOME", "GIT_DIR")
_HOMES = {"XDG_CONFIG_HOME": "<root>/xdg", "HOME": "<root>/h", "GIT_DIR": None}
_NO_HOME = {"XDG_CONFIG_HOME": None, "HOME": None, "GIT_DIR": None}
_OID = bytes(range(20))


def _repo(git: str = "r/.git", config: str = _INIT) -> list[list[Any]]:
    return [
        ["dir", git + "/objects"],
        ["dir", git + "/refs"],
        ["write", git + "/HEAD", "ref: refs/heads/main\n"],
        ["write", git + "/config", config],
    ]


def _ix(version: int, *entries: bytes) -> list[object]:
    return _runs(_index(version, list(entries)))


def _gm(version: int = 2, **kw: Any) -> bytes:
    return _entry(b".gitmodules", version=version, oid=_OID, **kw)


def _a(version: int = 2) -> bytes:
    return _entry(b".a", version=version)


def _big_index(count: int, version: int) -> bytes:
    """`count` entries ``.a00000`` onward before ``.gitmodules``."""
    names = [b".a%05d" % i for i in range(count)]
    return _index(version, [_entry(n, version=version) for n in names] + [_gm(version)])


def _attrs(env: dict[str, Any] = _HOMES, hold: bool = False) -> list[Any]:
    return ["attrs", "r", "r", env, hold]


_SIG = [_attrs(hold=True)]

SCENARIOS: list[list[list[Any]]] = [
    # attribute_files_signature
    [["dir", "x"], ["attrs", "x", "x", _HOMES, True]],
    [*_repo(), *_SIG, _attrs(), ["dir", "xdg/git"], _attrs(),
     ["write", "xdg/git/attributes", "* diff\n"], _attrs(hold=True),
     ["write", "xdg/git/attributes", "* -diff\n"], _attrs(),
     ["dir", "r/.git/info"], ["write", "r/.git/info/attributes", "x\n"], _attrs(hold=True),
     ["chmod", "r/.git/info/attributes", 0o600], _attrs(),
     ["write", "r/.git/config", _INIT + "[diff \"x\"]\n\tcommand = y\n"], _attrs(),
     ["rm", "r/.git/info/attributes"], _attrs()],
    [*_repo(), _attrs({"XDG_CONFIG_HOME": "", "HOME": "<root>/h", "GIT_DIR": None}, hold=True),
     _attrs({"XDG_CONFIG_HOME": None, "HOME": "", "GIT_DIR": None}),
     _attrs({"XDG_CONFIG_HOME": "rel", "HOME": None, "GIT_DIR": None}, hold=True),
     _attrs({"XDG_CONFIG_HOME": None, "HOME": "~", "GIT_DIR": None}),
     _attrs(_NO_HOME),
     _attrs({"XDG_CONFIG_HOME": "<root>/xdg", "HOME": None, "GIT_DIR": "<root>/r/.git"})],
    [*_repo(), ["dir", "x"], ["attrs", "x", "r", _HOMES, True],
     ["attrs", "x", "r", _NO_HOME, False],
     ["attrs", "x", "r", {"XDG_CONFIG_HOME": "", "HOME": None, "GIT_DIR": None}, False],
     ["attrs", "x", "x", _HOMES, False]],
    [*_repo(), ["dir", "r/a/b"], ["attrs", "r/a/b", "r", _HOMES, False]],
    [*_repo(config=_INIT + "[log]\n\tfollow = true\n"), *_SIG],
    [*_repo(config=_INIT + "[LOG]\n\tFollow\n"), *_SIG],
    [*_repo(config=_INIT + "[push]\n\tfollowTags = true\n# log.follow\n"), *_SIG],
    [*_repo(config=_INIT + "[log \"follow\"]\n\tx = 1\n"), *_SIG],
    [*_repo(config=_INIT + "[core]\n\tattributesFile = /a\n"), *_SIG],
    [*_repo(config=_INIT + "# attributesfile\n"), *_SIG],
    [*_repo(config=_INIT + "[attr]\n\ttree = HEAD\n"), *_SIG],
    [*_repo(config=_INIT + "[include]\n\tpath = x\n"), *_SIG],
    [*_repo(config=_INIT + "[includeIf \"gitdir:/x\"]\n\tpath = x\n"), *_SIG],
    [*_repo(config=_INIT + "[x\n"), *_SIG],
    [*_repo(config=_INIT + "[a]\n\tb = \0\n"), *_SIG],
    [*_repo(config=""), *_SIG],
    [*_repo(), ["rm", "r/.git/config"], *_SIG, ["dir", "r/.git/config"], *_SIG],
    [*_repo(), ["rm", "r/.git/config"], ["fifo", "r/.git/config"], *_SIG],
    [*_repo(), ["chmod", "r/.git/config", 0], *_SIG, ["chmod", "r/.git/config", 0o644]],
    [*_repo(config=_INIT + "[extensions]\n\tworktreeConfig = true\n"), *_SIG,
     ["write", "r/.git/config.worktree", "[diff]\n\tx = 1\n"], *_SIG,
     ["write", "r/.git/config.worktree", "[log]\n\tfollow\n"], *_SIG,
     ["write", "r/.git/config.worktree", "[include]\n\tpath = y\n"], *_SIG,
     ["write", "r/.git/config.worktree", "[x\n"], *_SIG,
     ["rm", "r/.git/config.worktree"], ["dir", "r/.git/config.worktree"], *_SIG],
    [*_repo(), ["write", "r/.git/config.worktree", "[log]\n\tfollow\n"], *_SIG],
    [*_repo(), ["dir", "r/.git/info"], ["chmod", "r/.git/info", 0], *_SIG,
     ["chmod", "r/.git/info", 0o755]],
    [*_repo(), ["dir", "xdg/git"], ["link", "xdg/git/attributes", "nowhere"], *_SIG,
     ["write", "f", "file"],
     _attrs({"XDG_CONFIG_HOME": "<root>/f", "HOME": None, "GIT_DIR": None}, hold=True)],
    [*_repo(), ["write", "h", "file"],
     _attrs({"XDG_CONFIG_HOME": None, "HOME": "<root>/h", "GIT_DIR": None}, hold=True)],
    [*_repo(git="main/.git"),
     ["dir", "main/.git/worktrees/w"],
     ["write", "main/.git/worktrees/w/HEAD", "ref: refs/heads/w\n"],
     ["write", "main/.git/worktrees/w/commondir", "../..\n"],
     ["write", "main/.git/worktrees/w/gitdir", "<root>/w/.git\n"],
     ["dir", "w"], ["write", "w/.git", "gitdir: <root>/main/.git/worktrees/w\n"],
     ["attrs", "w", "w", _HOMES, True],
     ["write", "main/.git/config", _INIT + "[extensions]\n\tworktreeConfig = 1\n"],
     ["write", "main/.git/worktrees/w/config.worktree", "[a]\n\tb = 1\n"],
     ["attrs", "w", "w", _HOMES, True], ["gar", "w", _HOMES], ["index", "w", False]],
    # gitattributes_signature
    [["dir", "t/a/b"], ["gattr", "t", ["a/b/c.py", "a/d", "e"], True],
     ["write", "t/a/.gitattributes", "*.py diff\n"],
     ["gattr", "t", ["a/b/c.py", "a/d", "e"], False],
     ["write", "t/a/.gitattributes", "*.py -diff\n"],
     ["gattr", "t", ["a/b/c.py", "a/d", "e"], False],
     ["gattr", "t", ["e", "a/b/c.py"], True],
     ["gattr", "t", ["a/b"], False], ["gattr", "t", ["a/b/"], False],
     ["gattr", "t", ["a//b"], False], ["gattr", "t", ["./a"], False],
     ["gattr", "t", ["a/../e"], False], ["gattr", "t", ["a/*.py"], False],
     ["gattr", "t", ["a?"], False], ["gattr", "t", ["[a]"], False],
     ["gattr", "t", ["a\\b"], False], ["gattr", "t", [":(glob)a"], False],
     ["gattr", "t", ["/a"], False], ["gattr", "t", [""], False],
     ["gattr", "t", [], True], ["gattr", "t", ["a\0b"], False],
     ["gattr", "t", ["n/o/p"], True], ["gattr", "nowhere", ["a"], True]],
    [["dir", "t/a"], ["write", "t/f", "x"], ["gattr", "t", ["f/g"], True],
     ["link", "t/l", "a"], ["gattr", "t", ["l"], False], ["gattr", "t", ["l/x"], True],
     ["link", "t/.gitattributes", "a"], ["gattr", "t", ["z"], False],
     ["rm", "t/.gitattributes"], ["dir", "t/.gitattributes"], ["gattr", "t", ["z"], False],
     ["chmod", "t/a", 0], ["gattr", "t", ["a/x"], False], ["chmod", "t/a", 0o755],
     ["link", "t/loop", "loop"], ["gattr", "t", ["loop"], False],
     ["gattr", "t", ["loop/x"], False]],
    # _gitdir_at_root
    [*_repo(), ["dir", "r/a"], ["dir", "x"], ["gar", "r", _HOMES], ["gar", "r/a", _HOMES],
     ["gar", "x", _HOMES], ["gar", "nowhere", _HOMES],
     ["gar", "r", {"XDG_CONFIG_HOME": None, "HOME": None, "GIT_DIR": "<root>/r/.git"}],
     ["index", "x", False]],
    [["dir", "w"], ["write", "w/.git", "gitdir: nowhere\n"], ["gar", "w", _HOMES]],
    # _index_gitmodules
    [*_repo(), ["index", "r", False], ["imemo"],
     ["bytes", "r/.git/index", _ix(2, _a(), _gm(), _entry(b"z", version=2))],
     ["index", "r", False], ["imemo"], ["index", "r", False],
     ["write", "r/.git/sharedindex.0", "x"], ["index", "r", False],
     ["bytes", "r/.git/index", _ix(2, _gm())], ["index", "r", False],
     ["rm", "r/.git/sharedindex.0"], ["index", "r", False], ["imemo"]],
    [*_repo(), ["bytes", "r/.git/index", _ix(2, _a())], ["index", "r", False],
     ["bytes", "r/.git/index", _ix(2)], ["index", "r", False],
     ["bytes", "r/.git/index", _ix(2, _gm(stage=1), _gm(stage=2), _gm(stage=3))],
     ["index", "r", False],
     ["bytes", "r/.git/index", _ix(2, _gm(stage=0), _gm(stage=2))], ["index", "r", False],
     ["bytes", "r/.git/index", _ix(2, _gm(mode=0o160000))], ["index", "r", False],
     ["bytes", "r/.git/index", _ix(3, _a(3), _gm(3, extended=0x4000))],
     ["index", "r", False],
     ["bytes", "r/.git/index", _ix(3, _gm(3, extended=0x0001))], ["index", "r", False],
     ["bytes", "r/.git/index", _ix(2, _gm(extended=0))], ["index", "r", False],
     ["bytes", "r/.git/index", _ix(4, _a(4), _gm(4, strip=1, suffix=b"gitmodules"))],
     ["index", "r", False],
     ["bytes", "r/.git/index", _ix(4, _gm(4))], ["index", "r", False],
     ["bytes", "r/.git/index", _ix(1, _gm())], ["index", "r", False],
     ["bytes", "r/.git/index", _ix(5, _gm())], ["index", "r", False],
     ["bytes", "r/.git/index", _ix(2, _a(), _gm())[:-1]], ["index", "r", False],
     ["bytes", "r/.git/index", _runs(_index(2, [_a(), _gm()])[:50])],
     ["index", "r", False],
     ["bytes", "r/.git/index", _runs(_index(2, [_a(), _gm()], count=5))],
     ["index", "r", False],
     ["bytes", "r/.git/index", _runs(b"")], ["index", "r", False],
     ["bytes", "r/.git/index", _runs(b"XIRC" + bytes(8))], ["index", "r", False]],
    [*_repo(), ["bigindex", "r/.git/index", 2000, 2], ["index", "r", False],
     ["bigindex", "r/.git/index", 4000, 4], ["index", "r", False],
     ["bigindex", "r/.git/index", 1000, 2], ["index", "r", True], ["index", "r", False],
     ["index", "r", True], ["imemo"]],
    [*_repo(), ["bytes", "r/.git/index", _ix(2, _gm())], ["index", "r", True],
     ["imemo"], ["index", "r", False], ["imemo"]],
    [*_repo(config=_INIT + "[extensions]\n\tobjectFormat = sha256\n"),
     ["bytes", "r/.git/index", _ix(2, _gm())], ["index", "r", False]],
    [*_repo(config=_INIT + "[x\n"), ["bytes", "r/.git/index", _ix(2, _gm())],
     ["index", "r", False]],
    [*_repo(), ["dir", "r/.git/index"], ["index", "r", False], ["imemo"]],
    [*_repo(), ["fifo", "r/.git/index"], ["index", "r", False]],
    [*_repo(), ["link", "r/.git/index", "nowhere"], ["index", "r", False]],
    [*_repo(), ["bytes", "r/.git/index", _ix(2, _gm())], ["chmod", "r/.git/index", 0],
     ["index", "r", False], ["chmod", "r/.git/index", 0o644], ["index", "r", False]],
]  # fmt: skip


def _unruns(runs: list[Any]) -> bytes:
    out = bytearray()
    for run in runs:
        if isinstance(run, str):
            out.extend(bytes.fromhex(run))
        else:
            out.extend(bytes([run[0]]) * run[1])
    return bytes(out)


def _change(step: list[Any], root: str) -> None:
    kind = step[0]
    at = Path(root) / step[1]
    time.sleep(PAUSE)
    if kind == "dir":
        at.mkdir(parents=True)
    elif kind == "write":
        at.write_bytes(step[2].replace("<root>", root).encode())
    elif kind == "bytes":
        at.write_bytes(_unruns(step[2]))
    elif kind == "bigindex":
        at.write_bytes(_big_index(step[2], step[3]))
    elif kind == "link":
        os.symlink(step[2], at)
    elif kind == "fifo":
        os.mkfifo(at)  # type: ignore[attr-defined,unused-ignore]
    elif kind == "chmod":
        os.chmod(at, step[2])
    elif kind == "rm":
        at.unlink()
    else:
        raise ValueError(kind)


@contextmanager
def _env(env: dict[str, Any], root: str) -> Iterator[None]:
    with mock.patch.dict(os.environ):
        for name in _ENV:
            value = env[name]
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value.replace("<root>", root)
        yield


def _held(held: dict[str, Any] | None, root: str) -> Any:
    return None if held is None else sorted(k.replace(root, "<root>") for k in held)


def _first(sigs: list[tuple[str, Any]], kind: str, sig: Any) -> int:
    for i, (k, s) in enumerate(sigs):
        if k == kind and s == sig:
            return i
    return -1


def _s(stamp: Any) -> Any:
    return None if stamp is None else "s"


def _index_answer(gd: Any, root: str, mid: bool) -> Any:
    real = origin_gitfiles._read_prefix

    def appending(fd: int, length: int) -> bytes:
        data = real(fd, length)
        with open(gd.gitdir / "index", "ab") as fh:
            fh.write(b"\0")
        return data

    patch = mock.patch.object(origin_gitfiles, "_read_prefix", appending)
    try:
        if mid:
            with patch:
                entry = _index_gitmodules(gd)
        else:
            entry = _index_gitmodules(gd)
    except _IndexUnsettled:
        return "unsettled"
    except (OSError, ValueError) as exc:
        return type(exc).__name__
    if entry is None or entry == origin_gitfiles._INDEX_UNMERGED:
        return None if entry is None else "unmerged"
    return list(entry)


def _run_step(step: list[Any], root: str, sigs: list[tuple[str, Any]]) -> Any:
    kind = step[0]
    if kind in ("dir", "write", "bytes", "bigindex", "link", "fifo", "chmod", "rm"):
        _change(step, root)
        return None
    if kind == "imemo":
        return [k.replace(root, "<root>") for k in origin_gitfiles._INDEX_MEMO]
    at = Path(root) / step[1]
    if kind == "attrs":
        held: dict[str, Any] | None = {} if step[4] else None
        with _env(step[3], root):
            sig = attribute_files_signature(at, Path(root) / step[2], directories=held)
        spelled = None
        if sig is not None:
            path = sig[3]
            assert path is None or isinstance(path, str)
            spelled = [_s(sig[0]), _s(sig[1]), _s(sig[2]),
                       None if path is None else path.replace(root, "<root>"),
                       _s(sig[4])]  # fmt: skip
        answer = [spelled, _first(sigs, kind, sig), _held(held, root)]
        sigs.append((kind, sig))
        return answer
    if kind == "gattr":
        held = {} if step[3] else None
        sig = gitattributes_signature(at, step[2], directories=held)
        spelled = None if sig is None else [_s(s) for s in sig]
        answer = [spelled, _first(sigs, kind, sig), _held(held, root)]
        sigs.append((kind, sig))
        return answer
    if kind == "gar":
        with _env(step[2], root):
            gd = _gitdir_at_root(at)
        if gd is None:
            return None
        return [str(gd.gitdir).replace(root, "<root>"),
                str(gd.commondir).replace(root, "<root>")]  # fmt: skip
    if kind == "index":
        with _env(_HOMES, root):
            gd = _gitdir_at_root(at)
        if gd is None:
            return "no repository"
        return _index_answer(gd, root, step[2])
    raise ValueError(kind)


@contextmanager
def _git() -> Iterator[None]:
    with (
        mock.patch.object(gitsystem, "is_at_least", lambda want: want <= RELEASE),
        mock.patch.object(
            gitfile_rule, "_git_is_at_least", lambda want: want <= RELEASE
        ),
        mock.patch.object(gitfile_rule, "_reads_compat", lambda: True),
    ):
        yield


@contextmanager
def _memos() -> Iterator[None]:
    """Both memos emptied for the run and given back as they were."""
    index = OrderedDict(origin_gitfiles._INDEX_MEMO)
    config = OrderedDict(origin_gitfiles._CONFIG_MEMO)
    try:
        yield
    finally:
        origin_gitfiles._clear_index_memo()
        origin_gitfiles._clear_config_memo()
        origin_gitfiles._INDEX_MEMO.update(index)
        origin_gitfiles._CONFIG_MEMO.update(config)


def generate_posix() -> dict[str, bytes]:
    refuse_root()
    rows: list[Any] = []
    with _sandbox.world() as world, _git(), _memos():
        base = world.root / "ogf-attrs"
        for i, steps in enumerate(SCENARIOS):
            scratch = base / str(i)
            scratch.mkdir(parents=True)
            root = os.path.realpath(scratch)
            origin_gitfiles._clear_index_memo()
            origin_gitfiles._clear_config_memo()
            sigs: list[tuple[str, Any]] = []
            try:
                answers = [_run_step(step, root, sigs) for step in steps]
            finally:
                for path in Path(root).rglob("*"):
                    if path.is_dir() and not path.is_symlink():
                        path.chmod(0o755)
                shutil.rmtree(scratch)
            rows.append(["scenario", steps, answers])
    header = {"fixture": "origin_gitfiles_attrs_posix", "scenarios": len(rows)}
    return {"origin_gitfiles_attrs_posix.jsonl": jsonl([header, *rows])}
