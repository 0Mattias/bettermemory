"""Fixtures: the per-stamp memo of config facts, whether a repository's
config may move its working tree, and the root read from the files where
``git rev-parse`` exited non-zero (``origin_gitfiles_root_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_gitfiles/root.rs``.

POSIX only (`generate.POSIX_ONLY`): the scenarios make links, a FIFO and
a mode-000 file, and the root is spelled by the host's ``Path`` (and on
macOS by ``F_GETPATH``). The git on PATH is data: `gitsystem.is_at_least`
and `gitfile_rule`'s release and compat reads are patched to release
2.54, as `gen_githead_discovery` patches them.

Each row is ``["scenario", steps, answers]``: a scenario in a fresh
scratch folder (``<root>``, resolved), with an empty memo, each step's
answer in order (null for a step that changes the tree):

- ``["dir", rel]``, ``["write", rel, text]``, ``["big", rel, n]`` (a
  ``[core]`` line and comment bytes to ``n`` bytes in all),
  ``["link", rel, target]``, ``["fifo", rel]``, ``["chmod", rel, mode]``,
  ``["rm", rel]``: changes, each made `PAUSE` after the step before.
- ``["facts", rel]``: `_config_facts`: null, or ``[missing, facts]`` with
  ``missing`` whether the stamp is None and ``facts`` as `_spell` spells
  them.
- ``["moves", rel]``: `_config_moves_worktree` of what `githead.locate`
  finds from ``rel`` (null where it finds none).
- ``["wtc", rel]``: `_worktree_config_facts` of what `locate` finds from
  ``rel``, under its common config's facts, spelled as ``facts``.
- ``["root", rel]``: `refused_worktree_root`: ``[root, indeterminate]``,
  the root with the scratch folder spelled ``<root>``.
- ``["memo"]``: the paths the memo holds, oldest first.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import gitfile_rule, githead, gitsystem, origin_gitfiles
from bettermemory.origin_gitfiles import (
    _config_facts,
    _config_moves_worktree,
    _ConfigFacts,
    _worktree_config_facts,
    refused_worktree_root,
)

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root

#: The pause before each change, as `gen_origin_gitfiles_facts.PAUSE`.
PAUSE = 0.05

#: The release the git on PATH is read as.
RELEASE = (2, 54)

_INIT = (
    "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n"
    "\tbare = false\n\tlogallrefupdates = true\n"
)


def _repo(git: str = "r/.git", config: str = _INIT) -> list[list[Any]]:
    return [
        ["dir", git],
        ["dir", git + "/objects"],
        ["dir", git + "/refs"],
        ["write", git + "/HEAD", "ref: refs/heads/main\n"],
        ["write", git + "/config", config],
    ]


def _all(rel: str = "r") -> list[list[Any]]:
    return [["moves", rel], ["root", rel], ["facts", "r/.git/config"]]


SCENARIOS: list[list[list[Any]]] = [
    [["dir", "x"], ["root", "x"], ["moves", "x"], ["facts", "x/config"], ["memo"]],
    [*_repo(), *_all(), ["root", "r/.git"], ["memo"]],
    [*_repo(), ["dir", "r/a/b"], ["root", "r/a/b"], ["moves", "r/a/b"]],
    [*_repo(config="[core]\n\tworktree = /elsewhere\n"), *_all()],
    [*_repo(config="[core]\n\tbare = true\n"), *_all()],
    [*_repo(config="[core]\n\tbare\n"), *_all()],
    [*_repo(config="[core]\n\tbare = no\n"), *_all()],
    [*_repo(config="[core]\n\tbare = OFF\n"), *_all()],
    [*_repo(config="[core]\n\tbare = false ; c\n"), *_all()],
    [*_repo(config="[core]\n\tbare = 0\r\n"), *_all()],
    [*_repo(config="[branch \"core.worktree\"]\n\tremote = x\n"), *_all()],
    [*_repo(config="[remote \"o\"]\n\tworktree = x\n"), *_all()],
    [*_repo(config="[core\n"), *_all()],
    [*_repo(config="[core]\n\tbigfilethreshold = -1\n"), *_all()],
    [*_repo(config="[core]\n\trepositoryformatversion = 2\n"), *_all()],
    [*_repo(config="[include]\n\tpath = other\n"), *_all()],
    [*_repo(config="[core]\n\tattributesfile = x\n"), *_all()],
    [*_repo(config=""), *_all()],
    [*_repo(), ["rm", "r/.git/config"], *_all()],
    [*_repo(), ["rm", "r/.git/config"], ["dir", "r/.git/config"], *_all()],
    [*_repo(), ["rm", "r/.git/config"], ["fifo", "r/.git/config"], *_all()],
    [*_repo(), ["chmod", "r/.git/config", 0], *_all(), ["chmod", "r/.git/config", 0o644]],
    [*_repo(), ["rm", "r/.git/config"], ["write", "real", _INIT],
     ["link", "r/.git/config", "../../real"], *_all(), ["memo"]],
    [*_repo(), ["rm", "r/.git/config"], ["link", "r/.git/config", "nowhere"], *_all()],
    [*_repo(), ["rm", "r/.git/config"], ["big", "r/.git/config", 1 << 20], *_all()],
    [*_repo(), ["rm", "r/.git/config"], ["big", "r/.git/config", (1 << 20) + 1],
     *_all()],
    [*_repo(), ["facts", "r/.git/config"], ["memo"],
     ["write", "r/.git/config", "[core]\n\tworktree = /w\n"],
     ["facts", "r/.git/config"], ["moves", "r"], ["root", "r"], ["memo"],
     ["write", "r/.git/config", _INIT], ["root", "r"], ["memo"]],
    [*_repo(config=_INIT + "[extensions]\n\tworktreeConfig = true\n"),
     ["wtc", "r"], *_all(),
     ["write", "r/.git/config.worktree", "[core]\n\tworktree = /w\n"],
     ["wtc", "r"], *_all(), ["memo"]],
    [*_repo(), ["write", "r/.git/config.worktree", "[core]\n\tworktree = /w\n"],
     ["wtc", "r"], *_all()],
    [*_repo(config=_INIT + "[extensions]\n\tworktreeConfig = false\n"),
     ["write", "r/.git/config.worktree", "[core]\n\tbare = true\n"],
     ["wtc", "r"], *_all()],
    [*_repo(config=_INIT + "[extensions]\n\tworktreeConfig\n"),
     ["dir", "r/.git/config.worktree"], ["wtc", "r"], *_all()],
    [*_repo(), ["dir", "r/.git/config.worktree"], *_all()],
    [*_repo(), ["write", "r/.git/config.worktree", "[x\n"], *_all()],
    [*_repo(git="main/.git"),
     ["dir", "main/.git/worktrees/w"],
     ["write", "main/.git/worktrees/w/HEAD", "ref: refs/heads/w\n"],
     ["write", "main/.git/worktrees/w/commondir", "../..\n"],
     ["write", "main/.git/worktrees/w/gitdir", "<root>/w/.git\n"],
     ["dir", "w"], ["write", "w/.git", "gitdir: <root>/main/.git/worktrees/w\n"],
     ["root", "w"], ["moves", "w"],
     ["write", "main/.git/worktrees/w/config.worktree", "[core]\n\tworktree = /w\n"],
     ["root", "w"],
     ["write", "main/.git/config", _INIT + "[extensions]\n\tworktreeConfig = 1\n"],
     ["wtc", "w"], ["root", "w"], ["memo"]],
    [["dir", "w"], ["write", "w/.git", "gitdir: nowhere\n"], ["root", "w"],
     ["moves", "w"]],
]  # fmt: skip


def _spell(facts: _ConfigFacts) -> list[Any]:
    names = None if facts.names is None else sorted(n.hex() for n in facts.names)
    return [
        names,
        facts.attributes_elsewhere,
        facts.bare_moves,
        facts.git_dies,
        facts.includes,
    ]


def _read(read: tuple[Any, _ConfigFacts] | None) -> Any:
    if read is None:
        return None
    return [read[0] is None, _spell(read[1])]


def _change(step: list[Any], root: str) -> None:
    kind = step[0]
    at = Path(root) / step[1]
    time.sleep(PAUSE)
    if kind == "dir":
        at.mkdir(parents=True)
    elif kind == "write":
        at.write_bytes(step[2].replace("<root>", root).encode())
    elif kind == "big":
        head = b"[core]\n#"
        at.write_bytes(head + b"x" * (step[2] - len(head)))
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


def _run_step(step: list[Any], root: str) -> Any:
    kind = step[0]
    if kind in ("dir", "write", "big", "link", "fifo", "chmod", "rm"):
        _change(step, root)
        return None
    if kind == "memo":
        return [k.replace(root, "<root>") for k in origin_gitfiles._CONFIG_MEMO]
    at = Path(root) / step[1]
    if kind == "facts":
        return _read(_config_facts(at))
    if kind == "root":
        found, indeterminate = refused_worktree_root(at)
        return [None if found is None else found.replace(root, "<root>"), indeterminate]
    gd = githead.locate(at)
    if gd is None:
        return None
    if kind == "moves":
        return _config_moves_worktree(gd)
    if kind == "wtc":
        common = _config_facts(gd.commondir / "config")
        if common is None:
            return "common unread"
        return _read(_worktree_config_facts(gd, common[1]))
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


def generate_posix() -> dict[str, bytes]:
    refuse_root()
    rows: list[Any] = []
    with _sandbox.world() as world, _git():
        base = world.root / "ogf-root"
        for i, steps in enumerate(SCENARIOS):
            scratch = base / str(i)
            scratch.mkdir(parents=True)
            root = os.path.realpath(scratch)
            origin_gitfiles._clear_config_memo()
            try:
                answers = [_run_step(step, root) for step in steps]
            finally:
                origin_gitfiles._clear_config_memo()
                shutil.rmtree(scratch)
            rows.append(["scenario", steps, answers])
    header = {"fixture": "origin_gitfiles_root_posix", "scenarios": len(rows)}
    return {"origin_gitfiles_root_posix.jsonl": jsonl([header, *rows])}
