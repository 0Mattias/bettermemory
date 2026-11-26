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

import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator
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

from ._common import captured_by, jsonl
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
