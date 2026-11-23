"""The shipped CLI's version action with process argv and redirected output."""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest.mock import patch

from bettermemory import _entry

from ._common import jsonl


def generate() -> dict[str, bytes]:
    cases: list[tuple[str | None, list[str]]] = [
        (width, ["--version"])
        for width in (
            None,
            "1",
            "13",
            "14",
            "23",
            "24",
            "80",
            "0",
            "-9",
            "bad",
            "+013",
            "1_3",
            "１3",
        )
    ]
    cases += [
        ("80", args)
        for args in (
            ["--v"],
            ["--vers"],
            ["--version", "extra"],
            ["--version", "--help"],
            ["--unknown-option", "--version"],
            ["--version", "hook"],
        )
    ]
    rows: list[dict[str, Any]] = []
    for columns, argv in cases:
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
                raise AssertionError("the version action must exit")
        rows.append(
            {
                "argv": argv,
                "columns": columns,
                "code": code,
                "out": out.getvalue(),
                "err": err.getvalue(),
            }
        )
    name = "native_process_entry"
    return {f"{name}.jsonl": jsonl([{"fixture": name, "cases": len(rows)}, *rows])}
