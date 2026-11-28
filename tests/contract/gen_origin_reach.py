"""Origin's repository root and head reads, `commit_reachable` and the
reachable walk (`origin.py`'s unit origin-3a).

``origin_reach.jsonl`` runs each function over scripted git replies and
scripted file reads: every git call's timeout and argv (after git's pins and
the as-stored config, the null device spelled ``<devnull>``), every file
read in order, the answer, the five tallies of `gen_origin_git` and the log
lines. `printed_path` is read as POSIX's (`_git._WINDOWS` false) and no path
holds a separator, so the file is the same on every host.

``origin_reach_posix.jsonl`` asks the host's git in repositories made in a
temporary directory, with HOME and git's global and system config isolated
as `gen_origin_git` isolates them: linear, a merge, a branch that is no
ancestor, a shallow file cutting a merge's side, a shallow clone, a linked
worktree, ``core.worktree`` set, a bare repository, one with no commit, a
machine config git may die reading and reads started from a subdirectory.
The temporary directory must lie outside any repository: the generator
refuses one with a ``.git`` or ``HEAD`` entry above it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bettermemory import (
    _git,
    git_read_budget,
    gitfile_rule,
    githead,
    location_git,
    origin,
    origin_history,
)

from bettermemory.origin_history import ReachableWalk

from ._common import captured_by, jsonl, text_field
from .gen_discovery import _clear_above
from .gen_fs_realpath import refuse_root
from .gen_origin_git import _counts, _Lines, _totals
from .gen_origin_gitfiles_attrs import _git as _pinned_git
from .gen_origin_gitfiles_attrs import _memos
from .gen_origin_gitfiles_walk import _machine, _packed

_FILE = "origin_reach.jsonl"
_POSIX = "origin_reach_posix.jsonl"

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
L = "0123456789abcdef" * 4
M = "\x01"


def _r(text: str = "", code: int = 0, error: str = "") -> list[Any]:
    return [code, text, error]


_FOUND: dict[str, Any] = {
    "gitdir": True,
    "head": A,
    "moves": False,
    "dies": False,
    "spelled": "wt",
}


def _cases() -> Iterator[dict[str, Any]]:
    script: list[Any]
    leaves: dict[str, Any]
    top = "repo_toplevel"
    for script, leaves in (
        ([_r("top\n")], {}),
        ([_r("top \n")], {}),
        ([_r("top\r\n")], {}),
        ([_r("a\nb\n")], {}),
        ([_r("top//\n")], {}),
        ([_r("top")], {}),
        ([_r("\n")], {}),
        ([_r("top\n\n")], {}),
        ([_r(code=128, error="fatal: not a git repository\n")], {}),
        (["timeout"], {}),
        (["missing"], {}),
        ([_r("top\n")], {"agrees": False}),
        ([_r("top\n")], {"resolve": "os"}),
        ([_r("top\n")], {"resolve": "value"}),
        ([], {"refusal": True}),
    ):
        yield {"fn": top, "cwd": "sample", "script": script, "leaves": leaves}
    yield {"fn": top, "cwd": None, "script": [], "leaves": {}}

    pair = "repo_toplevel_and_head"
    fallback = {"gitdir": False}
    for script, leaves in (
        ([], _FOUND),
        ([_r(f"top\n{A}\n")], fallback),
        ([_r(f"a\nb\n{A}\n")], fallback),
        ([_r(f"top \n{A}")], fallback),
        ([_r(f"top\r\n{A}\r\n")], fallback),
        ([_r(f"{A}\n")], fallback),
        ([_r("top\nabc\n")], fallback),
        ([_r(f"top\n{A.upper()}\n")], fallback),
        ([_r(f"top\n{L}\n")], fallback),
        ([_r(f"top\n{A}\n\n")], fallback),
        ([_r(f"top\n{A}x\n")], fallback),
        ([_r(code=128, error="fatal: bad revision 'HEAD'\n")], fallback),
        (["timeout"], fallback),
        ([_r(f"top\n{A}\n")], {**fallback, "agrees": False}),
        ([_r(f"top\n{A}\n")], {**fallback, "resolve": "os"}),
        ([_r(f"top\n{A}\n")], {**_FOUND, "dies": True}),
        ([_r(f"top\n{A}\n")], {**_FOUND, "moves": True}),
        ([_r(f"top\n{A}\n")], {**_FOUND, "spelled": None}),
    ):
        yield {"fn": pair, "cwd": "sample", "script": script, "leaves": leaves}
    yield {"fn": pair, "cwd": None, "script": [], "leaves": {}}

    files = "toplevel_and_head_from_files"
    for leaves in (
        {"gitdir": False},
        {**_FOUND, "head": None},
        {**_FOUND, "moves": True, "dies": True, "spelled": None},
        {**_FOUND, "dies": True, "spelled": None},
        {**_FOUND, "spelled": None},
        {**_FOUND, "spelled": "value"},
        _FOUND,
        {**_FOUND, "head": L, "spelled": "a\nb "},
    ):
        yield {"fn": files, "cwd": "sample", "script": [], "leaves": leaves}
    yield {"fn": files, "cwd": None, "script": [], "leaves": {}}

    for script in (
        [_r(f"{A}\n")],
        [_r(f"  {A.upper()}\t\r\n")],
        [_r(f"{L}\n")],
        [_r("abc\n")],
        [_r(f"{A[:-1]}\uff21\n")],
        [_r(f"{A}\n{A}\n")],
        [_r()],
        [_r(code=1)],
        ["timeout"],
    ):
        yield {"fn": "head_sha", "cwd": "sample", "script": script, "leaves": {}}
    yield {"fn": "head_sha", "cwd": None, "script": [], "leaves": {}}

    reach = "commit_reachable"
    no = _r(code=1)
    shallow = _r("false\n")
    other = _r(code=128, error="fatal: Not a valid commit name\n")
    for sha, script in (
        (A, [_r()]),
        (A, ["timeout"]),
        (A, ["missing"]),
        (A, [_r(code=1, error="error: object not found\n")]),
        (A, [_r(code=-9)]),
        (A, [no, shallow]),
        (A, [_r(code=1, error=" \n"), shallow]),
        (A, [no, _r(" false \r\n")]),
        (A, [no, _r("true\n")]),
        (A, [no, _r("False\n")]),
        (A, [no, _r(code=128)]),
        (A, [no, "timeout"]),
        (A, [other, shallow, _r(f"{B}\n"), _r(code=1)]),
        (A, [_r(code=2), shallow, _r(f"{B}\n"), _r(code=1)]),
        (A, [other, shallow, _r(code=128)]),
        (A, [other, shallow, _r(f"{B}\n"), _r()]),
        (A, [other, shallow, _r(f"{B}\n"), _r(code=1, error="fatal: bad\n")]),
        (A, [other, shallow, _r(f"{B}\n"), _r(code=128)]),
        (A, [other, shallow, _r(f"{B}\n"), "timeout"]),
        (A, [other, _r("true\n")]),
        (L, [no, shallow]),
        ("abc", []),
        (A.upper(), []),
        (A + "a", []),
    ):
        yield {"fn": reach, "cwd": "sample", "sha": sha, "script": script, "leaves": {}}
    yield {"fn": reach, "cwd": None, "sha": A, "script": [], "leaves": {}}
    yield {
        "fn": reach,
        "cwd": "sample",
        "sha": A,
        "script": [],
        "leaves": {"refusal": True},
    }

    walk = "walk"
    linear = f'{M}>{C}\n\nfile a\n"q\\303\\251"\n{M}>{B}\n\n b \n{M}-{A}\n'
    for anchor, head, names, script, cut in (
        (A, A, True, [], []),
        (A, C, True, [_r(linear)], []),
        (A, C, False, [_r(f"{M}>{C}\n{M}>{B}\n{M}-{A}\n")], []),
        (A, C, True, [_r(linear)], None),
        (A, C, True, [_r(linear)], [B]),
        (A, C, True, [_r(linear)], [A, D]),
        (A, C, True, [_r(f"{M}>{C}\nf\n{M}-{A}\n")], [C]),
        (D, C, True, [_r(linear)], []),
        (A, C, True, [_r(f"{M}-{A}\n{M}<{C}\nx\n{M}?{B}\ny\n{M}-{D}\nz\n")], []),
        (A, C, True, [_r(f"{M}>{C.upper()}\r\nf\r\n{M}- {A} \r\n")], []),
        (A, C, True, [_r(f"lead\n{M}>{C}\nf\n{M}>{C}\ng\n{M}-{A}\n")], []),
        (A, C, True, [_r(f"{M}>{C}\n{M}>abc\n{M}-{A}\n")], []),
        (A, C, True, [_r(f"{M}>{C}\n{M}\n{M}-{A}\n")], []),
        (A, C, True, [_r(f"{M}>{C}\n{M}->{A}\n")], []),
        (A, C, True, [_r(f"{M}>{L}\n{M}-{A}\n")], []),
        (A, C, True, [_r(f'{M}>{C}\n"bad\\q"\n"\n"\\377"\n"a\\nb"\n{M}-{A}\n')], []),
        (
            A,
            C,
            True,
            [_r(f'{M}>{C}\n"\\"x\\"\\t\\001"\n"\\1234"\n"a"b"\n{M}-{A}\n')],
            [],
        ),
        (A, C, True, [_r(f"{M}>{C}\ncaf\udce9\n{M}-{A}\n")], []),
        (A, C, True, [_r(f"{M}>{C}\npath\0nul\n{M}-{A}\n")], []),
        (A, C, True, [_r("\n \n")], []),
        (A, C, True, [_r()], []),
        (A, C, True, [_r(code=128, error="fatal: bad revision\n")], []),
        (A, C, True, ["timeout"], []),
        (A, C, False, [_r(f"{M}>{C}\nignored\n{M}-{A}\n")], []),
    ):
        yield {
            "fn": walk,
            "cwd": "top",
            "anchor": anchor,
            "head": head,
            "names": names,
            "script": script,
            "leaves": {"cuts": cut},
        }


def _wire(reply: Any) -> Any:
    """A reply as the file holds it: stdout that is not UTF-8 (a lone
    surrogate standing for a byte) as ``{"hex": ...}``."""
    if isinstance(reply, str):
        return reply
    code, stdout, stderr = reply
    raw = stdout.encode("utf-8", "surrogateescape")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return [code, {"hex": raw.hex()}, stderr]
    return reply


def _call(case: dict[str, Any]) -> Any:
    fn = case["fn"]
    cwd = None if case["cwd"] is None else Path(case["cwd"])
    if fn == "walk":
        assert cwd is not None
        walked = origin._walk_reachable(
            cwd, case["anchor"], case["head"], names=case["names"]
        )
        if walked is None:
            return None
        return {
            "anchor": walked.anchor,
            "head": walked.head,
            "commits": list(walked.commits),
            "touched": {p: sorted(s) for p, s in sorted(walked.touched.items())},
            "listed": walked.listed,
            "timed_out": walked.timed_out,
        }
    if fn == "commit_reachable":
        return origin.commit_reachable(cwd, case["sha"])
    if fn == "head_sha":
        return origin.head_sha(cwd)
    answer = getattr(origin, fn)(cwd)
    if fn == "repo_toplevel":
        return None if answer is None else str(answer)
    return None if answer is None else [str(answer[0]), answer[1]]


def _scripted(case: dict[str, Any]) -> dict[str, Any]:
    prefix = ["git", *_git.PINS, *origin_history.AS_STORED_CONFIG]
    script = iter(case["script"])
    leaves = case["leaves"]
    calls: list[Any] = []
    reads: list[Any] = []
    lines = _Lines()
    devnull = f"-O{os.devnull}"

    def run(command: list[str], **kw: Any) -> Any:
        assert command[: len(prefix)] == prefix
        assert kw["input"] is None and kw["cwd"] == case["cwd"]
        argv = ["-O<devnull>" if a == devnull else a for a in command[len(prefix) :]]
        calls.append([kw["timeout"], argv])
        reply = next(script)
        if reply == "timeout":
            raise subprocess.TimeoutExpired(command, kw["timeout"])
        if reply == "missing":
            raise FileNotFoundError("no executable")
        code, stdout, stderr = reply
        return subprocess.CompletedProcess(
            command, code, stdout.encode("utf-8", "surrogateescape"), stderr.encode()
        )

    def agrees(cwd: str, top: str) -> bool:
        reads.append(["agrees", cwd, top])
        return bool(leaves.get("agrees", True))

    def resolve(self: Path, strict: bool = False) -> Path:
        reads.append(["resolve", str(self)])
        how = leaves.get("resolve")
        if how == "os":
            raise OSError("scripted")
        if how == "value":
            raise ValueError("scripted")
        return Path(f"{self}~r")

    def find_gitdir(cwd: Path) -> Any:
        reads.append(["find_gitdir", str(cwd)])
        if not leaves.get("gitdir"):
            return None
        return SimpleNamespace(
            gitdir=Path("gd"), commondir=Path("cd"), worktree_root=Path("wt")
        )

    def head_sha(gd: Any) -> str | None:
        reads.append(["head_sha", str(gd.gitdir)])
        head: str | None = leaves["head"]
        return head

    def moves(gd: Any) -> bool:
        reads.append(["moves", str(gd.gitdir)])
        return bool(leaves["moves"])

    def dies(found: Any, git_dir: Path) -> bool:
        assert found is location_git.machine_config_reads
        reads.append(["dies", str(git_dir)])
        return bool(leaves["dies"])

    def spelled(root: Path) -> Path | None:
        reads.append(["spelled", str(root)])
        answer = leaves["spelled"]
        if answer == "value":
            raise ValueError("embedded null byte")
        return None if answer is None else Path(answer)

    def cuts(toplevel: Path) -> frozenset[str] | None:
        reads.append(["cuts", str(toplevel)])
        cut = leaves["cuts"]
        return None if cut is None else frozenset(cut)

    reason = "separate checkout" if leaves.get("refusal") else None
    token = git_read_budget._ACTIVE.set(None)
    try:
        with (
            _counts(),
            captured_by(_git.log, lines, logging.DEBUG),
            captured_by(origin.log, lines, logging.DEBUG),
            mock.patch.object(gitfile_rule, "discovery_refusal", return_value=reason),
            mock.patch.object(gitfile_rule, "toplevel_agrees", agrees),
            mock.patch.object(githead, "find_gitdir", find_gitdir),
            mock.patch.object(githead, "head_sha", head_sha),
            mock.patch.object(origin, "_config_moves_worktree", moves),
            mock.patch.object(origin, "_machine_config_dies", dies),
            mock.patch.object(origin, "_spelled_as_git", spelled),
            mock.patch.object(origin_history, "cuts", cuts),
            mock.patch.object(_git, "_WINDOWS", False),
            mock.patch("subprocess.run", run),
            mock.patch("os.path.isdir", return_value=True),
        ):
            with mock.patch.object(Path, "resolve", resolve):
                try:
                    result = _call(case)
                except ValueError as error:
                    result = {"raised": type(error).__name__}
            counts = _totals()
    finally:
        git_read_budget._ACTIVE.reset(token)
    assert next(script, None) is None, case
    return {
        **case,
        "script": [_wire(reply) for reply in case["script"]],
        "calls": calls,
        "reads": reads,
        "result": result,
        "counts": counts,
        "logs": lines.lines,
    }


def generate() -> dict[str, bytes]:
    return {_FILE: jsonl([_scripted(case) for case in _cases()])}


#: The fixture's environment: git's own configuration, discovery and
#: message locale explicit, each commit a second after the last.
def _environment(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root / "home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }


#: Each scenario: its label, the steps that make it (run in order, after the
#: previous scenarios'), the folder the reads start from, the anchors asked
#: of `commit_reachable` and the walks asked (anchor, head, names). A step
#: is ``["git", folder, *args]``, ``["commit", folder, tag, {path: text}]``,
#: ``["shallow", folder, tag]`` (the folder's ``.git/shallow`` naming the
#: tag's commit), ``["dir", path]``, ``["write", path, text]`` or
#: ``["rm", path]``; a revision is a tag, or ``"unknown"`` for a hash no
#: repository holds. ``<root>/machine`` is the global config the in-process
#: reads see (git's own processes read none): "machine" writes one git's
#: parser refuses, so the files decline and git answers. "subdir" starts
#: from a folder inside the working tree, where the walk's shallow file is
#: looked for at the folder itself and none is found.
SCENARIOS: list[list[Any]] = [
    ["outside", [], "empty", ["unknown"], []],
    [
        "unborn",
        [["git", ".", "init", "-q", "-b", "trunk", "repo"]],
        "repo",
        ["unknown"],
        [],
    ],
    [
        "linear",
        [
            ["commit", "repo", "c1", {"a.txt": "one\n"}],
            ["commit", "repo", "c2", {"a.txt": "two\n", "dir/b c.txt": "x\n"}],
            [
                "commit",
                "repo",
                "c3",
                {"caf\u00e9.txt": "x\n", 'q"t\tz': "x\n", " s ": "x\n"},
            ],
        ],
        "repo",
        ["c1", "c3", "unknown"],
        [
            ["c1", "c3", True],
            ["c1", "c3", False],
            ["c3", "c1", True],
            ["c3", "c3", True],
        ],
    ],
    [
        "merge",
        [
            ["git", "repo", "checkout", "-q", "-b", "side", "c2"],
            ["commit", "repo", "s1", {"side.txt": "s\n"}],
            ["git", "repo", "checkout", "-q", "trunk"],
            ["commit", "repo", "c4", {"a.txt": "four\n"}],
            ["git", "repo", "merge", "-q", "--no-ff", "-m", "merge", "side"],
            ["git", "repo", "tag", "m1"],
            ["git", "repo", "checkout", "-q", "-b", "other", "c1"],
            ["commit", "repo", "o1", {"o.txt": "o\n"}],
            ["git", "repo", "checkout", "-q", "trunk"],
        ],
        "repo",
        ["c2", "s1", "o1"],
        [
            ["c2", "m1", True],
            ["s1", "m1", True],
            ["o1", "m1", True],
            ["c2", "m1", False],
        ],
    ],
    [
        "cut",
        [["shallow", "repo", "s1"]],
        "repo",
        ["c2", "o1"],
        [["c2", "m1", True], ["c4", "m1", True]],
    ],
    [
        "shallow",
        [
            ["rm", "repo/.git/shallow"],
            [
                "git",
                ".",
                "clone",
                "-q",
                "--depth",
                "2",
                "--branch",
                "trunk",
                "<repo-url>",
                "clone",
            ],
        ],
        "clone",
        ["c1", "c4", "m1"],
        [["c1", "m1", True], ["c4", "m1", True], ["m1", "m1", True]],
    ],
    [
        "linked",
        [["git", "repo", "worktree", "add", "-q", "-b", "linked", "../linked", "c3"]],
        "linked",
        ["c1", "m1"],
        [["c1", "c3", True]],
    ],
    [
        "core-worktree",
        [
            ["git", ".", "init", "-q", "-b", "trunk", "moved"],
            ["commit", "moved", "w1", {"w.txt": "w\n"}],
            ["dir", "elsewhere"],
            ["git", "moved", "config", "core.worktree", "<root>/elsewhere"],
        ],
        "moved",
        ["w1"],
        [],
    ],
    [
        "bare",
        [
            ["git", ".", "init", "-q", "--bare", "-b", "trunk", "bare.git"],
            ["git", "repo", "push", "-q", "../bare.git", "trunk"],
        ],
        "bare.git",
        ["c1", "o1"],
        [["c1", "m1", True]],
    ],
    ["machine", [["write", "machine", "[core\n"]], "repo", ["c1"], []],
    ["subdir", [["rm", "machine"]], "repo/dir", ["c1"], [["c1", "m1", True]]],
]


def _posix_rows() -> list[Any]:
    rows: list[Any] = []
    run = subprocess.run
    with tempfile.TemporaryDirectory() as folder:
        root = Path(os.path.realpath(folder))
        _clear_above(str(root))
        environment = _environment(root)
        (root / "home").mkdir()
        (root / "empty").mkdir()
        tick = [0]
        tags: dict[str, str] = {}

        def git(at: str, *args: str) -> str:
            env = dict(environment)
            stamp = f"2001-02-03T04:05:{tick[0]:02d}Z"
            env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
            argv = [a.replace("<root>", str(root)) for a in args]
            argv = [a.replace("<repo-url>", (root / "repo").as_uri()) for a in argv]
            done = run(
                ["git", "-c", f"core.hooksPath={os.devnull}", *argv],
                cwd=root / at,
                env=env,
                capture_output=True,
                check=True,
                text=True,
            )
            return done.stdout

        def step(op: str, *args: Any) -> None:
            if op == "git":
                git(*args)
            elif op == "commit":
                at, tag, files = args
                for name, text in files.items():
                    path = root / at / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                git(at, "add", "-A")
                tick[0] += 1
                git(at, "commit", "-qm", tag)
                git(at, "tag", tag)
            elif op == "shallow":
                at, tag = args
                (root / at / ".git" / "shallow").write_text(tags[tag] + "\n")
            elif op == "dir":
                (root / args[0]).mkdir()
            elif op == "write":
                (root / args[0]).write_bytes(args[1].encode("utf-8"))
            elif op == "rm":
                (root / args[0]).unlink()
            else:
                raise AssertionError(op)
            tick[0] += 1

        def sha(name: str) -> str:
            if name == "unknown":
                return "e" * 40
            return tags[name]

        def call(command: list[str], **kw: Any) -> Any:
            env = dict(environment)
            env.update(origin_history.AS_STORED_ENV)
            return run(command, **{**kw, "env": env})

        for label, steps, start, anchors, walks in SCENARIOS:
            for op, *args in steps:
                step(op, *args)
            for tag in git("repo", "tag").split() if (root / "repo").is_dir() else []:
                tags[tag] = git("repo", "rev-parse", tag).strip()
            if (root / "moved").is_dir():
                tags["w1"] = git("moved", "rev-parse", "w1").strip()
            at = root / start
            with (
                _counts(),
                _machine(root),
                _pinned_git(),
                _memos(),
                _packed(),
                captured_by(_git.log, _Lines(), logging.DEBUG),
                captured_by(origin.log, _Lines(), logging.DEBUG),
                mock.patch.object(
                    location_git,
                    "_machine_configs",
                    return_value=(str(root / "machine"),),
                ),
                mock.patch.dict(os.environ, {"HOME": str(root / "home")}),
                mock.patch.object(location_git, "_STAMPED_READS", {}),
                mock.patch.object(location_git, "_FRESH_READS", {}),
                mock.patch("subprocess.run", call),
            ):
                top = origin.repo_toplevel(at)
                pair = origin.repo_toplevel_and_head(at)
                files = origin.toplevel_and_head_from_files(at)
                head = origin.head_sha(at)
                reached = [[a, origin.commit_reachable(at, sha(a))] for a in anchors]
                walked = []
                for anchor, end, names in walks:
                    w = origin._walk_reachable(at, sha(anchor), sha(end), names=names)
                    walked.append(
                        [
                            anchor,
                            end,
                            names,
                            None
                            if w is None
                            else [
                                list(w.commits),
                                {p: sorted(s) for p, s in sorted(w.touched.items())},
                                w.listed,
                            ],
                        ]
                    )
                answers = {
                    "top": None if top is None else str(top),
                    "pair": None if pair is None else [str(pair[0]), pair[1]],
                    "files": None if files is None else [str(files[0]), files[1]],
                    "head": head,
                    "reachable": reached,
                    "walks": walked,
                }
                rows.append([label, steps, start, anchors, walks, answers, _totals()])
        data = jsonl(
            [{"scenarios": len(rows), "tags": dict(sorted(tags.items()))}, *rows]
        )
        return [data.replace(str(root).encode(), b"<root>")]


def generate_posix() -> dict[str, bytes]:
    refuse_root()
    [data] = _posix_rows()
    return {_POSIX: data}


# Unit origin-3b: `commits_since_anchor`, its walk memo and `_held_anchor`.
#
# ``origin_reach_walk.jsonl`` runs call sequences over scripted git replies,
# a scripted `walk_files_signature` (a token for each read, ``sig`` before
# the walk and ``after`` for the reads after it), a scripted
# `directories_held` (``held``), the shallow cuts (``cuts``), the root and
# head leaves of `origin_reach.jsonl` (``leaves``) and a monotonic clock
# that reads the step's ``now``. Each row starts from an empty memo and
# zero tallies; its steps share one pass (`Walked`), passed where a step
# says ``walked``. Each step records the answer, the five tallies, the walks
# given up on and why the last was, every git call (its timeout, its
# working directory and its argv, a walk's as ``names`` or ``listing`` and
# its range; the header pins both argvs whole), every in-process read in
# order and both memos (their length, then every key while they hold four
# or fewer, else the first and the last). A ``fill`` step makes ``count``
# calls from anchors ``start`` onwards (the number in 40 hex digits), each a
# listed walk (``walk``) or one whose names and listing time out
# (``timeout``), and records the tallies, the git calls made and the memos.
# The last row is a seeded sweep of call sequences by count and SHA-256.

_WALK_FILE = "origin_reach_walk.jsonl"
_WALK_POSIX = "origin_reach_walk_posix.jsonl"

#: A step's fields where its row gives none.
WALK_DEFAULTS: dict[str, Any] = {
    "cwd": "sample",
    "anchor": A,
    "toplevel": "top",
    "head": C,
    "walked": False,
    "now": 100.0,
    "sig": "s1",
    "held": True,
    "cuts": [],
    "leaves": {},
    "script": [],
}

_DEVNULL_ARG = f"-O{os.devnull}"


def _walk_argv(anchor: str, head: str, names: bool) -> list[str]:
    """`_walk_reachable`'s argv after the pins, the null device spelled
    ``<devnull>``."""
    named = [
        *(
            "-O<devnull>" if a == _DEVNULL_ARG else a
            for a in origin_history.AS_STORED_DIFF
        ),
        "--no-renames",
        "-c",
        "--name-only",
    ]
    return [
        *(["-c", "core.quotePath=false"] if names else []),
        "log",
        "--boundary",
        f"--format={M}%m%H",
        *(named if names else []),
        f"{anchor}..{head}",
    ]


def _reduced(argv: list[str]) -> list[str]:
    """A walk's argv as ``names`` or ``listing`` and its range, checked
    whole against `_walk_argv`; any other argv as it is."""
    if "log" not in argv:
        return argv
    rev = argv[-1]
    anchor, head = rev.split("..")
    names = "--name-only" in argv
    assert argv == _walk_argv(anchor, head, names), argv
    return ["names" if names else "listing", rev]


def _ok(anchor: str, head: str = C, names: bool = True) -> list[Any]:
    """A walk git lists: `head`, one path where `names`, `anchor` at the
    boundary."""
    if names:
        return _r(f"{M}>{head}\n\nf.txt\n{M}-{anchor}\n")
    return _r(f"{M}>{head}\n{M}-{anchor}\n")


def _walk_answer(walk: Any) -> Any:
    if walk is None:
        return None
    touched = [[p, sorted(s)] for p, s in sorted(walk.touched.items())]
    return [list(walk.commits), touched, walk.listed, walk.timed_out]


def _dump(entries: list[Any]) -> list[Any]:
    if len(entries) <= 4:
        return [len(entries), *entries]
    return [len(entries), entries[0], entries[-1]]


def _memos_now() -> list[Any]:
    def key(k: Any) -> list[Any]:
        return [k[0], k[1], k[2], k[3][0]]

    memo = [key(k) for k in origin._WALK_MEMO]
    kept = [
        [*key(k), stamp, "walk" if isinstance(v, ReachableWalk) else v.value]
        for k, (stamp, v) in origin._WALK_TIMED_OUT.items()
    ]
    return [_dump(memo), _dump(kept)]


class _Mix:
    """splitmix64, which the Rust test runs the same way."""

    MASK = (1 << 64) - 1

    def __init__(self, seed: int) -> None:
        self.state = seed & self.MASK

    def below(self, n: int) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & self.MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & self.MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & self.MASK
        return (z ^ (z >> 31)) % n


class _WalkWorld:
    """The scripted git, reads and clock of one row or one sweep: a
    step's script and fields where `mix` is None, else replies and reads
    drawn from `mix` as each is asked for."""

    prefix = ["git", *_git.PINS, *origin_history.AS_STORED_CONFIG]

    def __init__(self) -> None:
        self.step: dict[str, Any] = dict(WALK_DEFAULTS)
        self.script: Iterator[Any] = iter(())
        self.calls: list[Any] = []
        self.reads: list[str] = []
        self.sigs = 0
        self.mix: _Mix | None = None

    def begin(self, step: dict[str, Any]) -> None:
        self.step = step
        self.script = iter(step["script"])
        self.calls, self.reads, self.sigs = [], [], 0

    def drawn(self, argv: list[str]) -> Any:
        assert self.mix is not None
        k = self.mix.below(20)
        if "log" not in argv:
            return (
                [_r(), _r(), _r(code=1), _r(code=128), _r(code=-9)][k % 5]
                if k < 15
                else (["timeout", "timeout", "missing", "oserror", "timeout"][k - 15])
            )
        anchor, head = argv[-1].split("..")
        names = "--name-only" in argv
        if k < 10:
            return _ok(anchor, head, names)
        if k < 14:
            return "timeout"
        if k < 17:
            return _r(code=128, error="fatal: bad revision\n")
        if k == 17:
            return _r(f"{M}>{head[:-1]}\n{M}-{anchor}\n") if names else "oserror"
        if k == 18:
            return "missing"
        return _r(f"{M}>{head}\n{M}-{D}\n")

    def run(self, command: list[str], **kw: Any) -> Any:
        assert command[: len(self.prefix)] == self.prefix
        assert kw["input"] is None
        argv = [
            "-O<devnull>" if a == _DEVNULL_ARG else a
            for a in command[len(self.prefix) :]
        ]
        self.calls.append([kw["timeout"], os.fspath(kw["cwd"]), *_reduced(argv)])
        reply = self.drawn(argv) if self.mix is not None else next(self.script)
        if reply == "timeout":
            raise subprocess.TimeoutExpired(command, kw["timeout"])
        if reply == "missing":
            raise FileNotFoundError("no executable")
        if reply == "oserror":
            raise PermissionError("scripted")
        code, stdout, stderr = reply
        return subprocess.CompletedProcess(
            command, code, stdout.encode("utf-8", "surrogateescape"), stderr.encode()
        )

    def signature(self, root: Path, *, directories: Any = None) -> Any:
        try:
            str(root).encode("utf-8", "surrogateescape")
        except UnicodeEncodeError:
            # A root no POSIX file name holds: no `.git` entry is found
            # there, so the real signature answers None and holds nothing.
            return None
        self.reads.append(f"{'sig+' if directories is not None else 'sig'} {root}")
        if self.mix is not None:
            k = self.mix.below(10)
            token = "s1" if k < 7 else "s2" if k < 9 else None
        else:
            token = (
                self.step["sig"]
                if self.sigs == 0
                else self.step.get("after", self.step["sig"])
            )
        self.sigs += 1
        return None if token is None else (token,)

    def held(self, held: Any) -> bool:
        self.reads.append("held")
        if self.mix is not None:
            return self.mix.below(8) != 0
        return bool(self.step["held"])

    def monotonic(self) -> float:
        self.reads.append("clock")
        now: float = self.step["now"]
        return now

    def cuts(self, toplevel: Path) -> frozenset[str] | None:
        self.reads.append(f"cuts {toplevel}")
        if self.mix is not None:
            return frozenset() if self.mix.below(10) else None
        cut = self.step["cuts"]
        return None if cut is None else frozenset(cut)

    def leaf(self, name: str, arg: Any) -> Any:
        self.reads.append(f"{name} {arg}")
        return self.step["leaves"].get(name)


@contextmanager
def _walk_patches(world: _WalkWorld) -> Iterator[None]:
    """The reference's reads replaced by `world`'s, each row's memo and
    tallies its own."""

    def find_gitdir(cwd: Path) -> Any:
        if not world.leaf("find_gitdir", cwd):
            return None
        return SimpleNamespace(
            gitdir=Path("gd"), commondir=Path("cd"), worktree_root=Path("wt")
        )

    def resolve(self: Path, strict: bool = False) -> Path:
        world.leaf("resolve", self)
        return Path(f"{self}~r")

    def spelled(root: Path) -> Path | None:
        answer = world.leaf("spelled", root)
        return None if answer is None else Path(answer)

    def refusal(cwd: str) -> str | None:
        return "separate checkout" if world.step["leaves"].get("refusal") else None

    token = git_read_budget._ACTIVE.set(None)
    try:
        with (
            mock.patch.object(gitfile_rule, "discovery_refusal", refusal),
            mock.patch.object(gitfile_rule, "toplevel_agrees", lambda cwd, top: True),
            mock.patch.object(githead, "find_gitdir", find_gitdir),
            mock.patch.object(
                githead, "head_sha", lambda gd: world.leaf("head_sha", gd.gitdir)
            ),
            mock.patch.object(
                origin,
                "_config_moves_worktree",
                lambda gd: bool(world.leaf("moves", gd.gitdir)),
            ),
            mock.patch.object(
                origin,
                "_machine_config_dies",
                lambda reads, d: bool(world.leaf("dies", d)),
            ),
            mock.patch.object(origin, "_spelled_as_git", spelled),
            mock.patch.object(origin, "walk_files_signature", world.signature),
            mock.patch.object(origin, "directories_held", world.held),
            mock.patch.object(
                origin, "time", SimpleNamespace(monotonic=world.monotonic)
            ),
            mock.patch.object(origin_history, "cuts", world.cuts),
            mock.patch.object(_git, "_WINDOWS", False),
            mock.patch("subprocess.run", world.run),
            mock.patch("os.path.isdir", return_value=True),
            mock.patch.object(Path, "resolve", resolve),
            captured_by(_git.log, _Lines(), logging.DEBUG),
            captured_by(origin.log, _Lines(), logging.DEBUG),
        ):
            yield
    finally:
        git_read_budget._ACTIVE.reset(token)
        origin._clear_walk_memo()


@contextmanager
def _fresh() -> Iterator[None]:
    """An empty memo, zero tallies and no walk given up on."""
    origin._clear_walk_memo()
    with (
        _counts(),
        mock.patch.object(origin_history._UNWALKED, "count", 0),
        mock.patch.object(
            origin_history._UNWALKED, "why", origin_history.HistoryUnread.TIMEOUT
        ),
    ):
        yield
    origin._clear_walk_memo()


def _since(world: _WalkWorld, step: dict[str, Any], walked: dict[Any, Any]) -> Any:
    world.begin(step)
    top = step["toplevel"]
    if "toplevel_cps" in step:
        top = "".join(map(chr, step["toplevel_cps"]))
    return origin.commits_since_anchor(
        None if step["cwd"] is None else Path(step["cwd"]),
        step["anchor"],
        toplevel=None if top is None else Path(top),
        head=step["head"],
        walked=walked if step["walked"] else None,
    )


def _given_up() -> list[Any]:
    return [origin_history.unwalked(), origin_history._UNWALKED.why.value]


def _walk_row(
    world: _WalkWorld, label: str, steps: list[dict[str, Any]]
) -> dict[str, Any]:
    out: list[Any] = []
    walked: dict[Any, Any] = {}
    with _fresh():
        for given in steps:
            step = {**WALK_DEFAULTS, **given}
            if "fill" in step:
                start, count, kind = step["fill"]
                runs = 0
                for i in range(start, start + count):
                    anchor = f"{i:040x}"
                    script = [_ok(anchor)] if kind == "walk" else ["timeout", "timeout"]
                    _since(world, {**step, "anchor": anchor, "script": script}, walked)
                    assert next(world.script, None) is None, label
                    runs += len(world.calls)
                out.append(
                    {
                        "counts": _totals(),
                        "unwalked": _given_up(),
                        "runs": runs,
                        "memos": _memos_now(),
                    }
                )
                continue
            answer = _since(world, step, walked)
            assert next(world.script, None) is None, label
            out.append(
                {
                    "result": _walk_answer(answer),
                    "counts": _totals(),
                    "unwalked": _given_up(),
                    "calls": world.calls,
                    "reads": world.reads,
                    "memos": _memos_now(),
                }
            )
    return {"label": label, "steps": steps, "out": out}


_FAIL = _r(code=128, error="fatal: bad revision\n")
_SHORT = _r(f"{M}>{C[:-1]}\n\nf.txt\n{M}-{A}\n")
_UNRELATED = _r(f"{M}>{C}\n{M}-{D}\n")
#: A root holding a lone surrogate, which no POSIX file name holds.
_LONE_ROOT = "top\ud800"
_FROM_FILES = {
    "find_gitdir": True,
    "head_sha": D,
    "moves": False,
    "dies": False,
    "spelled": "wt",
}

#: Each row: its label and its steps, each step what differs from
#: `WALK_DEFAULTS`.
WALK_ROWS: list[list[Any]] = [
    ["no cwd", [{"cwd": None}]],
    ["short anchor", [{"anchor": "abc"}]],
    ["uppercase anchor", [{"anchor": A.upper()}]],
    ["anchor is the head", [{"anchor": C}, {"anchor": C}]],
    ["anchor is the head, unkeyed", [{"anchor": C, "sig": None, "walked": True}]],
    [
        "anchor is the head, root no file name holds",
        [
            {"anchor": C, **text_field("toplevel", _LONE_ROOT), "walked": True},
            {"anchor": C, **text_field("toplevel", _LONE_ROOT), "walked": True},
        ],
    ],
    [
        "listed walk stored and served",
        [{"script": [_ok(A)]}, {}, {"sig": "s2", "script": [_ok(A)]}, {}],
    ],
    [
        "served walk moves to the end",
        [{"script": [_ok(A)]}, {"anchor": B, "script": [_ok(B)]}, {}],
    ],
    [
        "files moved during the walk",
        [{"after": "s2", "script": [_ok(A)]}, {"script": [_ok(A)]}],
    ],
    [
        "files gone during the walk",
        [{"after": None, "script": [_ok(A)]}, {"script": [_ok(A)]}],
    ],
    ["directories moved", [{"held": False, "script": [_ok(A)]}, {"script": [_ok(A)]}]],
    [
        "unkeyed walk",
        [{"sig": None, "script": [_ok(A)]}, {"sig": None, "script": [_ok(A)]}],
    ],
    [
        "unkeyed walk in a pass",
        [
            {"sig": None, "walked": True, "script": [_ok(A)]},
            {"sig": None, "walked": True},
            {"walked": True},
            {"script": [_ok(A)]},
        ],
    ],
    [
        "names timed out, listed alone, a minute kept",
        [
            {"script": ["timeout", _ok(A, names=False)]},
            {"now": 159.999},
            {"now": 160.0, "script": ["timeout", _ok(A, names=False)]},
            {"now": 160.0},
        ],
    ],
    [
        "listed alone, clock gone back",
        [
            {"script": ["timeout", _ok(A, names=False)]},
            {"now": 99.999, "script": ["timeout", _ok(A, names=False)]},
        ],
    ],
    [
        "listed alone in a pass",
        [
            {"sig": None, "walked": True, "script": ["timeout", _ok(A, names=False)]},
            {"sig": None, "walked": True},
            {"walked": True, "now": 200.0},
        ],
    ],
    [
        "listed alone, files moved",
        [
            {"after": "s2", "script": ["timeout", _ok(A, names=False)]},
            {"script": ["timeout", _ok(A, names=False)]},
        ],
    ],
    [
        "listed alone, directories moved",
        [
            {"held": False, "script": ["timeout", _ok(A, names=False)]},
            {"script": ["timeout", _ok(A, names=False)]},
        ],
    ],
    [
        "names failed, listed alone",
        [
            {"walked": True, "script": [_FAIL, _ok(A, names=False)]},
            {"walked": True},
            {"script": [_FAIL, _ok(A, names=False)]},
        ],
    ],
    [
        "names with a short hash, listed alone",
        [{"script": [_SHORT, _ok(A, names=False)]}],
    ],
    [
        "names not ancestor",
        [{"walked": True, "script": [_UNRELATED]}, {"walked": True}],
    ],
    ["names with no cuts", [{"cuts": None, "script": [_ok(A)]}]],
    ["names with a commit cut", [{"cuts": [C], "script": [_ok(A)]}]],
    ["names empty", [{"walked": True, "script": [_r()]}, {"walked": True}]],
    [
        "names timed out, listing not ancestor",
        [{"walked": True, "script": ["timeout", _UNRELATED]}, {"walked": True}],
    ],
    ["names failed, listing not ancestor", [{"script": [_FAIL, _UNRELATED]}]],
    [
        "both timed out",
        [
            {"walked": True, "script": ["timeout", "timeout"]},
            {"walked": True, "now": 130.0},
            {"walked": True, "now": 161.0},
            {"now": 161.0, "script": ["timeout", "timeout"]},
        ],
    ],
    [
        "both timed out, unkeyed",
        [
            {"sig": None, "walked": True, "script": ["timeout", "timeout"]},
            {"sig": None, "walked": True},
        ],
    ],
    [
        "both timed out, files moved",
        [
            {"after": "s2", "script": ["timeout", "timeout"]},
            {"script": ["timeout", "timeout"]},
        ],
    ],
    ["names failed, listing timed out", [{"script": [_FAIL, "timeout"]}, {}]],
    [
        "names timed out, listing failed, anchor held",
        [
            {"walked": True, "script": ["timeout", _FAIL, _r()]},
            {"walked": True},
            {"sig": None, "walked": True},
        ],
    ],
    [
        "both failed, anchor held",
        [
            {"walked": True, "script": [_FAIL, _FAIL, _r()]},
            {"walked": True},
            {"script": [_FAIL, _FAIL, _r()]},
        ],
    ],
    [
        "both failed, anchor absent",
        [
            {
                "walked": True,
                "script": [
                    _FAIL,
                    _FAIL,
                    _r(code=128, error="fatal: Not a valid object name\n"),
                ],
            },
            {"walked": True},
        ],
    ],
    ["both failed, anchor no commit", [{"script": [_FAIL, _FAIL, _r(code=1)]}]],
    ["both failed, check killed", [{"script": [_FAIL, _FAIL, _r(code=-9)]}]],
    ["both failed, check timed out", [{"script": [_FAIL, _FAIL, "timeout"]}, {}]],
    [
        "both failed, check missing",
        [{"walked": True, "script": [_FAIL, _FAIL, "missing"]}, {"walked": True}],
    ],
    ["both failed, check could not run", [{"script": [_FAIL, _FAIL, "oserror"]}]],
    ["git missing", [{"script": ["missing", "missing", "missing"]}]],
    ["git could not run", [{"script": ["oserror", "oserror", "oserror"]}]],
    [
        "refused checkout",
        [{"walked": True, "leaves": {"refusal": True}}, {"walked": True}],
    ],
    [
        "root and head from the files",
        [
            {
                "toplevel": None,
                "head": None,
                "leaves": _FROM_FILES,
                "script": [_ok(A, D)],
            }
        ],
    ],
    [
        "head given, root from the files",
        [{"toplevel": None, "leaves": _FROM_FILES, "script": [_ok(A, D)]}],
    ],
    [
        "root given, head from the files",
        [{"head": None, "leaves": _FROM_FILES, "script": [_ok(A, D)]}],
    ],
    [
        "root and head from git",
        [{"toplevel": None, "script": [_r(f"top\n{B}\n"), _ok(A, B)]}],
    ],
    ["root lookup timed out", [{"toplevel": None, "script": ["timeout"]}]],
    [
        "no root",
        [
            {
                "toplevel": None,
                "script": [_r(code=128, error="fatal: not a git repository\n")],
            }
        ],
    ],
    [
        "memo before the pass",
        [
            {"sig": None, "walked": True, "script": ["timeout", _ok(A, names=False)]},
            {"walked": True},
        ],
    ],
    [
        "timed-out memo before the pass",
        [
            {"walked": True, "script": ["timeout", "timeout"]},
            {"walked": True, "now": 150.0},
            {"walked": True, "now": 170.0},
        ],
    ],
    [
        "timed-out hit moves to the end",
        [
            {"script": ["timeout", "timeout"]},
            {"anchor": B, "now": 101.0, "script": ["timeout", "timeout"]},
            {"now": 102.0},
            {"anchor": B, "now": 161.5, "script": ["timeout", "timeout"]},
        ],
    ],
    [
        "memo cap",
        [
            {"fill": [0, 128, "walk"]},
            {"anchor": f"{128:040x}", "script": [_ok(f"{128:040x}")]},
            {"anchor": f"{1:040x}"},
            {"anchor": f"{0:040x}", "script": [_ok(f"{0:040x}")]},
        ],
    ],
    [
        "timed-out memo cap",
        [
            {"fill": [0, 129, "timeout"]},
            {"anchor": f"{1:040x}"},
            {"anchor": f"{0:040x}", "script": ["timeout", "timeout"]},
        ],
    ],
]

_SWEEP_SEED = 0x3B0A_57A1
_SWEEP_SEQUENCES = 2000
_SWEEP_SHIFTS = [0.0, 1.0, 30.0, 59.999, 60.0, 61.0, -0.5]


def _sweep(world: _WalkWorld) -> dict[str, Any]:
    """Seeded call sequences: each draws its steps' anchor, head, pass
    and clock shift, then every git reply, signature, held and cuts read
    as the reference asks for it. The digest is over each sequence's
    answers, tallies and walks given up on, and both memos' lengths."""
    digest = hashlib.sha256()
    steps = 0
    for i in range(_SWEEP_SEQUENCES):
        mix = _Mix(_SWEEP_SEED + i)
        world.mix = mix
        walked: dict[Any, Any] = {}
        now = 1000.0
        seen: list[Any] = []
        with _fresh():
            for _ in range(1 + mix.below(6)):
                anchor = [A, B, C][mix.below(3)]
                head = [C, D][mix.below(2)]
                passed = mix.below(2) == 0
                now += _SWEEP_SHIFTS[mix.below(len(_SWEEP_SHIFTS))]
                step = {
                    **WALK_DEFAULTS,
                    "anchor": anchor,
                    "head": head,
                    "walked": passed,
                    "now": now,
                }
                answer = _since(world, step, walked)
                seen.append([_walk_answer(answer), _totals(), *_given_up()])
                steps += 1
            seen.append([len(origin._WALK_MEMO), len(origin._WALK_TIMED_OUT)])
        digest.update((json.dumps(seen, separators=(",", ":")) + "\n").encode("ascii"))
    world.mix = None
    return {
        "label": "sweep",
        "seed": _SWEEP_SEED,
        "sequences": _SWEEP_SEQUENCES,
        "shifts": _SWEEP_SHIFTS,
        "steps": steps,
        "sha256": digest.hexdigest(),
    }


def generate_walk() -> dict[str, bytes]:
    world = _WalkWorld()
    with _walk_patches(world):
        rows = [_walk_row(world, label, steps) for label, steps in WALK_ROWS]
        rows.append(_sweep(world))
    header = {
        "defaults": WALK_DEFAULTS,
        "argv": {"names": _walk_argv(A, C, True), "listing": _walk_argv(A, C, False)},
        "rows": len(rows),
    }
    return {_WALK_FILE: jsonl([header, *rows])}


#: ``origin_reach_walk_posix.jsonl``: each scenario's label, its steps
#: (`SCENARIOS`' kinds, run in order after the previous scenarios') and its
#: calls of `commits_since_anchor`: the folder it starts from (the root and
#: head read from there), the anchor's tag (``unknown`` for a hash no
#: repository holds), whether the scenario's pass (`Walked`) is given, and
#: the fault scripted on git's walk where git cannot be made to fail:
#: ``names-timeout`` and ``names-fails`` on the walk with names,
#: ``log-timeout`` and ``log-fails`` on both walks (a timeout, or exit 128),
#: ``root-touched`` (a file created and removed in the root) and
#: ``index-touched`` (the index's times set to `_AGED`) inside each walk's
#: process before git runs; and, where a call has a fifth field, what the
#: monotonic clock reads through it (0 where it has none). A step ``age``
#: sets a folder's times to `_AGED`, so a file created there moves its
#: stamp on any host's clock. The memos and tallies run on across the
#: scenarios.
WALK_SCENARIOS: list[list[Any]] = [
    ["outside", [], [["empty", "unknown", False, None]]],
    [
        "linear",
        [
            ["git", ".", "init", "-q", "-b", "trunk", "repo"],
            ["commit", "repo", "c1", {"a.txt": "one\n"}],
            ["commit", "repo", "c2", {"a.txt": "two\n", "dir/b c.txt": "x\n"}],
            ["commit", "repo", "c3", {"café.txt": "x\n", 'q"t\tz': "x\n"}],
        ],
        [
            ["repo", "c1", False, None],
            ["repo", "c1", False, None],
            ["repo/dir", "c1", False, None],
            ["repo", "c3", False, None],
            ["repo", "unknown", True, None],
            ["repo", "unknown", True, None],
            ["repo", "c2", False, "names-timeout"],
            ["repo", "c2", False, None],
        ],
    ],
    [
        "moved",
        [["commit", "repo", "c4", {"a.txt": "four\n"}]],
        [
            ["repo", "c1", False, None],
            ["repo", "c2", True, "log-fails"],
            ["repo", "c2", True, None],
            ["repo", "c3", False, "log-timeout"],
            ["repo", "c3", False, None],
        ],
    ],
    [
        "gitmodules",
        [["write", "repo/.gitmodules", "# none\n"]],
        [
            ["repo", "c1", False, None],
            ["repo", "c1", False, None],
            ["repo", "c2", False, "names-fails"],
            ["repo", "c2", False, "names-fails"],
        ],
    ],
    [
        "merge",
        [
            ["rm", "repo/.gitmodules"],
            ["git", "repo", "checkout", "-q", "-b", "side", "c2"],
            ["commit", "repo", "s1", {"side.txt": "s\n"}],
            ["git", "repo", "checkout", "-q", "trunk"],
            ["commit", "repo", "c5", {"a.txt": "five\n"}],
            ["git", "repo", "merge", "-q", "--no-ff", "-m", "merge", "side"],
            ["git", "repo", "tag", "m1"],
        ],
        [["repo", "c2", False, None], ["repo", "s1", False, None]],
    ],
    [
        "rebased",
        [
            ["git", "repo", "checkout", "-q", "-b", "topic", "c3"],
            ["commit", "repo", "r1", {"r.txt": "r\n"}],
            ["git", "repo", "checkout", "-q", "trunk"],
            ["commit", "repo", "c6", {"a.txt": "six\n"}],
            ["git", "repo", "checkout", "-q", "topic"],
            ["git", "repo", "rebase", "-q", "trunk"],
        ],
        [
            ["repo", "r1", True, None],
            ["repo", "r1", True, None],
            ["repo", "c6", False, None],
        ],
    ],
    [
        "shallow",
        [["shallow", "repo", "c6"]],
        [["repo", "c3", False, None], ["repo", "c6", False, None]],
    ],
    [
        "held",
        [
            ["rm", "repo/.git/shallow"],
            ["commit", "repo", "t1", {"t.txt": "t\n"}],
            ["age", "repo"],
        ],
        [
            ["repo", "c6", False, "root-touched"],
            ["repo", "c6", False, None],
            ["repo", "c6", False, None],
            ["repo", "c5", False, "index-touched"],
            ["repo", "c5", False, None],
            ["repo", "c5", False, None],
        ],
    ],
    [
        "clock",
        [],
        [
            ["repo", "m1", False, "names-timeout", 100.0],
            ["repo", "m1", False, None, 159.999],
            ["repo", "m1", False, None, 160.0],
            ["repo", "c4", False, "names-timeout", 200.0],
            ["repo", "c4", False, None, 199.5],
        ],
    ],
]

#: The times `age` and ``index-touched`` set, in nanoseconds:
#: 2001-02-03T04:05:06Z.
_AGED = 981_173_106 * 10**9


def _walk_posix_rows() -> list[Any]:
    rows: list[Any] = []
    run = subprocess.run
    with tempfile.TemporaryDirectory() as folder:
        root = Path(os.path.realpath(folder))
        _clear_above(str(root))
        environment = _environment(root)
        (root / "home").mkdir()
        (root / "empty").mkdir()
        tick = [0]
        tags: dict[str, str] = {}
        fault: list[str | None] = [None]
        runs = [0]
        now = [0.0]

        def git(at: str, *args: str) -> str:
            env = dict(environment)
            stamp = f"2001-02-03T04:05:{tick[0]:02d}Z"
            env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
            done = run(
                ["git", "-c", f"core.hooksPath={os.devnull}", *args],
                cwd=root / at,
                env=env,
                capture_output=True,
                check=True,
                text=True,
            )
            return done.stdout

        def step(op: str, *args: Any) -> None:
            if op == "git":
                git(*args)
            elif op == "commit":
                at, tag, files = args
                for name, text in files.items():
                    path = root / at / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                git(at, "add", "-A")
                tick[0] += 1
                git(at, "commit", "-qm", tag)
                git(at, "tag", tag)
            elif op == "shallow":
                at, tag = args
                (root / at / ".git" / "shallow").write_text(tags[tag] + "\n")
            elif op == "write":
                (root / args[0]).write_bytes(args[1].encode("utf-8"))
            elif op == "rm":
                (root / args[0]).unlink()
            elif op == "age":
                os.utime(root / args[0], ns=(_AGED, _AGED))
            else:
                raise AssertionError(op)
            tick[0] += 1

        def call(command: list[str], **kw: Any) -> Any:
            runs[0] += 1
            kind = fault[0]
            if kind is not None and "log" in command:
                names = "--name-only" in command
                if kind == "root-touched":
                    touched = Path(kw["cwd"]) / ".touched"
                    touched.write_bytes(b"")
                    touched.unlink()
                if kind == "index-touched":
                    os.utime(Path(kw["cwd"]) / ".git" / "index", ns=(_AGED, _AGED))
                if kind == "log-timeout" or (kind == "names-timeout" and names):
                    raise subprocess.TimeoutExpired(command, kw["timeout"])
                if kind == "log-fails" or (kind == "names-fails" and names):
                    return subprocess.CompletedProcess(
                        command, 128, b"", b"fatal: scripted\n"
                    )
            env = dict(environment)
            env.update(origin_history.AS_STORED_ENV)
            return run(command, **{**kw, "env": env})

        origin._clear_walk_memo()
        with (
            _fresh(),
            _machine(root),
            _pinned_git(),
            _memos(),
            _packed(),
            captured_by(_git.log, _Lines(), logging.DEBUG),
            captured_by(origin.log, _Lines(), logging.DEBUG),
            mock.patch.object(
                location_git, "_machine_configs", return_value=(str(root / "machine"),)
            ),
            mock.patch.dict(os.environ, {"HOME": str(root / "home")}),
            mock.patch.object(location_git, "_STAMPED_READS", {}),
            mock.patch.object(location_git, "_FRESH_READS", {}),
            mock.patch.object(
                origin, "time", SimpleNamespace(monotonic=lambda: now[0])
            ),
            mock.patch("subprocess.run", call),
        ):
            for label, steps, calls in WALK_SCENARIOS:
                for op, *args in steps:
                    step(op, *args)
                if (root / "repo").is_dir():
                    for tag in git("repo", "tag").split():
                        tags[tag] = git("repo", "rev-parse", tag).strip()
                walked: dict[Any, Any] = {}
                answers = []
                for start, tag, passed, kind, *clock in calls:
                    fault[0], runs[0] = kind, 0
                    now[0] = clock[0] if clock else 0.0
                    anchor = "e" * 40 if tag == "unknown" else tags[tag]
                    walk = origin.commits_since_anchor(
                        root / start, anchor, walked=walked if passed else None
                    )
                    answers.append(
                        [
                            _walk_answer(walk),
                            runs[0],
                            _totals(),
                            *_given_up(),
                            len(origin._WALK_MEMO),
                            len(origin._WALK_TIMED_OUT),
                        ]
                    )
                fault[0] = None
                rows.append([label, steps, calls, answers])
        data = jsonl(
            [{"scenarios": len(rows), "tags": dict(sorted(tags.items()))}, *rows]
        )
        return [data.replace(str(root).encode(), b"<root>")]


def generate_walk_posix() -> dict[str, bytes]:
    refuse_root()
    [data] = _walk_posix_rows()
    return {_WALK_POSIX: data}
