"""The warning that a store opens for reading only because its key ring is
bound to another store file is said once per process per pair of files.

A copy of the store beside the bound one (`log.KeyRing.bind_store`) opens
for reading only, and every open says why. The daemon opens a worker
connection per thread (`Store.reopen`), each a reader's open that reads
the binding again, so a long-lived daemon serving the copy logged the same
WARNING for every worker. What is pinned here:

* the first open of a (store file, bound file) pair in a process logs the
  WARNING, naming both files and the binding; every later open of the
  pair, the owner's, a reader's or a worker's, logs the same line at
  DEBUG and nothing at WARNING;
* every open still reads the binding and still refuses writes: only the
  repetition is quieter;
* the guard keys on both files, resolved: another copy, or the same copy
  once the ring is bound to a third file, gets its own warning, and a
  second spelling of the same copy does not;
* two threads opening the pair at once (the daemon's workers on jobs that
  arrive together) still log one WARNING between them.
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import store_keys
from bettermemory.log import KeyRing
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


@pytest.fixture(autouse=True)
def fresh_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no pair warned about in this process."""
    monkeypatch.setattr(store_keys, "_BOUND_ELSEWHERE_WARNED", set())


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    return tmp_path / "keys"


@pytest.fixture
def home(tmp_path: Path, keys: Path) -> tuple[Path, str]:
    """A closed store this host created, its ring bound to it."""
    path = tmp_path / "home" / STORE_FILENAME
    with Store.create(path, keys_dir=keys) as created:
        created.put_memory(_memory("the staging database listens on 5432"))
        store_id = created.store_id
    return path, store_id


def _copy(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def _bound_lines(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "bettermemory.store"
        and record.levelno == level
        and "belongs to the store at" in record.getMessage()
    ]


def test_the_warning_is_said_once_and_every_later_open_at_debug(
    home: tuple[Path, str],
    keys: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, store_id = home
    copy = _copy(path, tmp_path / "repo" / ".claude-memory" / STORE_FILENAME)
    with caplog.at_level(logging.DEBUG, logger="bettermemory.store"):
        with Store.open_or_create(copy, keys_dir=keys) as twin:
            workers = [twin.reopen() for _ in range(3)]
            try:
                for worker in workers:
                    assert worker.has_key is False
                    with pytest.raises(RuntimeError, match="open for reading only"):
                        worker.put_memory(_memory("written by a worker"))
            finally:
                for worker in workers:
                    worker.close()
        # A reader's open (the hooks' kind) of the copy, and the owner's
        # open again: the same pair, so DEBUG only.
        with Store.open(copy, keys_dir=keys, allow_rekey=False) as reader:
            assert reader.has_key is False
        with Store.open_or_create(copy, keys_dir=keys) as again:
            assert again.has_key is False
            with pytest.raises(RuntimeError, match="open for reading only"):
                again.put_memory(_memory("written into the copy"))
    [warning] = _bound_lines(caplog, logging.WARNING)
    assert str(copy) in warning
    assert str(path.resolve()) in warning
    assert str(KeyRing(keys, store_id).binding_path()) in warning
    debug = _bound_lines(caplog, logging.DEBUG)
    # Three workers, the reader and the second owner's open.
    assert len(debug) == 5
    assert all(line == warning for line in debug)


def test_another_copy_gets_a_warning_of_its_own(
    home: tuple[Path, str],
    keys: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, _store_id = home
    first = _copy(path, tmp_path / "one" / STORE_FILENAME)
    second = _copy(path, tmp_path / "two" / STORE_FILENAME)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        for copy in (first, second, first, second):
            Store.open_or_create(copy, keys_dir=keys).close()
    warnings = _bound_lines(caplog, logging.WARNING)
    assert len(warnings) == 2
    assert str(first) in warnings[0]
    assert str(second) in warnings[1]


def test_the_same_copy_bound_to_a_third_file_is_warned_again(
    home: tuple[Path, str],
    keys: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The pair, not the copy: once the ring names another file that holds
    the id, the copy's open says so, though it said the first pair before."""
    path, store_id = home
    copy = _copy(path, tmp_path / "repo" / STORE_FILENAME)
    third = _copy(path, tmp_path / "third" / STORE_FILENAME)
    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        Store.open_or_create(copy, keys_dir=keys).close()
        KeyRing(keys, store_id).rebind_store(third)
        Store.open_or_create(copy, keys_dir=keys).close()
        Store.open_or_create(copy, keys_dir=keys).close()
    warnings = _bound_lines(caplog, logging.WARNING)
    assert len(warnings) == 2
    assert str(path.resolve()) in warnings[0]
    assert str(third.resolve()) in warnings[1]


def test_another_spelling_of_the_copy_shares_its_warning(
    home: tuple[Path, str],
    keys: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, _store_id = home
    copy = _copy(path, tmp_path / "repo" / ".claude-memory" / STORE_FILENAME)
    (tmp_path / "repo" / "sub").mkdir()
    spelled = tmp_path / "repo" / "sub" / ".." / ".claude-memory" / STORE_FILENAME
    assert spelled != copy and spelled.resolve() == copy.resolve()
    with caplog.at_level(logging.DEBUG, logger="bettermemory.store"):
        Store.open_or_create(copy, keys_dir=keys).close()
        Store.open_or_create(spelled, keys_dir=keys).close()
    assert len(_bound_lines(caplog, logging.WARNING)) == 1
    assert len(_bound_lines(caplog, logging.DEBUG)) == 1


class _MeetingSet(set[tuple[Path, Path]]):
    """A guard set whose first membership test, once answered, waits a
    short while for a second caller to ask too: a thread switch between
    one opener's test and its add, made certain rather than left to the
    scheduler. A guard whose test and add are one step keeps the second
    caller out until the first has added, so the wait runs out alone."""

    def __init__(self) -> None:
        super().__init__()
        self._callers = 0
        self._count_lock = threading.Lock()
        self._second = threading.Event()

    def __contains__(self, o: object) -> bool:
        answer = super().__contains__(o)
        with self._count_lock:
            self._callers += 1
            first = self._callers == 1
        if first:
            self._second.wait(timeout=0.5)
        else:
            self._second.set()
        return answer


def test_two_opens_at_once_log_one_warning(
    home: tuple[Path, str],
    keys: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two reader's opens of the copy in two threads, as two of the
    daemon's workers make on jobs that arrive together: one WARNING and
    one DEBUG line between them, not two warnings."""
    path, _store_id = home
    copy = _copy(path, tmp_path / "repo" / STORE_FILENAME)
    guard = _MeetingSet()
    monkeypatch.setattr(store_keys, "_BOUND_ELSEWHERE_WARNED", guard)
    failures: list[BaseException] = []

    def open_copy() -> None:
        try:
            with Store.open(copy, keys_dir=keys, allow_rekey=False) as reader:
                assert reader.has_key is False
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)

    with caplog.at_level(logging.DEBUG, logger="bettermemory.store"):
        threads = [threading.Thread(target=open_copy) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    assert failures == []
    assert guard._callers == 2, "premise: both opens asked the patched guard"
    assert len(_bound_lines(caplog, logging.WARNING)) == 1
    assert len(_bound_lines(caplog, logging.DEBUG)) == 1


def test_the_bound_store_itself_logs_nothing(
    home: tuple[Path, str],
    keys: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, _store_id = home
    with caplog.at_level(logging.DEBUG, logger="bettermemory.store"):
        with Store.open_or_create(path, keys_dir=keys) as owner:
            owner.reopen().close()
            assert owner.has_key
    assert _bound_lines(caplog, logging.WARNING) == []
    assert _bound_lines(caplog, logging.DEBUG) == []
