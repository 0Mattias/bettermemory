"""Fixture: the repository git finds from a directory, as `location_git`
reads it: `_discover`, `_from_env`, `_at_dotgit` and the two deaths,
`GitDies` and `DiscoveryDies` (``location_discovery.jsonl``).

The native build reads it in
``native/crates/bettermemory-core/src/location_discovery.rs``.

Each case is a tree built in a fresh real directory of the scratch world
(``<s>`` below, also inside file texts and link targets), as items
``["f", path, text]``, ``["d", path]`` and ``["l", path, target]``
(paths and texts spelled by `_b`). `_discover` runs from ``"start"``
under ``GIT_CEILING_DIRECTORIES`` set to ``<s>``, so the walk stays in
the case, with the variables ``"env"`` names set (every other of
``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_COMMON_DIR`` and
``GIT_DISCOVERY_ACROSS_FILESYSTEM`` unset), ``read_head`` False where
``"rh"`` is 0 and ``refused`` None where ``"nr"`` is 1. The git on PATH
is data (release 2.54, ``_reads_compat`` True), the pin records an empty
folder and the read budget raised, as `gen_discovery` sets them.

A line records ``"ac"``, `githead.across_filesystems()` under the case's
environment; ``"pr"``, the call `githead.probed` received where it was
called: ``"here"``, ``"dev"`` (whether a device was given), ``"p"`` the
directories it yields and ``"u"`` how many the walk took; ``"a"`` the
answer: null, ``{"dotgit", "git_dir", "common", "root", "kind"}``,
``{"die": text}`` for `GitDies`, ``{"disc": text}`` for `DiscoveryDies`
or ``{"e": class}``; and ``"r"`` the refusals, ``[path, git directory,
why, FormatUnknown]``.

Gates: POSIX only (links, and paths spelled with ``/``). Nothing depends
on root (`refuse_root`), the umask, the host's users, home, the scratch
path or ``listdir``'s order.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import gitdir_pin, githead, location_git
from bettermemory import gitfile_rule as g

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_gitconfig_file import _b

HEAD = b"ref: refs/heads/main\n"
CONFIG = b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
BARE = b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)

Item = list[Any]


def _repo(at: str, config: bytes = CONFIG, head: bytes = HEAD) -> list[Item]:
    return [
        ["f", f"{at}/HEAD", head],
        ["f", f"{at}/config", config],
        ["d", f"{at}/objects"],
        ["d", f"{at}/refs"],
    ]


def _linked() -> list[Item]:
    return [
        *_repo("repo/.git"),
        ["f", "repo/.git/worktrees/w/HEAD", HEAD],
        ["f", "repo/.git/worktrees/w/commondir", b"../..\n"],
        ["f", "repo/.git/worktrees/w/gitdir", b"<s>/w/.git\n"],
        ["f", "w/.git", b"gitdir: <s>/repo/.git/worktrees/w\n"],
        ["d", "w/x"],
    ]


def _cases() -> Iterator[dict[str, Any]]:
    plain = [*_repo("repo/.git"), ["d", "repo/sub/deeper"]]
    yield {"note": "a .git directory", "t": plain, "start": "repo/sub/deeper"}
    yield {"note": "start at the top", "t": plain, "start": "repo"}
    yield {
        "note": "start spelled with dots",
        "t": plain,
        "start": "repo/sub/./deeper/../deeper/",
    }
    yield {"note": "no repository", "t": [["d", "a/b/c"]], "start": "a/b/c"}
    yield {"note": "a start that is missing", "t": [["d", "a"]], "start": "a/none"}
    yield {"note": "linked worktree", "t": _linked(), "start": "w/x"}
    sep = [
        *_repo("store/sep"),
        ["f", "proj/.git", b"gitdir: <s>/store/sep\n"],
        ["d", "proj/in"],
    ]
    yield {
        "note": "a .git file to a git directory kept apart",
        "t": sep,
        "start": "proj/in",
    }
    yield {
        "note": "a .git file naming nothing",
        "t": [["f", "p/.git", b"gitdir: <s>/none\n"]],
        "start": "p",
    }
    yield {
        "note": "a .git file of junk",
        "t": [["f", "p/.git", b"junk\n"]],
        "start": "p",
    }
    yield {"note": "an empty .git file", "t": [["f", "p/.git", b""]], "start": "p"}
    unreg = [
        *_repo("repo/.git"),
        ["f", "repo/.git/worktrees/w/HEAD", HEAD],
        ["f", "repo/.git/worktrees/w/commondir", b"../..\n"],
        ["f", "repo/.git/worktrees/w/gitdir", b"<s>/other/.git\n"],
        ["f", "w/.git", b"gitdir: <s>/repo/.git/worktrees/w\n"],
    ]
    yield {
        "note": "a .git file to a worktree registered elsewhere",
        "t": unreg,
        "start": "w",
    }
    yield {
        "note": "a .git file to a worktree registered elsewhere, refused None",
        "t": unreg,
        "start": "w",
        "nr": 1,
    }
    bare = [*_repo("bare.git", BARE), ["d", "bare.git/refs/heads"]]
    yield {
        "note": "a bare repository from inside",
        "t": bare,
        "start": "bare.git/refs/heads",
    }
    yield {"note": "a bare repository at its top", "t": bare, "start": "bare.git"}
    yield {
        "note": "inside a .git directory",
        "t": [*plain, ["d", "repo/.git/refs/heads"]],
        "start": "repo/.git/refs/heads",
    }
    nobare = [*_repo("g", CONFIG), ["d", "g/refs/tags"]]
    yield {
        "note": "inside a git directory not named .git",
        "t": nobare,
        "start": "g/refs/tags",
    }
    moved = [
        *_repo(
            "m",
            b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tworktree = <s>/elsewhere\n",
        ),
        ["d", "elsewhere"],
    ]
    yield {
        "note": "inside a git directory whose core.worktree is elsewhere",
        "t": moved,
        "start": "m",
    }
    yield {"note": "the same, refused None", "t": moved, "start": "m", "nr": 1}
    inmoved = [
        *_repo(
            "r2/.git",
            b"[core]\n\trepositoryformatversion = 0\n\tworktree = <s>/elsewhere\n",
        ),
        ["d", "r2/.git/refs/heads"],
        ["d", "elsewhere"],
    ]
    yield {
        "note": "inside a .git directory whose core.worktree is elsewhere",
        "t": inmoved,
        "start": "r2/.git/refs/heads",
    }
    yield {
        "note": "the same, refused None",
        "t": inmoved,
        "start": "r2/.git/refs/heads",
        "nr": 1,
    }
    other = [*_repo("repo/.git"), ["f", "p/.git", b"gitdir: <s>/repo/.git\n"]]
    yield {
        "note": "a .git file naming another checkout's .git",
        "t": other,
        "start": "p",
    }
    yield {"note": "the same, refused None", "t": other, "start": "p", "nr": 1}
    yield {
        "note": "a .git directory whose commondir is a folder",
        "t": [*_repo("q/.git"), ["d", "q/.git/commondir"]],
        "start": "q",
    }
    yield {
        "note": "a .git directory whose commondir names nothing",
        "t": [*_repo("q/.git"), ["f", "q/.git/commondir", b"<s>/none\n"]],
        "start": "q",
    }
    yield {
        "note": "a .git directory whose own config names a working tree elsewhere",
        "t": [
            *_repo(
                "q/.git",
                b"[core]\n\trepositoryformatversion = 0\n\tworktree = <s>/elsewhere\n",
            ),
            ["d", "elsewhere"],
        ],
        "start": "q",
    }
    yield {
        "note": "a .git file naming a .git of an unknown format",
        "t": [
            *_repo("repo/.git", b"[core]\n\trepositoryformatversion = 2\n"),
            ["f", "p/.git", b"gitdir: <s>/repo/.git\n"],
        ],
        "start": "p",
    }
    future = b"[core]\n\trepositoryformatversion = 2\n"
    yield {
        "note": "a .git directory of an unknown format",
        "t": [*_repo("u/.git", future), ["d", "u/in"]],
        "start": "u/in",
    }
    yield {
        "note": "a bare directory of an unknown format",
        "t": _repo("ub", future),
        "start": "ub",
    }
    yield {
        "note": "a bare directory of an unknown format, refused None",
        "t": _repo("ub", future),
        "start": "ub",
        "nr": 1,
    }
    broken = b"[core\n"
    yield {
        "note": "a .git directory whose config git refuses",
        "t": [*_repo("k/.git", broken)],
        "start": "k",
    }
    foreign = [
        *plain,
        ["f", "x/HEAD", b"junk\n"],
        ["f", "x/commondir", b"<s>/repo/.git\n"],
        ["d", "x/in"],
    ]
    yield {"note": "a foreign commondir", "t": foreign, "start": "x/in"}
    yield {
        "note": "a foreign commondir, refused None",
        "t": foreign,
        "start": "x/in",
        "nr": 1,
    }
    junkhead = [*_repo("h/.git", head=b"junk\n"), ["d", "h/in"]]
    yield {"note": "a HEAD git does not read", "t": junkhead, "start": "h/in"}
    yield {
        "note": "a HEAD git does not read, read_head False",
        "t": junkhead,
        "start": "h/in",
        "rh": 0,
    }
    yield {
        "note": "a bare HEAD git does not read, read_head False",
        "t": _repo("hb", BARE, b"junk\n"),
        "start": "hb",
        "rh": 0,
    }
    links = [*_repo("repo/.git"), ["d", "l"], ["l", "l/.git", "<s>/repo/.git"]]
    yield {"note": "a .git link to a git directory", "t": links, "start": "l"}
    yield {
        "note": "a dangling .git link",
        "t": [["d", "l"], ["l", "l/.git", "<s>/none"]],
        "start": "l",
    }
    # The environment.
    yield {
        "note": "across true",
        "t": plain,
        "start": "repo/sub",
        "env": {"GIT_DISCOVERY_ACROSS_FILESYSTEM": "true"},
    }
    yield {
        "note": "across 0",
        "t": plain,
        "start": "repo/sub",
        "env": {"GIT_DISCOVERY_ACROSS_FILESYSTEM": "0"},
    }
    yield {
        "note": "across empty",
        "t": plain,
        "start": "repo/sub",
        "env": {"GIT_DISCOVERY_ACROSS_FILESYSTEM": ""},
    }
    yield {
        "note": "across junk",
        "t": plain,
        "start": "repo/sub",
        "env": {"GIT_DISCOVERY_ACROSS_FILESYSTEM": "junk"},
    }
    junk_dir = {"GIT_DISCOVERY_ACROSS_FILESYSTEM": "junk", "GIT_DIR": "<s>/repo/.git"}
    yield {
        "note": "across junk with GIT_DIR set",
        "t": plain,
        "start": "repo/sub",
        "env": junk_dir,
    }
    yield {
        "note": "GIT_DIR empty walks",
        "t": plain,
        "start": "repo/sub",
        "env": {"GIT_DIR": ""},
    }
    yield {
        "note": "GIT_DIR absolute",
        "t": plain,
        "start": "repo/sub",
        "env": {"GIT_DIR": "<s>/repo/.git"},
    }
    yield {
        "note": "GIT_DIR relative",
        "t": plain,
        "start": "repo/sub",
        "env": {"GIT_DIR": "../.git/"},
    }
    yield {
        "note": "GIT_DIR with a start spelled with dots",
        "t": plain,
        "start": "repo/./sub/",
        "env": {"GIT_DIR": "../.git"},
    }
    yield {
        "note": "GIT_DIR and GIT_WORK_TREE",
        "t": plain,
        "start": "repo/sub",
        "env": {"GIT_DIR": "../.git", "GIT_WORK_TREE": "../x/../wt"},
    }
    yield {
        "note": "GIT_DIR and a relative GIT_WORK_TREE from a start elsewhere",
        "t": [*plain, ["d", "x"]],
        "start": "x",
        "env": {"GIT_DIR": "<s>/repo/.git", "GIT_WORK_TREE": "tree"},
    }
    yield {
        "note": "GIT_DIR and an empty GIT_WORK_TREE",
        "t": plain,
        "start": "repo/sub",
        "env": {"GIT_DIR": "../.git", "GIT_WORK_TREE": ""},
    }
    yield {
        "note": "GIT_DIR naming nothing",
        "t": [["d", "a"]],
        "start": "a",
        "env": {"GIT_DIR": "nothing"},
    }
    yield {
        "note": "GIT_DIR bare",
        "t": bare,
        "start": "bare.git",
        "env": {"GIT_DIR": "."},
    }
    wt = [
        *_repo(
            "c", b"[core]\n\trepositoryformatversion = 0\n\tworktree = ../tree/./x\n"
        ),
        ["d", "tree"],
    ]
    yield {
        "note": "GIT_DIR with a relative core.worktree",
        "t": wt,
        "start": "tree",
        "env": {"GIT_DIR": "<s>/c"},
    }
    empty_wt = _repo("c", b"[core]\n\trepositoryformatversion = 0\n\tworktree =\n")
    yield {
        "note": "GIT_DIR with an empty core.worktree",
        "t": empty_wt,
        "start": "c",
        "env": {"GIT_DIR": "."},
    }
    yield {
        "note": "GIT_DIR with an empty core.worktree, refused None",
        "t": empty_wt,
        "start": "c",
        "env": {"GIT_DIR": "."},
        "nr": 1,
    }
    yield {
        "note": "GIT_DIR with an empty core.worktree and GIT_WORK_TREE",
        "t": empty_wt,
        "start": "c",
        "env": {"GIT_DIR": ".", "GIT_WORK_TREE": "t"},
    }
    yield {
        "note": "GIT_DIR with an empty core.worktree, bare",
        "t": _repo(
            "c", b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n\tworktree =\n"
        ),
        "start": "c",
        "env": {"GIT_DIR": "."},
    }
    yield {
        "note": "GIT_DIR of an unknown format",
        "t": _repo("ub", future),
        "start": "ub",
        "env": {"GIT_DIR": "."},
    }
    yield {
        "note": "GIT_DIR of a config git refuses",
        "t": _repo("ub", broken),
        "start": "ub",
        "env": {"GIT_DIR": "."},
    }
    yield {
        "note": "GIT_DIR naming a linked worktree",
        "t": _linked(),
        "start": "w",
        "env": {"GIT_DIR": "<s>/repo/.git/worktrees/w"},
    }
    yield {
        "note": "GIT_DIR and GIT_COMMON_DIR",
        "t": plain,
        "start": "repo",
        "env": {"GIT_DIR": ".git", "GIT_COMMON_DIR": "../elsewhere"},
    }
    yield {
        "note": "GIT_COMMON_DIR alone",
        "t": _linked(),
        "start": "w/x",
        "env": {"GIT_COMMON_DIR": "<s>/repo/.git"},
    }


def _build(base: str, items: list[Item]) -> None:
    for item in items:
        kind, path = item[0], os.path.join(base, item[1])
        os.makedirs(path if kind == "d" else os.path.dirname(path), exist_ok=True)
        if kind == "f":
            with open(path, "wb") as fh:
                fh.write(item[2].replace(b"<s>", os.fsencode(base)))
        elif kind == "l":
            os.symlink(item[2].replace("<s>", base), path)


def _spelled(items: list[Item]) -> list[Item]:
    return [[x if isinstance(x, str) else _b(x) for x in item] for item in items]


def _row(case: dict[str, Any], base: str, pins: str) -> dict[str, Any]:
    def rel(path: str | None) -> str | None:
        return None if path is None else path.replace(base, "<s>")

    _build(base, case["t"])
    probe: dict[str, Any] = {}
    real_probed = githead.probed

    def probed(here: str, device: int | None) -> Iterator[str]:
        listed = list(real_probed(here, device))
        probe.update(
            here=rel(here), dev=device is not None, p=[rel(d) for d in listed], u=0
        )
        for d in listed:
            probe["u"] += 1
            yield d

    env = {k: v.replace("<s>", base) for k, v in case.get("env", {}).items()}
    refused: list[tuple[str, str, str]] | None = None if case.get("nr") else []
    with (
        patch.dict(os.environ),
        patch.object(g, "_git_is_at_least", lambda want: (2, 54) >= want),
        patch.object(g, "_reads_compat", lambda: True),
        patch.object(g, "_RECORDED_BUDGET", 60.0),
        patch.object(gitdir_pin, "pins_dir", lambda: Path(pins)),
        patch.object(githead, "probed", probed),
    ):
        for name in _VARS:
            os.environ.pop(name, None)
        os.environ.update(env)
        os.environ["GIT_CEILING_DIRECTORIES"] = base
        across = githead.across_filesystems()
        start = os.path.join(base, case["start"])
        answer: Any
        try:
            found = location_git._discover(
                start, refused, read_head=case.get("rh", 1) == 1
            )
        except location_git.DiscoveryDies as e:
            answer = {"disc": str(e)}
        except location_git.GitDies as e:
            answer = {"die": rel(str(e))}
        except Exception as e:  # noqa: BLE001 - recorded as the reference raises it
            answer = {"e": type(e).__name__}
        else:
            answer = None
            if found is not None:
                answer = {
                    "dotgit": rel(found.dotgit),
                    "git_dir": rel(found.git_dir),
                    "common": rel(found.common_dir),
                    "root": rel(found.root),
                    "kind": found.kind,
                }
    row: dict[str, Any] = {
        "note": case["note"],
        "t": _spelled(case["t"]),
        "start": case["start"],
    }
    for key in ("env", "rh", "nr"):
        if key in case:
            row[key] = case[key]
    row["ac"] = across
    if probe:
        row["pr"] = probe
    row["a"] = answer
    row["r"] = [
        [rel(p), rel(d), rel(str(w)), int(isinstance(w, g.FormatUnknown))]
        for p, d, w in (refused or [])
    ]
    return row


def _file() -> bytes:
    refuse_root()
    rows: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        top = os.path.realpath(world.root / "location-discovery")
        os.makedirs(os.path.join(top, "pins"))
        for i, case in enumerate(_cases()):
            base = os.path.join(top, str(i))
            os.makedirs(base)
            rows.append(_row(case, base, os.path.join(top, "pins")))
    return jsonl([{"fixture": "location_discovery", "cases": len(rows)}, *rows])


def generate() -> dict[str, bytes]:
    return {"location_discovery.jsonl": _file()}
