"""The connected Windows Store's key directory, with its shell-folder call supplied."""

from __future__ import annotations

import logging
import ntpath
import os
import sqlite3
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import platformdirs
from platformdirs import windows

from bettermemory import log, store_open, store_schema
from bettermemory.store import Store
from tests.contract._common import jsonl
from tests.contract.gen_key_directory import in_fresh_process


class _WindowsPath(PureWindowsPath):
    # The shipped Path method, on real CPython Windows lexical components.
    expanduser = Path.expanduser


class _WindowsOs(SimpleNamespace):
    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


def _points(text: str) -> list[int]:
    return list(map(ord, text))


def _observe(
    database: Path,
    conn: sqlite3.Connection,
    explicit: str | None,
    env: dict[str, str],
    shell: str,
    *,
    default: bool = False,
) -> dict[str, Any]:
    class ReachedRing(Exception):
        pass

    shell_calls: list[str] = []

    def locate(name: str) -> str:
        shell_calls.append(name)
        if shell == "error":
            raise OSError(5, "folder lookup failed")
        if shell == "null":
            raise ValueError(
                "SHGetKnownFolderPath returned NULL for CSIDL_LOCAL_APPDATA"
            )
        return "" if shell == "empty" else "F:/profile/first/../local"

    def reached(directory: Path, identity: str) -> None:
        raise ReachedRing(log.KeyRing(directory, identity).keys_dir)

    view = _WindowsOs(environ=env, path=ntpath)
    # The Store database and its lease use the host filesystem. Supply the
    # Windows Path only to the three signing-key selection/constructor sites.
    with ExitStack() as st:
        st.enter_context(patch.object(platformdirs, "PlatformDirs", windows.Windows))
        st.enter_context(patch.object(windows, "_resolve_win_folder", locate))
        st.enter_context(patch.object(windows, "os", view))
        st.enter_context(patch.object(ntpath, "os", view))
        st.enter_context(patch.dict(Path.expanduser.__globals__, {"os": view}))
        st.enter_context(patch.object(store_schema, "os", view))
        for module in (store_open, store_schema, log):
            st.enter_context(patch.object(module, "Path", _WindowsPath))
        st.enter_context(patch.object(store_open, "KeyRing", reached))
        selected: Path | None = None
        try:
            if default:
                selected = log.default_keys_dir()
            else:
                store_open._open_connected(
                    Store,
                    database,
                    conn,
                    None,
                    keys_dir=explicit,
                    allow_rekey=False,
                    strict_provenance=None,
                    read_only=True,
                )
                raise AssertionError("the connected store skipped the key ring")
        except ReachedRing as done:
            selected = done.args[0]
        except (ValueError, RuntimeError, OSError) as exc:
            result = {
                "class": type(exc).__name__,
                "message": _points(str(exc)),
                "errno": getattr(exc, "errno", None),
                "winerror": getattr(exc, "winerror", None),
            }
        if selected is not None:
            text = str(selected)
            raw = text.encode("utf-16-le", "surrogatepass")
            result = {
                "path": _points(text),
                "units": [
                    int.from_bytes(raw[i : i + 2], "little")
                    for i in range(0, len(raw), 2)
                ],
            }
    return {
        "explicit": None if explicit is None else _points(explicit),
        "environment": {name: _points(value) for name, value in env.items()},
        "shell": shell,
        "default": default,
        "shell_calls": shell_calls,
        "out": result,
    }


def _from_store() -> bytes:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        db = root / "memory.sqlite"
        Store.create(db, keys_dir=root / "test-ring").close()
        with closing(sqlite3.connect(db)) as conn:
            rows.extend(_cases(db, conn))
    return jsonl(rows)


def _cases(db: Path, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in (
        "",
        ".",
        "~",
        "~other/a",
        "/rooted",
        "C:",
        "C:a/./b/../c",
        "C:/a/../b",
        "a//b/.",
        "//server/share",
        "//?/UNC/server/share/folder",
        "ȗ:leaf",
        "\ud83d-separated-\ude42",
        "🙂",
        "bad\udcff",
        "a\0b",
        "\U000f0000\U00100000\ud800",
    ):
        rows.append(
            _observe(db, conn, raw, {"BETTERMEMORY_KEYS_DIR": "~missing"}, "error")
        )
    profiles: tuple[dict[str, str], ...] = (
        {},
        {"USERPROFILE": "P:/profiles/Sam", "USERNAME": "Sam"},
        {"USERPROFILE": "P:/custom", "USERNAME": "Sam"},
        {"USERPROFILE": ""},
        {"HOMEDRIVE": "Q:", "HOMEPATH": "/profiles/Sam", "USERNAME": "Sam"},
        {"USERPROFILE": "D:", "USERNAME": "Sam"},
    )
    for override in (
        ".",
        "relative/../keys",
        "~",
        "~/sub",
        "~Sam/sub",
        "~sam/sub",
        "~Else/sub",
    ):
        for profile in profiles:
            rows.append(
                _observe(
                    db,
                    conn,
                    None,
                    {"BETTERMEMORY_KEYS_DIR": override, **profile},
                    "error",
                )
            )
    for candidate in (
        None,
        "",
        "  ",
        "r:/one/../two",
        "  //server/share/dir  ",
        "L:relative",
        "/rooted",
    ):
        for shell in ("value", "empty", "null", "error"):
            env = {"APPDATA": "X:/unused", "LOCALAPPDATA": "Y:/unused"}
            if candidate is not None:
                env["WIN_PD_OVERRIDE_LOCAL_APPDATA"] = candidate
            rows.append(_observe(db, conn, None, env, shell))
    rows.append(
        _observe(
            db, conn, None, {"BETTERMEMORY_KEYS_DIR": "override"}, "value", default=True
        )
    )
    return rows


def generate() -> dict[str, bytes]:
    return {"key_directory_windows.jsonl": in_fresh_process(__name__)}


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    sys.stdout.buffer.write(_from_store())
