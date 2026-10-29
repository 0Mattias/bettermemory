"""One write transaction per mutating call, and what a failed one leaves.

Every mutating `Store` method takes the write lock (``BEGIN IMMEDIATE``)
before it reads what it decides on, so the read, every check and the
write are one transaction (`Store._transaction`). Two writers built on one
snapshot, on two connections of one process or in two processes, then
never both land: exactly one update of two lands and the other raises
ConcurrentUpdateError, and an update or a verify racing a removal never
brings the record back while its tombstone stays.

The races are forced, not hoped for: each writer stops right after it has
read the record (`_row_to_memory`, which every one of them reads through)
until the other writer has read too, or for a bound. With the read under
the write lock the second writer cannot read until the first committed,
so the first waits out the bound alone; with the read before the lock,
as the store read before 9.0.0rc14, both read the same record and both
wrote.

A ``COMMIT`` that fails rolls back while the connection is still in the
transaction and leaves the store owning nothing, so the next write on that
connection, and every other writer, goes on; a transaction a failed
rollback left open is rolled back by the next call. A call nested in
another's transaction joins it under a savepoint: one commit for the
whole, and a nested failure takes back only what it wrote. A busy, locked
or full store reaches the tool boundary as a retryable refusal with its
code when the call kept nothing, and as a refusal that says the write was
kept, not retryable, when it had; what follows a kept write is
best-effort. A long run of writes commits in chunks with the lock free
between them, and no run takes the lock for nothing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import bettermemory.handlers as handlers_pkg
import bettermemory.store as store_module
import bettermemory.store_records as store_records
import bettermemory.store_rows as store_rows
from bettermemory._handlers import KeptWriteRefusal, RefusedError, RetryableRefusal
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.models import Memory, generate_ulid, utcnow
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import (
    DEFAULT_EPISODE_TTL_DAYS,
    STORE_BUSY,
    STORE_FULL,
    ConcurrentUpdateError,
    Store,
    TombstonedError,
    retryable_code,
)

from ._mcp import call_tool
from .conftest import shielded_child_env
from .test_membership_security import stand_in_checkout

#: How long a writer waits at the meeting point for the other writer to
#: read too. Long enough for a second thread or process that is not held
#: by the lock to arrive (it is already past its own open), short enough
#: to keep each race to a few seconds.
MEETING_SECONDS = 2.0
#: The busy timeout every racing writer waits under, threads and processes
#: alike: far past `MEETING_SECONDS`, so a writer held off by the other's
#: lock on a loaded runner waits for it instead of failing ``database is
#: locked``.
RACE_BUSY_TIMEOUT_SECONDS = 60.0


def _store_file(directory: Path) -> Path:
    return directory / store_module.STORE_FILENAME


def _racer(memory_dir: Path) -> Store:
    """A racing writer's own connection, at `RACE_BUSY_TIMEOUT_SECONDS`."""
    racer = Store(memory_dir)
    racer.set_busy_timeout(RACE_BUSY_TIMEOUT_SECONDS)
    return racer


@pytest.fixture
def written(store: Store) -> Memory:
    return store.write(content="the release runs from the tag", scopes=["global"])


# ---------------------------------------------------------------------------
# Threads: two connections of one process
# ---------------------------------------------------------------------------


@pytest.fixture
def meeting(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Arm the meeting point for a thread by name: its next read of a
    record waits there until the other armed thread has read too, or
    `MEETING_SECONDS`."""
    barrier = threading.Barrier(2)
    armed: set[str] = set()
    lock = threading.Lock()
    real = store_rows._row_to_memory

    def meet(row: sqlite3.Row) -> Memory:
        name = threading.current_thread().name
        with lock:
            here = name in armed
            armed.discard(name)
        if here:
            try:
                barrier.wait(MEETING_SECONDS)
            except threading.BrokenBarrierError:
                pass
        return real(row)

    monkeypatch.setattr(store_records, "_row_to_memory", meet)

    def arm(name: str) -> None:
        with lock:
            armed.add(name)

    yield arm
    assert len(armed) < 2, "premise: a writer read through the patched reader"


def _race(
    arm: Callable[[str], None],
    first: Callable[[], Any],
    second: Callable[[], Any],
) -> tuple[Any, Any]:
    """Run the two writers on two threads, both armed at the meeting
    point; each outcome is its value or the exception it raised."""
    outcomes: dict[str, Any] = {}

    def run(name: str, body: Callable[[], Any]) -> None:
        try:
            outcomes[name] = body()
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcomes[name] = exc

    threads = [
        threading.Thread(target=run, args=("a", first), name="race-a"),
        threading.Thread(target=run, args=("b", second), name="race-b"),
    ]
    for thread in threads:
        arm(thread.name)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
        assert not thread.is_alive()
    return outcomes["a"], outcomes["b"]


def test_of_two_threaded_updates_on_one_snapshot_exactly_one_lands(
    memory_dir: Path, written: Memory, meeting: Callable[[str], None]
) -> None:
    def update(body: str) -> Callable[[], Memory]:
        def run() -> Memory:
            with _racer(memory_dir) as mine:
                return mine.update(written.model_copy(update={"body": body}))

        return run

    a, b = _race(meeting, update("port 5433\n"), update("port 5434\n"))
    landed = [o for o in (a, b) if isinstance(o, Memory)]
    refused = [o for o in (a, b) if isinstance(o, ConcurrentUpdateError)]
    assert len(landed) == 1 and len(refused) == 1, (a, b)
    with Store(memory_dir) as check:
        assert check.load_one(written.id).body == landed[0].body
        assert check.log_verify()["status"] == "ok"


def test_a_threaded_update_racing_a_remove_never_brings_the_record_back(
    memory_dir: Path, written: Memory, meeting: Callable[[str], None]
) -> None:
    def update() -> Memory:
        with _racer(memory_dir) as mine:
            return mine.update(written.model_copy(update={"body": "port 5433\n"}))

    def remove() -> Any:
        with _racer(memory_dir) as mine:
            return mine.tombstone(written.id, "gone")

    a, b = _race(meeting, update, remove)
    assert not isinstance(b, BaseException), b
    assert isinstance(a, (Memory, TombstonedError)), a
    with Store(memory_dir) as check:
        assert not check.has_memory(written.id)
        dead = check.get_tombstone(written.id)
        if isinstance(a, Memory):
            # The update landed first: the tombstone keeps it.
            assert dead.body == a.body
        assert check.log_verify()["status"] == "ok"


def test_a_threaded_verify_racing_a_remove_never_brings_the_record_back(
    memory_dir: Path, written: Memory, meeting: Callable[[str], None]
) -> None:
    def verify() -> Memory:
        with _racer(memory_dir) as mine:
            return mine.mark_verified(written.id, verified_paths=["README.md"])

    def remove() -> Any:
        with _racer(memory_dir) as mine:
            return mine.tombstone(written.id, "gone")

    a, b = _race(meeting, verify, remove)
    assert not isinstance(b, BaseException), b
    assert isinstance(a, (Memory, TombstonedError)), a
    with Store(memory_dir) as check:
        assert not check.has_memory(written.id)
        dead = check.get_tombstone(written.id)
        if isinstance(a, Memory):
            assert dead.last_verified_at == a.last_verified_at
            assert dead.verified_paths == ["README.md"]
        else:
            assert check.verifications_for([written.id]) == {}
        assert check.log_verify()["status"] == "ok"


def test_a_put_never_writes_over_a_tombstone(store: Store, written: Memory) -> None:
    """The one way back from a tombstone is `restore`: a put of a removed
    id is refused, so no id is ever active and removed at once."""
    store.tombstone(written.id, "gone")
    with pytest.raises(TombstonedError, match="restore it"):
        store.put_memory(written)
    assert not store.has_memory(written.id)
    restored = store.restore(written.id)
    assert store.load_one(written.id) == restored
    assert store.count_tombstones() == 0


# ---------------------------------------------------------------------------
# Processes: two writers on one store file
# ---------------------------------------------------------------------------

#: The writer each child process runs: open the store, wait for the other
#: child, then make one call with the meeting point armed after its own
#: snapshot read. Prints its outcome as one JSON line.
_CHILD = r"""
import json, sys, time
from pathlib import Path

import bettermemory.store_records as store_records
from bettermemory.store import ConcurrentUpdateError, Store

op, path, keys, memory_id, sync, me, other, session, seconds, busy = sys.argv[1:11]
sync = Path(sync)
real = store_records._row_to_memory
armed = []


def meet(row):
    if armed:
        armed.clear()
        (sync / me).touch()
        deadline = time.monotonic() + float(seconds)
        while not (sync / other).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    return real(row)


store_records._row_to_memory = meet
store = Store.open(path, keys_dir=keys)
store.set_busy_timeout(float(busy))
snapshot = store.load_one(memory_id)
(sync / (me + ".ready")).touch()
while not (sync / (other + ".ready")).exists():
    time.sleep(0.01)
armed.append(True)
try:
    if op == "update":
        out = store.update(snapshot.model_copy(update={"body": "edited by " + me + "\n"}))
        result = out.body
    elif op == "remove":
        result = store.tombstone(memory_id, "removed by " + me).removed_reason
    elif op == "verify":
        result = store.mark_verified(memory_id).last_verified_at.isoformat()
    elif op == "corroborate":
        out = store.record_corroboration(memory_id, session=session)
        result = None if out is None else out.corroborations
    else:
        raise SystemExit("unknown op " + op)
    print(json.dumps({"ok": True, "result": result}))
except (ConcurrentUpdateError, KeyError) as exc:
    print(json.dumps({"ok": False, "error": type(exc).__name__}))
finally:
    store.close()
"""


def _two_processes(
    memory_dir: Path,
    keys_dir: Path,
    tmp_path: Path,
    memory_id: str,
    ops: tuple[str, str],
    *,
    session: str = "sess_race",
) -> tuple[dict[str, Any], dict[str, Any]]:
    sync = tmp_path / "sync"
    sync.mkdir()
    names = ("a", "b")
    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD,
                op,
                str(_store_file(memory_dir)),
                str(keys_dir),
                memory_id,
                str(sync),
                me,
                other,
                session,
                str(MEETING_SECONDS),
                str(RACE_BUSY_TIMEOUT_SECONDS),
            ],
            env=shielded_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for op, me, other in zip(ops, names, reversed(names))
    ]
    outcomes = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err
        outcomes.append(json.loads(out.strip().splitlines()[-1]))
    assert any((sync / name).exists() for name in names), (
        "premise: a child read through the patched reader"
    )
    return outcomes[0], outcomes[1]


def test_of_two_processes_updating_one_snapshot_exactly_one_lands(
    memory_dir: Path, keys_dir: Path, tmp_path: Path, written: Memory, store: Store
) -> None:
    a, b = _two_processes(
        memory_dir, keys_dir, tmp_path, written.id, ("update", "update")
    )
    landed = [o for o in (a, b) if o["ok"]]
    refused = [o for o in (a, b) if not o["ok"]]
    assert len(landed) == 1, (a, b)
    assert refused[0]["error"] == "ConcurrentUpdateError"
    assert store.load_one(written.id).body == landed[0]["result"]
    assert store.log_verify()["status"] == "ok"


def test_a_process_updating_while_another_removes_never_brings_it_back(
    memory_dir: Path, keys_dir: Path, tmp_path: Path, written: Memory, store: Store
) -> None:
    a, b = _two_processes(
        memory_dir, keys_dir, tmp_path, written.id, ("update", "remove")
    )
    assert b["ok"], b
    assert a["ok"] or a["error"] == "TombstonedError", a
    assert not store.has_memory(written.id)
    dead = store.get_tombstone(written.id)
    if a["ok"]:
        assert dead.body == a["result"]
    assert store.log_verify()["status"] == "ok"


def test_a_process_verifying_while_another_removes_never_brings_it_back(
    memory_dir: Path, keys_dir: Path, tmp_path: Path, written: Memory, store: Store
) -> None:
    a, b = _two_processes(
        memory_dir, keys_dir, tmp_path, written.id, ("verify", "remove")
    )
    assert b["ok"], b
    assert a["ok"] or a["error"] == "TombstonedError", a
    assert not store.has_memory(written.id)
    if not a["ok"]:
        assert store.verifications_for([written.id]) == {}
    assert store.log_verify()["status"] == "ok"


def test_two_processes_of_one_session_corroborate_once(
    memory_dir: Path, keys_dir: Path, tmp_path: Path, written: Memory, store: Store
) -> None:
    a, b = _two_processes(
        memory_dir, keys_dir, tmp_path, written.id, ("corroborate", "corroborate")
    )
    assert sorted([a["result"], b["result"]], key=str) == [1, None], (a, b)
    assert store.load_one(written.id).corroborations == 1


# ---------------------------------------------------------------------------
# Corroboration: once per memory and session
# ---------------------------------------------------------------------------


def test_parallel_corroborations_of_one_session_corroborate_once(
    memory_dir: Path, written: Memory
) -> None:
    """Eight parallel calls of one session, each on its own connection as
    the daemon's tool workers make them: one records, seven answer None."""
    start = threading.Barrier(8)
    outcomes: list[Any] = []
    lock = threading.Lock()

    def corroborate() -> None:
        with _racer(memory_dir) as mine:
            start.wait(30)
            try:
                out: Any = mine.record_corroboration(written.id, session="sess_one")
            except BaseException as exc:  # noqa: BLE001 - the outcome under test
                out = exc
        with lock:
            outcomes.append(out)

    threads = [threading.Thread(target=corroborate) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    recorded = [o for o in outcomes if isinstance(o, Memory)]
    assert len(outcomes) == 8
    assert len(recorded) == 1 and outcomes.count(None) == 7, outcomes
    with Store(memory_dir) as check:
        assert check.load_one(written.id).corroborations == 1
        assert check.log_verify()["status"] == "ok"


def test_another_session_corroborates_again_and_none_always_does(
    store: Store, written: Memory
) -> None:
    first = store.record_corroboration(written.id, session="sess_one")
    assert first is not None and first.corroborations == 1
    assert store.record_corroboration(written.id, session="sess_one") is None
    second = store.record_corroboration(written.id, session="sess_two")
    assert second is not None and second.corroborations == 2
    assert store.record_corroboration(written.id).corroborations == 3
    assert store.record_corroboration(written.id).corroborations == 4
    assert store.load_one(written.id).updated == written.updated


def test_the_corroboration_check_reads_its_index(store: Store, written: Memory) -> None:
    """The plan of the query `record_corroboration` runs, read from the
    constant the store runs it from, so a drift in the query is a drift
    in what this reads."""
    plan = " ".join(
        str(row[-1])
        for row in store.conn.execute(
            "EXPLAIN QUERY PLAN " + store_module.CORROBORATION_CHECK_SQL,
            ("sess_one", "x"),
        )
    )
    assert "USING INDEX log_corroborations" in plan, plan
    store.record_corroboration(written.id, session="sess_one")
    assert store.record_corroboration(written.id, session="sess_one") is None


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: -1)() == 0,
    reason="a file mode does not keep this user out",
)
def test_a_read_only_store_is_told_exactly_what_it_lacks(
    memory_dir: Path,
    keys_dir: Path,
    store: Store,
    written: Memory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store file this process cannot write is opened as it is. The log
    names what the open would have added, and says nothing when nothing
    is missing."""
    path = store.path
    store.close()
    path.chmod(0o444)
    try:
        with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
            Store.open(path, keys_dir=keys_dir, strict_provenance=False).close()
        assert not [r for r in caplog.records if "read-only" in r.getMessage()]
        path.chmod(0o644)
        with Store.open(path, keys_dir=keys_dir) as writable:
            writable.conn.execute("DROP INDEX log_corroborations")
        path.chmod(0o444)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
            Store.open(path, keys_dir=keys_dir, strict_provenance=False).close()
        said = [r.getMessage() for r in caplog.records if "read-only" in r.getMessage()]
        assert said == [f"store {path} is read-only; not added: log_corroborations"]
    finally:
        path.chmod(0o644)


def test_a_store_an_earlier_build_made_gains_the_index_at_open(
    memory_dir: Path, store: Store, written: Memory
) -> None:
    store.conn.execute("DROP INDEX log_corroborations")
    store.close()
    with Store(memory_dir) as reopened:
        names = {
            str(row[0])
            for row in reopened.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "log_corroborations" in names
        assert reopened.record_corroboration(written.id, session="s") is not None
        assert reopened.record_corroboration(written.id, session="s") is None


# ---------------------------------------------------------------------------
# A COMMIT that fails
# ---------------------------------------------------------------------------


class _Connection:
    """The store's connection, with a ``COMMIT`` that fails `fail` times
    and a ``ROLLBACK`` that fails `fail_rollback` times before they reach
    SQLite (a busy, full or failing disk), and every ``COMMIT`` counted."""

    def __init__(self, conn: sqlite3.Connection, *, fail: int = 0) -> None:
        self._conn = conn
        self.fail = fail
        self.fail_rollback = 0
        self.commits = 0

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql == "COMMIT":
            if self.fail:
                self.fail -= 1
                raise sqlite3.OperationalError("disk I/O error")
            self.commits += 1
        if sql == "ROLLBACK" and self.fail_rollback:
            self.fail_rollback -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@pytest.fixture
def counted(store: Store, monkeypatch: pytest.MonkeyPatch) -> _Connection:
    wrapped = _Connection(store._conn)
    monkeypatch.setattr(store, "_conn", wrapped)
    return wrapped


def test_a_failed_commit_rolls_back_and_the_connection_writes_on(
    store: Store, written: Memory, counted: _Connection, memory_dir: Path
) -> None:
    rows = len(store.log_rows())
    head = store.keyring.read_head()
    counted.fail = 1
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        store.update(written.model_copy(update={"body": "port 5433\n"}))
    assert store.conn.in_transaction is False
    assert store.in_batch is False
    assert store.load_one(written.id).body == written.body
    assert len(store.log_rows()) == rows
    assert store.keyring.read_head() == head
    # Another writer is not held off: the failed commit kept no lock.
    with Store(memory_dir) as other:
        other.set_busy_timeout(0.5)
        other.write(content="written beside the failed commit", scopes=["global"])
    # And this connection writes on, in a transaction of its own.
    updated = store.update(written.model_copy(update={"body": "port 5434\n"}))
    assert store.load_one(written.id).body == updated.body
    assert store.conn.in_transaction is False
    head_after = store.keyring.read_head()
    assert head_after is not None and head_after.seq == len(store.log_rows())
    assert store.log_verify()["status"] == "ok"


def test_a_failed_commit_of_a_batch_leaves_nothing_open(
    store: Store, counted: _Connection
) -> None:
    counted.fail = 1
    with pytest.raises(sqlite3.OperationalError):
        with store.batch():
            store.write(content="alpha", scopes=["global"])
            store.write(content="beta", scopes=["global"])
    assert store.conn.in_transaction is False and store.count_memories() == 0
    with store.batch():
        store.write(content="gamma", scopes=["global"])
    assert store.count_memories() == 1
    assert store.log_verify()["status"] == "ok"


def test_a_transaction_a_failed_rollback_left_is_rolled_back_by_the_next_call(
    store: Store,
    written: Memory,
    counted: _Connection,
    memory_dir: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMMIT fails and the ROLLBACK after it fails too: the connection
    is left inside a transaction nobody owns, holding the write lock. The
    next call on it (the stdio server's one connection, say) rolls that
    back and lands, instead of failing ``cannot start a transaction within
    a transaction`` while every other writer waits. Logged once per
    process, in the words the daemon's worker reconnect uses."""
    monkeypatch.setattr(store_module, "_residue_logged", False)
    counted.fail = 1
    counted.fail_rollback = 1
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
            store.update(written.model_copy(update={"body": "port 5433\n"}))
        assert store.conn.in_transaction and not store.in_batch
        with Store(memory_dir) as other:
            other.set_busy_timeout(0.1)
            with pytest.raises(sqlite3.OperationalError) as held:
                other.write(content="held off by the residue", scopes=["global"])
            assert retryable_code(held.value) == STORE_BUSY
        updated = store.update(written.model_copy(update={"body": "port 5434\n"}))
        assert store.conn.in_transaction is False
        assert store.load_one(written.id).body == updated.body
        # A second residue is rolled back the same way, and not logged again.
        counted.fail = 1
        counted.fail_rollback = 1
        with pytest.raises(sqlite3.OperationalError):
            store.write(content="lost to the second failed commit", scopes=["global"])
        store.write(content="written after the second residue", scopes=["global"])
    said = [
        r.getMessage() for r in caplog.records if "was left inside" in r.getMessage()
    ]
    assert said == [
        f"the connection to store {store.path} was left inside a transaction; "
        "it was rolled back"
    ]
    assert store.count_memories() == 2
    assert store.log_verify()["status"] == "ok"


# ---------------------------------------------------------------------------
# Nested calls: one owner, one commit
# ---------------------------------------------------------------------------


def test_a_put_inside_a_batch_and_inside_an_update_commits_once(
    store: Store, written: Memory, counted: _Connection
) -> None:
    with store.batch():
        store.put_memory(written.model_copy(update={"body": "port 5433\n"}))
        store.write(content="beside it", scopes=["global"])
        assert store.in_batch and counted.commits == 0
    assert counted.commits == 1
    current = store.load_one(written.id)
    # `update` reads under its own transaction and puts inside it.
    store.update(current.model_copy(update={"body": "port 5434\n"}))
    assert counted.commits == 2
    with store.batch():
        store.update(store.load_one(written.id).model_copy(update={"body": "5435\n"}))
    assert counted.commits == 3
    assert store.load_one(written.id).body == "5435\n"
    assert store.log_verify()["status"] == "ok"


def test_a_nested_failure_takes_back_only_what_it_wrote(store: Store) -> None:
    """A rename to a scope no record may carry fails at the record's
    put, after its log row was appended: the row goes with it, and the
    rename around it goes on. Before the savepoint the batch committed
    that row, and `log verify` read the store as tampered."""
    store.write(content="the release runs from the tag", scopes=["alpha"])
    kept = store.write(content="the deploy runs on merge", scopes=["beta"])
    rows = len(store.log_rows())
    result = store.rename_scope("alpha", "Not A Scope")
    assert result["active"] == [] and len(result["failed"]) == 1
    assert len(store.log_rows()) == rows
    assert store.log_verify()["status"] == "ok"
    with store.batch():
        store.tombstone(kept.id, "gone")
        with pytest.raises(TombstonedError):
            store.put_memory(kept)
        assert store.in_batch
    # The put that failed took back nothing; the removal before it stands.
    assert not store.has_memory(kept.id) and store.count_tombstones() == 1
    assert len(store.log_rows()) == rows + 2
    assert store.log_verify()["status"] == "ok"


# ---------------------------------------------------------------------------
# Long runs commit in chunks
# ---------------------------------------------------------------------------


@pytest.fixture
def yields(store: Store, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every pause between two chunks on `store`, by when it came, with
    one item to a chunk (`CHUNK_HOLD_SECONDS` 0) and no sleep."""
    monkeypatch.setattr(store_module, "CHUNK_HOLD_SECONDS", 0.0)
    came: list[float] = []
    monkeypatch.setattr(
        store, "_yield_between_chunks", lambda: came.append(time.monotonic())
    )
    return came


def _tombstoned(store: Store, count: int) -> list[str]:
    ids = [
        store.write(content=f"record {n} of the prune", scopes=["global"]).id
        for n in range(count)
    ]
    for memory_id in ids:
        store.tombstone(memory_id, "gone")
    return ids


def test_a_prune_commits_in_chunks_with_the_lock_free_between_them(
    store: Store, counted: _Connection, yields: list[float]
) -> None:
    ids = _tombstoned(store, 5)
    before = counted.commits
    pruned = store.prune_tombstones(
        older_than=timedelta(0), now=utcnow() + timedelta(1)
    )
    assert sorted(pruned) == sorted(ids)
    # One chunk a tombstone, a pause between two, none after the last.
    assert counted.commits - before == len(ids)
    assert len(yields) == len(ids) - 1
    assert store.count_tombstones() == 0
    assert store.log_verify()["status"] == "ok"


def test_a_prune_inside_a_batch_is_one_transaction(
    store: Store, counted: _Connection, yields: list[float], memory_dir: Path
) -> None:
    """Inside a transaction the caller holds, every item goes into it: no
    chunk commits, no pause frees the lock, and another connection sees
    nothing pruned until the batch commits."""
    ids = _tombstoned(store, 5)
    before = counted.commits
    with store.batch():
        pruned = store.prune_tombstones(
            older_than=timedelta(0), now=utcnow() + timedelta(1)
        )
        assert sorted(pruned) == sorted(ids)
        with Store(memory_dir) as other:
            assert other.count_tombstones() == len(ids)
        assert counted.commits == before and yields == []
    assert counted.commits - before == 1
    with Store(memory_dir) as other:
        assert other.count_tombstones() == 0
    assert store.log_verify()["status"] == "ok"


def test_a_rename_decides_each_record_under_its_own_chunk_s_lock(
    store: Store,
    counted: _Connection,
    monkeypatch: pytest.MonkeyPatch,
    memory_dir: Path,
) -> None:
    """Between two chunks another writer edits one record still to be
    renamed and moves another out of the scope. Each is read again under
    its own chunk's lock: the edit is kept under the new scope, and the
    record that left the scope is not renamed back into it."""
    monkeypatch.setattr(store_module, "CHUNK_HOLD_SECONDS", 0.0)
    ids = [
        store.write(content=f"record {n} of the rename", scopes=["alpha"]).id
        for n in range(4)
    ]
    changed: dict[str, str] = {}

    def another_writer_between_chunks() -> None:
        if changed:
            return
        with Store(memory_dir) as other:
            waiting = [i for i in ids if other.load_one(i).scopes == ["alpha"]]
            edited, moved = waiting[0], waiting[1]
            other.update(
                other.load_one(edited).model_copy(update={"body": "edited between"})
            )
            other.update(other.load_one(moved).model_copy(update={"scopes": ["gamma"]}))
        changed.update(edited=edited, moved=moved)

    monkeypatch.setattr(store, "_yield_between_chunks", another_writer_between_chunks)
    before = counted.commits
    result = store.rename_scope("alpha", "beta", include_tombstones=False)
    edited, moved = changed["edited"], changed["moved"]
    assert sorted(result["active"]) == sorted(i for i in ids if i != moved)
    # One chunk a record, the one that left the scope included: its chunk
    # read it under the lock and wrote nothing.
    assert counted.commits - before == len(ids)
    assert store.load_one(edited).body == "edited between"
    assert store.load_one(edited).scopes == ["beta"]
    assert store.load_one(moved).scopes == ["gamma"]
    assert store.log_verify()["status"] == "ok"


@contextlib.contextmanager
def _held(store: Store) -> Iterator[None]:
    """Another process holds `store`'s write lock for the block."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(store.path)],
        env=shielded_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        yield
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(30)


def test_a_run_with_nothing_to_do_takes_no_lock(store: Store, written: Memory) -> None:
    """A prune with nothing past its cutoff and a rename of a scope no
    record carries return at once while another process holds the lock:
    they take it only for an item."""
    store.set_busy_timeout(0.2)
    with _held(store):
        started = time.monotonic()
        assert store.prune_tombstones(older_than=timedelta(days=1)) == []
        assert store.prune_episode_sessions() == []
        assert store.rename_scope("nowhere", "elsewhere") == {
            "active": [],
            "tombstoned": [],
        }
        assert time.monotonic() - started < 0.2


# ---------------------------------------------------------------------------
# The tool boundary: a busy or full store is a retryable refusal
# ---------------------------------------------------------------------------

#: Holds the store's write lock in another process until its stdin closes.
_HOLDER = r"""
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("BEGIN IMMEDIATE")
print("held", flush=True)
sys.stdin.read()
conn.execute("ROLLBACK")
conn.close()
"""


def _server(store: Store) -> Any:
    cfg = Config(
        storage=StorageConfig(directory=str(store.path.parent)),
        behavior=BehaviorConfig(),
    )
    state = SessionState()
    rec = Recorder(store=store, session_id=state.session_id, enabled=True)
    return build_server(config=cfg, store=store, state=state, recorder=rec)


def _refusal(exc: BaseException) -> RetryableRefusal:
    cause: BaseException | None = exc
    while cause is not None and not isinstance(cause, RetryableRefusal):
        cause = cause.__cause__
    assert isinstance(cause, RetryableRefusal), exc
    return cause


async def test_a_lock_another_process_holds_is_a_retryable_refusal(
    store: Store,
) -> None:
    server = _server(store)
    store.set_busy_timeout(0.2)
    with _held(store):
        with pytest.raises(ToolError, match="store_busy") as caught:
            await call_tool(
                server,
                "memory_write",
                {"content": "the release runs from the tag", "scopes": ["global"]},
            )
    refusal = _refusal(caught.value)
    assert refusal.code == STORE_BUSY == "store_busy"
    assert refusal.retryable is True
    # The code is in the text, after whatever the SDK puts before it.
    assert "store_busy (retryable): " in str(caught.value)
    assert "was not kept. Retry the call." in str(refusal)
    assert store.count_memories() == 0
    assert store.conn.in_transaction is False
    # The lock is free again: the same call lands.
    store.set_busy_timeout(5.0)
    done = await call_tool(
        server,
        "memory_write",
        {"content": "the release runs from the tag", "scopes": ["global"]},
    )
    assert done["status"] == "committed", done


async def test_a_full_store_is_a_retryable_refusal(store: Store) -> None:
    server = _server(store)
    pages = int(store.conn.execute("PRAGMA page_count").fetchone()[0])
    store.conn.execute(f"PRAGMA max_page_count = {pages}")
    try:
        with pytest.raises(ToolError, match="store_full") as caught:
            await call_tool(
                server,
                "memory_write",
                {"content": "x " * 20000 + "end", "scopes": ["global"]},
            )
    finally:
        store.conn.execute("PRAGMA max_page_count = 1073741823")
    refusal = _refusal(caught.value)
    assert refusal.code == STORE_FULL == "store_full"
    assert "Free disk space" in str(refusal)
    assert store.conn.in_transaction is False and not store.in_batch
    store.write(content="written once there is room", scopes=["global"])
    assert store.log_verify()["status"] == "ok"


@pytest.fixture
def episode_server(
    store: Store,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """The tool server over `store`, its caller standing in a checkout of
    its own."""
    stand_in_checkout(tmp_path_factory.mktemp("checkout"), monkeypatch)
    return _server(store)


async def test_a_busy_store_after_the_write_was_kept_is_not_retryable(
    store: Store, episode_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool body commits its episode, then a later step of it meets a
    lock another process holds. The call answers that its write was kept
    and is not to be retried: a retry would land a second episode."""

    async def kept_then_busy(deps: Any, action: str, **_: Any) -> dict[str, Any]:
        deps.store.write_episode(session_id="sess_kept", body="iteration 3: shipped")
        deps.store.set_busy_timeout(0.2)
        with _held(deps.store):
            deps.store.write_episode(session_id="sess_kept", body="a later step")
        return {"status": "committed"}

    monkeypatch.setattr(handlers_pkg, "episode", kept_then_busy)
    with pytest.raises(ToolError) as caught:
        await call_tool(episode_server, "episode", {"action": "write", "body": "x"})
    said = str(caught.value)
    assert "store_busy (not retryable): this call had committed its write" in said
    assert "Do not retry the call" in said
    cause = caught.value.__cause__
    assert isinstance(cause, KeptWriteRefusal) and cause.retryable is False
    assert cause.code == STORE_BUSY
    assert store.count_episodes() == 1
    assert store.conn.in_transaction is False


async def test_a_busy_store_before_any_write_is_retryable(
    store: Store, episode_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same body meeting the lock at its first write kept nothing: the
    call is retryable, and the store holds no episode."""

    async def busy_at_once(deps: Any, action: str, **_: Any) -> dict[str, Any]:
        deps.store.set_busy_timeout(0.2)
        with _held(deps.store):
            deps.store.write_episode(session_id="sess_kept", body="iteration 3")
        return {"status": "committed"}

    monkeypatch.setattr(handlers_pkg, "episode", busy_at_once)
    with pytest.raises(ToolError) as caught:
        await call_tool(episode_server, "episode", {"action": "write", "body": "x"})
    assert "store_busy (retryable): " in str(caught.value)
    assert _refusal(caught.value).retryable is True
    assert store.count_episodes() == 0


async def test_an_episode_write_is_kept_when_its_prune_meets_a_busy_store(
    store: Store,
    episode_server: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The write path's prune runs after the episode committed: a lock
    another process holds there leaves the old session for the next write
    and the call answers committed, with one new episode, never a refusal
    a client would retry into a second one."""
    old = utcnow() - timedelta(days=DEFAULT_EPISODE_TTL_DAYS + 1)
    store.write_episode(session_id="sess_old", body="an old session", now=old)
    real = Store.prune_episode_sessions

    def prune_held(self: Store, **kwargs: Any) -> list[str]:
        self.set_busy_timeout(0.2)
        with _held(self):
            return real(self, **kwargs)

    monkeypatch.setattr(Store, "prune_episode_sessions", prune_held)
    with caplog.at_level(logging.WARNING):
        done = await call_tool(
            episode_server,
            "episode",
            {"action": "write", "body": "iteration 3: shipped the fix"},
        )
    assert done["status"] == "committed" and done["pruned_sessions"] == []
    assert store.count_episodes() == 2
    assert any("SQLITE_BUSY" in r.getMessage() for r in caplog.records)
    assert store.conn.in_transaction is False


def test_only_busy_locked_and_full_are_retryable(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "probe.sqlite"), isolation_level=None)
    try:
        conn.execute("CREATE TABLE t (x BLOB)")
        pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        conn.execute(f"PRAGMA max_page_count = {pages}")
        with pytest.raises(sqlite3.OperationalError) as full:
            conn.execute("INSERT INTO t VALUES (zeroblob(1000000))")
        assert retryable_code(full.value) == STORE_FULL
        with pytest.raises(sqlite3.OperationalError) as plain:
            conn.execute("COMMIT")
        assert retryable_code(plain.value) is None
    finally:
        conn.close()
    assert retryable_code(ValueError("database is locked")) is None
    assert retryable_code(sqlite3.OperationalError("database is locked")) is None


async def test_an_update_that_loses_to_a_removal_is_a_refusal(
    store: Store, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_update reads its snapshot, then another writer removes the
    record before the update's transaction: the store answers
    TombstonedError under its lock, and the tool answers a refusal with
    its words, not a crash, and leaves the record removed."""
    server = _server(store)
    memory = store.write(content="the release runs from the tag", scopes=["global"])
    real_update = Store.update

    def removed_first(self: Store, edited: Memory, **kwargs: Any) -> Memory:
        with Store(memory_dir) as other:
            other.tombstone(edited.id, "removed by the other writer")
        return real_update(self, edited, **kwargs)

    monkeypatch.setattr(Store, "update", removed_first)
    with pytest.raises(ToolError, match="was removed") as caught:
        await call_tool(
            server,
            "memory_update",
            {"id": memory.id, "content": "the release runs from the signed tag"},
        )
    assert isinstance(caught.value.__cause__, RefusedError)
    assert not store.has_memory(memory.id)
    assert store.get_tombstone(memory.id).body == memory.body
    assert store.log_verify()["status"] == "ok"


async def test_removing_a_removed_or_unknown_id_is_a_refusal(store: Store) -> None:
    server = _server(store)
    memory = store.write(content="the release runs from the tag", scopes=["global"])
    await call_tool(server, "memory_remove", {"id": memory.id, "reason": "gone"})
    with pytest.raises(ToolError, match="was removed: gone") as again:
        await call_tool(server, "memory_remove", {"id": memory.id, "reason": "x"})
    assert isinstance(again.value.__cause__, RefusedError)
    unknown = generate_ulid()
    with pytest.raises(ToolError, match=f"no record {unknown}") as missing:
        await call_tool(server, "memory_remove", {"id": unknown, "reason": "x"})
    assert isinstance(missing.value.__cause__, RefusedError)
