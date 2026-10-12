"""A store created fresh beside a saved orphan says so until the orphan
is gone.

A daemon whose store path was removed or replaced saves the store it had
open as ``<store>.orphaned-<stamp>.sqlite`` beside the path
(`daemon.save_orphan`). The store created next at the path names those
files in its ``meta`` (`store.CREATED_AFTER_ORPHANS`), and while any of
them is still on disk ``/health`` lists them under ``store_orphans`` and
the session brief leads with one line on how to restore the old store.
A store that already existed, or one copied back from the orphan, carries
no such names, and nothing here deletes, restores or asks.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bettermemory.config import Config, StorageConfig, TelemetryConfig
from bettermemory.origin import Origin
from bettermemory.recall import RecallState, orphan_notice, session_brief
from bettermemory.store import CREATED_AFTER_ORPHANS, Store, orphans_beside

from .test_daemon import running_app


def _orphan(directory: Path, stamp: str, keys: Path) -> Path:
    """A real store saved under an orphan's name, as `save_orphan` leaves
    one."""
    source = Store(directory / "source" / "memory.sqlite", keys_dir=keys)
    source.write(content="the old store's one memory", scopes=["global"])
    source.close()
    orphan = directory / f"memory.sqlite.orphaned-{stamp}.sqlite"
    shutil.move(str(directory / "source" / "memory.sqlite"), orphan)
    shutil.rmtree(directory / "source")
    return orphan


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "store"
    directory.mkdir()
    return directory


def test_a_store_created_beside_orphans_names_them(
    store_dir: Path, tmp_path: Path
) -> None:
    older = _orphan(store_dir, "20261011T010000Z", tmp_path / "keys")
    newer = _orphan(store_dir, "20261012T053900Z", tmp_path / "keys")
    (store_dir / ".memory.sqlite.orphaned-x.tmp").write_bytes(b"partial")
    with Store(store_dir, keys_dir=tmp_path / "keys") as store:
        assert json.loads(store.meta()[CREATED_AFTER_ORPHANS]) == [
            older.name,
            newer.name,
        ]
        assert store.store_orphans() == [older, newer]
        older.unlink()
        assert store.store_orphans() == [newer]
        newer.unlink()
        assert store.store_orphans() == []


def test_a_store_created_with_no_orphan_beside_it_names_none(
    store_dir: Path, tmp_path: Path
) -> None:
    with Store(store_dir, keys_dir=tmp_path / "keys") as store:
        assert CREATED_AFTER_ORPHANS not in store.meta()
        assert store.store_orphans() == []
        assert orphan_notice(store) == ""


def test_a_store_that_already_existed_is_not_reported(
    store_dir: Path, tmp_path: Path
) -> None:
    """An orphan saved beside a store that is then opened, not created
    (the path was replaced by another store), changes nothing."""
    Store(store_dir, keys_dir=tmp_path / "keys").close()
    _orphan(store_dir, "20261012T053900Z", tmp_path / "keys")
    with Store(store_dir, keys_dir=tmp_path / "keys") as store:
        assert CREATED_AFTER_ORPHANS not in store.meta()
        assert store.store_orphans() == []


def test_a_restored_store_is_not_reported(store_dir: Path, tmp_path: Path) -> None:
    """Copying the orphan back over the store, the restore the notice
    names, clears it: the old store carries no names."""
    orphan = _orphan(store_dir, "20261012T053900Z", tmp_path / "keys")
    fresh = Store(store_dir, keys_dir=tmp_path / "keys")
    assert fresh.store_orphans() == [orphan]
    path = fresh.path
    fresh.close()
    for sibling in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        sibling.unlink(missing_ok=True)
    shutil.copyfile(orphan, path)
    with Store(store_dir, keys_dir=tmp_path / "keys") as restored:
        assert restored.count_memories() == 1
        assert restored.store_orphans() == []
    assert orphan.is_file()


def test_a_malformed_marker_reports_nothing(store_dir: Path, tmp_path: Path) -> None:
    orphan = _orphan(store_dir, "20261012T053900Z", tmp_path / "keys")
    with Store(store_dir, keys_dir=tmp_path / "keys") as store:
        for value in ("not json", '{"a": 1}', json.dumps(["../" + orphan.name])):
            store.conn.execute(
                "UPDATE meta SET value = ? WHERE key = ?",
                (value, CREATED_AFTER_ORPHANS),
            )
            assert store.store_orphans() == []


def test_orphans_beside_lists_only_saved_stores(store_dir: Path) -> None:
    (store_dir / "memory.sqlite.orphaned-20261012T053900Z.sqlite").write_bytes(b"")
    (store_dir / "memory.sqlite.orphaned-20261012T053900Z.sqlite-wal").write_bytes(b"")
    (store_dir / ".memory.sqlite.orphaned-abc.tmp").write_bytes(b"")
    (store_dir / "other.sqlite.orphaned-20261012T053900Z.sqlite").write_bytes(b"")
    assert [p.name for p in orphans_beside(store_dir / "memory.sqlite")] == [
        "memory.sqlite.orphaned-20261012T053900Z.sqlite"
    ]
    assert orphans_beside(store_dir / "gone" / "memory.sqlite") == []


# ---------------------------------------------------------------------------
# The session brief
# ---------------------------------------------------------------------------


def _config(store_dir: Path) -> Config:
    return Config(
        storage=StorageConfig(directory=str(store_dir)),
        telemetry=TelemetryConfig(enabled=True),
    )


def _outside_a_repository(tmp_path: Path) -> Origin:
    return Origin(cwd=str(tmp_path), repo=None, branch=None, worktree_root=None)


def test_the_brief_of_a_fresh_store_leads_with_the_notice(
    store_dir: Path, tmp_path: Path
) -> None:
    """The fresh store is empty and the caller may stand outside any
    repository, where the brief shows nothing else; the line still
    shows, on one line, naming the orphan and the store."""
    orphan = _orphan(store_dir, "20261012T053900Z", tmp_path / "keys")
    with Store(store_dir, keys_dir=tmp_path / "keys") as store:
        shown = session_brief(
            session_id="sess-1",
            origin=_outside_a_repository(tmp_path),
            config=_config(store_dir),
            store=store,
            state=RecallState(background=False),
        )
        assert shown.stdout == orphan_notice(store)
        assert "\n" not in shown.stdout
        assert "created fresh" in shown.stdout
        assert str(orphan) in shown.stdout
        assert str(store.path) in shown.stdout
        assert "bettermemory down" in shown.stdout
        assert shown.chars == len(shown.stdout)
        assert shown.ids == ()
        # Short: the paths aside, one sentence and a half.
        assert len(shown.stdout) - len(str(orphan)) - len(str(store.path)) < 200
        orphan.unlink()
        again = session_brief(
            session_id="sess-2",
            origin=_outside_a_repository(tmp_path),
            config=_config(store_dir),
            store=store,
            state=RecallState(background=False),
        )
        assert again.stdout == ""


def test_the_notice_names_the_newest_orphan(store_dir: Path, tmp_path: Path) -> None:
    _orphan(store_dir, "20261011T010000Z", tmp_path / "keys")
    newer = _orphan(store_dir, "20261012T053900Z", tmp_path / "keys")
    with Store(store_dir, keys_dir=tmp_path / "keys") as store:
        assert str(newer) in orphan_notice(store)


def test_the_notice_leads_a_brief_that_shows_memories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_recall_brief import _GIT_AVAILABLE, _World

    if not _GIT_AVAILABLE:
        pytest.skip("git not on PATH")
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    orphan = _orphan(store_dir, "20261012T053900Z", tmp_path / "keys")
    monkeypatch.setenv("BETTERMEMORY_KEYS_DIR", str(tmp_path / "keys"))
    world = _World(tmp_path)
    world.fresh("the deploy runs from the release branch")
    shown = world.brief()
    first, rest = shown.stdout.split("\n", 1)
    assert first == orphan_notice(world.store)
    assert str(orphan) in first
    assert len(shown.ids) == 1
    assert shown.ids[0] in rest
    world.store.close()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


async def test_health_lists_the_orphans_while_they_exist(
    store_dir: Path, tmp_path: Path
) -> None:
    orphan = _orphan(store_dir, "20261012T053900Z", tmp_path / "keys")
    store = Store(store_dir, keys_dir=tmp_path / "keys")
    try:
        async with running_app(_config(store_dir), store) as client:
            body = (await client.get("/health")).json()
            assert body["store_orphans"] == [str(orphan)]
            orphan.unlink()
            body = (await client.get("/health")).json()
            assert body["store_orphans"] == []
    finally:
        store.close()


async def test_health_lists_no_orphans_for_an_ordinary_store(
    store_dir: Path, tmp_path: Path
) -> None:
    store = Store(store_dir, keys_dir=tmp_path / "keys")
    try:
        async with running_app(_config(store_dir), store) as client:
            body = (await client.get("/health")).json()
        assert body["store_orphans"] == []
    finally:
        store.close()
