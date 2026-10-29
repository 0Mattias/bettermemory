"""The key checks a store open makes, and the key ring's binding to one file.

`Store.open` checks whether a key signs a segment of the log
(`_signs_a_segment`) and reads the ring's current key (`_current_key`).
The owner's opener claims the ring for its file (`_claim_key_ring`); a
reader whose key matches asks whether the ring is bound to another file
still holding the store id (`_key_ring_elsewhere`), read without opening
it as a store (`_store_id_at`), warned about once per pair of files
(`_warn_bound_elsewhere`). Also `configured_strict_provenance`. It logs as
`bettermemory.store`. `store` re-exports every name a caller reads; a test
that patches `_BOUND_ELSEWHERE_WARNED` patches this module. It lives apart
from `store.py` to keep each module under the package's 2,000-line bound
(ENG-96), and of the store modules imports only `store_schema`, for the
check a bound file is admitted by (`store_schema.admit_store`).
"""

from __future__ import annotations

import errno
import logging
import sqlite3
import stat
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from ._fsutil import sqlite_file_uri
from .log import KeyRing, KeyUnreadableError, Segment, fingerprint, row_verifies
from .store_schema import StoreRefusedError, admit_store, read_store_meta

log = logging.getLogger("bettermemory.store")


def _signs_a_segment(segments: Sequence[Segment], key: bytes) -> bool:
    """Does ``key`` sign a segment of the log: is a segment under its
    fingerprint, and does that segment's first row verify under it? The
    MAC is the evidence. A fingerprint is public (meta and every rekey row
    carry it), so a rekey row naming this key proves nothing until the
    key verifies it; the leading segment's first row is the store's own
    ``store_created`` row."""
    own = fingerprint(key)
    return any(
        segment.key_fingerprint == own
        and segment.first_row is not None
        and row_verifies(key, segment.first_row)
        for segment in segments
    )


def _current_key(keyring: KeyRing, path: Path, *, writing: bool) -> bytes | None:
    """The key ring's current key for the store at ``path`` (`open`). A key
    file that is there and cannot be read (`log.KeyUnreadableError`) fails
    the owner's open, naming the file: read as a missing key it would be
    replaced and the log rekeyed, for a failure that may pass in a moment.
    A reader opens without it, as it opens without a missing key, and says
    why in the log."""
    try:
        return keyring.load_current()
    except KeyUnreadableError as exc:
        if writing:
            raise
        log.warning("store %s opens without its key: %s", path, exc)
        return None


# SQLite's primary result codes that, from the query, answer what a file
# is rather than fail to read it: SQLITE_ERROR (here, no ``meta`` table: a
# database that is no store), SQLITE_CORRUPT and SQLITE_NOTADB. Any other
# (SQLITE_CANTOPEN, SQLITE_IOERR, SQLITE_BUSY, SQLITE_PERM, ...) is a read
# that failed, which may pass in a moment, and so is every failure of the
# open itself, whatever its code: an open reads nothing of the file, and
# SQLite answers SQLITE_ERROR there for a URI it will not take, such as
# one with a server as its authority, which `Path.as_uri` once wrote for a
# Windows UNC path (`_fsutil.sqlite_file_uri`, `_store_id_at`).
_NOT_A_STORE_CODES = frozenset({1, 11, 26})


def _store_id_at(path: Path) -> str | None:
    """The store id meta records in the store file at ``path``, read
    without opening it as a store (`Store.open` runs the additive schema
    step and sets WAL mode, which would write to it): ``immutable=1``, no
    lock and no ``-wal`` or ``-shm`` created, or, when a ``-wal`` and its
    ``-shm`` are beside the file (a daemon has it open, or crashed with
    it open), ``mode=ro`` first, so the read sees a store whose meta is
    still only in its WAL, as a store a running daemon just created is.

    None when there is no file there or it does not read as a store (no
    database, one with no store id, or one every opener refuses that has
    no meta table: `store_schema.admit_store` checks it before its meta
    is read, under the same deadline). OSError when it cannot be read to
    tell (a directory on the way that cannot be searched, a file that
    cannot be opened, by any SQLite error, an I/O error, a store whose
    meta is only in its WAL and whose lock is held, a read that ran past
    the admission deadline, a refused file with a meta table, such as
    another build's store with one more index): a failure that may
    pass in a moment, or that SQLite meets before it reads a byte, says
    nothing about whether the file still holds a store, and the caller
    keeps the key ring bound to it (`_claim_key_ring`). A symbolic link
    that loops is no file there, as `Path.is_file` reads it: ELOOP, or on
    Windows ERROR_CANT_RESOLVE_FILENAME (1921), the reparse-point cycle,
    which CPython does not fold into ELOOP (`origin.py` reads it the
    same way)."""
    try:
        status = path.stat()
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP or getattr(exc, "winerror", None) == 1921:
            return None
        raise
    if not stat.S_ISREG(status.st_mode):
        return None
    resolved = path.resolve()
    wal = resolved.with_name(resolved.name + "-wal")
    shm = resolved.with_name(resolved.name + "-shm")
    queries = ["mode=ro&immutable=1"]
    if wal.exists() and shm.exists():
        queries.insert(0, "mode=ro")
    failed: sqlite3.Error | None = None
    for query in queries:
        try:
            conn = sqlite3.connect(
                f"{sqlite_file_uri(resolved)}?{query}",
                uri=True,
                timeout=2.0,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            # The open failed, so nothing of the file was read: no answer.
            # Read as no store, a bound store SQLite cannot open by this
            # URI would release the ring to every second file.
            failed = exc
            continue
        try:
            admit_store(conn, resolved)
            store_id = read_store_meta(conn, resolved).get("store_id")
        except StoreRefusedError as exc:
            # A schema SQLite cannot read, or a file this build refuses
            # with no meta table (a view put in its place), holds no
            # store. One with a meta table (another build's store with one
            # more index) may still hold this store's id, and one whose
            # read ran past the deadline or whose schema was not read
            # cannot be read to tell: the ring stays bound to it.
            cause = getattr(exc.__cause__, "sqlite_errorcode", 0) & 0xFF
            if not exc.timed_out and (
                exc.holds_meta is False or cause in _NOT_A_STORE_CODES
            ):
                return None
            failed = sqlite3.OperationalError(str(exc))
            continue
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF not in _NOT_A_STORE_CODES:
                failed = exc
            continue
        finally:
            conn.close()
        if store_id is not None:
            return store_id
    if failed is not None:
        raise OSError(f"{path} could not be read ({failed})")
    return None


def _bound_store_check(store_id: str) -> tuple[Callable[[Path], bool], list[str]]:
    """The ``holds_store`` answer `log.KeyRing.bind_store` asks for, for a
    store with id ``store_id``, and the list it fills with why a bound
    file could not be read. A bound file that cannot be read to tell
    (`_store_id_at` raises) is taken to hold the store still: binding the
    ring to this file instead, for a failure that may pass in a moment,
    would let this file sign for the bound store and move its head, and
    leave the bound store opening for reading only once it is back."""
    unread: list[str] = []

    def holds(other: Path) -> bool:
        try:
            return _store_id_at(other) == store_id
        except OSError as exc:
            unread.append(str(exc))
            return True

    return holds, unread


#: The (store file, bound file) pairs this process has warned about
#: (`_warn_bound_elsewhere`). Once per process, by `config`'s
#: `_DEPRECATED_KEY_WARNED_PATHS` rule: the daemon opens a worker
#: connection per thread (`Store.reopen`), and each is a reader's open
#: that reads the binding again, so a copy left beside the bound store
#: repeated the same warning for every worker of a long-lived daemon.
#: Keyed on both files, so a second copy, or the same copy once the ring
#: is bound to a third file, still gets its own warning.
_BOUND_ELSEWHERE_WARNED: set[tuple[Path, Path]] = set()
#: Makes the guard's test and its add one step. The workers are the case
#: the guard is for, and they can open at once: each worker thread reopens
#: on its first job, and jobs that arrive together reach several workers
#: together. A thread switch between one worker's test and its add would
#: let another find the pair new too, and both would warn.
_BOUND_ELSEWHERE_LOCK = threading.Lock()


def _resolved_for_guard(path: Path) -> Path:
    """`path` resolved for the one-shot guard's key, so two spellings of
    one file share a warning; as given when it cannot be resolved (the
    rule of `config._resolved_for_guard`, for a store file). A symlink
    loop raises RuntimeError before Python 3.13, and a log line must not
    fail the open that says it."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _warn_bound_elsewhere(
    keyring: KeyRing, path: Path, store_id: str, other: Path, unread: Sequence[str]
) -> None:
    """Say in the log that the key ring is another store file's, naming both
    files and the remedy (`_claim_key_ring`, `_key_ring_elsewhere`): a
    warning the first time this process opens ``path`` while the ring is
    bound to ``other``, the same line at DEBUG on every later open of the
    pair (`_BOUND_ELSEWHERE_WARNED`). Every open still reads the binding
    and still opens for reading only; only the repetition is quieter."""
    guard_key = (_resolved_for_guard(path), _resolved_for_guard(other))
    with _BOUND_ELSEWHERE_LOCK:
        first = guard_key not in _BOUND_ELSEWHERE_WARNED
        _BOUND_ELSEWHERE_WARNED.add(guard_key)
    level = logging.WARNING if first else logging.DEBUG
    holds = (
        f"which could not be read to tell whether it still holds that id ({unread[-1]})"
        if unread
        else "which still holds that id"
    )
    log.log(
        level,
        "store %s carries store id %s, and the key ring under %s belongs to "
        "the store at %s, %s; this one opens for reading only, and nothing is "
        "rekeyed or written. Move or remove one of the two stores, or delete %s "
        "to bind the key ring to this one",
        path,
        store_id,
        keyring.keys_dir,
        other,
        holds,
        keyring.binding_path(),
    )


def _key_ring_elsewhere(keyring: KeyRing, path: Path, store_id: str) -> Path | None:
    """The other store file the key ring is bound to when it still holds
    this store id, for a reader's open whose key matches (`open`), else
    None; reads the binding and writes nothing. A reader's open with the
    key can write (the session-start hook records the brief it shows, and
    a worker connection writes as its owner does), so on a second file
    with the bound store's id it would sign rows with that store's key and
    move its head past that store's log. A binding that cannot be read is
    none, as it was before rings were bound."""
    holds, unread = _bound_store_check(store_id)
    try:
        other = keyring.bound_elsewhere(path, holds_store=holds)
    except OSError:
        return None
    if other is not None:
        _warn_bound_elsewhere(keyring, path, store_id, other, unread)
    return other


def _claim_key_ring(keyring: KeyRing, path: Path, store_id: str) -> Path | None:
    """Claim the key ring for the store file ``path``, the owner's opener's
    first step (`open`): bind the ring to it when it is bound to no file,
    or to one that no longer holds this store id (moved, deleted, another
    store there), and return None; return the other store file when the
    ring is bound to one that still does, or that cannot be read to tell,
    and is not this file under another spelling (`log.KeyRing.bind_store`).
    That file is the ring's store: its key, head and anchor are that
    file's, and an open of a second file with its id (a copy in another
    directory, or a file made to borrow a known id) must not retire the
    key, write a new one, set meta, move the head or write the anchor, all
    of which the owner's opener would otherwise do. A ring whose binding
    cannot be read or written is said in the log and the open goes on, as
    it did before rings were bound; the binding is left as it is."""
    holds, unread = _bound_store_check(store_id)
    try:
        binding = keyring.bind_store(path, holds_store=holds)
    except OSError as exc:
        log.warning(
            "store %s: the key ring under %s could not be bound to it (%s)",
            path,
            keyring.keys_dir,
            exc,
        )
        return None
    if binding.elsewhere:
        assert binding.previous is not None
        _warn_bound_elsewhere(keyring, path, store_id, binding.previous, unread)
        return binding.previous
    if binding.written and binding.previous is not None:
        log.info(
            "store %s: the key ring under %s was bound to %s, which no longer "
            "holds store id %s; it is bound to this file now",
            path,
            keyring.keys_dir,
            binding.previous,
            store_id,
        )
    return None


def configured_strict_provenance() -> bool:
    """``[security] strict_provenance`` as `load_config` reads it, so a
    value an organisation pins in a managed config applies here too. A
    config that cannot be read reads as strict, with a warning: it is a
    switch that narrows what is trusted, and a broken file must not widen
    trust by default. Nor may it fail the read that asked: a daemon reads
    this long after its own start, from a file edited since, and a
    section of the wrong type makes `load_config` raise TypeError or
    AttributeError, not only OSError or ValueError.

    Read without the first-run write (``create=False``): a missing file
    reads as the defaults it would be written with, the managed file still
    applied. A read-only command (`memory history`) labels rows through
    this, and must not write a config.toml, nor read a config directory it
    cannot write as a config it cannot read. Every writing entry point
    (the daemon, the hooks, the CLI's writers) has written the file at its
    own start already."""
    from .config import load_config

    try:
        return load_config(create=False).security.strict_provenance
    except Exception as exc:  # noqa: BLE001 - a bad config must not fail a read
        log.warning(
            "[security] strict_provenance could not be read (%s); rows under a "
            "key this machine does not hold read unaccounted",
            exc,
        )
        return True
