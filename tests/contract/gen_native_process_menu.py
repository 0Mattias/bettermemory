"""The shipped CLI's menus and parser exits with process argv and redirected output.

Root, ``log`` and ``log verify`` help, the bare ``log`` menu, the parser's
errors and the hook parser reached past ``--``, as 3.13.13's `argparse`
words and wraps them. Every case ends in the parser or in `log_cmd.run`'s
menu: none reads a configuration, a store or a key.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest.mock import patch

from bettermemory import _entry

from ._common import jsonl


def cases() -> list[tuple[str | None, list[str]]]:
    out: list[tuple[str | None, list[str]]] = [
        (width, argv)
        for argv in (["--help"], ["log", "--help"], ["log", "verify", "--help"])
        for width in (None, "80", "40", "120", "20", "1")
    ]
    out += [
        ("80", argv)
        for argv in (
            ["-h"],
            ["log", "-h"],
            ["log", "verify", "-h"],
            ["log"],
            ["--", "log"],
            ["log", "--"],
            ["--help", "--version"],
            ["--version", "--help"],
            ["--help", "log"],
            ["log", "-h", "verify"],
            ["log", "verify", "--json", "--help"],
            ["log", "verify", "--anchor", "a", "-h"],
            ["log", "--version"],
            ["log", "nope"],
            ["log", "ver"],
            ["log", "verify", "--nope"],
            ["log", "verify", "extra"],
            ["log", "verify", "--anchor"],
            ["log", "verify", "--json=1"],
            ["log", "verify", "--anchor", "--json"],
            ["nope"],
            ["lo"],
            ["--nope"],
            ["--nope", "log"],
            ["--"],
            ["--", "--"],
            ["--", "hook"],
            ["--", "hook", "--help"],
            ["--", "hook", "stop"],
            ["--", "hook", "prompt", "--quiet"],
            ["--", "hook", "bogus"],
            ["--", "hook", "stop", "--nope"],
            ["--", "nope"],
        )
    ]
    out += [
        ("20", ["log"]),
        (None, ["log"]),
        ("20", ["log", "nope"]),
        ("40", ["log", "verify", "--nope"]),
        ("20", ["nope"]),
    ]
    return out


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for columns, argv in cases():
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"NO_COLOR": "1", "PYTHON_COLORS": "0"}),
            patch.object(sys, "argv", ["bettermemory", *argv]),
            patch.object(sys, "__stdout__", None),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            if columns is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = columns
            try:
                _entry.main()
            except SystemExit as exit_:
                code = exit_.code
            else:
                raise AssertionError(f"{argv}: the menu or the parser must exit")
        rows.append(
            {
                "argv": argv,
                "columns": columns,
                "code": code,
                "out": out.getvalue(),
                "err": err.getvalue(),
            }
        )
    name = "native_process_menu_python"
    return {f"{name}.jsonl": jsonl([{"fixture": name, "cases": len(rows)}, *rows])}
