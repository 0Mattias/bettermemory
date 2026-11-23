"""Physical reader observations from ordinary signed SQLite stores and their keyrings."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bettermemory import log, store_keys
from bettermemory.store import Store

from ._common import jsonl
from ._sandbox import world


def _create(root: Path):
    path = root / "memory.sqlite"
    with Store.create(path, keys_dir=root / "keys", strict_provenance=False) as owner:
        first = owner.keyring.head_path().read_bytes()
        owner.record_event("search", session="ordinary", query="sample")
        return path, owner.keyring, owner.key_fingerprint, first


def _reader(path: Path):
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True))


def generate() -> dict[str, bytes]:
    rows = []
    with world(), TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        path, original, expected, _ = _create(root / "original")
        backup = root / "backup.sqlite"
        with _reader(path) as source, closing(sqlite3.connect(backup)) as dest:
            source.backup(dest)
        for mode in (
            "original",
            "backup",
            "unbound",
            "unclaimed",
            "empty",
            "wrong-expected",
        ):
            directory = root / mode / "ring-copy"
            shutil.copytree(original.keys_dir, directory)
            ring = log.KeyRing(directory, original.store_id)
            target = backup if mode == "backup" else path
            if mode in ("unbound", "unclaimed", "empty"):
                ring.binding_path().unlink()
            if mode == "unclaimed":
                target, _, _, _ = _create(root / "independent")
            if mode == "empty":
                for file in directory.iterdir():
                    file.unlink()
            named = "0" * 64 if mode == "wrong-expected" else expected
            levels: list[str] = []

            class Capture(logging.Handler):
                def emit(self, record):
                    levels.append(record.levelname)

            with (
                _reader(target) as conn,
                patch.object(store_keys, "_BOUND_ELSEWHERE_WARNED", set()),
                patch.object(store_keys.log, "handlers", [Capture()]),
                patch.object(store_keys.log, "level", logging.DEBUG),
                patch.object(store_keys.log, "_cache", {}),
                patch.object(store_keys.log, "propagate", False),
            ):
                for joined, warn in ((False, False), (True, True), (False, True)):
                    if joined:
                        conn.execute("BEGIN")
                    levels.clear()
                    state = store_keys.key_state(
                        ring, target, named, conn=conn, warn=warn
                    )
                    rows.append(
                        {
                            "mode": mode,
                            "joined": joined,
                            "warn": warn,
                            "result": [
                                state.state,
                                state.held == expected,
                                state.bound_to == path,
                                state.unfit is not None,
                                levels.copy(),
                                conn.in_transaction,
                            ],
                        }
                    )
                    if joined:
                        conn.rollback()
    return {"store_key_read.jsonl": jsonl(rows)}


def signed() -> dict[str, bytes]:
    rows = []
    with world(), TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        path, original, named, first_head = _create(root / "owner")
        before = original.load_current()
        assert before is not None
        other, _, _, _ = _create(root / "independent")
        for mode in ("current", "stale", "no-head", "no-origin", "retired", "no-key"):
            directory = root / mode / "keys"
            shutil.copytree(original.keys_dir, directory)
            ring = log.KeyRing(directory, original.store_id)
            if mode == "stale":
                ring.head_path().write_bytes(first_head)
            if mode == "no-head":
                ring.head_path().unlink()
            if mode == "no-origin":
                ring.origin_path().unlink()
            if mode == "retired":
                ring.current_path().rename(directory / f"{ring.store_id}.ordinary.key")
            if mode == "no-key":
                ring.current_path().unlink()
            for target, file in (("owner", path), ("independent", other)):
                with _reader(file) as conn:
                    conn.execute("BEGIN")
                    for hold, key in (
                        ("file", None),
                        ("retained", before),
                        ("different", b"other"),
                    ):
                        reads: list[str] = []
                        conn.set_trace_callback(reads.append)
                        fit = store_keys.ring_fit(conn, ring, current_key=key)
                        conn.set_trace_callback(None)
                        rows.append(
                            {
                                "mode": mode,
                                "target": target,
                                "hold": hold,
                                "result": [
                                    fit.origin.status,
                                    fit.origin.reason,
                                    fit.origin.signer == named,
                                    fit.head_status,
                                    fit.log_end.seq,  # type: ignore[union-attr]
                                    fit.fits,
                                    len(reads),
                                    conn.in_transaction,
                                ],
                            }
                        )
    return {"store_key_read_signed.jsonl": jsonl(rows)}


def posix_paths() -> dict[str, bytes]:
    rows = []
    with world(), TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        path, owner, expected, _ = _create(root / "owner")
        with _reader(path) as conn:
            for name in (
                "bad\0dir",
                "bad\ud800dir",
                "\0A\ud800\ud801B",
                "A\ud800\udcffB",
            ):
                ring = log.KeyRing(Path(name), owner.store_id)
                for operation in ("fit", "classify"):
                    sql: list[str] = []
                    conn.set_trace_callback(sql.append)
                    try:
                        if operation == "fit":
                            fit = store_keys.ring_fit(conn, ring)
                            result = [
                                fit.origin.status,
                                fit.head_status,
                                fit.log_end.seq,  # type: ignore[union-attr]
                            ]
                        else:
                            store_keys.key_state(ring, path, expected, conn=conn)
                    except ValueError as exc:
                        result = [type(exc).__name__, str(exc)]
                        if isinstance(exc, UnicodeEncodeError):
                            result += [
                                [
                                    ord(c)
                                    for c in exc.object.replace(owner.store_id, "ID")
                                ],
                                exc.start,
                                exc.end,
                            ]
                    conn.set_trace_callback(None)
                    rows.append(
                        {
                            "directory": list(map(ord, name)),
                            "op": operation,
                            "result": result,
                            "sql": len(sql),
                        }
                    )
    return {"store_key_read_paths_posix.jsonl": jsonl(rows)}
