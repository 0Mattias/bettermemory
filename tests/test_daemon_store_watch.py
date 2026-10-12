"""The daemon stops once its store path no longer names the file it
opened, and saves the store it had open beside the path first.

A daemon whose store was removed, or replaced by a rename over its path,
serves an inode nobody can open again, and before the watch it did so
forever (the toolcost bench removed its sandbox under one per run). The
watch records the device and inode the path names and stats the path on
every tick: the path gone or naming another inode stops the daemon
through the shutdown path, and a copy over the path, which keeps the
inode, does not. It holds no descriptor on the store: closing one would
drop the daemon connection's POSIX locks on the file
(`test_the_watch_leaves_a_connections_locks_alone`).

Before it stops, the daemon backs the store it still holds up through
its own connection to ``<store>.orphaned-<stamp>.sqlite`` beside the path
(`daemon.save_orphan`). As the app shuts down it saves it again over the
same file, keeping writes still in flight, and saves a removal no tick
saw before a ``down`` or a signal. A store another process created at
the path inside the window is given the orphan's name; an older store
moved back over the path is not.

The watch itself is tested in-process on a plain file; the daemon is
tested as a process, started by ``up`` under the test's own state
directory, store and keys, on an ephemeral port, with the check
interval lowered in the child by a ``sitecustomize``.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bettermemory import daemon as _daemon
from bettermemory._daemon_client import (
    _stopped,
    health,
    pid_alive,
    post,
    read_state,
)
from bettermemory.store import CREATED_AFTER_ORPHANS, Store

from .test_daemon_lifecycle import (
    _cli,
    _env,
    _state_path,
    _store_path,
    _wait_running,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="an open store cannot be unlinked or renamed over on Windows; "
    "the daemon does not watch there",
)

#: The check interval the daemon children run at.
INTERVAL = 1.0
#: How long a daemon may take to exit once its store is unlinked: two
#: check intervals, the bound the watch promises. A shared CI runner
#: gets room for a slow shutdown; the bound there only catches a hang.
EXIT_BOUND = 15.0 if os.environ.get("CI") else 2 * INTERVAL


# ---------------------------------------------------------------------------
# The watch, in-process
# ---------------------------------------------------------------------------


def _watch(path: Path, interval: float) -> tuple[_daemon._StoreWatch, threading.Event]:
    fired = threading.Event()
    watch = _daemon._StoreWatch(path, fired.set, interval=interval)
    assert watch.start()
    return watch, fired


def test_the_watch_fires_when_the_file_is_unlinked(tmp_path: Path) -> None:
    store = tmp_path / "memory.sqlite"
    store.write_bytes(b"store")
    watch, fired = _watch(store, 0.05)
    try:
        store.unlink()
        assert fired.wait(2 * 0.05 + 1.0)
    finally:
        watch.stop()


def test_the_watch_fires_when_the_file_is_renamed_over(tmp_path: Path) -> None:
    store = tmp_path / "memory.sqlite"
    store.write_bytes(b"store")
    watch, fired = _watch(store, 0.05)
    try:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"another store")
        os.replace(replacement, store)
        assert fired.wait(2 * 0.05 + 1.0)
    finally:
        watch.stop()


def test_the_watch_ignores_a_copy_over_the_file(tmp_path: Path) -> None:
    store = tmp_path / "memory.sqlite"
    store.write_bytes(b"store")
    source = tmp_path / "source"
    source.write_bytes(b"copied over")
    watch, fired = _watch(store, 0.05)
    try:
        shutil.copyfile(source, store)
        assert not fired.wait(5 * 0.05)
    finally:
        watch.stop()


def test_a_failing_stat_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the path gone or naming another file stops the daemon: a stat
    that fails any other way is logged and checked again next tick."""
    store = tmp_path / "memory.sqlite"
    store.write_bytes(b"store")
    calls = {"n": 0}
    real_stat = os.stat

    def failing_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fspath(path) != str(store):
            return real_stat(path, *args, **kwargs)
        calls["n"] += 1
        raise PermissionError("transient")

    watch, fired = _watch(store, 0.05)
    monkeypatch.setattr(os, "stat", failing_stat)
    try:
        deadline = time.monotonic() + 2.0
        while calls["n"] < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls["n"] >= 3
        assert not fired.is_set()
    finally:
        monkeypatch.setattr(os, "stat", real_stat)
        watch.stop()


def test_the_watch_fires_when_a_symlinked_store_is_repointed(tmp_path: Path) -> None:
    """The path is followed as SQLite follows it: a symlink pointed at
    another file names another store."""
    first = tmp_path / "first.sqlite"
    first.write_bytes(b"store")
    second = tmp_path / "second.sqlite"
    second.write_bytes(b"another store")
    link = tmp_path / "memory.sqlite"
    link.symlink_to(first)
    watch, fired = _watch(link, 0.05)
    try:
        assert not fired.wait(3 * 0.05)
        link.unlink()
        link.symlink_to(second)
        assert fired.wait(2 * 0.05 + 1.0)
    finally:
        watch.stop()


def test_sqlite_maintenance_does_not_fire_the_watch(tmp_path: Path) -> None:
    """VACUUM, a truncating checkpoint and an online backup into the store
    all write through the inode the path names."""
    store = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(store, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    source = sqlite3.connect(tmp_path / "source.sqlite")
    source.execute("CREATE TABLE t(x)")
    source.execute("INSERT INTO t VALUES (1)")
    source.commit()
    watch, fired = _watch(store, 0.05)
    try:
        conn.execute("DELETE FROM t WHERE x % 2 = 0")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source.backup(conn)
        assert not fired.wait(5 * 0.05)
    finally:
        watch.stop()
        source.close()
        conn.close()


def test_stopping_the_watch_does_not_wait_out_its_interval(tmp_path: Path) -> None:
    store = tmp_path / "memory.sqlite"
    store.write_bytes(b"store")
    watch, fired = _watch(store, 30.0)
    started = time.monotonic()
    watch.stop()
    assert time.monotonic() - started < 2.0
    assert not fired.is_set()


# ---------------------------------------------------------------------------
# The watch and the daemon connection's locks
# ---------------------------------------------------------------------------

#: Run in a second process: whether it can take an exclusive lock on the
#: database at argv[1] without waiting.
_TAKE_EXCLUSIVE = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
try:
    conn.execute("BEGIN EXCLUSIVE")
except sqlite3.OperationalError:
    print("locked")
else:
    conn.execute("ROLLBACK")
    print("taken")
"""

#: Run in a second process: a write to the WAL database at argv[1], then a
#: close, which checkpoints and deletes the WAL when no other connection
#: holds its shared lock on the file.
_WRITE_AND_CLOSE = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("INSERT INTO t VALUES (2)")
conn.close()
"""


def _second_process(script: str, path: Path) -> str:
    done = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return done.stdout.strip()


def _close_a_descriptor(path: Path) -> None:
    """What the watch did before: open the store and close it again."""
    os.close(os.open(path, os.O_RDONLY))


def _cycle_the_watch(path: Path) -> None:
    watch, fired = _watch(path, 0.02)
    time.sleep(0.1)
    watch.stop()
    assert not fired.is_set()


_BETWEEN = pytest.mark.parametrize(
    ("between", "keeps_locks"),
    [(_cycle_the_watch, True), (_close_a_descriptor, False)],
    ids=["the-watch", "a-closed-descriptor"],
)


@_BETWEEN
def test_the_watch_leaves_a_connections_locks_alone(
    tmp_path: Path, between: Callable[[Path], None], keeps_locks: bool
) -> None:
    """Closing any descriptor on a file drops every POSIX advisory lock
    the process holds on it. With a read transaction open, the daemon's
    connection holds a shared lock another process's exclusive lock must
    wait for; a watch started and stopped beside it must leave that so.
    The closed descriptor is the control: it shows the check sees a lost
    lock."""
    store = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(store, isolation_level=None)
    try:
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.execute("BEGIN")
        conn.execute("SELECT * FROM t").fetchall()
        assert _second_process(_TAKE_EXCLUSIVE, store) == "locked"
        between(store)
        expected = "locked" if keeps_locks else "taken"
        assert _second_process(_TAKE_EXCLUSIVE, store) == expected
        conn.execute("COMMIT")
    finally:
        conn.close()


@_BETWEEN
def test_the_watch_leaves_the_wal_to_the_open_connection(
    tmp_path: Path, between: Callable[[Path], None], keeps_locks: bool
) -> None:
    """The daemon's store is in WAL mode: an open connection's shared lock
    on the file is what stops another process's close from checkpointing
    and deleting the WAL under it. After the watch it still does."""
    store = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(store, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.execute("SELECT * FROM t").fetchall()
        between(store)
        _second_process(_WRITE_AND_CLOSE, store)
        assert Path(f"{store}-wal").exists() is keeps_locks
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The orphan, in-process
# ---------------------------------------------------------------------------

_ORPHAN_NAME = re.compile(r"memory\.sqlite\.orphaned-\d{8}T\d{6}Z(-\d+)?\.sqlite")


def _store_with_one_memory(tmp_path: Path) -> Store:
    store = Store(tmp_path / "store", keys_dir=tmp_path / "keys")
    store.write(content="the release runs from the tag", scopes=["global"])
    return store


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def _orphans(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if _ORPHAN_NAME.fullmatch(p.name))


def test_save_orphan_copies_the_unlinked_store_beside_its_path(
    tmp_path: Path,
) -> None:
    store = _store_with_one_memory(tmp_path)
    try:
        path = store.path
        path.unlink()
        orphan = _daemon.save_orphan(
            store, now=datetime(2026, 10, 12, 5, 39, tzinfo=timezone.utc)
        )
    finally:
        store.close()
    assert orphan is not None
    assert orphan == path.with_name("memory.sqlite.orphaned-20261012T053900Z.sqlite")
    assert orphan.is_file()
    assert _leftovers(path.parent) == []
    copy = sqlite3.connect(orphan)
    try:
        (count,) = copy.execute("SELECT COUNT(*) FROM memories").fetchone()
        (store_id,) = copy.execute(
            "SELECT value FROM meta WHERE key = 'store_id'"
        ).fetchone()
    finally:
        copy.close()
    assert count == 1
    assert store_id == store.store_id


def test_save_orphan_never_overwrites_an_earlier_orphan(tmp_path: Path) -> None:
    store = _store_with_one_memory(tmp_path)
    when = datetime(2026, 10, 12, 5, 39, tzinfo=timezone.utc)
    try:
        first = _daemon.save_orphan(store, now=when)
        second = _daemon.save_orphan(store, now=when)
    finally:
        store.close()
    assert first is not None and second is not None
    assert first != second
    assert _orphans(store.path.parent) == sorted([first, second])


def _integrity_and_counts(path: Path) -> tuple[str, dict[str, int]]:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        (integrity,) = conn.execute("PRAGMA integrity_check").fetchone()
        tables = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {
            name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in tables
        }
    finally:
        conn.close()
    return str(integrity), counts


def test_save_orphan_makes_the_directory_again_when_it_is_gone(
    tmp_path: Path,
) -> None:
    """``rm -rf`` of the store's directory: the orphan is saved in the
    directory made again, owner-only."""
    store = _store_with_one_memory(tmp_path)
    try:
        directory = store.path.parent
        expected = _integrity_and_counts(store.path)
        shutil.rmtree(directory)
        orphan = _daemon.save_orphan(store)
    finally:
        store.close()
    assert orphan is not None and orphan.parent == directory
    assert _orphans(directory) == [orphan]
    assert _leftovers(directory) == []
    assert _integrity_and_counts(orphan) == expected
    assert expected[0] == "ok"
    assert (directory.stat().st_mode & 0o777) == 0o700


def test_save_orphan_logs_and_returns_none_when_the_directory_cannot_be_made(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A regular file where the directory was: nothing to save into."""
    store = _store_with_one_memory(tmp_path)
    try:
        shutil.rmtree(store.path.parent)
        store.path.parent.write_bytes(b"not a directory")
        with caplog.at_level(logging.ERROR, logger="bettermemory.daemon"):
            assert _daemon.save_orphan(store) is None
    finally:
        store.close()
    assert store.path.parent.read_bytes() == b"not a directory"
    assert any(
        r.levelno == logging.ERROR and str(store.path) in r.getMessage()
        for r in caplog.records
    )


def test_save_orphan_saves_again_over_the_same_file(tmp_path: Path) -> None:
    """`into` replaces an orphan saved earlier, with what was written to
    the store since, and logs nothing new."""
    store = _store_with_one_memory(tmp_path)
    try:
        store.path.unlink()
        first = _daemon.save_orphan(store)
        assert first is not None
        store.write(content="written after the first save", scopes=["global"])
        again = _daemon.save_orphan(store, into=first)
    finally:
        store.close()
    assert again == first
    assert _orphans(store.path.parent) == [first]
    assert _leftovers(store.path.parent) == []
    assert _integrity_and_counts(first)[1]["memories"] == 2


def _unlink_store(path: Path) -> None:
    for sibling in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        sibling.unlink(missing_ok=True)


def _marker(path: Path) -> list[str] | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (CREATED_AFTER_ORPHANS,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else json.loads(row[0])


def test_save_orphan_tells_a_store_created_at_the_path_since(tmp_path: Path) -> None:
    """A store another process created at the path after the watch
    started, before the orphan existed to be seen beside it, is given its
    name, once, beside any it already had."""
    store = _store_with_one_memory(tmp_path)
    try:
        watched_since = datetime.now(timezone.utc)
        _unlink_store(store.path)
        time.sleep(0.01)
        Store.create(store.path, keys_dir=tmp_path / "keys").close()
        assert _marker(store.path) is None
        orphan = _daemon.save_orphan(store, successor_after=watched_since)
        assert orphan is not None
        assert _marker(store.path) == [orphan.name]
        later = _daemon.save_orphan(store, successor_after=watched_since)
        assert later is not None
        _daemon.save_orphan(store, into=orphan, successor_after=watched_since)
    finally:
        store.close()
    assert _marker(store.path) == [orphan.name, later.name]
    with Store(store.path, keys_dir=tmp_path / "keys") as successor:
        assert successor.store_orphans() == [orphan, later]


def test_save_orphan_leaves_an_older_store_moved_over_the_path_alone(
    tmp_path: Path,
) -> None:
    """Restoring an older store by a rename over the path is not a store
    created after the loss: no notice."""
    older = Store(tmp_path / "older", keys_dir=tmp_path / "keys")
    older_id = older.store_id
    older.close()
    store = _store_with_one_memory(tmp_path)
    try:
        time.sleep(0.01)
        watched_since = datetime.now(timezone.utc)
        _unlink_store(store.path)
        os.replace(older.path, store.path)
        assert _daemon.save_orphan(store, successor_after=watched_since) is not None
    finally:
        store.close()
    assert _store_id(store.path) == older_id
    assert _marker(store.path) is None


def test_save_orphan_never_opens_the_wal_the_daemon_still_writes(
    tmp_path: Path,
) -> None:
    """A newer store renamed over the path with the daemon's WAL and
    shared memory still beside it: opening that store would open them
    too, and closing them would drop the daemon's locks on its own WAL.
    It is not told, and the daemon's WAL is left where it was."""
    store = _store_with_one_memory(tmp_path)
    newer = tmp_path / "newer"
    try:
        watch = _daemon._StoreWatch(store.path, lambda: None, interval=30.0)
        assert watch.start()
        watch.stop()
        assert set(watch.files) == {"", "-wal", "-shm"}
        wal = Path(f"{store.path}-wal")
        wal_identity = (wal.stat().st_dev, wal.stat().st_ino)
        assert watch.started_at is not None
        time.sleep(0.01)
        Store.create(newer, keys_dir=tmp_path / "keys").close()
        os.replace(newer / "memory.sqlite", store.path)
        orphan = _daemon.save_orphan(
            store, successor_after=watch.started_at, own_files=watch.files
        )
        assert orphan is not None
        assert (wal.stat().st_dev, wal.stat().st_ino) == wal_identity
        store.write(content="written after the save", scopes=["global"])
    finally:
        store.close()
    for sibling in (wal, Path(f"{store.path}-shm")):
        sibling.unlink(missing_ok=True)
    assert _marker(store.path) is None


def test_save_orphan_never_marks_a_copy_of_the_store_it_saved(tmp_path: Path) -> None:
    """A file at the path with the orphan's store id is the orphan's own
    store, whenever it was made: never written to."""
    store = _store_with_one_memory(tmp_path)
    try:
        before = datetime(2000, 1, 1, tzinfo=timezone.utc)
        _unlink_store(store.path)
        orphan = _daemon.save_orphan(store)
        assert orphan is not None
        shutil.copyfile(orphan, store.path)
        _daemon.save_orphan(store, successor_after=before)
    finally:
        store.close()
    assert _store_id(store.path) == store.store_id
    assert _marker(store.path) is None


def test_save_orphan_leaves_no_partial_file_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A full disk at the rename: no orphan, no temporary file, one error."""
    store = _store_with_one_memory(tmp_path)

    def full(src: Any, dst: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", full)
    try:
        with caplog.at_level(logging.ERROR, logger="bettermemory.daemon"):
            assert _daemon.save_orphan(store) is None
    finally:
        store.close()
    directory = store.path.parent
    assert _leftovers(directory) == []
    assert _orphans(directory) == []
    assert [r for r in caplog.records if r.levelno == logging.ERROR]


def test_save_orphan_from_another_thread_fails_without_raising(
    tmp_path: Path,
) -> None:
    """The store's connection serves only the thread that opened it, which
    is why the daemon hands the backup to its event loop: called from the
    watch thread it fails, logged, with nothing written."""
    store = _store_with_one_memory(tmp_path)
    got: list[Path | None] = []
    try:
        thread = threading.Thread(target=lambda: got.append(_daemon.save_orphan(store)))
        thread.start()
        thread.join(10)
    finally:
        store.close()
    assert got == [None]
    assert _leftovers(store.path.parent) == []
    assert _orphans(store.path.parent) == []


_HOLD_THE_WRITE_LOCK = """
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE meta SET value = value WHERE key = 'created'")
print("locked", flush=True)
time.sleep(1.0)
conn.execute("COMMIT")
conn.close()
"""


def test_save_orphan_waits_out_a_writer_on_the_new_store(tmp_path: Path) -> None:
    """Another process holding the new store's write lock for a second
    holds the notice back for that second, not off: the write lock is
    taken up front, where the busy timeout applies. A read upgraded to a
    write failed at once."""
    store = _store_with_one_memory(tmp_path)
    try:
        watch = _daemon._StoreWatch(store.path, lambda: None, interval=30.0)
        assert watch.start()
        watch.stop()
        assert watch.started_at is not None
        _unlink_store(store.path)
        time.sleep(0.01)
        Store.create(store.path, keys_dir=tmp_path / "keys").close()
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLD_THE_WRITE_LOCK, str(store.path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "locked"
            started = time.monotonic()
            orphan = _daemon.save_orphan(
                store, successor_after=watch.started_at, own_files=watch.files
            )
            waited = time.monotonic() - started
        finally:
            assert holder.wait(30) == 0
    finally:
        store.close()
    assert orphan is not None
    assert waited < 5.0
    assert _marker(store.path) == [orphan.name]


@pytest.mark.parametrize("data", [b"", b"no database at all, " * 64])
def test_save_orphan_never_writes_to_a_file_that_is_no_store(
    tmp_path: Path, data: bytes
) -> None:
    """An empty file or one that is no SQLite database at the path is
    left byte for byte as it was, with nothing made beside it."""
    store = _store_with_one_memory(tmp_path)
    try:
        _unlink_store(store.path)
        store.path.write_bytes(data)
        before = datetime(2000, 1, 1, tzinfo=timezone.utc)
        assert _daemon.save_orphan(store, successor_after=before) is not None
    finally:
        store.close()
    assert store.path.read_bytes() == data
    beside = sorted(
        p.name
        for p in store.path.parent.iterdir()
        if p.name.startswith(store.path.name) and not _ORPHAN_NAME.fullmatch(p.name)
    )
    assert beside == [store.path.name]


# ---------------------------------------------------------------------------
# The daemon, as a process
# ---------------------------------------------------------------------------


def _interval_env(
    env: dict[str, str], site: Path, interval: float = INTERVAL
) -> dict[str, str]:
    """`env` with a ``sitecustomize`` that sets the daemon's check
    interval in every Python child, the daemon `up` spawns among them."""
    site.mkdir(parents=True, exist_ok=True)
    (site / "sitecustomize.py").write_text(
        f"import bettermemory.daemon as _d\n_d.STORE_WATCH_SECONDS = {interval!r}\n",
        encoding="utf-8",
    )
    out = dict(env)
    out["PYTHONPATH"] = str(site) + os.pathsep + out.get("PYTHONPATH", "")
    return out


@pytest.fixture
def watched_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    env = _interval_env(_env(tmp_path), tmp_path / "site")
    yield env
    _cli(["down"], env, timeout=30)


def _start(tmp_path: Path, env: dict[str, str]) -> dict:
    up = _cli(["up", "--port", "0"], env)
    assert up.returncode == 0, up.stderr
    return _wait_running(tmp_path)


def _exited_within(state: dict, bound: float) -> float | None:
    """Seconds until the daemon in `state` was gone, None when it outlived
    `bound`. Gone is `_daemon_client._stopped`: the process gone, or its
    port closed, which covers a daemon the shim started and has not
    reaped yet (a zombie still answers ``kill(pid, 0)``)."""
    started = time.monotonic()
    while time.monotonic() - started < bound:
        if _stopped(int(state["pid"]), int(state["port"])):
            return time.monotonic() - started
        time.sleep(0.02)
    return None


def _process_gone(pid: int, bound: float = 10.0) -> bool:
    """Whether `pid` is gone within `bound`: the daemon's last act, after
    its port closed, is removing its state file on the way out. A daemon
    `up` started is reaped by init once it exits, never left a zombie."""
    deadline = time.monotonic() + bound
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _orphans_at_exit(state: dict, directory: Path, bound: float) -> list[Path] | None:
    """The orphans beside the store at the first moment the daemon in
    `state` was seen gone, None when it outlived `bound`: the backup runs
    before the shutdown, so it is there before the port closes."""
    started = time.monotonic()
    while time.monotonic() - started < bound:
        if _stopped(int(state["pid"]), int(state["port"])):
            return _orphans(directory)
        time.sleep(0.02)
    return None


def _store_id(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        (value,) = conn.execute(
            "SELECT value FROM meta WHERE key = 'store_id'"
        ).fetchone()
    finally:
        conn.close()
    return str(value)


def test_the_daemon_exits_when_its_store_is_unlinked(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    state = _start(tmp_path, watched_env)
    store = _store_path(tmp_path)
    store_id = _store_id(store)
    store.unlink()
    orphans = _orphans_at_exit(state, store.parent, EXIT_BOUND)
    assert orphans is not None, f"the daemon outlived its store by {EXIT_BOUND} s"
    # Saved through the daemon's own connection before the port closed.
    assert len(orphans) == 1
    assert _store_id(orphans[0]) == store_id
    # The normal shutdown ran: the state file is gone with the daemon.
    assert _process_gone(state["pid"])
    assert not _state_path(tmp_path).exists()
    log = _state_path(tmp_path).with_suffix(".log").read_text(encoding="utf-8")
    assert log.count("the daemon stops") == 1
    assert log.count(f"is saved at {orphans[0]}") == 1
    assert _leftovers(store.parent) == []


def test_the_daemon_exits_when_its_store_is_renamed_over(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    """The store it had open is saved, not the file now at the path."""
    state = _start(tmp_path, watched_env)
    store = _store_path(tmp_path)
    store_id = _store_id(store)
    replacement = tmp_path / "replacement.sqlite"
    replacement.write_bytes(b"not the store the daemon opened")
    os.replace(replacement, store)
    orphans = _orphans_at_exit(state, store.parent, EXIT_BOUND)
    assert orphans is not None
    assert [_store_id(path) for path in orphans] == [store_id]
    assert store.read_bytes() == b"not the store the daemon opened"
    assert _process_gone(state["pid"])
    assert not _state_path(tmp_path).exists()


def test_the_daemon_stays_up_through_a_copy_over_its_store(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    state = _start(tmp_path, watched_env)
    store = _store_path(tmp_path)
    backup = tmp_path / "backup.sqlite"
    shutil.copyfile(store, backup)
    shutil.copyfile(backup, store)
    # More than two check intervals.
    time.sleep(3 * INTERVAL)
    assert pid_alive(state["pid"])
    assert health(state["port"]) is not None
    assert _orphans(store.parent) == []
    # And the normal shutdown still works with the watch running.
    down = _cli(["down"], watched_env)
    assert down.returncode == 0, down.stderr
    assert _exited_within(state, 10.0) is not None
    assert _process_gone(state["pid"])
    assert not _state_path(tmp_path).exists()


def _session_start(env: dict[str, str], cwd: Path) -> str:
    done = subprocess.run(
        [sys.executable, "-m", "bettermemory", "hook", "session-start"],
        input='{"session_id": "sess-orphan", "source": "startup"}',
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_next_daemon_reports_the_store_it_replaced(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    """The daemon started after one saved its orphan opens a new, empty
    store beside it: `/health` names the orphan and the session brief
    says how to restore it, until the orphan is deleted."""
    first = _start(tmp_path, watched_env)
    store = _store_path(tmp_path)
    store.unlink()
    orphans = _orphans_at_exit(first, store.parent, EXIT_BOUND)
    assert orphans is not None and len(orphans) == 1
    assert _process_gone(first["pid"])
    second = _start(tmp_path, watched_env)
    assert second["pid"] != first["pid"]
    answered = health(second["port"])
    assert answered is not None
    assert answered["store_orphans"] == [str(orphans[0])]
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    brief = _session_start(watched_env, workdir)
    assert "created fresh" in brief
    assert orphans[0].name in brief
    assert "memory.sqlite-wal" in brief
    orphans[0].unlink()
    answered = health(second["port"])
    assert answered is not None
    assert answered["store_orphans"] == []
    assert "created fresh" not in _session_start(watched_env, workdir)


def test_a_normal_shutdown_is_not_held_by_the_watch(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    """At the shipped 30 s interval, `down` must not wait out a tick."""
    env = _env(tmp_path)  # no sitecustomize: the shipped interval
    state = _start(tmp_path, env)
    started = time.monotonic()
    down = _cli(["down"], env)
    assert down.returncode == 0, down.stderr
    assert _exited_within(state, 10.0) is not None
    assert _process_gone(state["pid"])
    assert time.monotonic() - started < 10.0
    assert not _state_path(tmp_path).exists()


async def test_an_open_shim_restarts_a_daemon_that_stopped_this_way(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    """The shim follows its daemon through a restart: once a daemon stops
    because its store was removed, the shim's next call finds no daemon
    listening, starts one and answers."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "bettermemory"], env=watched_env
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.list_tools()
            first = _wait_running(tmp_path)
            _store_path(tmp_path).unlink()
            assert _exited_within(first, EXIT_BOUND) is not None
            answered = await session.call_tool(
                "memory_search", {"query": "anything at all"}
            )
            assert not answered.is_error
            second = _wait_running(tmp_path)
            assert second["pid"] != first["pid"]


# ---------------------------------------------------------------------------
# A stop the watch had no tick to see
# ---------------------------------------------------------------------------

#: A check interval no test outlasts: the watch never ticks, so whatever
#: is saved is saved as the daemon shuts down.
NO_TICK = 600.0


@pytest.fixture
def unticked_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    env = _interval_env(_env(tmp_path), tmp_path / "site", NO_TICK)
    yield env
    _cli(["down"], env, timeout=30)


def _stop(state: dict, env: dict[str, str], how: str) -> None:
    if how == "down":
        down = _cli(["down"], env)
        assert down.returncode == 0, down.stderr
    else:
        os.kill(int(state["pid"]), signal.SIGTERM)
    assert _process_gone(int(state["pid"]))


def _daemon_writes(state: dict, workdir: Path, n: int = 3) -> None:
    """`n` rows the daemon writes: a compact opens a segment, which is
    logged when no brief carries it."""
    workdir.mkdir(exist_ok=True)
    for i in range(n):
        post(
            int(state["port"]),
            str(state["token"]),
            "/api/v1/hook/session-start",
            {"session_id": f"sess-{i}", "source": "compact", "cwd": str(workdir)},
            timeout=30,
        )


@pytest.mark.parametrize("how", ["down", "sigterm"])
def test_a_stop_before_the_tick_still_saves_the_removed_store(
    tmp_path: Path, unticked_env: dict[str, str], how: str
) -> None:
    """The store, its WAL and its shared memory unlinked with rows only in
    the WAL, then the daemon stopped as `down` or a signal does before
    the watch ticks: the store is saved as the app shuts down, whole."""
    state = _start(tmp_path, unticked_env)
    store = _store_path(tmp_path)
    _daemon_writes(state, tmp_path / "cwd")
    expected = _integrity_and_counts(store)
    main_only = tmp_path / "main-only.sqlite"
    shutil.copyfile(store, main_only)
    # The daemon holds the store open, so nothing has checkpointed them.
    assert _integrity_and_counts(main_only)[1] != expected[1]
    _unlink_store(store)
    _stop(state, unticked_env, how)
    orphans = _orphans(store.parent)
    assert len(orphans) == 1
    assert _integrity_and_counts(orphans[0]) == expected
    assert expected[0] == "ok"
    assert _leftovers(store.parent) == []
    log = _state_path(tmp_path).with_suffix(".log").read_text(encoding="utf-8")
    assert log.count("already stopping") == 1
    assert log.count(f"is saved at {orphans[0]}") == 1


def test_a_normal_stop_saves_nothing(
    tmp_path: Path, unticked_env: dict[str, str]
) -> None:
    state = _start(tmp_path, unticked_env)
    _stop(state, unticked_env, "down")
    assert _orphans(_store_path(tmp_path).parent) == []


def test_a_store_created_before_the_orphan_was_saved_reports_it(
    tmp_path: Path, unticked_env: dict[str, str]
) -> None:
    """The store removed and a CLI command run before the save: the
    command creates a fresh store, which never saw the orphan beside it,
    and is told of it as the daemon saves it. The next daemon reports it
    in `/health` and the brief."""
    first = _start(tmp_path, unticked_env)
    store = _store_path(tmp_path)
    lost_id = _store_id(store)
    _unlink_store(store)
    _cli(["health"], unticked_env)
    fresh_id = _store_id(store)
    assert fresh_id != lost_id
    assert _orphans(store.parent) == []
    _stop(first, unticked_env, "down")
    orphans = _orphans(store.parent)
    assert [_store_id(path) for path in orphans] == [lost_id]
    second = _start(tmp_path, unticked_env)
    assert _store_id(store) == fresh_id
    answered = health(second["port"])
    assert answered is not None
    assert answered["store_orphans"] == [str(orphans[0])]
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    brief = _session_start(unticked_env, workdir)
    assert "created fresh" in brief
    assert orphans[0].name in brief


def test_an_older_store_moved_over_the_path_reports_nothing(
    tmp_path: Path, unticked_env: dict[str, str]
) -> None:
    """Restoring an older store by a rename over the path while the
    daemon runs: the daemon's store is saved, the restored one is not
    told of it."""
    older = Store(tmp_path / "older", keys_dir=tmp_path / "keys")
    older_id = older.store_id
    older.close()
    first = _start(tmp_path, unticked_env)
    store = _store_path(tmp_path)
    lost_id = _store_id(store)
    _unlink_store(store)
    os.replace(older.path, store)
    _stop(first, unticked_env, "down")
    assert [_store_id(path) for path in _orphans(store.parent)] == [lost_id]
    second = _start(tmp_path, unticked_env)
    assert _store_id(store) == older_id
    answered = health(second["port"])
    assert answered is not None
    assert answered["store_orphans"] == []
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    assert "created fresh" not in _session_start(unticked_env, workdir)


# ---------------------------------------------------------------------------
# The daemon in-process: what lands after the first save
# ---------------------------------------------------------------------------


def _serve_in_a_thread(
    tmp_path: Path, between: Callable[[Store], None]
) -> tuple[threading.Thread, dict[str, Any]]:
    """`serve` on a thread of its own, which opens the store as the CLI
    does so the loop's thread is the connection's. `between` runs on the
    loop right after the first save, before the daemon stops."""
    directory = tmp_path / "store"
    out: dict[str, Any] = {}
    opened = threading.Event()
    real_save = _daemon.save_orphan

    def save_then(store: Store, **kwargs: Any) -> Path | None:
        orphan = real_save(store, **kwargs)
        if kwargs.get("into") is None:
            between(store)
        return orphan

    def run() -> None:
        from bettermemory.config import Config, StorageConfig

        config = Config(storage=StorageConfig(directory=str(directory)))
        store = Store(directory, keys_dir=tmp_path / "keys")
        store.write(content="the release runs from the tag", scopes=["global"])
        out["store"] = store
        opened.set()
        try:
            out["code"] = _daemon.serve(
                config=config, store=store, state_dir=tmp_path / "state", port=0
            )
        finally:
            store.close()

    thread = threading.Thread(target=run, daemon=True)
    out["save_then"] = save_then
    thread.start()
    assert opened.wait(30)
    # Serving, so the watch has recorded the store.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = read_state(tmp_path / "state", out["store"].path)
        if state is not None and health(int(state["port"]), timeout=0.5):
            return thread, out
        time.sleep(0.02)
    raise AssertionError("the daemon on its thread never answered")


def test_a_write_after_the_first_save_reaches_the_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that commits to the orphaned store after the first save,
    as a request or hook job still in flight would, is in the orphan the
    daemon leaves: it saves again over the same file once nothing else
    writes."""
    monkeypatch.setattr(_daemon, "STORE_WATCH_SECONDS", 0.1)

    def late_write(store: Store) -> None:
        store.write(content="committed after the first save", scopes=["global"])

    thread, out = _serve_in_a_thread(tmp_path, late_write)
    monkeypatch.setattr(_daemon, "save_orphan", out["save_then"])
    store = out["store"]
    _unlink_store(store.path)
    thread.join(30)
    assert not thread.is_alive()
    assert out["code"] == 0
    orphans = _orphans(store.path.parent)
    assert len(orphans) == 1
    integrity, counts = _integrity_and_counts(orphans[0])
    assert integrity == "ok"
    assert counts["memories"] == 2
    assert _leftovers(store.path.parent) == []


def test_the_watch_tells_a_store_created_inside_its_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watch's own save names the orphan in a store created at the
    path before it ticked."""
    monkeypatch.setattr(_daemon, "STORE_WATCH_SECONDS", 1.0)
    thread, out = _serve_in_a_thread(tmp_path, lambda store: None)
    store = out["store"]
    _unlink_store(store.path)
    Store.create(store.path, keys_dir=tmp_path / "keys").close()
    thread.join(30)
    assert not thread.is_alive()
    orphans = _orphans(store.path.parent)
    assert len(orphans) == 1
    assert _marker(store.path) == [orphans[0].name]


def test_a_save_that_raises_still_stops_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The watch fires once: a save failing in a way it does not expect
    is logged and the daemon stops anyway, never left serving the lost
    store."""
    monkeypatch.setattr(_daemon, "STORE_WATCH_SECONDS", 0.1)
    thread, out = _serve_in_a_thread(tmp_path, lambda store: None)

    def broken(store: Store, **kwargs: Any) -> Path | None:
        raise RuntimeError("an unexpected failure")

    monkeypatch.setattr(_daemon, "save_orphan", broken)
    caplog.set_level(logging.ERROR, logger="bettermemory.daemon")
    _unlink_store(out["store"].path)
    thread.join(30)
    assert not thread.is_alive()
    assert any(
        record.exc_info is not None and "saving the removed store" in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize("how", ["signals", "in-process", "tasks-left"])
def test_a_forced_exit_still_saves_the_removed_store(
    tmp_path: Path,
    unticked_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    """A second SIGINT forces uvicorn out without the app's shutdown,
    where the store is saved. Closing the loop cancels the app's
    lifespan, which saves it then; a loop closed without cancelling it
    (``tasks-left``) leaves the save to `serve` after the server
    returns."""
    if how == "signals":
        state = _start(tmp_path, unticked_env)
        store = _store_path(tmp_path)
        _daemon_writes(state, tmp_path / "cwd")
        expected = _integrity_and_counts(store)
        _unlink_store(store)
        os.kill(int(state["pid"]), signal.SIGINT)
        os.kill(int(state["pid"]), signal.SIGINT)
        assert _process_gone(int(state["pid"]))
        directory = store.parent
    else:
        import uvicorn

        monkeypatch.setattr(_daemon, "STORE_WATCH_SECONDS", NO_TICK)
        servers: list[uvicorn.Server] = []
        real_init = uvicorn.Server.__init__

        def recording(self: uvicorn.Server, *args: Any, **kwargs: Any) -> None:
            real_init(self, *args, **kwargs)
            servers.append(self)

        monkeypatch.setattr(uvicorn.Server, "__init__", recording)
        loops: list[asyncio.AbstractEventLoop] = []
        if how == "tasks-left":

            def leaving_tasks(main: Any, **kwargs: Any) -> Any:
                loop = asyncio.new_event_loop()
                loops.append(loop)
                return loop.run_until_complete(main)

            monkeypatch.setattr(uvicorn.server, "asyncio_run", leaving_tasks)
        thread, out = _serve_in_a_thread(tmp_path, lambda store: None)
        store = out["store"].path
        expected = _integrity_and_counts(store)
        _unlink_store(store)
        (server,) = servers
        server.force_exit = True
        server.should_exit = True
        thread.join(30)
        assert not thread.is_alive()
        assert len(loops) == (how == "tasks-left")
        directory = store.parent
    orphans = _orphans(directory)
    assert len(orphans) == 1
    assert expected[0] == "ok"
    assert _integrity_and_counts(orphans[0]) == expected
    assert _leftovers(directory) == []
