"""Fixtures: the ask (``gitsystem_ask.jsonl``, ``gitsystem_ask_unicode.jsonl``,
``gitsystem_ask_python.jsonl``, ``gitsystem_which_windows.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitsystem/ask.rs``. This pins it to
`gitsystem`'s `_ask`, `_older_system`, `_version`, `_reads_remotes` and
`_probe_ask`, `_git.run_git`'s command shaping, `git_read_budget`'s
allowance and `shutil.which`.

No git runs. ``subprocess.run`` is a script here: each call is recorded
whole (argv, working directory, the complete environment, timeout) and
answered by the case's next response; ``time.monotonic`` (gitsystem's
and `git_read_budget`'s) and ``time.time`` read the case's clocks;
``os.environ`` is the case's map and ``os.confstr("CS_PATH")`` its
default path. So the fixture holds nothing of the host's git,
environment or clock.

``gitsystem_ask.jsonl`` (POSIX only: `shutil.which` reads modes, the ask
folder and the probe are made on disk) runs ``_ask(key, now)`` in a
scratch folder ``R`` (the state directory ``R/state``, the working
directory ``R`` and the rest the case's tree, `gen_gitsystem_probe`'s
spelling). A case:

- ``t``: the tree (default: ``bin/git``, mode 0755);
- ``e``: the environment, ``[name, value]`` texts (`_t`), ``@`` standing
  for ``R`` (default: PATH ``@/bin``);
- ``cs``: what ``os.confstr("CS_PATH")`` gives (absent: None);
- ``g``: git's responses in order: ``{"x": status, "o": stdout}``
  (`_content`), ``{"d": i}`` (the header's ``git[i]``), ``"n"`` (could
  not run: FileNotFoundError) or ``"t"`` (timed out); ``"w"`` beside a response writes those files (``[rel,
  bytes]``) before it answers (default: the header's ``git``);
- ``c``: the monotonic clock's readings, `repr` texts (default: the
  header's ``clock``); ``wc``: the wall clock's (default ``2e9``);
- ``b``: `git_read_budget`'s active budget: absent none, ``"new"`` one
  not started, or its deadline;
- ``k`` and ``now``: `_ask`'s arguments (default ``"k"`` and ``"1000.0"``).

Answered in ``q`` (each command: its index in the header's
``commands``, each argv, working directory and environment with ``R`` as
``@``, and its timeout), ``r`` (the answer, or the class raised),
and ``n`` (how many times each clock was read); with ``b``, ``bd`` (the
budget's deadline afterwards, or null).

A case goes in ``_unicode`` where git's text answer (``var``, ``version``,
the probe's) holds a byte past ASCII, which `str.strip` and
`gitversion.parse` read through the Unicode data; in ``_python`` where a
version part runs past `int`'s 4,300 digits.

``gitsystem_which_windows.jsonl`` (every system) runs `shutil.which("git")`
as Windows runs it, over a file system of the case's (``f`` files, ``d``
folders, ``C:``-rooted, matched as written) with the working directory
``C:\\w``: ``sys.platform``, ``os.path`` (`ntpath`), ``os.pathsep`` and
``_winapi.NeedCurrentDirectoryForExePath`` (True unless
``NoDefaultCurrentDirectoryInExePath`` is set) stand in for Windows'.
Names are ASCII: `ntpath.normcase` off Windows lower-cases where Windows
asks ``LCMapStringEx``. A case goes in ``gitsystem_which_windows_python``
(Python-gated) where its input names ``NoDefaultCurrentDirectoryInExePath``
or a PATHEXT entry ending in a dot: `shutil.which` before 3.12 reads
neither as 3.13 does.
"""

from __future__ import annotations

import json
import ntpath
import os
import shutil
import subprocess
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import _daemon_client, _git, git_read_budget, gitconfig, gitsystem

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_gitconfig_file import _content, _expand
from .gen_gitsystem_probe import _make, _open_up
from .gen_gitsystem_record import _sp, _t

#: The default script: a git that answers every question.
LISTING = (
    b"system\0file:/etc/gitconfig\0core.x\ny\0"
    b"unknown\0file:/Library/Developer/gitconfig\0a.b\nc\0"
    b"global\0file:/dev/null\0d.e\nf\0"
)
GIT: list[Any] = [
    {"x": 0, "o": b"/etc/gitconfig\n"},
    {"x": 0, "o": LISTING},
    {"x": 0, "o": b"git version 2.54.0\n"},
    {"x": 0, "o": b"bettermemory-probe-b\n"},
    {"x": 0, "o": b"bettermemory.prefix\n/usr/z\0bettermemory.hasconfig\nyes\0"},
    {"x": 0, "o": b".\n"},
]
#: The default monotonic clock: 100.0 and on by 0.01.
CLOCK = (100.0, 0.01, 64)
DEFAULT_TREE: list[Any] = [
    ["d", "bin"],
    ["f", "bin/git", b"#!"],
    ["m", "bin/git", 0o755],
]
DEFAULT_ENV = [["PATH", "@/bin"]]
OUTPUT_LIMIT = 1 << 20
DEADLINE = 100.0 + gitsystem.ASK_SECONDS


def _clock_default() -> list[float]:
    start, step, n = CLOCK
    return [start + i * step for i in range(n)]


class Case:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw

    def get(self, key: str, default: Any) -> Any:
        return self.kw.get(key, default)

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in self.kw.items():
            if key == "g":
                out[key] = [_spell_response(r) for r in value]
            elif key == "e":
                out[key] = [[_t(k), _t(v)] for k, v in value]
            elif key == "t":
                out[key] = [
                    [*e[:2], _sp(e[2]), *e[3:]]
                    if e[0] == "f" and isinstance(e[2], bytes)
                    else e
                    for e in value
                ]
            elif key in ("c", "wc"):
                out[key] = [repr(float(v)) for v in value]
            elif key in ("b", "now"):
                out[key] = value if isinstance(value, str) else repr(float(value))
            elif key == "cs":
                out[key] = None if value is None else _t(value)
            else:
                out[key] = value
        return out


def _spell_response(r: Any, short: bool = True) -> Any:
    if isinstance(r, str):
        return r
    if short and r in GIT:
        return {"d": GIT.index(r)}
    out: dict[str, Any] = {"x": r["x"], "o": _out(r["o"])}
    if "w" in r:
        out["w"] = [[rel, _sp(data)] for rel, data in r["w"]]
    return out


def _out(data: Any) -> Any:
    return _content(data) if isinstance(data, list) else _sp(data)


def _with(i: int, response: Any, base: list[Any] | None = None) -> list[Any]:
    script = list(GIT if base is None else base)
    script[i] = response
    return script


def _cases() -> Iterator[Case]:
    yield Case()
    # Every exit status and output shape, at each question.
    for i in range(6):
        for response in (
            {"x": 1, "o": b""},
            {"x": 128, "o": b"fatal: x\n"},
            {"x": -9, "o": b""},
            {"x": 0, "o": b""},
            "n",
            "t",
        ):
            yield Case(g=_with(i, response) + list(GIT[i + 1 :]))
    # git var failing, so _older_system runs; its answers.
    older = [{"x": 1, "o": b""}]
    for response in (
        {"x": 0, "o": b"file:/etc/gitconfig\0core.x\ny\0"},
        {"x": 0, "o": b"file:/opt/git/etc/gitconfig\0a\nb\0file:/x\0"},
        {"x": 0, "o": b"file:etc/gitconfig\0a\nb\0"},
        {"x": 0, "o": b"command line:\0a\nb\0"},
        {"x": 0, "o": b""},
        {"x": 0, "o": b"file:/" + b"a" * 4095 + b"\0"},
        {"x": 0, "o": b"file:/" + b"a" * 4096 + b"\0"},
        {"x": 0, "o": b"file:/caf\xc3\xa9\xff\0"},
        {"x": 1, "o": b"file:/etc/gitconfig\0"},
        "n",
        "t",
    ):
        yield Case(g=[*older, response, *GIT[1:]])
    # What git var names, taken as it prints it.
    for out in (
        b"",
        b"\n",
        b"  /etc/gitconfig \t\n",
        b"relative/gitconfig\n",
        b"/etc/a\n/etc/b\n",
        b"/etc/gitconfig\r\n",
        b"/etc/" + b"x" * 5000 + b"\n",
        b"/\xff\n",
        b"/caf\xc3\xa9\n",
        b"/etc/\x85gitconfig\xc2\x85\n",
    ):
        yield Case(g=_with(0, {"x": 0, "o": out}))
    # The listing: own files, at and past what is kept; at the output
    # limit.
    many = b"".join(b"system\0file:/f%d\0a\nb\0" % i for i in range(12))
    yield Case(g=_with(1, {"x": 0, "o": many}))
    yield Case(g=_with(1, {"x": 0, "o": b"system\0file:/\xff\xfe\0a\nb\0"}))
    yield Case(g=_with(1, {"x": 0, "o": b"system\0file:rel\0a\nb\0"}))
    filler = OUTPUT_LIMIT - len(LISTING) - len(b"system\0file:/z\0a\n\0")
    yield Case(
        g=_with(
            1,
            {
                "x": 0,
                "o": [
                    (LISTING, 1),
                    (b"system\0file:/z\0a\n", 1),
                    (b"v", filler),
                    (b"\0", 1),
                ],
            },
        )
    )
    # The version: shapes, and a part past u32.
    for out in (
        b"git version 2.42.0.windows.1\n",
        b"git version 2.39.5 (Apple Git-154)\n",
        b"git version 2.45\n",
        b"git version 99999999999.0.0\n",
        b"version 2\n",
        b"git version 007.08.09\n",
    ):
        yield Case(g=_with(2, {"x": 0, "o": out}))
    yield Case(g=_with(2, {"x": 0, "o": b"git version \xd9\xa2.3.4\n"}))
    yield Case(g=_with(2, {"x": 0, "o": b"git version " + b"9" * 5000 + b".1.2\n"}))
    # The probe's answers.
    for out in (b"bettermemory-probe-a\n", b"p\r\n", b"other\n", b""):
        yield Case(g=_with(3, {"x": 0, "o": out}))
    for out in (
        b"bettermemory.prefix\n/usr/z\0",
        b"bettermemory.prefix\n%(prefix)/z\0bettermemory.hasconfig\nyes\0",
        b"bettermemory.prefix\n/usr/z\r\n",
        b"",
        b"bettermemory.prefix\n/caf\xc3\xa9/z\0",
    ):
        yield Case(g=_with(4, {"x": 0, "o": out}))
    # The probe changed while git read it: not held.
    yield Case(
        g=_with(
            3,
            {"x": 0, "o": b"bettermemory-probe-b\n", "w": [["P/remotes/q", b"x"]]},
        )
    )
    yield Case(g=_with(5, {"x": 0, "o": b".\n", "w": [["P/config", b"[core]\n"]]}))
    # The deadline: a clock that jumps before, between and during the
    # questions, at exactly the deadline and past it.
    base = _clock_default()
    for at in range(1, 9):
        for jump in (DEADLINE, DEADLINE - 1e-9, DEADLINE + 5.0):
            clock = base[:at] + [jump + j * 0.01 for j in range(40)]
            yield Case(c=clock)
    # max(left, 0) at exactly zero and below, for the listing.
    yield Case(c=[100.0, 100.1, DEADLINE, DEADLINE + 0.01, DEADLINE + 0.02])
    yield Case(c=[100.0, 100.1, DEADLINE + 1.0, DEADLINE + 1.01, DEADLINE + 1.02])
    # git var failing and the clock past the deadline before the older
    # question.
    yield Case(g=[*older, *GIT[1:]], c=[100.0, 100.1, DEADLINE])
    # The budget: none, one not started, one past, one shorter than the
    # ask, and a timeout under it.
    yield Case(b="new")
    yield Case(b=repr(99.0))
    yield Case(b=repr(100.15))
    yield Case(b=repr(100.15), g=_with(2, "t"))
    yield Case(b=repr(100.95), g=_with(2, "t"))
    yield Case(b=repr(200.0), g=_with(2, "t"))
    yield Case(b="new", g=_with(0, "t"))
    # The budget's edges: no time left at exactly its deadline, and a
    # timeout read at exactly its deadline.
    yield Case(b=repr(100.0 + 1 * 0.01))
    yield Case(b=repr(100.0 + 7 * 0.01), g=_with(2, "t"))
    # A budget's deadline set by the first question (100.01 + 5) and
    # deciding the second: a little time left, none left, and a timeout
    # read at its deadline. The scripted clock serves the budget and the
    # ask both, so it runs ahead for the budget's read and comes back.
    after = [100.05 + i * 0.01 for i in range(20)]
    yield Case(b="new", c=[100.0, 100.01, 100.02, 100.03, 104.9, *after])
    yield Case(b="new", c=[100.0, 100.01, 100.02, 100.03, 105.2, *after])
    yield Case(
        b="new",
        g=_with(1, "t"),
        c=[100.0, 100.01, 100.02, 100.03, 104.9, 105.01, *after],
    )
    # Time left on the budget exactly the timeout asked: not shared, so a
    # timeout there is the ask's own and the budget's clock is not read.
    yield Case(
        b=repr(DEADLINE),
        g=_with(1, "t"),
        c=[100.0, 100.01, 100.02, 100.03, 100.03, DEADLINE, *after],
    )
    # The listing failing with output: it names nothing.
    yield Case(g=_with(1, {"x": 1, "o": LISTING}))
    # The environment: every scrubbed, reshaping and overriding variable,
    # names differing only in case, the variables the run sets.
    hostile = [
        *[[n, "/x"] for n in _git.SCRUBBED_ENV],
        *[[n, "x"] for n in _git.RESHAPING_ENV],
        ["GIT_CONFIG_SYSTEM", "/evil"],
        ["GIT_CONFIG_NOSYSTEM", "1"],
        ["GIT_CONFIG_GLOBAL", "/evil"],
        ["GIT_CONFIG_PARAMETERS", "'core.x'='y'"],
        ["GIT_CONFIG_COUNT", "1"],
        ["GIT_CONFIG_KEY_0", "include.path"],
        ["GIT_CONFIG_VALUE_0", "/evil"],
        ["GIT_OPTIONAL_LOCKS", "1"],
        ["GIT_NO_LAZY_FETCH", "0"],
        ["GIT_ALLOW_PROTOCOL", "ext"],
        ["GIT_CEILING_DIRECTORIES", "/"],
        ["GIT_EXEC_PATH", "/evil"],
        ["git_dir", "/x"],
        ["Git_Dir", "/x"],
        ["GIT_DIR ", "/x"],
        ["HOME", "/h/x"],
        ["PATH", "@/bin"],
        ["LANG", "C"],
    ]
    yield Case(e=hostile)
    yield Case(e=[["PATH", "@/bin"], ["X", "caf\u00e9"], ["Y", "\udcff"]])
    # PATH: missing, empty, empty and relative entries, a folder named
    # git, a git that may not run, links, two candidates.
    yield Case(e=[], cs="@/bin")
    yield Case(e=[], cs=None)
    yield Case(e=[], cs="")
    yield Case(e=[["PATH", ""]], cs="@/bin")
    yield Case(e=[["PATH", ":"]])
    yield Case(e=[["PATH", "::@/bin"]])
    yield Case(e=[["PATH", "bin"]])
    yield Case(e=[["PATH", "./bin:"]])
    yield Case(e=[["PATH", ""]], t=[["f", "git", b"#!"], ["m", "git", 0o755]])
    yield Case(
        e=[["PATH", ":@/bin"]],
        t=[["f", "git", b"#!"], ["m", "git", 0o755], *DEFAULT_TREE],
    )
    yield Case(t=[["d", "bin"], ["d", "bin/git"]])
    yield Case(e=[["PATH", "@/a:@/bin"]], t=[["d", "a"], ["d", "a/git"], *DEFAULT_TREE])
    yield Case(t=[["d", "bin"], ["f", "bin/git", b"#!"], ["m", "bin/git", 0o644]])
    yield Case(
        e=[["PATH", "@/a:@/bin"]],
        t=[["d", "a"], ["f", "a/git", b"#!"], ["m", "a/git", 0o644], *DEFAULT_TREE],
    )
    yield Case(
        t=[
            ["d", "bin"],
            ["d", "x"],
            ["f", "x/real", b"#!"],
            ["m", "x/real", 0o755],
            ["l", "bin/git", "@/x/real"],
        ]
    )
    yield Case(t=[["d", "bin"], ["l", "bin/git", "nothing"]])
    yield Case(t=[["d", "bin"], ["d", "x"], ["l", "bin/git", "@/x"]])
    yield Case(
        e=[["PATH", "@/a:@/bin"]],
        t=[["d", "a"], ["f", "a/git", b"#!"], ["m", "a/git", 0o755], *DEFAULT_TREE],
    )
    yield Case(
        e=[["PATH", "@/bin:@/bin/:@/bin"]],
        t=[["d", "bin"], ["f", "bin/git", b"#!"], ["m", "bin/git", 0o700]],
    )
    yield Case(
        e=[["PATH", "@/locked:@/bin"]],
        t=[
            ["d", "locked"],
            ["f", "locked/git", b"#!"],
            ["m", "locked", 0o000],
            ["m", "locked/git", 0o755],
            *DEFAULT_TREE,
        ],
    )
    yield Case(t=[["d", "bin"], ["p", "bin/git"], ["m", "bin/git", 0o755]])
    yield Case(e=[["PATH", "@/nothing"]])
    # The ask folder or the probe not made: the state directory a file;
    # the ask folder holding an entry.
    yield Case(t=[*DEFAULT_TREE, ["f", "state", b"x"]])
    yield Case(
        t=[
            *DEFAULT_TREE,
            ["d", "state"],
            ["d", "state/git-ask"],
            ["f", "state/git-ask/x", b""],
        ]
    )
    yield Case(
        t=[
            *DEFAULT_TREE,
            ["d", "state"],
            ["l", "state/" + gitsystem.PROBE_FOLDER, "@/bin"],
        ]
    )
    # The wall clock the sweep reads, and the key and the time.
    yield Case(wc=[0.0], k="0123", now="nan")


def _gated(case: Case) -> str:
    """The file a case goes in, by its input: the question each response
    answers, in the order `_ask` asks them."""
    script = case.get("g", GIT)
    first = script[0]
    older = isinstance(first, dict) and first["x"] != 0
    questions = [
        "var",
        *(["older"] if older else []),
        "list",
        "version",
        "remote",
        "prefix",
        "compat",
    ]
    gate = ""
    for question, r in zip(questions, script):
        if not isinstance(r, dict) or isinstance(r["o"], list):
            continue
        out = r["o"]
        if question == "version" and any(len(run) > 4300 for run in out.split(b".")):
            return "python"
        # `str.strip` and `gitversion.parse` read the Unicode data.
        if question in ("var", "version", "remote") and any(b > 0x7F for b in out):
            gate = "unicode"
    return gate


class _Script:
    def __init__(self, root: Path, responses: list[Any]) -> None:
        self.root = root
        self.responses = list(responses)
        self.asked: list[Any] = []

    def __call__(self, command: list[str], **kw: Any) -> Any:
        env = kw["env"]
        self.asked.append(
            [
                [_t(_lab(self.root, a)) for a in command],
                _t(_lab(self.root, kw["cwd"])),
                [[_t(k), _t(_lab(self.root, v))] for k, v in env.items()],
                repr(float(kw["timeout"])),
            ]
        )
        assert self.responses, "the script ran out"
        r = self.responses.pop(0)
        if isinstance(r, dict):
            for rel, data in r.get("w", []):
                path = self.root / (
                    "state/" + gitsystem.PROBE_FOLDER + rel[1:]
                    if rel.startswith("P/")
                    else rel
                )
                path.write_bytes(data)
        if r == "n":
            raise FileNotFoundError(2, "No such file or directory", "git")
        if r == "t":
            raise subprocess.TimeoutExpired(command, kw["timeout"])
        out = _expand(r["o"]) if isinstance(r["o"], list) else r["o"]
        return subprocess.CompletedProcess(command, r["x"], stdout=out, stderr=b"")


def _lab(root: Path, text: str) -> str:
    return text.replace(str(root), "@")


class _Reads:
    def __init__(self, values: list[float]) -> None:
        self.values = list(values)
        self.n = 0

    def __call__(self) -> float:
        assert self.n < len(self.values), "the clock ran out"
        self.n += 1
        return self.values[self.n - 1]


def _answer(root: Path, got: gitsystem._Answer) -> dict[str, Any]:
    s = got.sources
    fresh = None if got.git is None else gitconfig.file_stamp(got.git)
    return {
        "g": None if got.git is None else _t(_lab(root, got.git)),
        "st": None if got.stamp is None else got.stamp == fresh,
        "a": repr(got.asked),
        "ok": got.ok,
        "own": [_t(o) for o in s.own],
        "sy": None if s.system is None else _t(s.system),
        "lg": s.legacy,
        "rs": s.resets,
        "px": None if s.prefix is None else _t(s.prefix),
        "cd": s.compat_differ,
        "hc": s.hasconfig,
        "v": None if s.version is None else [str(p) for p in s.version],
    }


def _run(root: Path, case: Case) -> dict[str, Any]:
    _make(root, case.get("t", DEFAULT_TREE))
    env = {k: v.replace("@", str(root)) for k, v in case.get("e", DEFAULT_ENV)}
    cs = case.get("cs", None)
    cs = None if cs is None else cs.replace("@", str(root))
    script = _Script(root, case.get("g", GIT))
    mono = _Reads([float(v) for v in case.get("c", _clock_default())])
    wall = _Reads([float(v) for v in case.get("wc", [2e9])])
    clock = types.SimpleNamespace(monotonic=mono, time=wall)
    budget = case.get("b", None)
    token = None
    held = None
    if budget is not None:
        deadline = None if budget == "new" else float(budget)
        held = git_read_budget.ReadBudget(deadline=deadline)
        token = git_read_budget._ACTIVE.set(held)
    saved = os.getcwd()
    try:
        os.chdir(root)
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(os, "confstr", lambda name: cs),
            mock.patch.object(
                _daemon_client, "resolve_state_dir", lambda: root / "state"
            ),
            mock.patch.object(gitsystem, "time", clock),
            mock.patch.object(git_read_budget, "_clock", mono),
            mock.patch.object(subprocess, "run", script),
            mock.patch.object(_git.log, "disabled", True),
        ):
            try:
                got = gitsystem._ask(
                    case.get("k", "k"), float(case.get("now", "1000.0"))
                )
                answer: Any = _answer(root, got)
            except (OSError, ValueError, git_read_budget.ReadDeadline) as e:
                answer = type(e).__name__
    finally:
        os.chdir(saved)
        if token is not None:
            git_read_budget._ACTIVE.reset(token)
    out = {"q": script.asked, "r": answer, "n": [mono.n, wall.n]}
    if held is not None:
        out["bd"] = None if held.deadline is None else repr(held.deadline)
    return out


def _ask_file(which: str) -> bytes:
    refuse_root()
    cases = [c for c in _cases() if _gated(c) == which]
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = Path(os.path.realpath(world.root / "gitsystem-ask"))
        base.mkdir()
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir()
            os.chmod(root, 0o755)
            got = _run(root, case)
            _open_up(root)
            shutil.rmtree(root)
            lines.append({**case.record(), **got})
    # Each distinct command (argv, working directory, environment) once,
    # in the header; a case names it by its index, with its timeout.
    table: list[Any] = []
    index: dict[str, int] = {}
    for line in lines:
        named = []
        for argv, cwd, env, timeout in line["q"]:
            key = json.dumps([argv, cwd, env])
            if key not in index:
                index[key] = len(table)
                table.append([argv, cwd, env])
            named.append([index[key], timeout])
        line["q"] = named
    name = "gitsystem_ask" + (f"_{which}" if which else "")
    header = {
        "fixture": name,
        "cases": len(lines),
        "git": [_spell_response(r, False) for r in GIT],
        "clock": [repr(CLOCK[0]), repr(CLOCK[1]), CLOCK[2]],
        "tree": DEFAULT_TREE[:1]
        + [[*DEFAULT_TREE[1][:2], _sp(DEFAULT_TREE[1][2])], DEFAULT_TREE[2]],
        "env": [[_t(k), _t(v)] for k, v in DEFAULT_ENV],
        "limit": OUTPUT_LIMIT,
        "commands": table,
    }
    return jsonl([header, *lines])


# -- which under Windows ------------------------------------------------------

WIN_CWD = "C:\\w"


def _key(path: str) -> str:
    if not ntpath.splitdrive(path)[0] and not path.startswith(("\\", "/")):
        path = WIN_CWD + "\\" + path
    return "\\".join(p for p in path.replace("/", "\\").split("\\") if p and p != ".")


def _windows_which(
    files: list[str], dirs: list[str], env: list[list[str]]
) -> str | None:
    file_keys = {_key(f) for f in files}
    dir_keys = {_key(d) for d in dirs}
    for f in file_keys | dir_keys:
        parts = f.split("\\")
        for i in range(1, len(parts)):
            dir_keys.add("\\".join(parts[:i]))
    environ: dict[str, str] = {}
    for k, v in env:
        environ[k.upper()] = v
    # `ntpath` whole, so every version's `shutil.which` finds what it
    # reads; only the questions put to the disk are the case's.
    fake_path = types.SimpleNamespace(
        **{k: getattr(ntpath, k) for k in dir(ntpath) if not k.startswith("__")}
    )
    fake_path.__dict__.update(
        exists=lambda p: _key(p) in file_keys or _key(p) in dir_keys,
        isdir=lambda p: _key(p) in dir_keys,
        isfile=lambda p: _key(p) in file_keys,
    )
    fake_os = types.SimpleNamespace(
        path=fake_path,
        pathsep=";",
        curdir=".",
        environ=types.SimpleNamespace(get=lambda k, d=None: environ.get(k.upper(), d)),
        getenv=lambda k, d=None: environ.get(k.upper(), d),
        access=lambda p, m: _key(p) in file_keys or _key(p) in dir_keys,
        fsdecode=lambda p: p,
        fsencode=lambda p: p.encode(),
        F_OK=os.F_OK,
        X_OK=os.X_OK,
        defpath=".;C:\\bin",
    )
    winapi = types.SimpleNamespace(
        NeedCurrentDirectoryForExePath=lambda name: (
            "NODEFAULTCURRENTDIRECTORYINEXEPATH" not in environ or "\\" in name
        )
    )
    with (
        mock.patch.object(shutil, "os", fake_os),
        mock.patch.object(shutil, "sys", types.SimpleNamespace(platform="win32")),
        mock.patch.object(shutil, "_winapi", winapi, create=True),
    ):
        return shutil.which("git")


def _which_cases() -> Iterator[tuple[list[str], list[str], list[list[str]]]]:
    exe = ["C:\\bin\\git.EXE"]
    yield exe, [], [["PATH", "C:\\bin"]]
    yield exe, [], [["Path", "C:\\bin"]]
    yield exe, [], [["PATH", "C:\\none"], ["path", "C:\\bin"]]
    yield exe, [], [["path", "C:\\bin"], ["PATH", "C:\\none"]]
    yield exe, [], []
    yield exe, [], [["PATH", ""]]
    yield exe, [], [["PATH", ";;C:\\bin;"]]
    yield exe, [], [["PATH", "C:/bin"]]
    yield exe, [], [["PATH", "c:\\BIN;C:\\bin"]]
    yield exe, [], [["PATH", "C:\\bin"], ["PATHEXT", ""]]
    yield exe, [], [["PATH", "C:\\bin"], ["PATHEXT", ".BAT"]]
    yield exe, [], [["PATH", "C:\\bin"], ["PATHEXT", ".exe"]]
    yield ["C:\\bin\\git.exe"], [], [["PATH", "C:\\bin"], ["PATHEXT", ".exe"]]
    yield exe, [], [["PATH", "C:\\bin"], ["PATHEXT", ".EXE."]]
    yield exe, [], [["PATH", "C:\\bin"], ["PATHEXT", ";;.exe;;"]]
    yield exe, [], [["PATH", "C:\\bin"], ["PATHEXT", "."]]
    yield exe, [], [["PATH", "C:\\bin"], ["PATHEXT", "EXE"]]
    yield ["C:\\bin\\gitEXE"], [], [["PATH", "C:\\bin"], ["PATHEXT", "EXE"]]
    yield ["C:\\bin\\git"], [], [["PATH", "C:\\bin"], ["PATHEXT", "."]]
    yield ["C:\\bin\\git"], [], [["PATH", "C:\\bin"]]
    yield (
        ["C:\\bin\\git", "C:\\bin\\git.EXE"],
        [],
        [["PATH", "C:\\bin"], ["PATHEXT", ".exe;.git"]],
    )
    yield ["C:\\bin\\git.CMD", "C:\\bin\\git.EXE"], [], [["PATH", "C:\\bin"]]
    yield ["C:\\w\\git.EXE", "C:\\bin\\git.EXE"], [], [["PATH", "C:\\bin"]]
    yield (
        ["C:\\w\\git.EXE", "C:\\bin\\git.EXE"],
        [],
        [["PATH", "C:\\bin"], ["NoDefaultCurrentDirectoryInExePath", ""]],
    )
    yield ["C:\\w\\sub\\git.EXE"], [], [["PATH", "sub"]]
    yield (
        ["C:\\w\\git.EXE"],
        [],
        [["PATH", "C:\\bin"], ["NODEFAULTCURRENTDIRECTORYINEXEPATH", "1"]],
    )
    yield [], ["C:\\bin\\git.EXE"], [["PATH", "C:\\bin"]]
    yield ["C:\\a\\git.EXE", "C:\\b\\git.EXE"], [], [["PATH", "C:\\b;C:\\a"]]


def _which_gated(env: list[list[str]]) -> bool:
    """Whether `shutil.which`'s version changes the answer, by input.

    Before 3.12 it searches the current folder always (no
    ``NeedCurrentDirectoryForExePath``) and keeps a PATHEXT entry's
    trailing dot.
    """
    for k, v in env:
        if k.upper() == "NODEFAULTCURRENTDIRECTORYINEXEPATH":
            return True
        if k.upper() == "PATHEXT" and any(e.endswith(".") for e in v.split(";")):
            return True
    return False


def _which_file(gated: bool) -> bytes:
    lines = []
    for files, dirs, env in _which_cases():
        if _which_gated(env) != gated:
            continue
        got = _windows_which(files, dirs, env)
        lines.append({"f": files, "d": dirs, "e": env, "r": got})
    name = "gitsystem_which_windows" + ("_python" if gated else "")
    header = {"fixture": name, "cases": len(lines), "cwd": WIN_CWD}
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"gitsystem_ask.jsonl": _ask_file("")}


def generate_unicode() -> dict[str, bytes]:
    return {"gitsystem_ask_unicode.jsonl": _ask_file("unicode")}


def generate_python() -> dict[str, bytes]:
    return {"gitsystem_ask_python.jsonl": _ask_file("python")}


def generate_which_windows() -> dict[str, bytes]:
    return {"gitsystem_which_windows.jsonl": _which_file(False)}


def generate_which_windows_python() -> dict[str, bytes]:
    return {"gitsystem_which_windows_python.jsonl": _which_file(True)}
