"""The supplied-path CLI policy, calling the reference with observed stat, bundle
and opener outcomes. Filesystem paths and diagnostics may contain a surrogate.
The portable operations do not depend on the host's filesystem or errno text.
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import stat
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from bettermemory import backup
from bettermemory.cli._common import open_read_only
from bettermemory.restore_lock import RestoreRunningError
from bettermemory.store import Store

from ._common import jsonl, text_field


def _case(phase: str, behavior: str, code: int, raw: bool) -> dict[str, Any]:
    path = Path("raw-\udcff" if raw else "memory.sqlite")
    trace: list[str] = []
    output = io.StringIO()
    owner = object()
    render_error = LookupError("format fault")

    families: dict[str, type[BaseException]] = {
        "missing": FileNotFoundError,
        "not_directory": NotADirectoryError,
        "os": OSError,
        "value": ValueError,
        "database": sqlite3.DatabaseError,
        "restore": RestoreRunningError,
        "other": ArithmeticError,
        "lookalike": type("RestoreRunningError", (Exception,), {}),
    }
    key = (
        behavior.removesuffix("_bad_str")
        .removesuffix("_custom_format")
        .removesuffix("_bad_format")
    )

    def render(self: BaseException) -> str:
        trace.append("str")
        if behavior.endswith("_bad_str"):
            raise render_error
        return "reason-\udcfe" if raw else "reason"

    def custom_format(self: BaseException, spec: str) -> str:
        assert spec == ""
        trace.append("format")
        if behavior.endswith("_bad_format"):
            raise render_error
        return "formatted"

    methods: dict[str, Any] = {"__str__": render}
    if behavior.endswith(("_custom_format", "_bad_format")):
        methods["__format__"] = custom_format
    label = "RestoreRunningError" if key == "lookalike" else "Source"
    error_type = type(label, (families.get(key, Exception),), methods)
    source = error_type(path) if key == "restore" else error_type()

    def step(at: str, supplied: Path) -> None:
        assert supplied is path
        trace.append(at)
        if at == phase and key in families:
            raise source

    def stat_result(supplied: Path) -> SimpleNamespace:
        step("stat", supplied)
        mode = (
            stat.S_IFDIR if phase == "stat" and key == "non_regular" else stat.S_IFREG
        )
        return SimpleNamespace(st_mode=mode)

    def bundle(supplied: Path) -> str | None:
        step("bundle", supplied)
        if phase == "bundle" and key == "refuse":
            return "bundle-\udcfe" if raw else "bundle"
        if phase == "bundle" and key == "empty":
            return ""
        return None

    def reader(supplied: Path, *, read_only: bool) -> object:
        assert read_only is True
        step("open", supplied)
        return owner

    parser = argparse.ArgumentParser(prog="bettermemory log")
    result: str | int
    with (
        patch.object(Path, "stat", stat_result),
        patch.object(backup, "bundle_refusal", bundle),
        patch.object(Store, "open", side_effect=reader),
        redirect_stderr(output),
    ):
        try:
            assert open_read_only(parser, path, exit_code=code) is owner
            result = "owner"
        except SystemExit as exc:
            assert isinstance(exc.code, int)
            result = exc.code
        except BaseException as exc:
            assert exc is source or exc is render_error
            result = "original" if exc is source else "format"
    return {
        "phase": phase,
        "behavior": behavior,
        "code": code,
        "raw": raw,
        "trace": trace,
        "result": result,
        **text_field("stderr", output.getvalue()),
    }


def generate() -> dict[str, bytes]:
    rows = []
    for phase, behaviors in (
        (
            "stat",
            (
                "non_regular",
                "missing_bad_str",
                "not_directory",
                "os",
                "os_bad_str",
                "other",
                "value_bad_str",
            ),
        ),
        ("bundle", ("empty", "refuse", "os_bad_str", "restore", "other")),
        (
            "open",
            (
                "ok",
                "missing",
                "not_directory",
                "os",
                "value",
                "database",
                "restore",
                "lookalike_bad_str",
                "other",
                "os_bad_str",
                "restore_bad_str",
            ),
        ),
    ):
        for behavior in behaviors:
            for code, raw in ((2, False), (1, True)):
                rows.append(_case(phase, behavior, code, raw))
    for phase, behavior in (
        ("stat", "os_custom_format"),
        ("stat", "os_bad_format"),
        ("open", "os_custom_format"),
        ("open", "os_bad_format"),
        ("open", "restore_custom_format"),
        ("open", "restore_bad_format"),
    ):
        rows.append(_case(phase, behavior, 7, False))
    for code in (0, -7, 17, 75):
        for phase, behavior in (
            ("open", "restore"),
            ("stat", "missing"),
            ("open", "os"),
            ("bundle", "empty"),
        ):
            rows.append(_case(phase, behavior, code, False))
    return {"cli_open_policy.jsonl": jsonl(rows)}
