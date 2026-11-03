"""A writing open that races a restore's lock and probe is refused or seen,
whatever the restore lock is (ENG-208).

Where the lock is the store's directory (POSIX), a writing open holds it
shared from its check until its connection has read the store, so a
restore waits for it and is refused. Where the lock is the record file a
restore makes (Windows, whose ``msvcrt`` has no shared lock, and a POSIX
mount where ``flock`` cannot lock a directory), an opener can only check
it, and 9.0.0rc14 left this: an opener that checked just before a restore
took the lock, and opened the file just after the restore's probe, was not
seen. On Windows the opener now holds the store file open from its check
on, and checks again behind the handle: Windows will not let the swap
rename a file that is open, so a restore that comes later fails at its
swap and changes nothing, and one that came first is caught by the second
check. Elsewhere the opener checks again once its connection has read the
store, and refuses a store file swapped in since its first check.

The `record` cases run where the platform makes the lock the record file:
on Windows natively (the CI leg runs them), elsewhere with ``flock``
failing on directories as a mount without locks fails it. The `directory`
cases run on POSIX only, and so do the `marker` cases: ``flock`` fails on
the record file too, as on a ``nolocks`` mount, where nothing is locked
and only the record's marker speaks.

The record cases stop the opener right after its first check
(`restore_lock._take`, which every build since 9.0.0rc14 calls there), so
against a build without the handle and the second check they fail on what
the opener does: it writes into the file the restore moves aside.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bettermemory import _fsutil, backup, restore_lock
from bettermemory import store as store_module
from bettermemory.backup import create_backup, restore_bundle
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store, StoreRefusedError

#: How long a test waits on a thread it started before it calls the run a
#: hang; a restore of the fixture's two-memory store takes well under one.
_SECONDS = 60.0
#: The lock modes this platform can run (module docstring).
MODES = ["record"] if sys.platform == "win32" else ["directory", "record", "marker"]
#: The modes where the lock is the record file.
RECORD_MODES = ["record"] if sys.platform == "win32" else ["record", "marker"]


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


class _Gate:
    """A point one thread stops at until the test lets it go: `stop` is
    called in the code under test, and holds only the thread `arm` named."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.go = threading.Event()
        self.thread: int | None = None

    def arm(self) -> None:
        self.thread = threading.get_ident()

    def stop(self) -> None:
        if threading.get_ident() == self.thread:
            self.reached.set()
            assert self.go.wait(_SECONDS), "the test never let the thread go"


def _before(gate: _Gate, real: Callable[..., Any]) -> Callable[..., Any]:
    def stopped(*args: Any, **kwargs: Any) -> Any:
        gate.stop()
        return real(*args, **kwargs)

    return stopped


def _after(gate: _Gate, real: Callable[..., Any]) -> Callable[..., Any]:
    def stopped(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        gate.stop()
        return result

    return stopped


@pytest.fixture
def store_file(tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    path = directory / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as created:
        created.put_memory(_memory("alpha"))
    return path


@pytest.fixture
def bundle(store_file: Path, keys_dir: Path) -> Path:
    made = Path(create_backup(store_file)["bundle"])
    with Store.open(store_file, keys_dir=keys_dir) as opened:
        opened.put_memory(_memory("beta, after the backup"))
    return made


@pytest.fixture(params=MODES)
def lock_mode(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, store_file: Path
) -> str:
    """The restore lock's mode for the test, and short waits: a restore
    waits half a second for the lock, an opener a fifth (the shipped waits
    are for a person). ``record`` on POSIX: ``flock`` fails on a directory
    with ENOTSUP and locks a file as ever. ``marker``: it fails on the
    store's record file too, as a ``nolocks`` mount fails every lock; the
    other files bettermemory locks (the record beside the head, the
    daemon's locks) lock as ever, since they stand elsewhere."""
    mode = str(request.param)
    if mode in ("record", "marker") and sys.platform != "win32":
        import fcntl

        real = fcntl.flock
        record = restore_lock.lock_path(store_file)

        def unlockable(fd: int) -> bool:
            found = os.fstat(fd)
            if stat.S_ISDIR(found.st_mode):
                return True
            if mode != "marker":
                return False
            try:
                there = os.stat(record)
            except FileNotFoundError:
                return False
            return (found.st_dev, found.st_ino) == (there.st_dev, there.st_ino)

        def flock(fd: int, operation: int) -> None:
            if unlockable(fd):
                raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
            real(fd, operation)

        monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(restore_lock, "_RECORD_MODE_DIRS", set())
    acquire = restore_lock.acquire

    def quick(store_file: Path, *, wait: float = 0.5) -> Any:
        return acquire(store_file, wait=min(wait, 0.5))

    monkeypatch.setattr(restore_lock, "acquire", quick)
    monkeypatch.setattr(restore_lock, "OPEN_WAIT_SECONDS", 0.2)
    return mode


def _opener(
    store_file: Path, keys_dir: Path, gate: _Gate
) -> tuple[threading.Thread, dict[str, str]]:
    """A writing open of the store on a thread of its own that writes one
    memory, stopping wherever `gate` is placed; its outcome lands in the
    returned dict (``wrote``, or ``refused`` and the refusal)."""
    outcome: dict[str, str] = {}

    def run() -> None:
        gate.arm()
        try:
            with Store.open(store_file, keys_dir=keys_dir) as opened:
                opened.put_memory(_memory("written by the racing opener"))
        except StoreRefusedError as exc:
            outcome.update(status="refused", said=str(exc))
            return
        outcome["status"] = "wrote"

    return threading.Thread(target=run, daemon=True), outcome


def _restore(
    bundle: Path, store_file: Path, gate: _Gate
) -> tuple[threading.Thread, dict[str, Any]]:
    """The restore on a thread of its own, stopping wherever `gate` is
    placed; its report lands in the returned dict."""
    report: dict[str, Any] = {}

    def run() -> None:
        gate.arm()
        report.update(restore_bundle(bundle, store_file, apply=True))

    return threading.Thread(target=run, daemon=True), report


def _until_stopped_or_done(gate: _Gate, thread: threading.Thread) -> None:
    while not gate.reached.is_set() and thread.is_alive():
        thread.join(0.02)
    assert gate.reached.is_set() or not thread.is_alive()


def _verifies(store_file: Path, keys_dir: Path) -> None:
    with Store.open(store_file, keys_dir=keys_dir, read_only=True) as opened:
        assert opened.log_verify()["status"] == "ok"


def test_an_opener_that_checked_before_a_restore_and_opens_after_its_swap_is_refused_or_seen(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    lock_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opener checks the lock and finds no restore; before it opens the
    file, a restore takes the lock, probes (no connection to see) and
    swaps its file in. With the directory locked, the opener's check was
    its shared hold, so the restore waits for it and is refused, and the
    opener writes. With the record file as the lock, the opener opens the
    restored file, checks again, finds the restore holding the lock and is
    refused; the restore finishes. Where nothing locks (``marker``) the
    second check finds the restore's marker and is refused the same way,
    in the words of a restore that did not finish (no lock tells a running
    restore from a dead one there)."""
    checked, swapped = _Gate(), _Gate()
    if lock_mode == "directory":
        monkeypatch.setattr(restore_lock, "admit", _after(checked, restore_lock.admit))
    else:
        monkeypatch.setattr(restore_lock, "_take", _after(checked, restore_lock._take))
    monkeypatch.setattr(backup, "_install_keys", _before(swapped, backup._install_keys))
    opener, opened = _opener(store_file, keys_dir, checked)
    restorer, report = _restore(bundle, store_file, swapped)

    opener.start()
    assert checked.reached.wait(_SECONDS), "the opener never checked"
    restorer.start()
    _until_stopped_or_done(swapped, restorer)
    checked.go.set()
    opener.join(_SECONDS)
    swapped.go.set()
    restorer.join(_SECONDS)

    if lock_mode == "directory":
        assert report["status"] == "refused", report
        assert "opening or reading the store" in report["detail"]
        assert opened["status"] == "wrote", opened
    else:
        assert opened["status"] == "refused", opened
        said = (
            "a restore of it is running"
            if lock_mode == "record"
            else "a restore of it did not finish"
        )
        assert said in opened["said"]
        assert report["status"] == "restored", report["detail"]
    _verifies(store_file, keys_dir)


def test_an_opener_that_connected_before_a_restores_swap_is_refused_or_seen(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    lock_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opener passed its check and has its connection, and has not read
    the store yet, so SQLite holds no lock a restore's probe could see,
    when a restore runs from start to end. With the directory locked the
    opener's hold refuses the restore. On Windows the opener holds the
    store file open, so the restore's swap is refused and changes nothing
    (seen by the swap's own rename refusal), and the opener writes into
    the store it opened. On a POSIX mount without directory locks the
    rename goes through under the open file; the opener, checking again
    once it has read, finds a store file other than the one it checked and
    is refused, so it writes nothing into the file the restore moved
    aside; where nothing locks (``marker``) too."""
    connected = _Gate()
    monkeypatch.setattr(
        store_module, "admit_store", _before(connected, store_module.admit_store)
    )
    opener, opened = _opener(store_file, keys_dir, connected)
    restorer, report = _restore(bundle, store_file, _Gate())
    inode = store_file.stat().st_ino

    opener.start()
    assert connected.reached.wait(_SECONDS), "the opener never connected"
    restorer.start()
    restorer.join(_SECONDS)
    assert not restorer.is_alive()
    connected.go.set()
    opener.join(_SECONDS)

    if lock_mode == "directory":
        assert report["status"] == "refused", report
        assert opened["status"] == "wrote", opened
        assert store_file.stat().st_ino == inode
    elif sys.platform == "win32":
        assert report["status"] == "failed", report
        windows = "Windows does not rename a file another process has open"
        assert windows in report["detail"]
        assert report["moved_aside"] == [] and report["lock"]["kept"] is False
        assert restore_lock.read_marker(store_file) is None
        assert opened["status"] == "wrote", opened
        assert store_file.stat().st_ino == inode
    else:
        assert report["status"] == "restored", report["detail"]
        assert opened["status"] == "refused", opened
        replaced = "a restore replaced the store file while it was being opened"
        assert replaced in opened["said"]
    _verifies(store_file, keys_dir)


@pytest.mark.parametrize("lock_mode", RECORD_MODES, indirect=True)
def test_an_opener_refused_behind_its_handle_lets_the_restore_swap(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    lock_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is the record file. The opener checks and finds no
    restore; a restore takes the lock and probes, and is about to swap;
    the opener opens the store file and checks again, finds the restore
    holding the lock and is refused (where nothing locks, finding its
    marker). It lets go of the file as it is refused (on Windows its
    handle), so the restore's swap goes through and the restore
    finishes."""
    checked, probed = _Gate(), _Gate()
    monkeypatch.setattr(restore_lock, "_take", _after(checked, restore_lock._take))
    monkeypatch.setattr(backup, "_swap_in", _before(probed, backup._swap_in))
    opener, opened = _opener(store_file, keys_dir, checked)
    restorer, report = _restore(bundle, store_file, probed)

    opener.start()
    assert checked.reached.wait(_SECONDS), "the opener never checked"
    restorer.start()
    assert probed.reached.wait(_SECONDS), "the restore never reached its swap"
    checked.go.set()
    opener.join(_SECONDS)
    probed.go.set()
    restorer.join(_SECONDS)

    assert opened["status"] == "refused", opened
    assert "a restore of it" in opened["said"]
    assert report["status"] == "restored", report["detail"]
    _verifies(store_file, keys_dir)


#: A dead restore's marker, as `RestoreLock.mark` writes it: the record a
#: restore that was killed after its swap leaves.
_DEAD_MARKER = {
    "format": restore_lock.MARKER_FORMAT,
    "pid": 2**22 + 7,
    "started_at": "2026-11-03T00:00:00Z",
    "bundle": "a bundle",
    "stage": "installing the keys and rewinding the head",
}


@pytest.mark.parametrize("lock_mode", ["record"], indirect=True)
@pytest.mark.parametrize(
    ("read_only", "left"),
    [(False, "empty"), (True, "empty"), (True, "unfinished")],
    ids=["writing-empty", "reading-empty", "reading-unfinished"],
)
def test_an_open_whose_second_check_meets_another_openers_check_is_not_refused(
    store_file: Path,
    keys_dir: Path,
    lock_mode: str,
    read_only: bool,
    left: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows locks the record file exclusively for every check (``msvcrt``
    has no shared lock), and the record file can be there with no restore
    running: left empty by a release whose unlink met a sharing violation,
    or holding the marker of a restore that did not finish (where the
    runbook asks for `log verify` and `doctor` before `backup unlock`). An
    open checks it, takes its handle on the store file, and as it checks
    again behind the handle another opener's first check holds the lock.
    No restore runs, and the open goes on: it looks again while the lock
    is held, and the other check lets go between two looks (here, at the
    first pause, so no timing is raced). A read of the store a restore did
    not finish is read as it stands; it was refused as one a restore was
    running on. Off Windows the test makes every check exclusive and hands
    the open a handle, as Windows does."""
    lock = restore_lock.lock_path(store_file)
    lock.write_bytes(b"" if left == "empty" else json.dumps(_DEAD_MARKER).encode())
    if sys.platform != "win32":
        real_try = restore_lock._try_lock

        def exclusive(fd: int, *, shared: bool) -> bool:
            return real_try(fd, shared=False)

        def handle(path: Path) -> int | None:
            return os.open(path, os.O_RDONLY)

        monkeypatch.setattr(restore_lock, "_try_lock", exclusive)
        monkeypatch.setattr(restore_lock, "_store_handle", handle)
    real_handle = restore_lock._store_handle
    real_pause = restore_lock._pause
    met: list[bool] = []
    others: list[int] = []

    def let_go() -> None:
        while others:
            other = others.pop()
            restore_lock._unlock(other)
            os.close(other)

    def handle_then_another_check(path: Path) -> int | None:
        found = real_handle(path)
        other = os.open(lock, os.O_RDWR | getattr(os, "O_BINARY", 0))
        others.append(other)
        met.append(restore_lock._try_lock(other, shared=True))
        return found

    def pause_then_let_go(seconds: float) -> None:
        let_go()
        real_pause(seconds)

    monkeypatch.setattr(restore_lock, "_store_handle", handle_then_another_check)
    monkeypatch.setattr(restore_lock, "_pause", pause_then_let_go)
    try:
        with Store.open(store_file, keys_dir=keys_dir, read_only=read_only) as opened:
            if read_only:
                assert opened.log_verify()["status"] == "ok"
            else:
                opened.put_memory(_memory("written past another opener's check"))
    finally:
        let_go()
    assert met == [True]


def _open_on_thread(
    store_file: Path, keys_dir: Path, gate: _Gate, *, read_only: bool
) -> tuple[threading.Thread, dict[str, str]]:
    """A read (``read_only``: `log verify`) or a writing open (one memory
    written) of the store on a thread of its own, stopping wherever `gate`
    is placed; its outcome lands in the returned dict (``done``, or
    ``refused`` and the refusal)."""
    outcome: dict[str, str] = {}

    def run() -> None:
        gate.arm()
        try:
            with Store.open(
                store_file, keys_dir=keys_dir, read_only=read_only
            ) as opened:
                if read_only:
                    opened.log_verify()
                else:
                    opened.put_memory(_memory("written by the racing opener"))
        except StoreRefusedError as exc:
            outcome.update(status="refused", said=str(exc))
            return
        outcome["status"] = "done"

    return threading.Thread(target=run, daemon=True), outcome


class _SharingViolation(PermissionError):
    """Windows' ERROR_SHARING_VIOLATION (32), as `os.replace` raises it for
    a file another handle holds open without the share for deletion. The
    class attribute stands where the platform sets none (off Windows); on
    Windows `_refusal` sets it."""

    winerror = 32


class _AccessDenied(PermissionError):
    """Windows' ERROR_ACCESS_DENIED (5), as `os.replace` raises it for a
    move an ACL denies, or for a file pending deletion: no handle's."""

    winerror = 5


def _refusal(kind: type[PermissionError], path: Path) -> PermissionError:
    """A `kind` refusing a rename of `path`, its ``winerror`` the class's on
    every platform (None for a plain ``PermissionError``)."""
    return kind(errno.EACCES, "refused", str(path), getattr(kind, "winerror", None))


@dataclass
class _Handles:
    """What `_held_handles_refuse_renames` keeps: the swap's renames of the
    store file a handle refused, and (off Windows) the handles that hold
    the file open, which a test may add SQLite's to."""

    refused: list[Path] = field(default_factory=list)
    held: set[int] = field(default_factory=set)


def _held_handles_refuse_renames(
    store_file: Path, monkeypatch: pytest.MonkeyPatch
) -> _Handles:
    """Count the swap's renames of the store file a handle refused. Off
    Windows, make Windows' rule hold for the handles an opener or a read
    takes on the store file (`restore_lock._store_handle`,
    `restore_lock._holds_by_handle`): while one is open, the swap's rename
    of the file raises the sharing violation Windows raises
    (`_SharingViolation`), and where no file stands there is no handle,
    as on Windows."""
    handles = _Handles()
    real_replace = _fsutil.replace_atomic

    def replace(source: Path, target: Path) -> None:
        if Path(source) == store_file and handles.held:
            handles.refused.append(Path(source))
            raise _refusal(_SharingViolation, Path(source))
        try:
            real_replace(source, target)
        except PermissionError:
            handles.refused.append(Path(source))
            raise

    monkeypatch.setattr(backup, "replace_atomic", replace)
    if sys.platform == "win32":
        return handles

    def handle(path: Path) -> int | None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return None
        handles.held.add(fd)
        return fd

    def closing(cls: type[Any], attribute: str) -> None:
        real_close = cls.close

        def close(self: Any) -> None:
            handles.held.discard(getattr(self, attribute))
            real_close(self)

        monkeypatch.setattr(cls, "close", close)

    monkeypatch.setattr(restore_lock, "_store_handle", handle)
    monkeypatch.setattr(restore_lock, "_holds_by_handle", lambda: True)
    closing(restore_lock._SharedHold, "_fd")
    closing(restore_lock._RecordCheck, "_handle")
    return handles


@pytest.mark.parametrize("lock_mode", ["record"], indirect=True)
@pytest.mark.parametrize("read_only", [True, False], ids=["read", "writing-open"])
def test_a_restore_whose_swap_meets_a_checks_handle_waits_for_it_and_finishes(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    lock_mode: str,
    read_only: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is the record file, on Windows. A read (or a writing open)
    checks it and finds no restore; a restore takes the lock, probes,
    copies and digests its file and reaches its swap; the read opens its
    handle on the store file and checks again behind it, finds the lock
    held, and looks again until `OPEN_WAIT_SECONDS` have passed (a held
    lock does not tell a restore from another checker), its handle open
    all the while. The swap's first rename meets the handle. It tries
    again until the check refuses the read and closes the handle, and the
    restore finishes; it failed where it had changed nothing, after
    `replace_atomic`'s 150 ms, when its probe, copy and digest took less
    than the check's wait. The wait here is 0.6 s, four times that retry.
    Windows runs it natively; elsewhere the handle refuses the rename as
    Windows' does (`_held_handles_refuse_renames`)."""
    monkeypatch.setattr(restore_lock, "OPEN_WAIT_SECONDS", 0.6)
    handles = _held_handles_refuse_renames(store_file, monkeypatch)
    checked, at_swap = _Gate(), _Gate()
    looking = threading.Event()
    real_pause = restore_lock._pause

    def pause(seconds: float) -> None:
        if threading.get_ident() == checked.thread:
            looking.set()
        real_pause(seconds)

    monkeypatch.setattr(restore_lock, "_take", _after(checked, restore_lock._take))
    monkeypatch.setattr(restore_lock, "_pause", pause)
    monkeypatch.setattr(backup, "_swap_in", _before(at_swap, backup._swap_in))
    racer, outcome = _open_on_thread(store_file, keys_dir, checked, read_only=read_only)
    restorer, report = _restore(bundle, store_file, at_swap)

    racer.start()
    assert checked.reached.wait(_SECONDS), "the read never checked"
    restorer.start()
    assert at_swap.reached.wait(_SECONDS), "the restore never reached its swap"
    checked.go.set()
    assert looking.wait(_SECONDS), "the read never looked again behind its handle"
    at_swap.go.set()
    racer.join(_SECONDS)
    restorer.join(_SECONDS)

    assert handles.refused, "the swap never met the handle"
    assert report["status"] == "restored", report["detail"]
    assert outcome["status"] == "refused", outcome
    said = "a restore is running" if read_only else "a restore of it is running"
    assert said in outcome["said"]
    _verifies(store_file, keys_dir)


def _log_end(path: Path) -> int:
    """The last seq of the log in the store file at `path`, read only."""
    conn = sqlite3.connect(_fsutil.sqlite_file_uri(path) + "?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT max(seq) FROM log").fetchone()[0])
    finally:
        conn.close()


@pytest.mark.parametrize("lock_mode", ["record"], indirect=True)
def test_an_opener_admitted_before_a_restore_took_the_lock_is_refused_once_it_has_read(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    lock_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is the record file, on Windows. A writing open passes its
    check and its check behind its handle: no restore yet. Before its
    connection reads the store, a restore takes the lock and probes (no
    connection has read the file, so there is none to see), copies and
    digests, and reaches its swap. The opener's connection reads the
    store; it checks once more, finds the restore holding the lock, and is
    refused before it reads meta or keys or writes, closing its connection
    and its handle; the swap, which met them, goes on. The file moved
    aside ends where the restore record says the replaced log ended: the
    opener wrote nothing into it. Round 4 of ENG-208 let the opener write
    there while the restore held its lock, and the restore went on once
    the opener closed. On Windows SQLite's own handle refuses the rename
    from the connect to the close; off Windows the test adds it to the
    handles that refuse (`_held_handles_refuse_renames`)."""
    monkeypatch.setattr(restore_lock, "OPEN_WAIT_SECONDS", 0.6)
    handles = _held_handles_refuse_renames(store_file, monkeypatch)
    admitted, at_swap = _Gate(), _Gate()
    went_on = threading.Event()
    connection = -1
    real_admit_store = store_module.admit_store
    real_connect = store_module._connect
    real_close = Store.close
    real_put = Store.put_memory
    real_pause = restore_lock._pause

    def mine() -> bool:
        return threading.get_ident() == admitted.thread

    def admit_store(conn: sqlite3.Connection, path: Path) -> None:
        if mine() and sys.platform != "win32":
            handles.held.add(connection)
        real_admit_store(conn, path)

    def connect(path: Path) -> sqlite3.Connection:
        try:
            return real_connect(path)
        except BaseException:
            if mine():
                handles.held.discard(connection)
            raise

    def close(self: Store) -> None:
        real_close(self)
        if mine():
            handles.held.discard(connection)

    def put_memory(self: Store, *args: Any, **kwargs: Any) -> Any:
        if mine():
            went_on.set()
        return real_put(self, *args, **kwargs)

    def pause(seconds: float) -> None:
        if mine():
            went_on.set()
        real_pause(seconds)

    monkeypatch.setattr(restore_lock, "admit", _after(admitted, restore_lock.admit))
    monkeypatch.setattr(store_module, "admit_store", admit_store)
    monkeypatch.setattr(store_module, "_connect", connect)
    monkeypatch.setattr(Store, "close", close)
    monkeypatch.setattr(Store, "put_memory", put_memory)
    monkeypatch.setattr(restore_lock, "_pause", pause)
    monkeypatch.setattr(backup, "_swap_in", _before(at_swap, backup._swap_in))
    opener, opened = _open_on_thread(store_file, keys_dir, admitted, read_only=False)
    restorer, report = _restore(bundle, store_file, at_swap)

    opener.start()
    assert admitted.reached.wait(_SECONDS), "the opener was never admitted"
    restorer.start()
    assert at_swap.reached.wait(_SECONDS), "the restore never reached its swap"
    admitted.go.set()
    assert went_on.wait(_SECONDS), "the opener never checked again nor wrote"
    at_swap.go.set()
    opener.join(_SECONDS)
    restorer.join(_SECONDS)

    assert handles.refused, "the swap never met the opener's handles"
    assert report["status"] == "restored", report["detail"]
    assert opened["status"] == "refused", opened
    assert "a restore of it is running" in opened["said"]
    (aside,) = [m["to"] for m in report["moved_aside"] if m["from"] == str(store_file)]
    entry = json.loads(Path(report["record"]).read_text())["restores"][-1]
    assert _log_end(Path(aside)) == entry["log_end_before"]["seq"]
    _verifies(store_file, keys_dir)


@pytest.mark.parametrize("lock_mode", ["record"], indirect=True)
@pytest.mark.parametrize("read_only", [True, False], ids=["read", "writing-open"])
def test_an_open_that_finds_no_file_between_the_swaps_moves_is_refused(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    lock_mode: str,
    read_only: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is the record file, on Windows. A read (or a writing open)
    checks it before a restore takes it, then stalls across the restore's
    probe, copy and digest, and opens its handle in the moment between the
    swap's first move and its last rename, when no file stands at the
    name: there is no handle. It looks at the lock again before it
    connects, finds the restore holding it, and is refused; the restore
    finishes. Before, a read went on with no hold and no second check,
    connected once the restored file was in place, and read it against
    keys and a head the restore had not yet installed (`tampered` for a
    healthy restore); a writing open's SQLite made an empty file at the
    vacated name, which left every move of the swap standing. Off Windows
    the test makes the handle Windows' (`_held_handles_refuse_renames`)."""
    monkeypatch.setattr(restore_lock, "OPEN_WAIT_SECONDS", 0.6)
    _held_handles_refuse_renames(store_file, monkeypatch)
    checked, connecting, first_moved, installing = _Gate(), _Gate(), _Gate(), _Gate()
    connect = "connect_read_only" if read_only else "admit_store"
    monkeypatch.setattr(restore_lock, "_take", _after(checked, restore_lock._take))
    monkeypatch.setattr(
        store_module, connect, _before(connecting, getattr(store_module, connect))
    )
    monkeypatch.setattr(backup, "_move_aside", _after(first_moved, backup._move_aside))
    monkeypatch.setattr(
        backup, "_install_keys", _before(installing, backup._install_keys)
    )
    racer, outcome = _open_on_thread(store_file, keys_dir, checked, read_only=read_only)
    restorer, report = _restore(bundle, store_file, first_moved)

    racer.start()
    assert checked.reached.wait(_SECONDS), "the open never checked"
    connecting.thread = checked.thread
    restorer.start()
    assert first_moved.reached.wait(_SECONDS), "the swap never moved the store file"
    installing.thread = first_moved.thread
    assert not store_file.exists()
    checked.go.set()
    _until_stopped_or_done(connecting, racer)
    first_moved.go.set()
    _until_stopped_or_done(installing, restorer)
    connecting.go.set()
    racer.join(_SECONDS)
    installing.go.set()
    restorer.join(_SECONDS)

    assert outcome.get("status") == "refused", outcome
    said = "a restore is running" if read_only else "a restore of it is running"
    assert said in outcome["said"]
    assert report["status"] == "restored", report["detail"]
    _verifies(store_file, keys_dir)


@pytest.mark.parametrize(
    ("refusal", "said"),
    [
        (_SharingViolation, "a process has"),
        (_AccessDenied, "cannot move"),
        (PermissionError, "cannot move"),
    ],
    ids=["held-open", "access-denied", "no-winerror"],
)
def test_the_swap_waits_out_only_a_file_held_open(
    bundle: Path,
    store_file: Path,
    keys_dir: Path,
    refusal: type[PermissionError],
    said: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swap's first rename refused by a sharing violation (Windows'
    ERROR_SHARING_VIOLATION: another handle holds the file open) is tried
    again until a check's wait and the margin have passed, here 0.2 s
    each, and the swap then fails naming the handle. One refused for any
    other reason (ERROR_ACCESS_DENIED: an ACL that denies the move, or a
    file pending deletion; or a POSIX EACCES, with no ``winerror``) fails
    at the first refusal in its own words, since no wait lets it through:
    it was waited out for 3.2 s and then named a handle that was not
    there. Either way nothing moved and the store opens as it was. The
    rename raises the refusal on every platform."""
    monkeypatch.setattr(restore_lock, "OPEN_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(backup, "_SWAP_MARGIN_SECONDS", 0.2)
    tries: list[float] = []
    real_replace = _fsutil.replace_atomic

    def replace(source: Path, target: Path) -> None:
        if Path(source) == store_file:
            tries.append(time.monotonic())
            raise _refusal(refusal, Path(source))
        real_replace(source, target)

    monkeypatch.setattr(backup, "replace_atomic", replace)
    report = restore_bundle(bundle, store_file, apply=True)

    assert report["status"] == "failed", report
    detail = str(report["detail"])
    assert said in detail
    assert report["moved_aside"] == [] and report["lock"]["kept"] is False
    if refusal is _SharingViolation:
        assert len(tries) > 2 and tries[-1] - tries[0] >= 0.35, tries
        assert "close every session" in detail
    else:
        assert len(tries) == 1, tries
        assert "a process has" not in detail
    _verifies(store_file, keys_dir)


def test_the_swaps_first_move_waits_a_checks_whole_wait_and_the_margin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a clock the test keeps (`backup.time`), a handle that holds the
    store file for a check's whole wait (`restore_lock.OPEN_WAIT_SECONDS`)
    and 0.9 s more, as a check on a loaded machine may, does not fail the
    swap's first move; one held past the wait and the margin
    (`backup._SWAP_MARGIN_SECONDS`) fails it, naming the handle; and a
    later move is not waited for. The margin is so pinned whatever the
    machine's load: a swap with none failed the first."""
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(
        backup, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
    )
    source, target = tmp_path / STORE_FILENAME, tmp_path / "aside"
    wait = restore_lock.OPEN_WAIT_SECONDS

    def move(*, first: bool, held_for: float) -> str:
        clock[0] = 0.0
        source.write_bytes(b"store")
        target.unlink(missing_ok=True)

        def replace(src: Path, dst: Path) -> None:
            if clock[0] < held_for:
                raise _refusal(_SharingViolation, Path(src))
            os.replace(src, dst)

        monkeypatch.setattr(backup, "replace_atomic", replace)
        try:
            backup._move_aside(source, target, first=first)
        except backup.BackupError as exc:
            return str(exc)
        return "moved"

    assert move(first=True, held_for=wait + 0.9) == "moved"
    margin = backup._SWAP_MARGIN_SECONDS
    assert "a process has" in move(first=True, held_for=wait + margin + 0.5)
    assert "a process has" in move(first=False, held_for=0.1)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' rename rule")
def test_windows_will_not_rename_a_store_file_a_read_holds_open(
    store_file: Path, keys_dir: Path
) -> None:
    """The fact the Windows half rests on, on the platform: a read's hold
    on the store file (`restore_lock.reading`) is a handle Windows will not
    rename the file under, the refusal a sharing violation, the one the
    swap waits out (`backup._SHARING_VIOLATION`), and the rename works
    once it is closed."""
    aside = store_file.with_name(STORE_FILENAME + ".aside")
    hold = restore_lock.reading(store_file)
    assert hold is not None
    try:
        with pytest.raises(PermissionError) as refused:
            os.replace(store_file, aside)
        assert getattr(refused.value, "winerror", None) == backup._SHARING_VIOLATION
    finally:
        hold.close()
    os.replace(store_file, aside)
    os.replace(aside, store_file)
