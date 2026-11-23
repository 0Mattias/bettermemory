"""The read-only CLI's load refusal, and the public operation on real POSIX files.

The POSIX matrix interns identical results to keep all 1,440 observations in
the fixture, including exact stderr, warning order and surrogateescaped bytes.
Every call also checks the untouched config files, home and working directory.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import os
import tempfile
from collections.abc import Callable
from itertools import product
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import config, managed
from bettermemory.cli import _common

from ._common import captured_by, dumps_line, jsonl, text_field

_FILES = {
    "empty": "",
    "invalid": "[broken\n",
    "relative": '[storage]\ndirectory="link/../chosen"\n',
    "managed": '[storage]\ndirectory="~/org"\n',
    "homefallback": "[storage]\ncwd_store=false\n",
    "unknown": '[storage]\ndirectory="~bettermemory_invalid_reference_user_x/alice"\n',
}
_ENVS = (
    None,
    "",
    "link/../env",
    "~/one",
    "~bettermemory_invalid_reference_user_x/env",
    "raw-\udcff",
)


class Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _answer(call: Callable[[], Path], root: Path | None = None) -> dict[str, Any]:
    err = io.StringIO()

    def scrub(text: str) -> str:
        return text if root is None else text.replace(str(root), "<root>")

    with contextlib.redirect_stderr(err):
        try:
            selection = call()
        except SystemExit as exc:
            return {"exit": exc.code, **text_field("stderr", scrub(err.getvalue()))}
        except (ValueError, OSError, RuntimeError) as exc:
            return {
                "raised": type(exc).__name__,
                **text_field("message", scrub(str(exc))),
            }
    return text_field("path", scrub(str(selection)))


def generate() -> dict[str, bytes]:
    rows = []
    with patch.dict(
        os.environ, {"COLUMNS": "80", "NO_COLOR": "1", "PYTHON_COLORS": "0"}
    ):
        parser = argparse.ArgumentParser(prog="bettermemory log")
        for code in (1, 2, 7):
            with patch.object(
                _common, "load_config", side_effect=ValueError("shared reason")
            ) as reader:
                out = _answer(
                    lambda: _common.read_only_store_path(parser, exit_code=code)
                )
                reader.assert_called_once_with(create=False)
            rows.append({"code": code, "out": out})
    return {"cli_read_only_path.jsonl": jsonl(rows)}


def _remove(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        p.rmdir()


def _file(p: Path, kind: str) -> None:
    _remove(p)
    if kind == "directory":
        p.mkdir()
    elif kind == "dangling":
        p.symlink_to(p.parent / "not-there")
    elif kind in _FILES:
        p.write_bytes(_FILES[kind].encode())


def _project(p: Path, kind: str, target: Path) -> None:
    _remove(p)
    if kind == "dir":
        p.mkdir()
    elif kind == "file":
        p.write_bytes(b"fixture")
    elif kind == "symlink":
        p.symlink_to(target, target_is_directory=True)
    elif kind == "dangling":
        p.symlink_to(p.parent / "not-there")


def _snapshot(p: Path) -> object:
    if p.is_symlink():
        return ("link", os.readlink(p))
    if p.is_file():
        return ("file", p.read_bytes())
    return ("dir", sorted(os.listdir(p))) if p.is_dir() else None


def generate_posix() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        home = root / "fixture-account"
        cwd = root / "cwd"
        other = root / "actual" / "other"
        home.mkdir()
        cwd.mkdir()
        other.mkdir(parents=True)
        (cwd / "link").symlink_to(other, target_is_directory=True)
        user, org, local = (
            root / "user.toml",
            root / "managed.toml",
            cwd / ".claude-memory",
        )
        watch = (user, org, local, home, root, cwd)
        env = {
            "HOME": str(home),
            "COLUMNS": "80",
            "NO_COLOR": "1",
            "PYTHON_COLORS": "0",
            "BETTERMEMORY_STATE_DIR": str(root / "state"),
            "BETTERMEMORY_KEYS_DIR": str(root / "keys"),
        }
        sink = Warnings()
        rows: list[dict[str, Any]] = []
        outcomes: list[dict[str, Any]] = []
        distinct: dict[str, int] = {}
        with (
            contextlib.chdir(cwd),
            patch.dict(os.environ, env),
            patch.object(config, "default_config_path", return_value=user),
            patch.object(managed, "managed_config_path", return_value=org),
            patch.object(config, "_DEPRECATED_KEY_WARNED_PATHS", set()),
            captured_by(
                logging.getLogger("bettermemory.config"), sink, logging.WARNING
            ),
        ):
            parser = argparse.ArgumentParser(prog="bettermemory log")
            for u in (
                "missing",
                "empty",
                "invalid",
                "relative",
                "homefallback",
                "unknown",
                "directory",
                "dangling",
            ):
                _file(user, u)
                for m in ("missing", "managed", "invalid"):
                    _file(org, m)
                    for p in ("missing", "dir", "file", "symlink", "dangling"):
                        _project(local, p, other)
                        before = tuple(map(_snapshot, watch))
                        result = []
                        for v, code in product(_ENVS, (1, 2)):
                            if v is None:
                                os.environ.pop(config.ENV_DIR_OVERRIDE, None)
                            else:
                                os.environ[config.ENV_DIR_OVERRIDE] = v
                            sink.messages.clear()
                            out = _answer(
                                lambda: _common.read_only_store_path(
                                    parser, exit_code=code
                                ),
                                root,
                            )
                            out["warnings"] = [
                                text_field("text", msg.replace(str(root), "<root>"))
                                for msg in sink.messages
                            ]
                            assert before == tuple(map(_snapshot, watch))
                            key = dumps_line(out)
                            if key not in distinct:
                                distinct[key] = len(outcomes)
                                outcomes.append(out)
                            result.append(distinct[key])
                        rows.append(
                            {"user": u, "managed": m, "project": p, "results": result}
                        )
        header = {
            "outcomes": outcomes,
            "env": [None if v is None else text_field("value", v) for v in _ENVS],
            "codes": [1, 2],
        }
        return {"cli_read_only_path_posix.jsonl": jsonl([header, *rows])}
