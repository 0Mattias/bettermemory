"""Fixtures of the flat-parser subset of `argparse` (`argparse::parse`,
native/README.md, "The hook mains' argument parser").

The reference is the standard library's `argparse` as the hook mains build
it (`hook.main`, `hook.prompt_main`): their parsers are built here again
from the same calls, beside a few made to reach every branch. No module of
the product is imported. Each case builds its parser afresh with the help
formatter at a given width and calls `parse_args`, `SystemExit` caught and
stdout and stderr captured.

The header lists the parsers (``parsers``), each ``{"prog", "desc",
"help", "args"}``, where ``args`` holds each `add_argument` call as
``[names, keywords]`` (``action`` one of ``store``, ``store_true``,
``version``; ``dest``, ``default``, ``choices``, ``required``,
``metavar``, ``help``, ``version``). A case row holds ``p`` (the parser),
``w`` (the formatter's width), ``a`` (argv) and either ``ns`` (the
namespace, each ``[dest, value]`` in its order) or ``code``, ``out`` and
``err``. A text holding a surrogate is the list of its code points.

Two files:

- ``argparse.jsonl``, not gated: the cases whose parse ends in a namespace
  and whose every argument is an exact option string of the parser or does
  not start with ``-``; 3.11 to 3.14 answer them alike.
- ``argparse_python.jsonl``, gated on the Python release: every other
  case (the error texts, the usage line and its wrapping, ``--``,
  ``=``, prefixes and abbreviations, short-option clusters, negative
  numbers, help and version output), and a sweep: ``SWEEP_COUNT`` argv
  vectors drawn from ``SWEEP_TOKENS`` by the generator ``_lcg`` from
  ``SWEEP_SEED`` over parser 2 at width 78, pinned by the count and the
  SHA-256 of the outcome lines (``dumps_line`` of each, joined by
  newlines). Under another interpreter than 3.13.13 the generator writes
  its header alone.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import sys
from typing import Any

from ._common import dumps_line, jsonl

REPRODUCES = "3.13.13"

AUDIT_TURN: dict[str, Any] = {
    "prog": "bettermemory audit-turn",
    "desc": "Run a silent-miss audit for the just-completed turn.",
    "help": True,
    "args": [
        [["--transcript-path"], {"default": None, "help": "Path to the transcript."}],
        [["--session-id"], {"default": None, "help": "Session id."}],
        [["--quiet"], {"action": "store_true", "help": "Suppress the summary."}],
        [["--dry-run"], {"action": "store_true", "help": "Write no event."}],
    ],
}

PROMPT_RECALL: dict[str, Any] = {
    "prog": "bettermemory prompt-recall",
    "desc": "Pointer recall for a just-submitted prompt.",
    "help": True,
    "args": [
        [["--prompt"], {"default": None, "help": "Prompt text to probe."}],
        [["--session-id"], {"default": None, "help": "Session id."}],
    ],
}

TOOL: dict[str, Any] = {
    "prog": "tool",
    "desc": None,
    "help": True,
    "args": [
        [["mode"], {"choices": ["a", "b"]}],
        [["name"], {}],
        [["-v", "--verbose"], {"action": "store_true"}],
        [["-o", "--out"], {}],
        [["--output-dir"], {"default": "."}],
        [["-x"], {"action": "store_true"}],
        [["-c"], {"choices": ["red", "green"]}],
        [["--version"], {"action": "version", "version": "tool  1.0\n"}],
    ],
}

NEGATIVE: dict[str, Any] = {
    "prog": "neg",
    "desc": None,
    "help": True,
    "args": [[["-1"], {"action": "store_true"}], [["n"], {}], [["-f"], {}]],
}

BARE: dict[str, Any] = {
    "prog": "bare",
    "desc": None,
    "help": False,
    "args": [
        [["--need"], {"required": True, "metavar": "N"}],
        [["--also", "-a"], {"required": True}],
        [["target"], {"metavar": "TARGET"}],
        [["--flag"], {"action": "store_true", "default": None}],
        [["-s"], {"dest": "dup"}],
        [["-t"], {"dest": "dup", "default": "t"}],
    ],
}

LONG: dict[str, Any] = {
    "prog": "a-rather-long-program-name-that-takes-up-the-line",
    "desc": None,
    "help": True,
    "args": [
        [["--alpha-option"], {}],
        [["--beta"], {"action": "store_true"}],
        [["first"], {}],
        [["second"], {"choices": ["one", "two", "three"]}],
    ],
}

EMPTY: dict[str, Any] = {"prog": "empty", "desc": None, "help": False, "args": []}

ONLY_POS: dict[str, Any] = {
    "prog": "pos",
    "desc": None,
    "help": False,
    "args": [[["one"], {}], [["two"], {}]],
}

LONG_VERSION: dict[str, Any] = {
    "prog": "lv",
    "desc": None,
    "help": False,
    "args": [[["-V"], {"action": "version", "version": " 1.0\tbuilt　x  　"}]],
}

PARSERS = [
    AUDIT_TURN,
    PROMPT_RECALL,
    TOOL,
    NEGATIVE,
    BARE,
    LONG,
    EMPTY,
    ONLY_POS,
    LONG_VERSION,
]

SWEEP_SEED = 20261121
SWEEP_COUNT = 3000
SWEEP_TOKENS = [
    "a",
    "b",
    "c",
    "-v",
    "--verbose",
    "-o",
    "--out",
    "--o",
    "--ou",
    "--outp",
    "--output-dir",
    "-x",
    "-xv",
    "-vx",
    "-vo",
    "-ov",
    "-o=",
    "--out=",
    "-x=1",
    "--verbose=1",
    "-c",
    "red",
    "blue",
    "-cred",
    "--",
    "-",
    "-1",
    "-.5",
    "x y",
    "--version",
    "--vers",
    "--nope",
    "-q",
    "--out=a=b",
    "-vxo",
    "",
]


def _lcg(state: int) -> int:
    return (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)


def _sweep_argvs() -> list[list[str]]:
    out = []
    state = SWEEP_SEED
    for _ in range(SWEEP_COUNT):
        state = _lcg(state)
        n = (state >> 33) % 5
        argv = []
        for _ in range(n):
            state = _lcg(state)
            argv.append(SWEEP_TOKENS[(state >> 33) % len(SWEEP_TOKENS)])
        out.append(argv)
    return out


def _t(s: Any) -> Any:
    if isinstance(s, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        return [ord(c) for c in s]
    return s


def _build(spec: dict[str, Any], width: int) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=spec["prog"],
        description=spec["desc"],
        add_help=spec["help"],
        formatter_class=lambda prog: argparse.HelpFormatter(prog, width=width),
    )
    for names, kw in spec["args"]:
        parser.add_argument(*names, **kw)
    return parser


def _run(p: int, width: int, argv: list[str]) -> dict[str, Any]:
    parser = _build(PARSERS[p], width)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            ns = parser.parse_args(argv)
        except SystemExit as e:
            code = e.code
        else:
            return {"ns": [[k, _t(v)] for k, v in vars(ns).items()]}
    return {"code": code, "out": _t(out.getvalue()), "err": _t(err.getvalue())}


def _cases() -> list[tuple[int, int, list[str]]]:
    w = 78
    c: list[tuple[int, int, list[str]]] = []
    for argv in [
        [],
        ["--quiet"],
        ["--dry-run", "--quiet"],
        ["--transcript-path", "/t.jsonl", "--session-id", "s1"],
        ["--session-id", "s1", "--session-id", "s2"],
        ["--transcript-path", "x y", "--dry-run"],
        ["--transcript-path", "é\U0001f600"],
        ["--transcript-path", "a\udcff"],
        ["--transcript-path", ""],
        ["--quiet", "extra"],
        ["--transcript-path"],
        ["--transcript-path", "--quiet"],
        ["--transcript-path=/t", "--quiet=1"],
        ["--trans", "/t"],
        ["--q", "--d"],
        ["--s=s3"],
        ["--", "--quiet"],
        ["--quiet", "--"],
        ["-q"],
        ["-h"],
        ["--help", "--bogus"],
        ["--bogus", "--help"],
        ["-hq"],
        ["-h=x"],
        ["--transcript-path", "-1"],
        ["--transcript-path", "-x"],
        ["--quiet", "-1"],
        ["--he"],
        ["--transcript-path", "a", "b", "--dry-run", "c"],
        ["-"],
        ["--"],
        ["--=x"],
        ["--quiet=", "x"],
    ]:
        c.append((0, w, argv))
    for argv in [
        [],
        ["--prompt", "hello", "--session-id", "s"],
        ["--prompt", "multi\nline"],
        ["--prompt"],
        ["--pro", "x"],
        ["--prompt", "--", "x"],
        ["--session", "s"],
        ["--se=s"],
        ["prompt"],
        ["-p", "x"],
        ["--prompt", "-.5"],
    ]:
        c.append((1, w, argv))
    for argv in [
        ["a", "n"],
        ["b", "n", "--verbose", "-x"],
        ["-v", "a", "n"],
        ["a", "-o", "f", "n"],
        ["a", "n", "--out", "o1", "--output-dir", "d"],
        ["a", "n", "-c", "red"],
        ["c", "n"],
        ["a"],
        [],
        ["a", "n", "m"],
        ["a", "n", "-c", "blue"],
        ["a", "n", "--ou", "x"],
        ["a", "n", "--outp", "x"],
        ["a", "n", "--o", "x"],
        ["a", "n", "-ofile"],
        ["a", "n", "-o=file"],
        ["a", "n", "-vx"],
        ["a", "n", "-vxo", "f"],
        ["a", "n", "-vxofile"],
        ["a", "n", "-vq"],
        ["a", "n", "-x=1"],
        ["a", "n", "-v-x"],
        ["a", "n", "--verbose=1"],
        ["a", "n", "--verbose="],
        ["a", "n", "-cred"],
        ["a", "n", "-cblue"],
        ["--", "a", "n"],
        ["a", "--", "n"],
        ["a", "n", "--"],
        ["--", "-v", "n"],
        ["a", "--", "--", "n"],
        ["-", "n"],
        ["a", "-1"],
        ["a", "-1.5"],
        ["a", "-.5\n"],
        ["a", "-1\n"],
        ["a", "-١"],
        ["a", "-1x"],
        ["a", "-v x"],
        ["--version"],
        ["--vers"],
        ["a", "n", "--version", "--bogus"],
        ["--bogus", "a", "n"],
        ["a", "--bogus", "n"],
        ["a", "n", "-o"],
        ["a", "n", "-o", "--", "x"],
        ["a", "n", "--out=a=b"],
        ["a", "n", "--output-dir="],
        ["-o", "x", "-o", "y", "a", "n"],
        ["\udc80", "n"],
        ["a", "n", "-c", "\udc80"],
        ["a", "n", "-c", "\ud83d\ude00"],
        ["a", "n", "--verbose=\udc80"],
        ["a", "n", "-x\udc80"],
    ]:
        c.append((2, w, argv))
    for argv in [
        ["5"],
        ["-1"],
        ["-1", "5"],
        ["-2"],
        ["-f", "-2"],
        ["-f", "-1"],
        ["-1.5"],
        ["--", "-2"],
    ]:
        c.append((3, w, argv))
    for argv in [
        ["--need", "1", "-a", "2", "t"],
        ["t"],
        [],
        ["--need", "1", "t"],
        ["--need", "1", "-a", "2", "t", "--flag"],
        ["-s", "1", "-t", "2", "--need", "n", "--also", "x", "t"],
        ["-h"],
    ]:
        c.append((4, w, argv))
    for width in [78, 60, 40, 30, 20, 10, 0, -5]:
        c.append((5, width, ["x"]))
        c.append((4, width, ["x", "y"]))
        c.append((0, width, ["--bogus"]))
        c.append((7, width, []))
    c.append((5, 78, ["x", "two", "--beta", "--alpha-option", "q"]))
    c.append((5, 20, ["x", "four"]))
    for argv in [[], ["x"], ["--x"]]:
        c.append((6, 78, argv))
    for argv in [
        ["1", "2"],
        ["1", "2", "3"],
        ["1"],
        ["--", "1", "2"],
        ["-q z", "b"],
        ["-1.", "b"],
        ["-.", "b"],
        ["-1.5", "b"],
    ]:
        c.append((7, 78, argv))
    for width in [78, 11, 8, 5]:
        c.append((8, width, ["-V"]))
    c.append((8, 78, ["-V", "-V"]))
    return c


def _ungated(p: int, argv: list[str], outcome: dict[str, Any]) -> bool:
    exact = {n for names, _ in PARSERS[p]["args"] for n in names}
    if PARSERS[p]["help"]:
        exact |= {"-h", "--help"}
    return "ns" in outcome and all(a in exact or not a.startswith("-") for a in argv)


def _header(name: str) -> dict[str, Any]:
    return {"fixture": name, "python": REPRODUCES, "parsers": PARSERS}


def _rows(gated: bool) -> list[dict[str, Any]]:
    rows = []
    for p, width, argv in _cases():
        outcome = _run(p, width, argv)
        if _ungated(p, argv, outcome) != gated:
            rows.append({"p": p, "w": width, "a": [_t(a) for a in argv], **outcome})
    return rows


def generate() -> dict[str, bytes]:
    rows = _rows(False)
    header = {**_header("argparse"), "cases": len(rows)}
    return {"argparse.jsonl": jsonl([header, *rows])}


def generate_python() -> dict[str, bytes]:
    v = sys.version_info
    running = f"{v.major}.{v.minor}.{v.micro}"
    header = _header("argparse_python")
    if running != REPRODUCES:
        # Declined: the rows are 3.13.13's alone.
        header["declined"] = running
        return {"argparse_python.jsonl": jsonl([header])}
    rows = _rows(True)
    lines = [dumps_line(_run(2, 78, argv)) for argv in _sweep_argvs()]
    sweep = {
        "sweep": {"seed": SWEEP_SEED, "tokens": SWEEP_TOKENS, "parser": 2, "w": 78},
        "count": len(lines),
        "sha256": hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest(),
    }
    header["cases"] = len(rows)
    return {"argparse_python.jsonl": jsonl([header, *rows, sweep])}
