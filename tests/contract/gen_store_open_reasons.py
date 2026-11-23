"""Exceptions before a connected reader constructs its first key ring."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bettermemory
from bettermemory import store_open, store_versions
from bettermemory.store import Store
from tests.contract._common import jsonl


class _ReachedKeys(Exception):
    """Stop after checking the connected database and before reading keys."""


def _observe(name: str, metadata: dict[str, str]) -> dict[str, Any]:
    path = Path(name)
    with (
        closing(sqlite3.connect(":memory:", isolation_level=None)) as db,
        patch.object(bettermemory, "__version__", "2.4.1"),
        patch.object(store_open, "KeyRing", side_effect=_ReachedKeys) as ring,
        patch.object(store_versions, "_READ_ONLY_TOLD", set()),
        patch.object(store_versions.log, "warning") as warning,
    ):
        db.execute("CREATE TABLE meta(key TEXT, value TEXT)")
        db.executemany("INSERT INTO meta VALUES (?, ?)", metadata.items())
        db.execute("BEGIN")
        try:
            store_open._open_connected(
                Store,
                path,
                db,
                None,
                keys_dir=Path("fixture-keys"),
                allow_rekey=False,
                strict_provenance=None,
                read_only=True,
            )
        except _ReachedKeys:
            error = None
        except ValueError as exc:
            error = {
                "class": type(exc).__name__,
                "message_cps": list(map(ord, str(exc))),
                "key": getattr(exc, "key", None),
                "needed": getattr(exc, "needed", None),
            }
        return {
            "path_cps": list(map(ord, str(path))),
            "meta": metadata,
            "error": error,
            "reached_keys": ring.called,
            "keyring_id": ring.call_args.args[1] if ring.called else None,
            "read_only_reason": list(map(ord, warning.call_args.args[1]))
            if warning.called
            else None,
            "query_only": db.execute("PRAGMA query_only").fetchone()[0],
            "in_transaction": db.in_transaction,
        }


def generate() -> dict[str, bytes]:
    name = "store_open_reasons"
    valid = {"store_id": "a" * 32, "key_fingerprint": "f" * 64}
    rows = []
    for path in ("café", "caf\udcc3\udca9", "store-\udcff", "store-\ufffd"):
        for meta in (
            {},
            {"key_fingerprint": "f"},
            {"store_id": "wrong", "schema_version": "oops"},
            {**valid, "store_id": "", "schema_version": "oops"},
            {**valid, "key_fingerprint": "", "min_reader_revision": "99"},
            {**valid, "store_id": "'\0é\n" * 12},
            {**valid, "schema_version": "  +００_٢  ", "min_reader_revision": "bad"},
            {**valid, "schema_version": " \x1e0"},
            {**valid, "schema_version": "broken'\n" * 48},
            {**valid, "schema_version": "-９", "min_reader_revision": "99"},
            {**valid, "min_writer_revision": "99"},
            {**valid, "schema_version": "1"},
            {**valid, "schema_version": "  +٠_١  "},
            valid,
        ):
            rows.append(_observe(path, meta))
    return {name + ".jsonl": jsonl([{"fixture": name}, *rows])}
