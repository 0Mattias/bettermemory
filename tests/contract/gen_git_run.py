"""Fixtures: one git call whole (``git_run.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/git_run.rs``. This pins it to
`_git.run_git` (the command, the environment, stdin, the timeout given,
both streams as text or bytes, the counts, the `os.path.isdir` calls and
the lines logged) and to `_git.log_nonzero_exit`.

`subprocess.run` is replaced by a recorder that returns or raises a
scripted outcome, `os.environ` by a small fixed map, `os.path.isdir` by a
recorder, and the budget's clock by scripted values. Every exception text
is a scripted string, so no row follows the Python release or the
platform: not gated.

One JSON object per line, keys sorted, ASCII. The first line is the
header (``{"k": "header", "cases": n, "environ": [[name, value], ...]}``).
Every other line is one of:

- ``{"k": "run", ...}``: a call. Inputs: ``a`` the args, ``cwd``,
  ``gd`` (git_dir), ``in`` (stdin, hex, or null), ``t`` (the timeout's
  repr), ``text``, ``env`` (pairs), ``unset``, ``dl`` (the active
  budget's deadline's repr, or null for no budget), ``clock`` (the
  clock's answers, reprs), ``sp`` (the outcome: ``["ok", rc, stdout hex,
  stderr hex]``, ``["nf", str(exc)]``, ``["to"]``, ``["os", str(exc)]``
  or ``["v"]``) and ``isdir``. Outputs: ``spawn`` (null where nothing
  was started, else ``[argv, cwd, stdin hex, timeout repr, env pairs]``),
  ``ans`` (``"None"``, an exception's class name, or ``[args, rc,
  stdout, stderr]`` with each stream text or ``{"hex": ...}``),
  ``moved`` (unanswered, failed, timed out and missing, as they moved),
  ``asked`` (the paths `os.path.isdir` was asked), ``log`` (``[level,
  message]`` pairs) and ``n`` (clock reads).
- ``{"k": "nz", "a": args, "rc": n, "e": text or "eb": hex, "w": where
  or null, "o": [level, message]}``: `log_nonzero_exit`.
- ``{"k": "nzc", ...}``: the same with every text a list of code
  points, for texts that hold a lone surrogate, which JSON readers
  refuse as a string.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any
from unittest import mock

from bettermemory import _git, git_read_budget as grb

from ._common import captured_by, jsonl

ENVIRON = {
    "PATH": "/usr/bin:/bin",
    "GIT_DIR": "/elsewhere/.git",
    "LANG": "C.UTF-8",
    "GIT_OPTIONAL_LOCKS": "1",
    "GIT_EXTERNAL_DIFF": "/bin/false",
    "GIT_ALLOW_PROTOCOL": "file",
    "HOME": "/h",
    "GIT_INDEX_FILE": "/i",
}


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[list[str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append([record.levelname, record.getMessage()])


def _stream(value: Any) -> Any:
    return {"hex": value.hex()} if isinstance(value, bytes) else value


def _run(case: dict[str, Any]) -> dict[str, Any]:
    clock = [float(c) for c in case.get("clock", [])]
    reads = [0]

    def tick() -> float:
        value = clock[reads[0]]
        reads[0] += 1
        return value

    sp = case["sp"]
    spawn: list[Any] = []

    def run(command: Any, **kw: Any) -> Any:
        given = kw["input"]
        spawn.extend(
            [
                list(command),
                kw["cwd"],
                None if given is None else given.hex(),
                repr(kw["timeout"]),
                [list(p) for p in kw["env"].items()],
            ]
        )
        if sp[0] == "nf":
            raise FileNotFoundError(2, sp[1])
        if sp[0] == "to":
            raise subprocess.TimeoutExpired(command, kw["timeout"])
        if sp[0] == "os":
            raise OSError(13, sp[1])
        if sp[0] == "v":
            raise ValueError("embedded null byte")
        return subprocess.CompletedProcess(
            command, sp[1], bytes.fromhex(sp[2]), bytes.fromhex(sp[3])
        )

    asked: list[Any] = []

    def is_dir(path: Any) -> bool:
        asked.append(str(path))
        return bool(case["isdir"])

    budget = None
    if case.get("dl") is not None:
        budget = grb.ReadBudget(deadline=float(case["dl"]))
    capture = _Capture()
    before = (
        _git.unanswered_calls(),
        _git.failed_calls(),
        _git.timed_out_calls(),
        _git.missing_calls(),
    )
    token = grb._ACTIVE.set(budget)
    try:
        with (
            captured_by(_git.log, capture, logging.DEBUG),
            mock.patch("subprocess.run", run),
            mock.patch("os.path.isdir", is_dir),
            mock.patch("os.environ", dict(ENVIRON)),
            mock.patch.object(grb, "_clock", tick),
        ):
            try:
                done = _git.run_git(
                    case["a"],
                    cwd=case.get("cwd"),
                    git_dir=case.get("gd"),
                    stdin=None if case.get("in") is None else bytes.fromhex(case["in"]),
                    timeout=float(case["t"]),
                    text=case.get("text", True),
                    env=dict(case.get("env", [])),
                    unset=case.get("unset", ()),
                )
                if done is None:
                    ans: Any = "None"
                else:
                    ans = [
                        list(done.args),
                        done.returncode,
                        _stream(done.stdout),
                        _stream(done.stderr),
                    ]
            except (grb.ReadDeadline, ValueError) as exc:
                ans = type(exc).__name__
    finally:
        grb._ACTIVE.reset(token)
    after = (
        _git.unanswered_calls(),
        _git.failed_calls(),
        _git.timed_out_calls(),
        _git.missing_calls(),
    )
    row = {
        "k": "run",
        "a": case["a"],
        "cwd": case.get("cwd"),
        "gd": case.get("gd"),
        "in": case.get("in"),
        "t": case["t"],
        "text": case.get("text", True),
        "env": case.get("env", []),
        "unset": case.get("unset", []),
        "dl": case.get("dl"),
        "clock": case.get("clock", []),
        "sp": sp,
        "isdir": case["isdir"],
        "spawn": spawn or None,
        "ans": ans,
        "moved": [a - b for a, b in zip(after, before, strict=True)],
        "asked": asked,
        "log": capture.lines,
        "n": reads[0],
    }
    return row


def _hex(b: bytes) -> str:
    return b.hex()


OK0 = ["ok", 0, _hex(b"a\r\nb\rc\n"), ""]


def _cases() -> list[dict[str, Any]]:
    c: list[dict[str, Any]] = []

    def add(**kw: Any) -> None:
        kw.setdefault("t", "1.0")
        kw.setdefault("isdir", True)
        c.append(kw)

    # Completed processes: text and bytes, CR kept, invalid UTF-8.
    add(a=["status"], cwd="/r", sp=OK0)
    add(a=["status"], cwd="/r", sp=OK0, text=False)
    bad = _hex(b"\xff\xfe ok \xe2\x82 \xed\xa0\x80 \xf4\x90\x80\x80 \xc3\xa9\r")
    add(a=["show"], cwd="/r", sp=["ok", 0, bad, bad])
    add(a=["show"], cwd="/r", sp=["ok", 0, bad, bad], text=False)
    fatal = _hex(b"fatal: not a git repository\r\nhint: x\n")
    add(a=["rev-parse"], cwd="/r", sp=["ok", 128, "", fatal])
    add(a=["rev-parse"], cwd="/r", sp=["ok", 128, "", fatal], text=False)
    add(a=["x"], cwd="/r", sp=["ok", -9, "", ""])
    add(a=["x"], cwd="/r", sp=["ok", 255, _hex(b"\n"), _hex(b"\r")])
    # git_dir, stdin, both places, neither.
    add(a=["log", "--stdin"], gd="/g.git", sp=OK0)
    add(a=["log", "--stdin"], gd="/g.git", sp=OK0, **{"in": _hex(b"HEAD\n--\n")})
    add(a=["log"], cwd="/r", gd="/r/.git", sp=OK0, **{"in": ""})
    add(a=["version"], sp=OK0)
    add(a=[], sp=OK0)
    add(a=["-c", "a=b", "log", "-p"], cwd="/r", sp=OK0)
    # The environment: extra beneath the scrub and the pins, unset.
    add(
        a=["log"],
        cwd="/r",
        sp=OK0,
        env=[
            ["GIT_DIR", "/x"],
            ["A", "2"],
            ["GIT_OPTIONAL_LOCKS", "1"],
            ["GIT_ALLOW_PROTOCOL", "ssh"],
            ["PATH", "/opt"],
            ["GIT_DIFF_OPTS", "-u9"],
        ],
        unset=["HOME", "B", "PATH", "GIT_NO_LAZY_FETCH"],
    )
    add(a=["var"], sp=OK0, unset=["LANG"], env=[["LANG", "C"]])
    # Not found.
    for cwd, isdir in (("/gone", False), ("/r", True), (None, False)):
        add(a=["log"], cwd=cwd, isdir=isdir, sp=["nf", "No such file or directory"])
    add(a=["log"], gd="/g", isdir=False, sp=["nf", "x: 'git'"])
    # Timed out, each timeout and place.
    for t in ("1.0", "5.0", "0.1", "2.5e-07", "1e+16"):
        add(a=["-c", "x=y", "log"], cwd="/r", t=t, sp=["to"])
    add(a=["log"], gd="/g.git", sp=["to"])
    add(a=["log"], sp=["to"])
    add(a=[], cwd="/r", sp=["to"])
    # Another OSError.
    for cwd, gd, isdir in (
        ("/gone", None, False),
        ("/r", None, True),
        (None, "/g", False),
        (None, None, False),
        ("/gone", "/g", False),
    ):
        add(a=["log"], cwd=cwd, gd=gd, isdir=isdir, sp=["os", "Permission denied"])
    # Another exception passes through.
    add(a=["log"], cwd="/r", sp=["v"])
    add(a=["log"], cwd="/gone", isdir=False, sp=["v"])
    # The shared budget: a clipped fraction, an unclipped one, expired.
    add(a=["log"], cwd="/r", dl="10.0", clock=["9.7"], sp=OK0)
    add(a=["log"], cwd="/r", dl="10.0", clock=["9.7", "9.9"], sp=["to"])
    add(a=["log"], cwd="/r", dl="10.0", clock=["9.7", "10.0"], sp=["to"])
    add(a=["log"], cwd="/r", dl="100.0", clock=["1.0"], t="5.0", sp=["to"])
    add(a=["log"], cwd="/r", dl="10.0", clock=["10.0"], sp=OK0)
    add(a=["log"], gd="/g", dl="10.0", clock=["9.0"], t="0.1", sp=["nf", "n"])
    add(a=["log"], cwd="/r", dl="10.0", clock=["9.25"], sp=["os", "p"], isdir=False)
    return c


def _nonzero() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cases: list[tuple[list[str], int, Any, Any]] = [
        (["rev-parse"], 128, "fatal: bad\nsecond\n", "/r"),
        (["-c", "a=b", "log"], 1, "", None),
        (["log"], 1, b"", "/r"),
        (["log"], 1, b"\xff\xfeboom\r\nnext", "/r"),
        (["log"], 2, b"  \n\r\x0c\x1cfirst\x1dsecond", "/g.git"),
        ([], -15, " a\u0085b", None),
        (["show"], 0, b"\xe2\x80\xa8x\xc2\x85y", "/r"),
        (["\ud800"], 1, "", "/r"),
        (["-c", "\udc80", "\udfff", "x"], 3, " \ud800a\nb", "/\udc80"),
        (["log"], 1, b"\xed\xa0\x80", None),
    ]
    for args, rc, err, where in cases:
        capture = _Capture()
        with captured_by(_git.log, capture, logging.DEBUG):
            result = subprocess.CompletedProcess(args, rc, b"", err)
            _git.log_nonzero_exit(args, result, where)
        level, message = capture.lines[0]
        texts = [*args, message, *([err] if isinstance(err, str) else [])]
        if where is not None:
            texts.append(where)
        if any(0xD800 <= ord(ch) <= 0xDFFF for t in texts for ch in t):
            rows.append(_points_row(args, rc, err, where, level, message))
            continue
        row: dict[str, Any] = {"k": "nz", "a": args, "rc": rc, "w": where}
        if isinstance(err, bytes):
            row["eb"] = err.hex()
        else:
            row["e"] = err
        row["o"] = capture.lines[0]
        rows.append(row)
    return rows


def _points(text: str) -> list[int]:
    return [ord(ch) for ch in text]


def _points_row(
    args: list[str], rc: int, err: Any, where: Any, level: str, message: str
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "k": "nzc",
        "a": [_points(a) for a in args],
        "rc": rc,
        "w": None if where is None else _points(where),
    }
    if isinstance(err, bytes):
        row["eb"] = err.hex()
    else:
        row["e"] = _points(err)
    row["o"] = [level, _points(message)]
    return row


def generate() -> dict[str, bytes]:
    rows = [_run(case) for case in _cases()]
    rows.extend(_nonzero())
    header = {
        "k": "header",
        "cases": len(rows),
        "environ": [list(p) for p in ENVIRON.items()],
    }
    return {"git_run.jsonl": jsonl([header, *rows])}
