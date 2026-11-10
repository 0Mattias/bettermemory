"""Claim 28: /health must answer ok over a store whose
created_after_orphans meta is malformed (deeply nested JSON)."""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from pathlib import Path

import httpx2
import pytest

from bettermemory.config import BehaviorConfig, Config, ScopesConfig, StorageConfig
from bettermemory.daemon import (
    _migrate_at_start,
    build_app,
    save_orphan,
    _tell_successor,
)
from bettermemory.store import Store
from bettermemory.store_schema import orphan_names

from .test_daemon_store_watch import _integrity_and_counts

TOKEN = "test-token-0123456789abcdef"
BASE = "http://127.0.0.1:7397"
HOSTILE = "[" * 2000


@pytest.mark.parametrize(
    "value", [HOSTILE, "[" * 2000 + "]" * 2000, '{"a":' * 2000 + "0" + "}" * 2000]
)
def test_orphan_names_is_empty_for_malformed_value(value: str) -> None:
    assert orphan_names(value) == []


async def test_health_answers_ok_over_a_malformed_orphans_meta(tmp_path: Path) -> None:
    first = Store(tmp_path / "store")
    path = first.path
    first.close()
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('created_after_orphans', ?)",
        (HOSTILE,),
    )
    raw.commit()
    raw.close()

    store = Store(tmp_path / "store")  # opens (is admitted) normally
    config = Config(
        storage=StorageConfig(directory=str(tmp_path / "store")),
        behavior=BehaviorConfig(),
        scopes=ScopesConfig(),
    )
    _migrate_at_start(store, config)
    app = build_app(config=config, store=store, token=TOKEN, shutdown=lambda: None)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as client:
            response = await client.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["store_orphans"] == []


def _malformed_successor(path: Path, keys: Path) -> str:
    with Store.create(path, keys_dir=keys) as successor:
        successor_id = successor.store_id
        successor.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('created_after_orphans', ?)",
            (HOSTILE,),
        )
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()
    return successor_id


def _names(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'created_after_orphans'"
        ).fetchone()
    finally:
        connection.close()
    return json.loads(row[0])


def test_save_orphan_records_its_one_backup_despite_deep_successor_metadata(
    tmp_path: Path,
) -> None:
    keys = tmp_path / "keys"
    with Store.create(tmp_path / "old.sqlite", keys_dir=keys) as original:
        original.write(
            content="the old connection still holds this fact", scopes=["tools"]
        )
        successor_path = tmp_path / "memory.sqlite"
        assert _malformed_successor(successor_path, keys) != original.store_id
        # Keep a real old connection under its replacement path, without
        # unlinking an open SQLite file on Windows. Backup and successor
        # admission both use real stores; only this path facade is synthetic.
        held = cast(
            Store,
            SimpleNamespace(
                path=successor_path, conn=original.conn, store_id=original.store_id
            ),
        )
        since = datetime.now(timezone.utc) - timedelta(days=1)
        expected = _integrity_and_counts(original.path)
        orphan = save_orphan(held, successor_after=since)
        assert orphan is not None and orphan.is_file()
        assert _names(successor_path) == [orphan.name]
        assert not Path(str(successor_path) + "-wal").exists()
        assert not Path(str(successor_path) + "-shm").exists()
        assert save_orphan(held, into=orphan, successor_after=since) == orphan
        assert _names(successor_path) == [orphan.name]
        assert list(tmp_path.glob("memory.sqlite.orphaned-*.sqlite")) == [orphan]
        assert _integrity_and_counts(orphan) == expected
        assert expected[0] == "ok" and expected[1]["memories"] == 1


def test_tell_successor_replaces_malformed_metadata_with_only_the_new_name(
    tmp_path: Path,
) -> None:
    keys = tmp_path / "keys"
    path = tmp_path / "memory.sqlite"
    successor_id = _malformed_successor(path, keys)
    orphan = tmp_path / "memory.sqlite.orphaned-20261110T010000Z.sqlite"
    _tell_successor(
        path,
        orphan,
        store_id=successor_id + "-older",
        created_after=datetime.now(timezone.utc) - timedelta(days=1),
        own_files={},
    )
    assert _names(path) == [orphan.name]
