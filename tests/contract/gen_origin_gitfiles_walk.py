"""Fixtures: the origin cache's signature, the files beside the history,
the located head and the check at the end of a search
(``origin_gitfiles_walk_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_gitfiles/walk.rs``.

POSIX only (`generate.POSIX_ONLY`): discovery checks each path's owner,
which declines off POSIX. The git on PATH is data: release 2.54, patched
as `gen_origin_gitfiles_attrs` patches it. The generator neither changes
directory nor touches ``os.environ``; the memos it empties are given back.

Each row is ``["scenario", steps, answers]``: a scenario in a fresh
scratch folder (``<root>``, resolved), with empty memos, each step's
answer in order (null for a step that changes the tree):

- ``["dir", rel]``, ``["write", rel, text]``, ``["bytes", rel, runs]``
  (`gen_origin_gitfiles._runs`), ``["rm", rel]``: changes, each made
  `PAUSE` after the step before.
- ``["capture", start, other]``: `capture_files`, with ``other`` true run
  as if the effective user owned nothing (``os.geteuid`` answering one
  more): ``[first, equal, repository, digest]`` where ``first`` is null outside a
  repository, the git directory, or ``"undetermined"``; ``equal`` the
  position of the first earlier equal capture signature (-1 where none);
  and the repository ``[gitdir, commondir, worktree_root]`` or null.
- ``["walk", root, hold]``: `walk_files_signature`: ``[spelled, equal,
  held, digest]``, each stamp ``"s"`` or null and the index slot null,
  ``"unmerged"`` or ``[mode, oid]``.
- ``["key", cwd, root, pathspecs]``: what a search keys: the attribute
  files, the chain of ``pathspecs`` and the walk files, all holding one
  dict of directories; answers which of the three are None.
- ``["held"]``: `files_held` over the last key.
- ``["head", root]``: `located_head`.

The SHA-256 digests cover the ordered signature fields. Each stamp is
spelled as its first equal stamp, kind and regular-file size, independent
of the host's paths, times and inodes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import gitdir_pin, gitfile_rule, githead, origin_gitfiles
from bettermemory.origin_gitfiles import (
    attribute_files_signature,
    capture_files,
    files_held,
    gitattributes_signature,
    located_head,
    walk_files_signature,
)

from ._common import dumps_line, jsonl
from .gen_fs_realpath import refuse_root
from .gen_githead_signature import _stamp
from .gen_origin_gitfiles import _entry, _index, _runs
from .gen_origin_gitfiles_attrs import _git, _memos, _unruns

#: The pause before each change, as `gen_origin_gitfiles_attrs.PAUSE`.
PAUSE = 0.05

_INIT = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
_BARE = "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
_A = "1" * 40
_B = "2" * 40
_OID = bytes(range(20))


def _repo(git: str = "r/.git", config: str = _INIT, sha: str = _A) -> list[list[Any]]:
    return [
        ["dir", git + "/objects"],
        ["dir", git + "/refs/heads"],
        ["write", git + "/HEAD", "ref: refs/heads/main\n"],
        ["write", git + "/config", config],
        ["write", git + "/refs/heads/main", sha + "\n"],
    ]


def _ix(*entries: bytes) -> list[object]:
    return _runs(_index(2, list(entries)))


def _gm(**kw: Any) -> bytes:
    return _entry(b".gitmodules", version=2, oid=_OID, **kw)


_KEY = ["key", "r", "r", ["a/b.py"]]
_WALK = ["walk", "r", False]

SCENARIOS: list[list[list[Any]]] = [
    # a plain checkout, each file the keys read changed in turn
    [*_repo(), ["dir", "r/a"], ["capture", "r", False], ["capture", "r", False],
     ["capture", "r/a", False], ["head", "r"], ["walk", "r", True], _KEY, ["held"],
     ["held"],
     ["write", "r/a/.gitattributes", "*.py diff\n"], ["held"], _KEY, ["held"],
     ["write", "r/.gitmodules", "[submodule \"s\"]\n"], ["held"], _WALK, _KEY,
     ["bytes", "r/.git/index", _ix(_gm())], ["held"], _WALK, _KEY, ["held"],
     ["rm", "r/.gitmodules"], ["held"], _WALK, _KEY,
     ["bytes", "r/.git/index", _ix(_entry(b".a", version=2), _gm())], ["held"], _WALK,
     _KEY,
     ["bytes", "r/.git/index", _ix(_gm(mode=0o160000))], ["held"], _WALK, _KEY,
     ["bytes", "r/.git/index", _ix(_gm(stage=1), _gm(stage=2))], ["held"], _WALK,
     ["write", "r/.gitmodules", "x"], _WALK, _KEY,
     ["write", "r/.git/shallow", _A + "\n"], ["held"], _WALK, _KEY,
     ["rm", "r/.git/shallow"], ["held"], _WALK, _KEY,
     ["write", "r/.git/refs/heads/main", _B + "\n"], ["held"], ["head", "r"],
     ["capture", "r", False],
     ["write", "r/.git/config", _INIT + "[diff]\n\tx = 1\n"], ["held"], _WALK, _KEY,
     ["dir", "r/.git/info"],
     ["held"], _KEY, ["write", "r/.git/info/attributes", "x\n"], ["held"],
     ["walk", "r", True], ["walk", "r/a", False], ["head", "r/a"],
     ["capture", "r", True], ["capture", "r/a", True]],
    # a checkout whose index or config git would not read as the key does
    [*_repo(config=_INIT + "[x\n"), _WALK, ["capture", "r", False], ["head", "r"], _KEY,
     ["held"]],
    [*_repo(config=_INIT + "[extensions]\n\tobjectFormat = sha256\n"),
     ["bytes", "r/.git/index", _ix(_gm())], _WALK],
    [*_repo(), ["bytes", "r/.git/index", _runs(b"XIRC" + bytes(8))], _WALK, _KEY,
     ["held"]],
    [*_repo(), ["write", "r/.git/sharedindex.0", "x"],
     ["bytes", "r/.git/index", _ix(_gm())], _WALK],
    [*_repo(), ["rm", "r/.git/config"], ["dir", "r/.git/config"], _WALK, ["capture", "r", False]],
    [*_repo(), ["rm", "r/.git/config"], _WALK, ["dir", "r/.git/shallow"], _WALK],
    [*_repo(config=_INIT + "[extensions]\n\tworktreeConfig = true\n"), _WALK, _KEY,
     ["held"], ["write", "r/.git/config.worktree", "[a]\n\tb = 1\n"], ["held"], _WALK,
     _KEY, ["write", "r/.git/config.worktree", "[x\n"], _WALK,
     ["rm", "r/.git/config.worktree"], ["dir", "r/.git/config.worktree"], _WALK],
    # HEAD in other shapes
    [*_repo(), ["write", "r/.git/HEAD", _B + "\n"], ["head", "r"],
     ["capture", "r", False], ["write", "r/.git/HEAD", "ref: refs/tags/t\n"],
     ["head", "r"], ["capture", "r", False], ["write", "r/.git/HEAD", "junk\n"],
     ["head", "r"], ["capture", "r", False], ["capture", "r", True],
     ["rm", "r/.git/refs/heads/main"], ["write", "r/.git/HEAD", "ref: refs/heads/main\n"],
     ["head", "r"], ["capture", "r", False], _WALK],
    # a bare repository
    [*_repo(git="b.git", config=_BARE), ["capture", "b.git", False],
     ["capture", "b.git", True], ["walk", "b.git", True], ["head", "b.git"],
     ["key", "b.git", "b.git", ["x"]], ["held"]],
    # a linked worktree
    [*_repo(git="main/.git"), ["write", "main/.git/refs/heads/w", _B + "\n"],
     ["dir", "main/.git/worktrees/w"],
     ["write", "main/.git/worktrees/w/HEAD", "ref: refs/heads/w\n"],
     ["write", "main/.git/worktrees/w/commondir", "../..\n"],
     ["write", "main/.git/worktrees/w/gitdir", "<root>/w/.git\n"],
     ["dir", "w"], ["write", "w/.git", "gitdir: <root>/main/.git/worktrees/w\n"],
     ["capture", "w", False], ["capture", "w", True], ["walk", "w", True],
     ["head", "w"], ["head", "main"], ["key", "w", "w", ["f"]], ["held"],
     ["write", "main/.git/shallow", _A + "\n"], ["held"], ["walk", "w", False],
     ["bytes", "main/.git/worktrees/w/index", _ix(_gm())], ["held"],
     ["walk", "w", False]],
    # a .git file naming a git directory elsewhere
    [*_repo(git="store"), ["dir", "g"], ["write", "g/.git", "gitdir: ../store\n"],
     ["capture", "g", False], ["walk", "g", True], ["head", "g"],
     ["write", "g/.git", "gitdir: nowhere\n"], ["capture", "g", False],
     ["walk", "g", False], ["head", "g"]],
    # outside any repository
    [["dir", "x"], ["capture", "x", False], ["capture", "x", True],
     ["walk", "x", True], ["head", "x"], ["key", "x", "x", ["a"]], ["held"],
     ["capture", "nowhere", False], ["walk", "nowhere", False], ["head", "nowhere"]],
]  # fmt: skip


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
    elif kind == "rm":
        at.unlink()
    else:
        raise ValueError(kind)


def _s(stamp: Any) -> Any:
    return None if stamp is None else "s"


def _digest(fields: Any) -> str:
    return hashlib.sha256(dumps_line(fields).encode("ascii")).hexdigest()


def _first(sigs: list[tuple[str, Any]], kind: str, sig: Any) -> int:
    for i, (k, s) in enumerate(sigs):
        if k == kind and s == sig:
            return i
    return -1


def _text(path: object, root: str) -> str:
    return str(path).replace(root, "<root>")


@contextmanager
def _owner(system: SimpleNamespace, other: bool) -> Iterator[None]:
    if not other:
        yield
        return
    euid = os.geteuid()  # type: ignore[attr-defined,unused-ignore]
    with mock.patch.object(system, "geteuid", lambda: euid + 1):
        yield


class _Run:
    def __init__(self, root: str, system: SimpleNamespace) -> None:
        self.root = root
        self.system = system
        self.sigs: list[tuple[str, Any]] = []
        self.stamps: list[Any] = []
        self.keyed: tuple[Any, ...] | None = None

    def step(self, step: list[Any]) -> Any:
        kind = step[0]
        if kind in ("dir", "write", "bytes", "rm"):
            _change(step, self.root)
            return None
        root = self.root
        if kind == "held":
            assert self.keyed is not None
            cwd, at, repo, chains, walk, held = self.keyed
            return files_held(cwd, at, repo, chains, walk, held)
        at = Path(root) / step[1]
        if kind == "capture":
            with _owner(self.system, step[2]):
                sig, gd = capture_files(at)
            first = sig[0]
            if first is not None and not isinstance(first, str):
                first = "undetermined"
            elif first is not None:
                first = _text(first, root)
            repo = None
            if gd is not None:
                repo = [_text(gd.gitdir, root), _text(gd.commondir, root),
                        _text(gd.worktree_root, root)]  # fmt: skip
            _, head, config, head_file, refs, packed, *env = sig
            assert head is None or isinstance(head, bytes)
            assert refs is None or isinstance(refs, tuple)
            digest = _digest([
                first, None if head is None else head.hex(),
                _stamp(config, self.stamps), _stamp(head_file, self.stamps),
                None if refs is None else [_stamp(ref, self.stamps) for ref in refs],
                _stamp(packed, self.stamps), *env,
            ])  # fmt: skip
            answer = [first, _first(self.sigs, kind, sig), repo, digest]
            self.sigs.append((kind, sig))
            return answer
        if kind == "walk":
            hold: dict[str, Any] | None = {} if step[2] else None
            walk = walk_files_signature(at, directories=hold)
            spelled = fields = None
            if walk is not None:
                slot: Any = walk[3]
                if slot == origin_gitfiles._INDEX_UNMERGED:
                    slot = "unmerged"
                elif slot is not None:
                    slot = list(slot)
                spelled = [_s(walk[0]), _s(walk[1]), _s(walk[2]), slot, _s(walk[4])]
                fields = [
                    *[_stamp(s, self.stamps) for s in walk[:3]],
                    slot,
                    _stamp(walk[4], self.stamps),
                ]
            keys = None if hold is None else sorted(_text(k, root) for k in hold)
            answer = [spelled, _first(self.sigs, kind, walk), keys, _digest(fields)]
            self.sigs.append((kind, walk))
            return answer
        if kind == "key":
            cwd = at
            top = Path(root) / step[2]
            held = {}
            repo = attribute_files_signature(cwd, top, directories=held)
            chain = gitattributes_signature(top, step[3], directories=held)
            walk = walk_files_signature(top, directories=held)
            self.keyed = (cwd, top, repo, {tuple(step[3]): chain}, walk, held)
            return [repo is None, chain is None, walk is None]
        if kind == "head":
            return located_head(at)
        raise ValueError(kind)


@contextmanager
def _machine(root: Path) -> Iterator[SimpleNamespace]:
    """Only the reference sees the scratch HOME, empty git environment and pin directory."""
    system = SimpleNamespace(**(vars(os) | {"environ": {"HOME": str(root / "h")}}))
    with (
        mock.patch.object(githead, "os", system),
        mock.patch.object(gitfile_rule, "os", system),
        mock.patch.object(origin_gitfiles, "os", system),
        mock.patch.object(gitdir_pin, "pins_dir", lambda: root / "pins"),
    ):
        yield system


@contextmanager
def _packed() -> Iterator[None]:
    """`githead`'s packed-refs memos given back as they were."""
    reads = OrderedDict(githead._PACKED_READS)
    stamped = OrderedDict(githead._PACKED_STAMPED)
    try:
        yield
    finally:
        githead._PACKED_READS.clear()
        githead._PACKED_READS.update(reads)
        githead._PACKED_STAMPED.clear()
        githead._PACKED_STAMPED.update(stamped)


def generate_posix() -> dict[str, bytes]:
    refuse_root()
    rows: list[Any] = []
    with (
        tempfile.TemporaryDirectory() as raw,
        _machine(Path(raw)) as system,
        _git(),
        _memos(),
        _packed(),
    ):
        base = Path(raw) / "ogf-walk"
        for i, steps in enumerate(SCENARIOS):
            scratch = base / str(i)
            scratch.mkdir(parents=True)
            root = os.path.realpath(scratch)
            origin_gitfiles._clear_index_memo()
            origin_gitfiles._clear_config_memo()
            run = _Run(root, system)
            try:
                answers = [run.step(step) for step in steps]
            finally:
                shutil.rmtree(scratch)
            rows.append(["scenario", steps, answers])
    header = {"fixture": "origin_gitfiles_walk_posix", "scenarios": len(rows)}
    return {"origin_gitfiles_walk_posix.jsonl": jsonl([header, *rows])}
