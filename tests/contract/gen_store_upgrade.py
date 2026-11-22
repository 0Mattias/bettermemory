"""Fixtures: `store_upgrade._prove_exclusive` (``store_upgrade.jsonl``,
``store_upgrade_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/store/upgrade.rs``. Each case makes
a store file ``memory.sqlite`` in a scratch folder (one table ``t``),
opens a connection to it and sets it up:

- ``journal``: the journal mode (``delete`` or ``wal``);
- ``timeout`` and ``mode``: the busy timeout and locking mode set first;
- ``other``: a second connection on the file, or null: ``open`` (open,
  one read done), ``reserved`` (``BEGIN IMMEDIATE`` held), ``exclusive``
  (``BEGIN EXCLUSIVE`` held), ``read`` (a read transaction held);
- ``own``: ``BEGIN`` and one insert on the connection itself first.

It then calls `_prove_exclusive(conn, Path(name))` with ``name`` (a bare
file name in the ungated file; in the POSIX file also names that
``str(Path(...))`` respells, with doubled or trailing separators, a
leading ``./`` or nothing, which a Windows host would spell with
backslashes) and records ``r`` (null,
or ``{"raised": class, "message": str(exc)}``), then ``after``: the busy
timeout, the locking mode, whether a transaction is open, the rows of
``t`` (the caller's insert rolled back or not) and whether the second
connection, its own transaction ended (before the rows are read), can then take a write lock with
no wait. The cases with a second connection are POSIX only: they hold
SQLite's file locks, which this fixture has seen on POSIX only.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from bettermemory import store_upgrade


def _run(case: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "memory.sqlite"
        setup = sqlite3.connect(file)
        setup.execute(f"PRAGMA journal_mode = {case['journal']}").fetchone()
        setup.execute("CREATE TABLE t (x INTEGER)")
        setup.commit()
        setup.close()
        conn = sqlite3.connect(file, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout = {case['timeout']}").fetchone()
        conn.execute(f"PRAGMA locking_mode = {case['mode']}").fetchone()
        other = None
        if case["other"] is not None:
            other = sqlite3.connect(file, isolation_level=None)
            other.execute("PRAGMA busy_timeout = 0").fetchone()
            kind = case["other"]
            if kind == "reserved":
                other.execute("BEGIN IMMEDIATE")
            elif kind == "exclusive":
                other.execute("BEGIN EXCLUSIVE")
            elif kind == "read":
                other.execute("BEGIN")
            other.execute("SELECT COUNT(*) FROM t").fetchone()
        if case["own"]:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO t VALUES (1)")
        try:
            store_upgrade._prove_exclusive(conn, Path(case["name"]))
            r: Any = None
        except Exception as exc:  # noqa: BLE001 - the class is the answer
            r = {"raised": type(exc).__name__, "message": str(exc)}
        after: dict[str, Any] = {
            "timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
            "mode": conn.execute("PRAGMA locking_mode").fetchone()[0],
            "in_tx": conn.in_transaction,
        }
        if other is not None and other.in_transaction:
            other.execute("ROLLBACK")
        after["rows"] = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        if other is not None:
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
                after["other_writes"] = True
            except sqlite3.OperationalError:
                after["other_writes"] = False
            other.close()
        conn.close()
    return {**case, "r": r, "after": after}


def _case(label: str, **kw: Any) -> dict[str, Any]:
    base = {
        "case": label,
        "journal": "delete",
        "timeout": 1234,
        "mode": "normal",
        "other": None,
        "own": False,
        "name": "memory.sqlite",
    }
    return {**base, **kw}


def cases() -> list[dict[str, Any]]:
    return [
        _case("alone"),
        _case("alone_wal", journal="wal"),
        _case("alone_exclusive", mode="exclusive", timeout=0),
        _case("own_transaction", own=True),
        _case("own_transaction_wal", journal="wal", own=True, timeout=7),
        _case("quoted_name", own=True, name='it\'s "a"\tstoreé.sqlite'),
    ]


def posix_cases() -> list[dict[str, Any]]:
    out = []
    for journal in ("delete", "wal"):
        for other in ("open", "read", "reserved", "exclusive"):
            out.append(_case(f"{journal}_{other}", journal=journal, other=other))
    for label, name in (
        ("name_doubled_separators", "a//b/"),
        ("name_dot_prefix", "./x.sqlite"),
        ("name_empty", ""),
        ("name_dots_inside", "/p/./q//"),
    ):
        out.append(_case(label, own=True, name=name))
    return out


def _text(rows: list[dict[str, Any]], fixture: str) -> bytes:
    lines = [{"fixture": fixture}] + rows
    return "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    ).encode("ascii")


def generate() -> dict[str, bytes]:
    return {"store_upgrade.jsonl": _text([_run(c) for c in cases()], "store_upgrade")}


def generate_posix() -> dict[str, bytes]:
    return {
        "store_upgrade_posix.jsonl": _text(
            [_run(c) for c in posix_cases()], "store_upgrade_posix"
        )
    }
