"""The restore lock: a restore holds its store for its whole length, and
every writing open of the store respects it.

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
writes: `bettermemory serve`, the shim's in-process fallback, the CLI's
writing commands and the in-process hooks, which all open the store
through `store._connect`.

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
  opens the file until `_connect` returns, after
  `store_schema.admit_store` has read it (on a WAL-mode store, the
  shared lock SQLite keeps from then on), so the restore's exclusive
  `acquire` waits for it, and its probe then sees the connection.

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

The restore's own process opens the restored store itself, to check its
log: `admit` lets an open through when this process is the one that
holds the lock (`_HELD`, keyed by the locked file's device and inode,
so the spelling of the path does not matter).

POSIX locks the directory with ``flock`` (shared and exclusive).
Windows cannot lock a directory, and has only exclusive byte-range locks
(``msvcrt.locking``): there the marker file is the lock, locked on one
byte far past the marker so the marker stays readable, and only a
restore makes it. An opener checks it when it is there, for the moment
it checks, and does not hold it across `_connect`; the order "opened
before the restore" is still caught by the probe, and Windows itself
refuses to rename a store file another process has open, which the swap
does.

Not covered: an opener that writes without `store._connect` (none in this
package; a copy of the file by hand is not a writer); a store file in
rollback-journal mode (one copied in by hand and not opened since, since
`_connect` switches every store to WAL) whose opener switched it to WAL
and has not read since: SQLite holds no shared lock on it in that
moment, so the probe does not see it.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._fsutil import fsync_file, sqlite_file_uri
from .store_schema import StoreRefusedError

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
#: inode: an open of the store by the restore itself is let through.
_HELD: dict[tuple[int, int], RestoreLock] = {}


def lock_path(store_file: Path) -> Path:
    """``<store file>.restore-lock``, beside the store file: the marker a
    restore keeps (and on Windows the file it locks)."""
    path = Path(store_file)
    return path.with_name(path.name + LOCK_SUFFIX)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def _try_lock(fd: int, *, shared: bool) -> bool:
    """Take the lock on `fd` without waiting; False when another holds it
    (or another open file of it in this process does)."""
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
        except OSError:
            return False
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


def _open_lock_target(store_file: Path, *, create: bool) -> int:
    """An open file of what the restore lock is taken on: the store's
    directory, or on Windows the marker file, made (owner-only) only when
    `create`. Raises OSError when it cannot be opened."""
    if _LOCKS_THE_DIRECTORY:
        return os.open(Path(store_file).parent, _DIRECTORY_FLAGS)
    flags = os.O_RDWR | _BINARY | (os.O_CREAT if create else 0)
    return os.open(lock_path(store_file), flags, 0o600)


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
    """A writing open of a store a restore holds (``running``), or one a
    restore that did not finish left its marker beside. A
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
    ) -> None:
        from .history import printable

        self.store_file = store_file
        self.lock = lock
        self.marker = marker
        self.running = running
        if running:
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


class _SharedHold:
    """An opener's shared hold on a restore lock, given back when the
    object goes: `store._connect` keeps it in a local until it returns."""

    __slots__ = ("_fd",)

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def __del__(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._fd)


def admit(store_file: Path) -> object | None:
    """Admit a writing open of the store file `store_file`: the first thing
    `store._connect` does, before SQLite opens the file. Makes nothing.
    Returns the shared hold on the restore lock, which the caller keeps
    until the connection has read the store (`_connect` keeps it in a
    local until it returns), or None when there is nothing to hold: this
    process holds the lock itself (the restore's own open), the store's
    directory cannot be opened (no restore by this account can lock it
    either), or Windows (module docstring), where there is nothing to
    check either when no marker is there.

    Raises `RestoreLockedError` when a restore holds the lock past
    `OPEN_WAIT_SECONDS`, or when a restore that did not finish left its
    marker."""
    lock = lock_path(store_file)
    try:
        fd = _open_lock_target(store_file, create=False)
    except OSError:
        return None
    try:
        if _file_key(fd) in _HELD:
            os.close(fd)
            return None
        if not _lock_within(fd, shared=True, wait=OPEN_WAIT_SECONDS):
            raise RestoreLockedError(
                Path(store_file), lock, read_marker(store_file), running=True
            )
        marker = read_marker(store_file)
        if marker is not None:
            _unlock(fd)
            raise RestoreLockedError(Path(store_file), lock, marker, running=False)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    if not _LOCKS_THE_DIRECTORY:
        _unlock(fd)
        os.close(fd)
        return None
    return _SharedHold(fd)


@dataclass
class RestoreLock:
    """A restore's exclusive hold on its store's lock (`acquire`), and the
    marker file it keeps. ``previous`` is the marker a restore that did
    not finish left, which this one takes over; None when there was
    none."""

    path: Path
    store_file: Path
    previous: dict[str, Any] | None
    _fd: int
    _lock_fd: int
    _key: tuple[int, int]
    _marker: dict[str, Any] = field(default_factory=dict)

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
                if _LOCKS_THE_DIRECTORY:
                    # Under the lock, so no restore makes it meanwhile.
                    with contextlib.suppress(OSError):
                        self.path.unlink()
        finally:
            _HELD.pop(self._key, None)
            _unlock(self._lock_fd)
            for fd in {self._fd, self._lock_fd}:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if clear and not _LOCKS_THE_DIRECTORY:
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
    shared that long). Raises OSError when the lock or the marker cannot
    be opened."""
    lock = lock_path(store_file)
    lock_fd = _open_lock_target(store_file, create=True)
    try:
        key = _file_key(lock_fd)
        if key in _HELD or not _lock_within(lock_fd, shared=False, wait=wait):
            os.close(lock_fd)
            return None
        try:
            fd = (
                os.open(lock, os.O_RDWR | os.O_CREAT | _BINARY, 0o600)
                if _LOCKS_THE_DIRECTORY
                else lock_fd
            )
        except BaseException:
            _unlock(lock_fd)
            raise
        held = RestoreLock(lock, Path(store_file), _read_marker(fd), fd, lock_fd, key)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(lock_fd)
        raise
    _HELD[key] = held
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


def unlock(store_file: Path, *, apply: bool) -> dict[str, Any]:
    """Remove the marker a restore that did not finish left beside the
    store, so writing opens of the store go on as it stands, and return
    ``{status, lock, marker}``: ``clear`` (no marker, nothing done),
    ``held`` (a restore holds the lock now, nothing done), ``planned``
    (a marker, `apply` false: nothing done) or ``cleared``."""
    lock = lock_path(store_file)
    report: dict[str, Any] = {"status": CLEAR, "lock": str(lock), "marker": None}
    if not lock.exists():
        return report
    held = acquire(store_file, wait=OPEN_WAIT_SECONDS)
    if held is None:
        report.update(status=HELD, marker=read_marker(store_file))
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


__all__ = [
    "CLEAR",
    "CLEARED",
    "HELD",
    "LOCK_SUFFIX",
    "MARKER_FORMAT",
    "OPEN_WAIT_SECONDS",
    "PLANNED",
    "RESTORE_WAIT_SECONDS",
    "UNLOCK_EXIT",
    "RestoreLock",
    "RestoreLockedError",
    "acquire",
    "admit",
    "describe",
    "lock_path",
    "probe",
    "read_marker",
    "unlock",
]
