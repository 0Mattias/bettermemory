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
import sqlite3
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


def _meta_store_id(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        (value,) = conn.execute(
            "SELECT value FROM meta WHERE key = 'store_id'"
        ).fetchone()
    finally:
        conn.close()
    return str(value)


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
        assert orphan.name in shown.stdout
        assert str(store.path) in shown.stdout
        assert "bettermemory down" in shown.stdout
        assert shown.chars == len(shown.stdout)
        assert shown.ids == ()
        # Short: the paths aside, two sentences.
        assert len(shown.stdout) - len(orphan.name) - len(str(store.path)) < 300
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
    """Oldest to newest is the stamp, then the counter a second orphan
    from the same second carries; a plain sort of the names puts
    ``-2.sqlite`` before ``.sqlite``."""
    keys = tmp_path / "keys"
    oldest = _orphan(store_dir, "20261011T010000Z", keys)
    first = _orphan(store_dir, "20261012T053900Z", keys)
    tenth = _orphan(store_dir, "20261012T053900Z-10", keys)
    second = _orphan(store_dir, "20261012T053900Z-2", keys)
    assert orphans_beside(store_dir / "memory.sqlite") == [oldest, first, second, tenth]
    with Store(store_dir, keys_dir=keys) as store:
        assert store.store_orphans() == [oldest, first, second, tenth]
        assert tenth.name in orphan_notice(store)
        tenth.unlink()
        assert second.name in orphan_notice(store)


def test_the_notice_shortens_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home directory shows as ``~``, and a path too long for one
    line keeps its two ends."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    keys = tmp_path / "keys"
    near = home / ".bettermemory"
    near.mkdir(parents=True)
    _orphan(near, "20261012T053900Z", keys)
    with Store(near, keys_dir=keys) as store:
        notice = orphan_notice(store)
    assert "bettermemory: ~/.bettermemory/memory.sqlite was created" in notice
    assert str(home) not in notice
    deep = tmp_path / "/".join(["a-directory-name-of-some-length"] * 7)
    deep.mkdir(parents=True)
    _orphan(deep, "20261012T053900Z", keys)
    with Store(deep, keys_dir=keys) as store:
        notice = orphan_notice(store)
    assert len(str(deep / "memory.sqlite")) > 200
    assert "..." in notice
    assert str(deep / "memory.sqlite")[:90] in notice
    assert notice.split(" was created")[0].endswith("-of-some-length/memory.sqlite")
    assert len(notice) < 540


@pytest.mark.parametrize("follows_the_steps", [True, False])
def test_the_notice_steps_restore_the_saved_store(
    store_dir: Path, tmp_path: Path, follows_the_steps: bool
) -> None:
    """The restore the notice gives, step by step, over a WAL the fresh
    store left behind: the store reads back as the saved one. Skipping
    the deletion is the control: the copied file then reads through the
    stale WAL as the fresh store."""
    keys = tmp_path / "keys"
    orphan = _orphan(store_dir, "20261012T053900Z", keys)
    saved_id = _meta_store_id(orphan)
    fresh = Store(store_dir, keys_dir=keys)
    fresh.write(content="a memory in the fresh store", scopes=["global"])
    fresh.write(content="another memory in the fresh store", scopes=["global"])
    fresh_id = fresh.store_id
    notice = orphan_notice(fresh)
    path = fresh.path
    wal, shm = Path(f"{path}-wal"), Path(f"{path}-shm")
    # A session that did not close cleanly leaves the WAL in place.
    kept = {sibling: sibling.read_bytes() for sibling in (wal, shm)}
    # 1. Close the sessions and 2. `bettermemory down`: nothing has the
    # store open any more (no daemon runs here).
    fresh.close()
    for sibling, data in kept.items():
        sibling.write_bytes(data)
    assert wal.stat().st_size > 0
    # 3. Delete the WAL and shared-memory files beside the store.
    assert f"delete {wal.name} and {shm.name}" in notice
    if follows_the_steps:
        wal.unlink()
        shm.unlink()
    # 4. Copy the saved file over the store.
    assert f"saved beside it as {orphan.name}" in notice
    assert notice.index("bettermemory down") < notice.index(wal.name)
    shutil.copyfile(orphan, path)
    store_id = _meta_store_id(path)
    if not follows_the_steps:
        assert store_id == fresh_id
        return
    assert store_id == saved_id
    with Store(store_dir, keys_dir=keys) as restored:
        assert restored.store_id == saved_id
        assert restored.count_memories() == 1
        assert restored.store_orphans() == []


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
    assert orphan.name in first
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
