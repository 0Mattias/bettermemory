"""The shipped log verify declaration and its ordered parse results, without dispatch.

At 512 columns the full root usage fits on one line on Python 3.11–3.14.
This lets the same fixture preserve both root-owned and verify-owned errors in full.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import itertools
import io
import os
from typing import Any
from unittest.mock import patch

from bettermemory.cli import _build_parser

from ._common import dumps_line, jsonl
from .gen_argparse import _t


def _wire(value: Any) -> Any:
    if isinstance(value, list):
        return [_wire(item) for item in value]
    return _t(value)


def _parse(root: argparse.ArgumentParser, argv: list[str]) -> dict[str, Any]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            args = root.parse_args(argv)
        except SystemExit as exc:
            return {
                "code": exc.code,
                "out": _t(out.getvalue()),
                "err": _t(err.getvalue()),
            }
    return {"ns": [[name, _wire(value)] for name, value in vars(args).items()]}


def generate() -> dict[str, bytes]:
    cases = [
        [],
        ["--json"],
        ["--anchor", "a"],
        ["--anc=x", "--a=x"],
        ["--an="],
        ["--a", ""],
        ["--a", "-"],
        ["--a", "-2"],
        ["--a=--json"],
        ["--a=\udcff"],
        ["--anchor", "--json"],
        ["--anchor"],
        ["--anchor", "--help"],
        ["--json=yes"],
        ["--"],
        ["--extra"],
        ["--anchor=f", "stray"],
        ["--", "--json"],
        ["--a=1", "--js", "--anchor", "two"],
        ["--json", "--json"],
    ]
    rows: list[Any] = []
    with patch.dict(
        os.environ, {"COLUMNS": "512", "NO_COLOR": "1", "PYTHON_COLORS": "0"}
    ):
        root, commands = _build_parser()
        choices = next(
            a
            for a in commands["log"]._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        verify = choices.choices["verify"]
        rows.append(
            {
                "description": verify.description,
                "choice_help": next(
                    a.help for a in choices._choices_actions if a.dest == "verify"
                ),
                "options": [
                    [action.option_strings, action.dest, action.metavar, action.help]
                    for action in verify._actions
                    if action.dest != "help"
                ],
            }
        )
        for tail in cases:
            argv = ["log", "verify", *tail]
            rows.append(
                {"argv": [_t(arg) for arg in argv], "outcome": _parse(root, argv)}
            )
        tokens = [
            "--a=f",
            "--anchor",
            "--json",
            "--j",
            "-",
            "-7",
            "--an=",
            "value",
            "--",
        ]
        digest = hashlib.sha256()
        for triple in itertools.product(tokens, repeat=3):
            outcome = _parse(root, ["log", "verify", *triple])
            digest.update((dumps_line(outcome) + "\n").encode("ascii"))
        rows.append(
            {"tokens": tokens, "count": len(tokens) ** 3, "sha256": digest.hexdigest()}
        )
    return {"cli_log_args.jsonl": jsonl(rows)}
