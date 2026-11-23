"""Version, transaction and exclusive-proof diagnostics from a real SQLite connection."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bettermemory
from bettermemory import store_upgrade, store_versions
from bettermemory.store_schema import SCHEMA_REVISION
from tests.contract._common import jsonl

_ENGINE = "2.4.1"
_READER = store_versions.MIN_READER_KEY
_WRITER = store_versions.MIN_WRITER_KEY
_SCHEMA = store_versions.SCHEMA_REVISION_KEY


def _points(text: str) -> list[int]:
    return list(map(ord, text))


def _exception(error: BaseException) -> dict[str, Any]:
    return {
        "class": type(error).__name__,
        "message": _points(str(error)),
        "key": getattr(error, "key", None),
        "needed": getattr(error, "needed", None),
        "sqlite_code": getattr(error, "sqlite_errorcode", None),
        "sqlite_name": getattr(error, "sqlite_errorname", None),
        "cause": _exception(error.__cause__) if error.__cause__ else None,
    }


def _case(
    op: str, name: str, meta: dict[str, str], missing: bool = False
) -> dict[str, Any]:
    # The bare filename speaks the same way on POSIX and Windows. It is
    # only the diagnostic argument; the SQLite connection here is in memory.
    path = Path(name)
    error = None
    result: dict[str, object] | bool | None = None
    with (
        closing(sqlite3.connect(":memory:", isolation_level=None)) as conn,
        patch.object(bettermemory, "__version__", _ENGINE),
        patch.object(store_versions, "_READ_ONLY_TOLD", set()),
        patch.object(store_versions.log, "warning") as warning,
    ):
        conn.execute("PRAGMA busy_timeout = 273")
        conn.execute("CREATE TABLE trial(value)")
        if not missing:
            conn.execute("CREATE TABLE meta(key, value)")
            conn.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        try:
            if op == "check":
                verdict = store_versions.check_versions(conn, path, dict(meta), False)
                result = {
                    "revision": verdict.revision,
                    "min_reader": verdict.min_reader,
                    "min_writer": verdict.min_writer,
                    "read_only": verdict.read_only,
                    "reason": _points(verdict.reason)
                    if verdict.reason is not None
                    else None,
                }
            elif op == "writer":
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = store_versions.require_current_writer(conn, path)
                finally:
                    conn.execute("ROLLBACK")
            else:
                assert op in {"upgrade", "upgrade-cleanup"}
                # The second BEGIN raises a real OperationalError with its
                # SQLite code. The proof then rolls this insert back.
                conn.execute("BEGIN")
                conn.execute("INSERT INTO trial VALUES (1)")
                if op == "upgrade-cleanup":
                    conn.set_authorizer(
                        lambda action, arg, *_: (
                            sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_PRAGMA
                            and arg == "schema_version"
                            else sqlite3.SQLITE_OK
                        )
                    )
                store_upgrade._prove_exclusive(conn, path)
        except (store_versions.StoreVersionError, sqlite3.Error) as exc:
            error = _exception(exc)
        finally:
            conn.set_authorizer(None)
        warnings = [
            _points(fmt % tuple(args)) for (fmt, *args), _ in warning.call_args_list
        ]
        return {
            "op": op,
            "path": _points(name),
            "meta": meta,
            "missing_meta_table": missing,
            "result": result,
            "error": error,
            "warnings": warnings,
            "in_transaction": conn.in_transaction,
            "query_only": conn.execute("PRAGMA query_only").fetchone()[0],
            "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
            "rows": conn.execute("SELECT count(*) FROM trial").fetchone()[0],
        }


def generate() -> dict[str, bytes]:
    fixture = "store_version_diagnostics"
    current = str(SCHEMA_REVISION)
    high = str(SCHEMA_REVISION + 7)
    cases = [
        _case("check", "ordinary", {_SCHEMA: current}),
        _case("check", "quoted-'π", {_SCHEMA: current, _READER: high}),
        _case("check", "three", {_SCHEMA: "invalid", _READER: "æ", _WRITER: "bad"}),
        _case("check", "two", {_SCHEMA: current, _READER: "x", _WRITER: "bad"}),
        _case("check", "unicode", {_SCHEMA: current, _READER: "６", _WRITER: "bad"}),
        _case("check", "size64", {_WRITER: "界" * 64}),
        _case("check", "size65", {_WRITER: "界" * 65}),
        _case("check", "after-repr", {_WRITER: "é" * 59 + "\u202e" + "x"}),
        _case("writer", "no-table", {}, True),
        _case("writer", "missing-key", {}),
        _case("writer", "ordinary", {_WRITER: current}),
        _case("upgrade", "quotes-'π", {}),
        _case("upgrade-cleanup", "finally", {}),
    ]
    for name in ["store-\udcff", "store-\udcfe", "store-\ufffd"]:
        cases.extend(
            [
                _case("check", name, {_WRITER: "broken"}),
                _case("check", name, {_READER: high, "engine_version": "rev-\nπ"}),
                _case("check", name, {_WRITER: high, "engine_version": ""}),
                _case("writer", name, {_WRITER: "broken"}),
                _case("writer", name, {_WRITER: high}),
                _case("upgrade", name, {}),
            ]
        )
    return {fixture + ".jsonl": jsonl([{"fixture": fixture}, *cases])}
