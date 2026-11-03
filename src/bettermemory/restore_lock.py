"""The restore lock: a restore holds its store for its whole length, and
every writing open and every read of the store respects it.

`backup restore --yes` moves the store file aside and renames the
bundle's copy into its place, then installs keys, rewinds the head
checkpoint, replaces the origin anchor and rebinds the key ring
(`backup.restore_bundle`). A process that has the store open while that
runs writes into the file that was moved aside and moves the head on
past the restored log, which then reads ``tampered``; one that opens it
part way meets a store whose keys or head are not yet the restored
file's, and the owner's opener may rekey it or create a new store where
the file was moved away. The daemon and the spawn locks keep a daemon
out (`backup._daemon_locks`); this module keeps out everything else that
writes: `bettermemory serve`, the shim's in-process fallback and the
CLI's writing commands, which all open the store through
`store._connect`.

Two halves, each closing one order of the race.

* **A writer that opens after the restore began is refused.** The lock
  is the store file's directory, locked with ``flock`` (`admit`,
  `acquire`): a restore holds it exclusively for its whole length, and
  every writing open takes it shared first thing in `store._connect`,
  before SQLite opens the store file (`admit`), and is refused, after a
  bounded wait of `OPEN_WAIT_SECONDS`, while a restore holds it:
  `RestoreLockedError`, a `store_schema.StoreRefusedError`, so every
  opener reports it in its own one line as it reports a file it does
  not admit. Refused, not kept waiting until the restore ends: a
  restore's length grows with the store (it verifies and copies all of
  it), and a writer that waited would then write into a store rewound
  under it. The wait only rides out the moment a restore or `backup
  unlock` takes the lock while no restore runs. The directory, not a
  file: it is the one thing every spelling of the store's path (a
  symbolic link, another case on a case-insensitive disk) reaches and
  the swap does not replace, and locking it makes nothing. A lock file
  every open made would sit beside the store for good: left beside a
  file an opener then refused as no store, and carried into a
  repository with a store kept there. The lock is that one store's: a
  store file is always ``memory.sqlite`` in its directory
  (`store_schema.store_path`), so no other store shares it.
* **A writer that opened before the restore began is found.** Every
  writing connection holds SQLite's shared lock on the store file for as
  long as it is open: a WAL-mode connection keeps it from its first read
  to its close. The restore, holding the lock, opens the store itself in
  SQLite's exclusive locking mode and reads (`probe`), which takes
  SQLite's exclusive lock on the file and fails with SQLITE_BUSY while
  any other connection has the store open; the restore is then refused.
  The two cannot cross: an opener holds the lock shared from before it
  opens the file until `_connect` returns, after it has switched the
  store to WAL and read it once more (the shared lock SQLite keeps
  from then on: a store in rollback-journal mode, switched by this
  open, holds none between the switch and its next read), so the
  restore's exclusive `acquire` waits for it, and its probe then sees
  the connection.

Reads too (ENG-208). A read-only open (`store.Store.open` with
``read_only``: `log verify`, `log export`, `stats`, `memory history`,
`doctor`'s store check, `security audit`'s key check) reads the store
file, then the keys, the head and the anchor beside it, and a restore
that ran in between had it read one restore's file against another's
head: `tampered` for a healthy restore. A read takes the lock shared too
(`reading`), and holds it until the store closes, not only through the
open: a restore waits `RESTORE_WAIT_SECONDS` for it and is refused,
naming a process that opens or reads the store. A read refused while a
restore holds the lock is `RestoreRunningError` (exit 75 from the
read-only commands).

The marker. A restore keeps a JSON record in ``<store>.restore-lock``
beside the store file (`lock_path`, `RestoreLock.mark`): its process,
when it began, the bundle and the step it has reached. Only a restore
makes the file, owner-only, and one that finishes, or fails having
changed nothing, removes it (`RestoreLock.release`). A marker that is
there while no restore holds the lock is a restore that did not finish:
it was killed, or it failed after the store file was swapped, and the
store may be part restored. Every writing open refuses it, naming the
two ways out: running the restore again (which takes the marker over and
finishes the job) or `bettermemory backup unlock --yes` (`unlock`), which
removes it and leaves the store as it stands. An opener reads the
marker and never makes it.

The restore opens the restored store itself, to check its log: `admit`
lets an open through when the thread that holds the lock makes it
(`_HELD`, keyed by the locked file's device and inode, so the spelling
of the path does not matter). Another thread of the same process is
refused as any other opener is.

POSIX locks the directory with ``flock`` (shared and exclusive).
Windows cannot lock a directory, and has only exclusive byte-range locks
(``msvcrt.locking``): there the marker file is the lock, locked on one
byte far past the marker so the marker stays readable, and only a
restore makes it. An opener or a read checks it when it is there and
cannot hold it shared; what it holds instead is the store file, open
(`_store_handle`, since ENG-208): Windows refuses to rename a file that
is open without the share for deletion, which Python's ``os.open`` never
gives, so no restore's swap can move the store file aside while it is
open. The order matters: the opener checks, opens the file, and checks
again behind the handle (`_record_admission`, `reading`), and a writing
open checks once more when its connection has read the store, before it
reads meta or keys (`_RecordCheck.confirm`), as a POSIX mount without
directory locks does. A restore that took the lock before the last of
those checks refuses the opener there, before it reads or writes a
record; one that swapped its file in before the handle opened still
held the lock at the next check, or had finished; and one that takes
the lock after a writing open's connection has read the store meets
that connection at its probe (`probe`: every WAL connection that has
read holds SQLite's shared lock) and is refused. A writing open keeps
the handle until SQLite's own handle is open, a read until its store
closes. A swap that meets a handle tries its first rename again for
`OPEN_WAIT_SECONDS` and a margin (`backup._move_aside`), since the
opener or read holding it is checking and, finding the restore, refuses
and lets go; a handle open past that (a read open since before the
restore, which the probe need not see, since a read-only open of a
closed WAL store holds no lock on it, or an opener stalled between its
checks for longer than the margin) fails the swap, which changes
nothing. So no opener is seen part way, and none writes after the
probe of a restore that goes on: what it wrote before is in the file
the restore moves aside, and in the log end its record keeps
(``log_end_before``, read after the probe). Where `Store.open` has
just found the store file and no handle opens it, the swap is between
its moves (`backup._swap_in`): the opener or read looks at the lock
again before it connects and is refused while the restore holds it
(`_open_store_handle`), so it neither reads the restored file against
keys and a head not yet installed nor has SQLite make an empty file in
the name the swap vacated. Every check there locks the file
exclusively, so the check behind the handle meets another opener's
first check now and then wherever the record file is there to meet
(one a release could not remove, a sharing violation, or the marker of
a restore that did not finish): it looks again while the lock is held,
for up to
`OPEN_WAIT_SECONDS`, since a checker lets go in an instant and a
restore holds on, and a marker alone does not tell a running restore
from a dead one (`_behind_handle`).

A POSIX directory whose ``flock`` fails as unsupported (ENOTSUP or
EOPNOTSUPP on a mount without locks, EINVAL, ENOLCK: any errno but the
ones for a lock another holds) is put in that same record mode for the
process, and the log says so once (`_take`); only EWOULDBLOCK and EAGAIN
read as a restore that holds the lock. A rename there does not care what
has the file open, so a writing open checks again once its connection
has read the store, before it reads meta or keys (`_RecordCheck`), and
is refused when a restore holds the lock then, left its record, or
swapped a file in since the first check (the store file's device and
file number). What no check there can see is a connection that opened
the file before a restore's swap and had not read it at the restore's
probe: nothing on such a mount holds a lock a restore could wait for,
SQLite's own included (a ``nolocks`` NFS mount answers ENOTSUP to every
lock, mount_nfs(8)), and SQLite runs WAL only where its locks hold
(https://sqlite.org/wal.html, "WAL does not work over a network
filesystem").

Every writer this package starts opens through `store._connect`, save
the daemon's handover note (`daemon._tell_successor`, a meta value
through its own ``mode=rw`` connection), which only a live daemon of the
store's state directory writes; a restore holds that daemon's lock and
the spawn lock for its whole length (`backup._daemon_locks`), so none
runs during a restore. A process that writes the file through a SQLite
connection of its own, not this package's code, takes no lock this
module knows of, and nothing this side of the file system can hold it
off; the restore's probe still refuses while it has the file open and
has read it, and what it writes is bounded as any write without the key
is (SECURITY.md, tamper evidence): a row it appends carries no MAC the
store's key made, and a record it changes no row accounts for. Rows it
writes into the restored file before the restore's own `log verify`
(run on the restored store before it reports ``restored``) make that
verify read ``tampered`` (`tests/test_restore_readers.py`). That verify
does not see the rest. A connection that opened the file before the
probe holds the moved-aside file (its inode) but opens ``-wal`` and
``-shm`` by name, so after the swap it pairs the moved-aside file with
the restored file's WAL and shared memory: what it writes, or a
checkpoint it runs, can corrupt either file or copy the restored WAL's
frames into the moved-aside one, and no check on the log tells that
apart from damage. A write after the verify comes after it, and the
next `log verify` of the file written reads its rows as any write
without the key. Neither is the restore's to catch. A copy of the file
by hand is not a writer.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ._fsutil import fsync_file, sqlite_file_uri
from .store_admission import StoreRefusedError

LOCK_SUFFIX = ".restore-lock"
MARKER_FORMAT = "bettermemory.restore_lock"
#: How long a writing open waits for a restore lock someone holds before
#: it is refused: long enough for the instant a restore or `backup
#: unlock` takes it while no restore runs, not for a restore.
OPEN_WAIT_SECONDS = 2.0
#: How long a restore, or `backup unlock`, waits to take the lock while
#: openers hold it shared: each holds it only through `store._connect`.
RESTORE_WAIT_SECONDS = 10.0
_POLL_SECONDS = 0.05
#: A marker is a few hundred bytes; what is past this is not read.
_MARKER_CAP_BYTES = 1 << 16
#: The byte Windows locks, past any marker.
_WINDOWS_LOCK_OFFSET = 1 << 20
#: SQLITE_BUSY and SQLITE_LOCKED: another connection holds the file.
_HELD_ELSEWHERE_CODES = frozenset({5, 6})
_BINARY = getattr(os, "O_BINARY", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
#: Where the lock is taken: the store's directory, or (Windows) the marker.
_LOCKS_THE_DIRECTORY = sys.platform != "win32"

#: The restore locks this process holds, by the locked file's device and
#: inode: an open of the store by the restore's own thread is let through.
_HELD: dict[tuple[int, int], RestoreLock] = {}
#: flock's errnos for a lock another holds (and a call interrupted, tried
#: again): any other is a filesystem that cannot lock (`_Unlockable`).
_RETRY_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EINTR})
#: The directories this process found ``flock`` cannot lock, by device and
#: inode: their restore lock is the record file, as on Windows (`_take`).
_RECORD_MODE_DIRS: set[tuple[int, int]] = set()

log = logging.getLogger(__name__)


def lock_path(store_file: Path) -> Path:
    """``<store file>.restore-lock``, beside the store file: the marker a
    restore keeps (and on Windows the file it locks)."""
    path = Path(store_file)
    return path.with_name(path.name + LOCK_SUFFIX)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


class _Unlockable(Exception):
    """``flock`` does not lock this file where it lives: an errno other
    than the ones for a lock another holds (ENOTSUP or EOPNOTSUPP on a
    mount without locks, EINVAL, ENOLCK)."""

    def __init__(self, error: OSError) -> None:
        super().__init__(f"{error.strerror or error} (errno {error.errno})")


def _try_lock(fd: int, *, shared: bool) -> bool:
    """Take the lock on `fd` without waiting; False when another holds it
    (or another open file of it in this process does). Raises
    `_Unlockable` when ``flock`` cannot lock the file at all."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    else:
        import fcntl

        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _RETRY_ERRNOS:
                return False
            raise _Unlockable(exc) from exc
        return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


def _lock_within(fd: int, *, shared: bool, wait: float) -> bool:
    deadline = time.monotonic() + wait
    while not _try_lock(fd, shared=shared):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)
    return True


def _file_key(fd: int) -> tuple[int, int]:
    found = os.fstat(fd)
    return (found.st_dev, found.st_ino)


@dataclass
class _Target:
    """What the restore lock of one store file is taken on: an open file
    of the store's directory, or of the record file (`record`: Windows,
    or a directory ``flock`` cannot lock), its device and inode (`_HELD`'s
    key), whether the lock was taken (`taken`; True with nothing taken
    where no lock works at all, and then only the record speaks), and
    whether the caller's own hold covers it (`mine`)."""

    fd: int
    key: tuple[int, int]
    record: bool
    taken: bool = False
    mine: bool = False


def _open_record(store_file: Path, *, create: bool) -> _Target | None:
    """The record file as the lock's target, made (owner-only) only when
    `create`; None when it is not there and not `create`."""
    flags = os.O_RDWR | _BINARY | (os.O_CREAT if create else 0)
    try:
        fd = os.open(lock_path(store_file), flags, 0o600)
    except FileNotFoundError:
        if create:
            raise
        return None
    try:
        return _Target(fd, _file_key(fd), record=True)
    except BaseException:
        os.close(fd)
        raise


def _open_target(store_file: Path, *, create: bool) -> _Target | None:
    """What the restore lock is taken on: the store's directory, or the
    record file on Windows and for a directory this process found
    ``flock`` cannot lock (`_RECORD_MODE_DIRS`). Raises OSError when the
    directory cannot be opened."""
    if _LOCKS_THE_DIRECTORY:
        fd = os.open(Path(store_file).parent, _DIRECTORY_FLAGS)
        try:
            key = _file_key(fd)
        except BaseException:
            os.close(fd)
            raise
        if key not in _RECORD_MODE_DIRS:
            return _Target(fd, key, record=False)
        os.close(fd)
    return _open_record(store_file, create=create)


def _take(
    store_file: Path,
    *,
    create: bool,
    shared: bool,
    wait: float,
    mine: Callable[[tuple[int, int]], bool],
) -> _Target | None:
    """Open what the restore lock is on (`_open_target`) and take it within
    `wait`: None when there is nothing to open (no record file, in record
    mode and not `create`), the target untaken and ``mine`` when `mine`
    names its key, else the target with ``taken`` saying whether the lock
    was had. A directory whose ``flock`` fails as unsupported
    (`_Unlockable`) is put in record mode, as on Windows, and the log says
    so once: the record file is opened and locked instead, and where that
    cannot be locked either nothing is taken and only the record refuses.
    Raises OSError when the target cannot be opened."""
    target = _open_target(store_file, create=create)
    if target is not None and not target.record:
        try:
            if mine(target.key):
                target.mine = True
                return target
            target.taken = _lock_within(target.fd, shared=shared, wait=wait)
            return target
        except _Unlockable as exc:
            os.close(target.fd)
            if target.key not in _RECORD_MODE_DIRS:
                _RECORD_MODE_DIRS.add(target.key)
                log.warning(
                    "%s: flock cannot lock this directory (%s); the restore lock "
                    "of its store is the record file %s, as on Windows",
                    Path(store_file).parent,
                    exc,
                    lock_path(store_file),
                )
            target = _open_record(store_file, create=create)
        except BaseException:
            os.close(target.fd)
            raise
    if target is None:
        return None
    try:
        if mine(target.key):
            target.mine = True
            return target
        try:
            target.taken = _lock_within(target.fd, shared=shared, wait=wait)
        except _Unlockable:
            target.taken = True
        return target
    except BaseException:
        os.close(target.fd)
        raise


def _this_thread_holds(key: tuple[int, int]) -> bool:
    """The restore lock `key` names is held by this thread: the restore's
    own opens of its store go through."""
    held = _HELD.get(key)
    return held is not None and held.thread == threading.get_ident()


def _read_marker(fd: int) -> dict[str, Any] | None:
    """The marker the file at `fd` holds: None when it is empty, the
    record when it reads as one, an empty dict for anything else there (a
    marker cut short by a crash still means a restore did not finish)."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, _MARKER_CAP_BYTES)
    except OSError as exc:
        return {"unreadable": str(exc)}
    if not data.strip():
        return None
    try:
        document = json.loads(data)
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def read_marker(store_file: Path) -> dict[str, Any] | None:
    """The marker beside the store, read without taking the lock or making
    the file: None when there is none or it is empty."""
    try:
        fd = os.open(lock_path(store_file), os.O_RDONLY | _BINARY)
    except OSError:
        return None
    try:
        return _read_marker(fd)
    finally:
        os.close(fd)


def describe(marker: dict[str, Any]) -> str:
    """What a marker says, as a refusal quotes it: the process, when it
    began, the bundle and the step it reached, each only when present."""
    from .history import printable

    parts: list[str] = []
    started = marker.get("started_at")
    pid = marker.get("pid")
    if isinstance(started, str):
        parts.append(f"started {started}")
    if isinstance(pid, int) and not isinstance(pid, bool):
        parts.append(f"by process {pid}")
    bundle = marker.get("bundle")
    if isinstance(bundle, str):
        parts.append(f"from {bundle}")
    stage = marker.get("stage")
    if isinstance(stage, str):
        parts.append(f"stopped at {stage}")
    return printable(", ".join(parts) if parts else "no record of it reads")


class RestoreLockedError(StoreRefusedError):
    """A writing open of a store a restore holds (``running``), one a
    restore that did not finish left its marker beside, or one a restore
    swapped the store file under while it opened (``replaced``, where the
    lock is the record file: `_RecordCheck`). A
    `store_schema.StoreRefusedError`, refused before the file is opened,
    so every opener reports it in its one line as it reports a store file
    it does not admit (`cli._common.open_owner_store`: `serve`, the
    shim's fallback, the CLI's writing commands, the daemon)."""

    def __init__(
        self,
        store_file: Path,
        lock: Path,
        marker: dict[str, Any] | None,
        *,
        running: bool,
        replaced: bool = False,
    ) -> None:
        from .history import printable

        self.store_file = store_file
        self.lock = lock
        self.marker = marker
        self.running = running
        if replaced:
            why = (
                "a restore replaced the store file while it was being opened "
                f"(its record is {lock}); open it again"
            )
        elif running:
            why = (
                f"a restore of it is running (its record is {lock}); open it "
                "again when the restore has finished"
            )
        else:
            bundle = (marker or {}).get("bundle")
            again = printable(
                f"`bettermemory backup restore {bundle} --yes`"
                if isinstance(bundle, str)
                else "`bettermemory backup restore BUNDLE --yes`"
            )
            why = (
                f"a restore of it did not finish ({describe(marker or {})}; "
                f"{lock} keeps its record), and the store may be part restored. "
                f"Run the restore again to finish it ({again}), or open the "
                "store as it stands after `bettermemory backup unlock --yes`"
            )
        super().__init__(Path(store_file), why)


#: The exit status of a command that reads the store and found a restore
#: running: EX_TEMPFAIL, as for a daemon that found its store served
#: (`_daemon_client.DAEMON_HELD_STATUS`), apart from the 1 `log verify`
#: gives a log that does not verify.
RESTORE_RUNNING_STATUS = 75


class RestoreRunningError(StoreRefusedError):
    """A read of a store a restore holds (`reading`): refused, not read
    part way, where its file, keys and head may not yet agree and `log
    verify` would call a healthy restore tampered."""

    def __init__(self, store_file: Path) -> None:
        super().__init__(
            Path(store_file), "a restore is running; try again when it has finished"
        )


class Closeable(Protocol):
    """What a hold on the restore lock is to its owner: a thing to close
    (`_SharedHold`, `_RecordCheck`)."""

    def close(self) -> None: ...


class _SharedHold:
    """A hold that keeps every restore of a store off while it lasts: an
    opener's or a reader's shared ``flock`` on the store's directory, or
    (Windows) a reader's open handle on the store file itself
    (`_store_handle`), which Windows will not let a restore's swap rename.
    `close` gives it back, and `store._connect` calls it on its way out,
    raised or not, so a failed open does not hold the lock for as long as
    something keeps its traceback (a restore would wait on it and name
    another restore); a reader's store calls it as it closes. An object
    never closed gives it back when it goes."""

    __slots__ = ("_fd",)

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def confirm(self) -> None:
        """Nothing to check again: the hold has spanned the whole open."""

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)

    def __del__(self) -> None:
        self.close()


def _store_handle(store_file: Path) -> int | None:
    """Windows: an open handle on the store file, or None (no file there
    yet, or one this account cannot open). Python opens a file there
    sharing reading and writing but not deletion (``_wopen``, the C
    runtime's ``_SH_DENYNO``), and Windows refuses to rename a file that
    is open without that share, so while the handle is open no restore's
    swap (`backup._swap_in`) can move the store file aside: the swap
    tries its first rename again for as long as a check behind the handle
    can take (`backup._move_aside`), then fails and puts back what it
    moved. Elsewhere None: POSIX renames a file whatever has it open. A
    caller that finds no file to hold looks at the lock again
    (`_open_store_handle`)."""
    if not _holds_by_handle():
        return None
    try:
        return os.open(store_file, os.O_RDONLY | _BINARY)
    except OSError:
        return None


def _holds_by_handle() -> bool:
    """Whether an opener or a read holds the store file by a handle on it
    (`_store_handle`): Windows, where an open handle keeps a restore's
    swap from renaming the file. Apart, so a test can make it hold off
    Windows."""
    return sys.platform == "win32"


def _open_store_handle(
    store_file: Path,
) -> tuple[int | None, str, dict[str, Any] | None]:
    """`_store_handle`, for an opener or a read past its first check, with
    the swap's gap looked at: ``(handle, state, record)``. On Windows a
    store file `Store.open` has just found and no handle opens is a file
    this account cannot open, or, the case that matters, the moment
    between a restore's first move and its last rename (`backup._swap_in`),
    when no file stands at the name. A read that connected once the
    restored file was in place read it against keys and a head the restore
    had not yet installed (`tampered` for a healthy restore), and a
    writing open's SQLite made an empty file at the vacated name, which
    left every move of the swap standing. So with no handle the lock is
    read again, as behind a handle (`_behind_handle`), and its state comes
    back for the caller to refuse while a restore runs; with none running
    the handle is tried once more (the restore has finished, its file in
    place). ``CLEAR`` with a handle, or with None where none can be held:
    off Windows, or a file this account cannot open."""
    handle = _store_handle(store_file)
    if handle is not None or not _holds_by_handle():
        return handle, CLEAR, None
    state, record = _behind_handle(store_file)
    if state != CLEAR:
        return None, state, record
    return _store_handle(store_file), CLEAR, None


def _identity(path: Path) -> tuple[int, int] | None:
    """The file at `path`, as its device and file number, or None when
    there is none or the filesystem numbers no file (0)."""
    try:
        found = os.stat(path)
    except OSError:
        return None
    return (found.st_dev, found.st_ino) if found.st_ino else None


def _refuse_restored(store_file: Path, *, wait: float) -> None:
    """Raise `RestoreLockedError` when a restore holds the store's lock
    past `wait` seconds or a restore that did not finish left its record
    (`restore_state`); return when neither."""
    state, record = restore_state(store_file, wait=wait)
    if state != CLEAR:
        raise RestoreLockedError(
            Path(store_file), lock_path(store_file), record, running=state == RUNNING
        )


class _RecordCheck:
    """A writing open's admission where the restore lock is the record
    file (Windows, or a POSIX directory ``flock`` cannot lock), which an
    opener cannot hold shared: what keeps a restore that took the lock
    after `admit`'s check from going unseen.

    On Windows that is the handle on the store file `admit` opens after
    its check, with the check made once more behind it
    (`_record_admission`) and again at `confirm`, once the connection has
    read the store: a restore that took the lock before either refuses
    the open there, before it reads or writes a record, and its swap,
    which cannot rename the file while the handle or SQLite's own handle
    is open, goes on once the refused open lets go (`backup._move_aside`);
    one that swapped its file in before the handle opened held the lock
    at the second check, or had finished; and one that takes the lock
    after `confirm` meets the connection at its probe and is refused.
    `close` closes the handle once `store._connect` has its connection.

    Elsewhere (a POSIX mount where ``flock`` cannot lock a directory) a
    rename does not care what has the file open, so `confirm` checks
    again once the connection has read the store, before the open reads
    its meta or its keys: a restore holding the lock then, or one that
    left its record, refuses the open as `admit` would have, and so does
    a store file that is not the one there at `admit` (its device and
    file number differ: a restore swapped it in meanwhile, and the
    connection may be reading the file moved aside)."""

    __slots__ = ("_before", "_handle", "_store_file")

    def __init__(self, store_file: Path, handle: int | None) -> None:
        self._store_file = Path(store_file)
        self._handle = handle
        self._before = _identity(self._store_file) if handle is None else None

    def confirm(self) -> None:
        """Refuse the open (`RestoreLockedError`) when a restore ran into
        it since `admit`'s check. On Windows the handle has kept every
        restore's swap off since then, not the restore: one that took the
        lock after the check behind the handle and probed before the
        connection read the store saw no connection, and waits at its swap
        for the handles to close. So the lock is read once more, as behind
        the handle (`_behind_handle`), and a restore holding it, or one
        that left its record, refuses the open before it reads meta or
        keys or writes; a restore that takes the lock later sees the
        connection at its probe."""
        if self._handle is not None:
            state, record = _behind_handle(self._store_file)
            if state != CLEAR:
                raise RestoreLockedError(
                    self._store_file,
                    lock_path(self._store_file),
                    record,
                    running=state == RUNNING,
                )
            return
        _refuse_restored(self._store_file, wait=OPEN_WAIT_SECONDS)
        after = _identity(self._store_file)
        if self._before is not None and after is not None and after != self._before:
            raise RestoreLockedError(
                self._store_file,
                lock_path(self._store_file),
                None,
                running=False,
                replaced=True,
            )

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            with contextlib.suppress(OSError):
                os.close(handle)

    def __del__(self) -> None:
        self.close()


def _behind_handle(store_file: Path) -> tuple[str, dict[str, Any] | None]:
    """`restore_state` for the check made again behind a Windows handle on
    the store file (`_record_admission`, `reading`, `_RecordCheck.confirm`),
    and where no handle opens a file just found (`_open_store_handle`).
    Windows locks the
    record file exclusively for every check (``msvcrt`` has no shared
    lock: `_try_lock`), so another opener's or reader's first check, made
    the instant this one looks, holds it too, whenever the record file is
    there to be held: left empty (a release whose unlink met a sharing
    violation, `RestoreLock.release`), or holding the marker of a restore
    that did not finish, which is when the runbook asks for `log verify`
    and `doctor` before `backup unlock --yes`. A marker does not tell a
    running restore from that dead one, and a held lock does not tell a
    restore from a checker; how long it is held does. So the check looks
    again while the lock is held, until `OPEN_WAIT_SECONDS` have passed
    (as `admit`'s first check waits), and answers what it then finds: a
    checker lets go in an instant, and a restore holds on. The handle is
    open while it looks, so a restore that reaches its swap meanwhile
    cannot move the file aside yet: its first rename is tried again until
    this wait and a margin have passed (`backup._move_aside`), so the
    restore goes on once this check refuses and closes the handle, and
    fails, changing nothing, only at a handle open past that. Read when
    called, so a patch reaches it."""
    deadline = time.monotonic() + OPEN_WAIT_SECONDS
    while True:
        state, record = restore_state(store_file, wait=0.0)
        if state != RUNNING or time.monotonic() >= deadline:
            return state, record
        _pause(_POLL_SECONDS)


def _pause(seconds: float) -> None:
    """Sleep between two looks at the lock (`_behind_handle`); apart, so a
    test can act between the looks."""
    time.sleep(seconds)


def _record_admission(store_file: Path) -> _RecordCheck:
    """`admit`'s answer where the lock is the record file, once its check
    found no restore: on Windows the store file held open and the check
    made again behind the handle (`_RecordCheck`, `_behind_handle`)."""
    handle, state, record = _open_store_handle(store_file)
    if state != CLEAR:
        raise RestoreLockedError(
            Path(store_file), lock_path(store_file), record, running=state == RUNNING
        )
    check = _RecordCheck(store_file, handle)
    if handle is not None:
        try:
            state, record = _behind_handle(store_file)
            if state != CLEAR:
                raise RestoreLockedError(
                    Path(store_file),
                    lock_path(store_file),
                    record,
                    running=state == RUNNING,
                )
        except BaseException:
            check.close()
            raise
    return check


def admit(store_file: Path) -> _SharedHold | _RecordCheck | None:
    """Admit a writing open of the store file `store_file`: the first thing
    `store._connect` does, before SQLite opens the file. Makes nothing.
    Returns what keeps a restore off the open, which the caller confirms
    once the connection has read the store (`confirm`) and closes on its
    way out, raised or not: the shared hold on the restore lock (the
    store's directory), or, where the lock is the record file (Windows,
    or a directory ``flock`` cannot lock: module docstring), which an
    opener cannot hold shared, a `_RecordCheck`, which on Windows holds
    the store file open from its check on and elsewhere checks again at
    `confirm`. None when there is nothing to keep off: this thread holds
    the lock itself (the restore's own open), or the store's directory
    cannot be opened (no restore by this account can lock it either).

    Raises `RestoreLockedError` when a restore holds the lock past
    `OPEN_WAIT_SECONDS`, or when a restore that did not finish left its
    marker."""
    lock = lock_path(store_file)
    try:
        target = _take(
            store_file,
            create=False,
            shared=True,
            wait=OPEN_WAIT_SECONDS,
            mine=_this_thread_holds,
        )
    except OSError:
        return None
    if target is None:
        # The lock is the record file, and there is none: no restore has
        # begun.
        return _record_admission(store_file)
    try:
        if target.mine:
            os.close(target.fd)
            return None
        if not target.taken:
            raise RestoreLockedError(
                Path(store_file), lock, read_marker(store_file), running=True
            )
        marker = read_marker(store_file)
        if marker is not None:
            _unlock(target.fd)
            raise RestoreLockedError(Path(store_file), lock, marker, running=False)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(target.fd)
        raise
    if target.record:
        _unlock(target.fd)
        os.close(target.fd)
        return _record_admission(store_file)
    return _SharedHold(target.fd)


def reading(store_file: Path) -> _SharedHold | None:
    """Keep every restore of the store file `store_file` off a read of it
    for the read's length: `store.Store.open` with ``read_only`` takes it
    before it connects, and the store gives it back as it closes. Makes
    nothing.

    Where the lock is the store's directory, the read holds it shared, as
    a writing open does through `store._connect`, but until it closes: a
    restore waits for it (`RESTORE_WAIT_SECONDS`) and is refused, naming
    a process that opens or reads the store. On Windows, where a read
    cannot hold the record file shared, it holds the store file open
    (`_store_handle`) after its check and checks once more behind it, so
    a restore that took the lock between the two refuses the read, and a
    later one cannot swap the file; where no file stands to hold (a swap
    between its moves) it checks again before it connects, and a restore
    holding the lock refuses it (`_open_store_handle`). Either way the
    store file, the keys, the head and the anchor a read reads stay one
    restore's from its first read to its last: before the hold, a `log
    verify` that started before a restore and read on across its swap
    read the file it had open against the restored head. None when there
    is nothing to hold: this thread holds the lock (the restore's own
    read), the directory cannot be opened, or (a POSIX mount where
    ``flock`` cannot lock a directory) the check found no restore and
    nothing there can be held (module docstring).

    Raises `RestoreRunningError` while a restore holds the lock past
    `OPEN_WAIT_SECONDS`. A record left by a restore that did not finish
    refuses no read: the store is read as it stands, which is what an
    operator checks before `backup unlock --yes`."""
    try:
        target = _take(
            store_file,
            create=False,
            shared=True,
            wait=OPEN_WAIT_SECONDS,
            mine=_this_thread_holds,
        )
    except OSError:
        return None
    if target is not None:
        try:
            if target.mine:
                os.close(target.fd)
                return None
            if not target.taken:
                raise RestoreRunningError(Path(store_file))
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(target.fd)
            raise
        if not target.record:
            return _SharedHold(target.fd)
        _unlock(target.fd)
        os.close(target.fd)
    handle, state, _record = _open_store_handle(store_file)
    if state == RUNNING:
        raise RestoreRunningError(Path(store_file))
    if handle is None:
        return None
    hold = _SharedHold(handle)
    try:
        if _behind_handle(store_file)[0] == RUNNING:
            raise RestoreRunningError(Path(store_file))
    except BaseException:
        hold.close()
        raise
    return hold


@dataclass
class RestoreLock:
    """A restore's exclusive hold on its store's lock (`acquire`), and the
    marker file it keeps. ``previous`` is the marker a restore that did
    not finish left, which this one takes over; None when there was
    none. ``record``: the lock is the record file itself (Windows, or a
    directory ``flock`` cannot lock). ``thread``: the thread that took it,
    the only one whose opens of the store `admit` lets through."""

    path: Path
    store_file: Path
    previous: dict[str, Any] | None
    _fd: int
    _lock_fd: int
    _key: tuple[int, int]
    _marker: dict[str, Any] = field(default_factory=dict)
    record: bool = False
    thread: int = field(default_factory=threading.get_ident)

    def mark(self, **fields: Any) -> None:
        """Write the marker, with `fields` over what it held, durably:
        what a refused opener and `backup unlock` read if this process
        dies before it releases the lock."""
        if not self._marker:
            self._marker = {
                "format": MARKER_FORMAT,
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "store": str(self.store_file),
            }
        self._marker.update(fields)
        data = (json.dumps(self._marker, sort_keys=True) + "\n").encode("utf-8")
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, data)
        fsync_file(self._fd)

    def release(self, *, clear: bool) -> None:
        """Give the lock back; with `clear`, remove the marker first (the
        restore finished, or changed nothing). Without it the marker stays
        and every writing open refuses the store until a restore finishes
        or `unlock` removes it."""
        try:
            if clear:
                with contextlib.suppress(OSError):
                    os.ftruncate(self._fd, 0)
                if sys.platform != "win32":
                    # Under the lock, so no restore makes it meanwhile.
                    with contextlib.suppress(OSError):
                        self.path.unlink()
        finally:
            _HELD.pop(self._key, None)
            _unlock(self._lock_fd)
            for fd in {self._fd, self._lock_fd}:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if clear and sys.platform == "win32":
                # Windows removes no open file: after the close. One a new
                # restore has open by then stays, empty, and is no marker.
                with contextlib.suppress(OSError):
                    self.path.unlink()


def acquire(
    store_file: Path, *, wait: float = RESTORE_WAIT_SECONDS
) -> RestoreLock | None:
    """The store's restore lock, held exclusively, with its marker file
    made (owner-only) when missing, or None when another process holds
    the lock after `wait` seconds (a restore runs, or openers kept it
    shared that long, or a restore in this process holds it). Raises
    OSError when the lock or the marker cannot be opened."""
    lock = lock_path(store_file)
    target = _take(
        store_file,
        create=True,
        shared=False,
        wait=wait,
        mine=lambda key: key in _HELD,
    )
    if target is None:
        raise FileNotFoundError(f"{lock} could not be made")
    try:
        if target.mine or not target.taken:
            os.close(target.fd)
            return None
        try:
            fd = (
                target.fd
                if target.record
                else os.open(lock, os.O_RDWR | os.O_CREAT | _BINARY, 0o600)
            )
        except BaseException:
            _unlock(target.fd)
            raise
        held = RestoreLock(
            lock,
            Path(store_file),
            _read_marker(fd),
            fd,
            target.fd,
            target.key,
            record=target.record,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(target.fd)
        raise
    _HELD[target.key] = held
    return held


# ---------------------------------------------------------------------------
# Exclusivity through SQLite
# ---------------------------------------------------------------------------


def probe(store_file: Path) -> str | None:
    """Why a restore must not replace the store file `store_file` now, or
    None: whether any SQLite connection, in any process, has it open.

    Opens it read-write in SQLite's exclusive locking mode and reads its
    schema version, which takes SQLite's exclusive lock on the file and
    fails with SQLITE_BUSY (or SQLITE_LOCKED) while another connection
    holds its shared lock, as every open WAL-mode connection does, a
    reader included. Nothing is written by the read; closing the
    connection, the store's only one, checkpoints what a crash left in
    its ``-wal`` into the file and removes the ``-wal``, as the last close
    of any store does, so the store the restore moves aside holds those
    rows in its one file. Any other error (no file, a file that is not a
    database) is no connection holding it: None."""
    path = Path(store_file)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(
            sqlite_file_uri(path.resolve()) + "?mode=rw",
            uri=True,
            timeout=0,
            isolation_level=None,
        )
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        conn.execute("PRAGMA schema_version").fetchone()
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(code, int) and code & 0xFF in _HELD_ELSEWHERE_CODES:
            return (
                f"a process has {path} open (`bettermemory serve`, the shim's "
                "in-process server, a hook, a CLI command, `backup create`, or a "
                "daemon this state directory does not name); close every session "
                "using bettermemory and run the restore again"
            )
        return None
    finally:
        conn.close()
    return None


# ---------------------------------------------------------------------------
# `backup unlock`
# ---------------------------------------------------------------------------


#: `unlock` outcomes, and the exit status each maps to.
CLEAR = "clear"
CLEARED = "cleared"
HELD = "held"
PLANNED = "planned"
UNLOCK_EXIT = {CLEAR: 0, CLEARED: 0, HELD: 1, PLANNED: 3}


#: Who holds the lock when `unlock` cannot take it (its report's
#: ``holder``): a restore, holding it exclusively, or openers and reads,
#: holding it shared (`reading`, `admit`).
HOLDER_RESTORE = "restore"
HOLDER_READER = "reader"


def _holder(store_file: Path) -> str:
    """Who holds the lock `unlock` could not take: `HOLDER_READER` when it
    can be taken shared now, so its holders hold it shared (a read-only
    command reading the store, `reading`, or a writing open opening it,
    `admit`), else `HOLDER_RESTORE`. Where the lock is the record file,
    no opener or read holds it past its check (on Windows a read holds
    the store file, not the lock), so a lock still held is a restore's."""
    try:
        target = _take(
            store_file, create=False, shared=True, wait=0.0, mine=_this_thread_holds
        )
    except OSError:
        return HOLDER_RESTORE
    if target is None:
        return HOLDER_RESTORE
    try:
        if target.record or target.mine or not target.taken:
            return HOLDER_RESTORE
        _unlock(target.fd)
        return HOLDER_READER
    finally:
        os.close(target.fd)


def unlock(store_file: Path, *, apply: bool) -> dict[str, Any]:
    """Remove the marker a restore that did not finish left beside the
    store, so writing opens of the store go on as it stands, and return
    ``{status, lock, marker, holder}``: ``clear`` (no marker, nothing
    done), ``held`` (the lock is held now, nothing done; ``holder`` says
    by whom, `_holder`: a restore, or a read-only command reading the
    store or an open opening it, which a dead restore's marker must not
    be blamed for), ``planned`` (a marker, `apply` false: nothing done)
    or ``cleared``. ``holder`` is None unless ``held``."""
    lock = lock_path(store_file)
    report: dict[str, Any] = {
        "status": CLEAR,
        "lock": str(lock),
        "marker": None,
        "holder": None,
    }
    if not lock.exists():
        return report
    held = acquire(store_file, wait=OPEN_WAIT_SECONDS)
    if held is None:
        report.update(
            status=HELD, marker=read_marker(store_file), holder=_holder(store_file)
        )
        return report
    keep = False
    try:
        report["marker"] = held.previous
        if held.previous is not None and not apply:
            keep = True
            report["status"] = PLANNED
        elif held.previous is not None:
            report["status"] = CLEARED
        return report
    finally:
        held.release(clear=not keep)


# ---------------------------------------------------------------------------
# What a reader of the lock sees: `doctor`
# ---------------------------------------------------------------------------


#: `restore_state`'s answers besides `CLEAR`.
RUNNING = "running"
UNFINISHED = "unfinished"


def restore_state(
    store_file: Path, *, wait: float = 0.0
) -> tuple[str, dict[str, Any] | None]:
    """Whether a restore of the store runs or did not finish, read without
    opening the store, holding the lock past the look, or making anything:
    ``(RUNNING, record)`` while another process, or another thread of this
    one, holds the restore lock past `wait` seconds; ``(UNFINISHED,
    record)`` when no one holds it and a restore that did not finish left
    its record; ``(CLEAR, None)`` otherwise, a store directory that does
    not open included (no restore by this account could lock it). `doctor`
    reads it: a store's read-only open never meets the refusal every
    writing open of such a store gets. The hook client reads the same
    with the standard library alone before it starts a daemon
    (`_store_refusal.restore_state`, `_store_refusal.restore_refusal`);
    tests/test_hook_client.py holds the two readings and their refusal
    equal."""
    try:
        target = _take(
            store_file, create=False, shared=True, wait=wait, mine=_this_thread_holds
        )
    except OSError:
        return CLEAR, None
    if target is None:
        return CLEAR, None
    try:
        if target.mine:
            return CLEAR, None
        if not target.taken:
            return RUNNING, read_marker(store_file)
        _unlock(target.fd)
    finally:
        os.close(target.fd)
    record = read_marker(store_file)
    return (CLEAR, None) if record is None else (UNFINISHED, record)


def state_finding(store_file: Path) -> tuple[str, str, str | None, dict[str, Any]]:
    """`restore_state` in `doctor`'s terms: ``(state, summary, remedy,
    details)``, the summary and remedy naming both ways out of a restore
    that did not finish."""
    from .history import printable

    state, record = restore_state(store_file)
    details: dict[str, Any] = {"state": state, "record": str(lock_path(store_file))}
    if state == UNFINISHED:
        details["restore"] = record
        bundle = (record or {}).get("bundle")
        again = printable(
            f"bettermemory backup restore {bundle} --yes"
            if isinstance(bundle, str)
            else "bettermemory backup restore BUNDLE --yes"
        )
        return (
            state,
            f"writes are refused: a restore did not finish ({describe(record or {})})",
            "run `bettermemory backup unlock --yes` after checking the store, or "
            f"re-run the restore (`{again}`)",
            details,
        )
    if state == RUNNING:
        details["restore"] = record
        return (
            state,
            "writes are refused while a restore runs",
            "run `bettermemory doctor` again when the restore has finished",
            details,
        )
    return state, "no restore is running or left unfinished", None, details


__all__ = [
    "CLEAR",
    "CLEARED",
    "HELD",
    "LOCK_SUFFIX",
    "MARKER_FORMAT",
    "OPEN_WAIT_SECONDS",
    "PLANNED",
    "RESTORE_RUNNING_STATUS",
    "RESTORE_WAIT_SECONDS",
    "RUNNING",
    "UNFINISHED",
    "UNLOCK_EXIT",
    "RestoreLock",
    "RestoreLockedError",
    "RestoreRunningError",
    "Closeable",
    "acquire",
    "admit",
    "describe",
    "lock_path",
    "probe",
    "read_marker",
    "reading",
    "restore_state",
    "state_finding",
    "unlock",
]
