"""A restore's plan counts the rows a crash left only in the store's WAL
(ENG-103).

`backup._log_end` and `backup._current_store_id` read the store at the
restore's path with ``immutable=1``, which reads the main file alone: rows
written since the last checkpoint, still only in the ``-wal`` a crashed
process left beside it, were not seen. The plan's `log_end_before` then
stopped at the last checkpoint, and a store whose first rows were only in
its ``-wal`` had no id, so the restore took it for another store's and
recorded no end at all. Both now read through the read-only commands'
open (`store_schema.connect_read_only`), which reads a ``-wal`` and
creates nothing beside the store.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory.backup import create_backup, restore_bundle
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store


def _memory(body: str) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )


def _crash_copy(store_file: Path, into: Path, *, with_shm: bool) -> Path:
    """The store's files as a process killed with it open leaves them:
    copied while the store is open, so the rows since the last checkpoint
    are only in the ``-wal``."""
    into.mkdir()
    for suffix in ("", "-wal", "-shm") if with_shm else ("", "-wal"):
        shutil.copyfile(
            store_file.with_name(store_file.name + suffix),
            into / (STORE_FILENAME + suffix),
        )
    return into / STORE_FILENAME


def _main_file_only(store_file: Path, sql: str) -> object:
    """What ``immutable=1`` reads: the main file, no ``-wal``."""
    conn = sqlite3.connect(
        f"{store_file.resolve().as_uri()}?mode=ro&immutable=1", uri=True
    )
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None if row is None else row[0]


@pytest.mark.parametrize("with_shm", [True, False], ids=["wal-and-shm", "wal-only"])
def test_the_plans_log_end_counts_the_rows_only_in_a_crashs_wal(
    tmp_path: Path, keys_dir: Path, with_shm: bool
) -> None:
    old = tmp_path / "old" / STORE_FILENAME
    with Store.create(old) as created:
        created.put_memory(_memory("before the bundle"))
        store_id = created.store_id
    bundle = Path(create_backup(old)["bundle"])
    with Store.open(old) as opened:
        for n in range(3):
            opened.put_memory(_memory(f"only in the wal {n}"))
        end = opened.log_rows()[-1].seq
        crashed = _crash_copy(old, tmp_path / "crashed", with_shm=with_shm)
    checkpointed = _main_file_only(crashed, "SELECT MAX(seq) FROM log")
    assert type(checkpointed) is int and checkpointed < end
    beside = sorted(p.name for p in crashed.parent.iterdir())

    plan = restore_bundle(bundle, crashed, apply=False)["plan"]
    assert plan["current_store_id"] == store_id
    assert plan["log_end_before"] is not None
    assert plan["log_end_before"]["seq"] == end
    # Read without a -wal or -shm made beside it.
    assert sorted(p.name for p in crashed.parent.iterdir()) == beside


def test_a_store_whose_first_rows_are_only_in_its_wal_is_the_bundles_store(
    tmp_path: Path, keys_dir: Path
) -> None:
    """Killed before its first checkpoint: the main file holds no meta row
    yet, and the plan still names the store and its log's end."""
    old = tmp_path / "old" / STORE_FILENAME
    created = Store.create(old)
    try:
        created.put_memory(_memory("never checkpointed"))
        store_id = created.store_id
        bundle = Path(create_backup(old)["bundle"])
        end = created.log_rows()[-1].seq
        crashed = _crash_copy(old, tmp_path / "crashed", with_shm=False)
    finally:
        created.close()
    assert (
        _main_file_only(crashed, "SELECT value FROM meta WHERE key = 'store_id'")
        is None
    )

    plan = restore_bundle(bundle, crashed, apply=False)["plan"]
    assert plan["current_store_id"] == store_id
    assert plan["log_end_before"] is not None
    assert plan["log_end_before"]["seq"] == end
