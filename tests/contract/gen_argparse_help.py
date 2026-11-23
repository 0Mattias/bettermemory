"""Default argparse help pages for the three registered log-verification nodes."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import textwrap
from typing import Any
from unittest.mock import patch

from bettermemory.cli import _build_parser

from ._common import jsonl
from .gen_argparse import _t


def _choices(parser: argparse.ArgumentParser) -> list[list[str]]:
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    rows: list[list[str]] = []
    for choice in sub._choices_actions:
        assert isinstance(choice.help, str)
        rows.append([choice.dest, choice.help])
    return rows


def _exit(parser: argparse.ArgumentParser, args: list[str]) -> dict[str, Any]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            parser.parse_args(args)
        except SystemExit as exc:
            return {
                "code": exc.code,
                "out": _t(stdout.getvalue()),
                "err": _t(stderr.getvalue()),
            }
    raise AssertionError("a help or version action must exit")


def generate_python() -> dict[str, bytes]:
    terminal_columns = [
        24,
        31,
        39,
        43,
        44,
        45,
        56,
        69,
        77,
        79,
        80,
        81,
        98,
        119,
        120,
        121,
        151,
        192,
    ]
    rows: list[Any] = []
    environment = {
        "COLUMNS": "80",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "PYTHON_COLORS": "0",
    }
    with patch.dict(os.environ, environment):
        root, commands = _build_parser()
        log = commands["log"]
        rows.append(
            {
                "root": _choices(root),
                "log": _choices(log),
                "description": log.description,
            }
        )
        for columns in terminal_columns:
            os.environ["COLUMNS"] = str(columns)
            for argv in (["-h"], ["log", "--help"], ["log", "verify", "-h"]):
                rows.append(
                    {"width": columns - 2, "argv": argv, "exit": _exit(root, argv)}
                )
        # Before 3.13 aliases repeated the metavar. Also exercise the actual
        # ASCII normalization before Unicode strip and %-substitution.
        for width in [16, 25, 40, 78]:
            os.environ["COLUMNS"] = str(width + 2)
            p = argparse.ArgumentParser(
                prog="archive", description=" %(prog)s\tuses  A\u00a0B and x\u2003y. "
            )
            p.add_argument(
                "-I", "--include", metavar="FILE", help="A\u2003B,  100%% for %(prog)s."
            )
            rows.append(
                {
                    "alias": True,
                    "width": width,
                    "argv": ["--help"],
                    "exit": _exit(p, ["--help"]),
                }
            )
    # Python 3.11 and 3.12 retained a space at a line boundary when the
    # next chunk exceeded the entire width.
    rows.append({"fill": "x abc", "width": 2, "out": textwrap.fill("x abc", 2)})
    return {"argparse_help_python.jsonl": jsonl(rows)}


def generate() -> dict[str, bytes]:
    rows: list[Any] = []
    for text, width in [
        ("ab-cd----ef other-word", 7),
        ("a-b-c x-y-foo A——B", 5),
        ("\talpha  beta\n  gamma\rd", 12),
        ("  \u2003Àé à\u00a0b x\u2003y", 8),
        ("use --this-very-long-option and wait", 11),
        ("a\udcffb----Âß _²-ab-cd", 9),
        ("😀e\u0301 non-breaking\u00a0space", 13),
        ("------", 2),
        ("", 78),
    ]:
        rows.append(
            {"fill": _t(text), "width": width, "out": _t(textwrap.fill(text, width))}
        )
    return {"argparse_help.jsonl": jsonl(rows)}
