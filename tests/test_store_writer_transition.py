"""An upgrade cannot leave an already-open older writer outside its gate."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from bettermemory import restore_lock
from bettermemory.origin import Origin
from bettermemory.store import Store
from bettermemory.store_schema import SCHEMA_REVISION, StoreVersionError
from bettermemory.store_versions import writes_rolled_back

from .test_store import _memory


def _legacy(path: Path, keys: Path) -> str:
    with Store.create(path, keys_dir=keys) as store:
        record = store.put_memory(_memory("before the upgrade"))
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_revision'")
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'min_writer_revision'")
        conn.execute("DROP TABLE native_membership_rows")
    return record.id


def _versions(path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as conn:
        return dict(conn.execute("SELECT key,value FROM meta"))


def test_upgrade_refuses_an_existing_legacy_connection_before_changing_the_store(
    tmp_path: Path,
) -> None:
    path, keys = tmp_path / "memory.sqlite", tmp_path / "keys"
    record_id = _legacy(path, keys)
    before = _versions(path)
    older = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        assert older.execute(
            "SELECT body FROM memories WHERE id = ?", (record_id,)
        ).fetchone()
        with pytest.raises(StoreVersionError, match="another connection has it open"):
            Store.open(path, keys_dir=keys, allow_rekey=True)
        assert _versions(path) == before
        assert restore_lock.read_marker(path) is None
    finally:
        older.close()
    with Store.open(path, keys_dir=keys, allow_rekey=True) as store:
        assert store.meta()["schema_revision"] == str(SCHEMA_REVISION)
        assert store.meta()["min_writer_revision"] == "5"
        raw = "/w/bytes-" + chr(0xDCFF)
        record = store.put_memory(
            _memory("native after upgrade", origin=Origin(cwd=raw))
        )
        assert store.get_memory(record.id).origin == record.origin
        assert store.log_verify()["status"] == "ok"
        with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as other:
            assert other.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")


@pytest.mark.parametrize("first", ["write", "read"])
def test_already_open_writer_refuses_a_later_minimum_and_still_serves_rolled_back_reads(
    tmp_path: Path,
    first: str,
) -> None:
    path = tmp_path / "memory.sqlite"
    with Store.create(path) as store:
        record = store.put_memory(_memory("original"))
        rows = len(store.log_rows())
        with closing(sqlite3.connect(path, isolation_level=None)) as other:
            # A revision past this build's, as a later build's step leaves.
            other.execute(
                "UPDATE meta SET value = ? "
                "WHERE key IN ('schema_revision','min_writer_revision')",
                (str(SCHEMA_REVISION + 1),),
            )
        if first == "write":
            with pytest.raises(
                sqlite3.OperationalError, match="readonly.*now needs a writer"
            ):
                store.put_memory(record.model_copy(update={"body": "must not land"}))
        assert not store.conn.in_transaction
        assert store.get_memory(record.id).body == record.body
        assert len(store.log_rows()) == rows
        with writes_rolled_back(store) as rolled_back:
            assert rolled_back
            store.put_memory(record.model_copy(update={"body": "read simulation"}))
            assert store.get_memory(record.id).body == "read simulation"
            with writes_rolled_back(store) as nested:
                assert nested
                assert not store.conn.execute("PRAGMA query_only").fetchone()[0]
                store.put_memory(
                    record.model_copy(update={"body": "nested read simulation"})
                )
        assert store.get_memory(record.id).body == record.body
        assert len(store.log_rows()) == rows
        assert store.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert not store._version_readback


def test_upgrade_borrows_the_restores_barrier_without_clearing_its_record(
    tmp_path: Path,
) -> None:
    path, keys = tmp_path / "memory.sqlite", tmp_path / "keys"
    _legacy(path, keys)
    held = restore_lock.acquire(path)
    assert held is not None
    try:
        held.mark(stage="checking the restored store")
        before = restore_lock.read_marker(path)
        with Store.open(path, keys_dir=keys, allow_rekey=True) as store:
            assert store.meta()["schema_revision"] == str(SCHEMA_REVISION)
            assert store.log_verify()["status"] == "ok"
        assert restore_lock.read_marker(path) == before
        assert restore_lock.held_here(path)
    finally:
        held.release(clear=True)
