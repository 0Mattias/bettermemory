"""The key checks a store open makes, and the key ring's binding to one file.

A store file's key is in one of six states (`key_state`, `KeyState`):
present, unreadable, missing, foreign, bound elsewhere (the key ring
belongs to another store file that still holds the store id), or
unclaimed (the ring is bound to no file and records a history this file
does not carry, `_unclaimed_fit`). `Store.open`,
doctor's `key` check and posture's `key.match` finding all read it through
`key_state`, and all three say it in its words (`KeyState.summary`,
`KeyState.remedy`). The owner's opener claims the ring for its file
(`_claim_key_ring`); every other reader asks whether the ring is bound to
another file still holding the store id (`_key_ring_elsewhere`), read
without opening it as a store (`_store_id_at`), warned about once per pair
of files (`_warn_bound_elsewhere`). `Store.open` also checks whether a key
signs a segment of the log (`_signs_a_segment`), and `Store._append`
which key a row signs with once meta has been read again under the
write lock (`_signing_key`). Also
`configured_strict_provenance`, and `prepare_store_dir`, which refuses
a new store whose keys directory is a symbolic link to nothing
(`KeysDirUnreadableError`). It logs as `bettermemory.store`. `store`
re-exports every name a caller reads; a test that patches
`_BOUND_ELSEWHERE_WARNED` patches this module. It lives apart from
`store.py` to keep each module under the package's 2,000-line bound
(ENG-96), and of the store modules imports only `store_schema`, for the
check a bound file is admitted by (`store_schema.admit_store`).
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import sqlite3
import stat
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._fsutil import (
    ensure_owner_only_dir,
    fsync_file,
    replace_atomic,
    sqlite_file_uri,
)
from .log import (
    KEY_BYTES,
    Head,
    KeyRing,
    KeyUnreadableError,
    LogRow,
    OriginCheck,
    Segment,
    check_origin,
    fingerprint,
    iter_rows,
    last_row,
    row_verifies,
    _dangling_link,
    same_mac,
)
from .store_schema import (
    StoreRefusedError,
    admit_store,
    connect_read_only,
    read_store_meta,
    resolve_keys_dir,
)

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


class KeysDirUnreadableError(KeyUnreadableError):
    """The keys directory a new store's key would go in, or a directory on
    the way to it, is a symbolic link to nothing (a keys directory kept on
    a volume that is not mounted right now). `Store.create` refuses before
    it writes anything (`prepare_store_dir`): ``mkdir`` met the link and
    raised `FileExistsError`, a traceback with no word of the link. A
    `KeyUnreadableError`, so every opener that ends a command in one line
    for an unreadable key (`cli._common.exit_if_key_unreadable`, `migrate
    v8`, the daemon) ends it so for this."""

    def __init__(self, keys_dir: Path, link: Path, reason: str) -> None:
        if link != keys_dir:
            reason = f"{link}, on the way to it, {reason.removeprefix('it ')}"
        super().__init__(
            keys_dir, FileNotFoundError(errno.ENOENT, reason), reason=reason
        )
        self.message = (
            f"the keys directory {keys_dir} cannot be used ({reason}); no store "
            "is created until it can keep the new store's key"
        )
        self.keys_dir = keys_dir


def prepare_store_dir(path: Path, keys_dir: Path | str | None) -> None:
    """Make the directory of the new store file ``path`` (owner-only), once
    the keys directory its key will go in (``keys_dir``, else the one the
    store reads, `store_schema.resolve_keys_dir`) is not a symbolic link to
    nothing, nor below one: the nearest entry on its way that is there
    decides. Raises `KeysDirUnreadableError` naming the link and its
    target, with nothing written. `Store.create`'s first write."""
    keys = Path(keys_dir) if keys_dir is not None else resolve_keys_dir()
    for entry in (keys, *keys.parents):
        if not os.path.lexists(entry):
            continue
        reason = _dangling_link(entry)
        if reason is not None:
            raise KeysDirUnreadableError(keys, entry, reason)
        break
    ensure_owner_only_dir(path.parent, parents=True)


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


def _bound_words(
    keyring: KeyRing, store_id: str, other: Path, unread: str | None
) -> tuple[str, str]:
    """What a store file whose key ring is bound to ``other`` is told, as
    a summary and a remedy: the warning (`_warn_bound_elsewhere`), the
    refusal of a write (`KeyState.refusal`), doctor's ``key`` check and
    posture's ``key.match`` finding all say these words (`KeyState`)."""
    holds = (
        f"which could not be read to tell whether it still holds store id "
        f"{store_id} ({unread})"
        if unread is not None
        else f"which still holds store id {store_id}"
    )
    summary = (
        f"the key ring under {keyring.keys_dir} is bound elsewhere: it belongs "
        f"to the store at {other}, {holds}; every open of this file is "
        "read-only, and nothing is rekeyed or written"
    )
    remedy = (
        "move or remove one of the two stores, or run `bettermemory key adopt "
        "--yes` with BETTERMEMORY_DIR naming this file's directory, which binds "
        f"the key ring to this one on the record (it rewrites {keyring.binding_path()})"
    )
    return summary, remedy


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _warn_bound_elsewhere(
    keyring: KeyRing, path: Path, store_id: str, other: Path, unread: str | None
) -> None:
    """Say in the log that the key ring is another store file's, naming both
    files and the remedy in the state's words (`_bound_words`): a warning
    the first time this process opens ``path`` while the ring is bound to
    ``other``, the same line at DEBUG on every later open of the pair
    (`_BOUND_ELSEWHERE_WARNED`). Every open still reads the binding and
    still opens for reading only; only the repetition is quieter."""
    guard_key = (_resolved_for_guard(path), _resolved_for_guard(other))
    with _BOUND_ELSEWHERE_LOCK:
        first = guard_key not in _BOUND_ELSEWHERE_WARNED
        _BOUND_ELSEWHERE_WARNED.add(guard_key)
    level = logging.WARNING if first else logging.DEBUG
    summary, remedy = _bound_words(keyring, store_id, other, unread)
    log.log(level, "store %s: %s. %s", path, summary, _sentence(remedy))


def _key_ring_elsewhere(
    keyring: KeyRing, path: Path, store_id: str, *, warn: bool = True
) -> tuple[Path | None, str | None]:
    """The other store file the key ring is bound to when it still holds
    this store id, for a reader's open (`key_state`), else None, and why
    that file could not be read to tell when it could not; reads the
    binding and writes nothing. A reader's open with the key can write
    (a worker connection, `Store.reopen`, writes as its owner does: the
    daemon's hook workers record the brief they show), so on a second
    file with the bound store's id it would sign rows with that store's
    key and move its head past that store's log; a reader without the key
    is told the same, so every reader of the file says one state. A
    binding that cannot be
    read is none, as it was before rings were bound."""
    holds, unread = _bound_store_check(store_id)
    try:
        other = keyring.bound_elsewhere(path, holds_store=holds)
    except OSError:
        return None, None
    if other is None:
        return None, None
    why = unread[-1] if unread else None
    if warn:
        _warn_bound_elsewhere(keyring, path, store_id, other, why)
    return other, why


def _claim_key_ring(
    keyring: KeyRing, path: Path, store_id: str, *, warn: bool = True
) -> tuple[Path | None, str | None]:
    """Claim the key ring for the store file ``path``, the owner's opener's
    first step (`key_state`): bind the ring to it when it is bound to no
    file, or to one that no longer holds this store id (moved, deleted,
    another store there), and return None; return the other store file
    when the ring is bound to one that still does, or that cannot be read
    to tell (and why, as the second item), and is not this file under
    another spelling (`log.KeyRing.bind_store`). That file is the ring's
    store: its key, head and anchor are that file's, and an open of a
    second file with its id (a copy in another directory, or a file made
    to borrow a known id) must not retire the key, write a new one, set
    meta, move the head or write the anchor, all of which the owner's
    opener would otherwise do. A ring whose binding cannot be read or
    written is said in the log and the open goes on, as it did before
    rings were bound; the binding is left as it is."""
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
        return None, None
    if binding.elsewhere:
        assert binding.previous is not None
        why = unread[-1] if unread else None
        if warn:
            _warn_bound_elsewhere(keyring, path, store_id, binding.previous, why)
        return binding.previous, why
    if binding.written and binding.previous is not None:
        log.info(
            "store %s: the key ring under %s was bound to %s, which no longer "
            "holds store id %s; it is bound to this file now",
            path,
            keyring.keys_dir,
            binding.previous,
            store_id,
        )
    return None, None


# ---------------------------------------------------------------------------
# The key's state (ENG-174)
# ---------------------------------------------------------------------------

#: The six states of a store file's key, as `key_state` reads them.
KEY_PRESENT = "present"
KEY_UNREADABLE = "unreadable"
KEY_MISSING = "missing"
KEY_FOREIGN = "foreign"
KEY_BOUND_ELSEWHERE = "bound_elsewhere"
KEY_UNCLAIMED = "unclaimed"
KEY_STATES = (
    KEY_PRESENT,
    KEY_UNREADABLE,
    KEY_MISSING,
    KEY_FOREIGN,
    KEY_BOUND_ELSEWHERE,
    KEY_UNCLAIMED,
)
#: How long `_fit_read_only` waits on a lock, as a CLI open does.
_FIT_READ_TIMEOUT = 5.0


@dataclass(frozen=True)
class KeyState:
    """A store file's key, as `key_state` read it, and the words every
    reader says it in (`summary`, `remedy`): `Store.open` keeps it
    (`Store.key_state`) and refuses a write in them (`refusal`); doctor's
    ``key`` check and posture's ``key.match`` finding render them. Only
    ``present`` signs. A reader opens the other five for reading only;
    the owner's opener fails on an unreadable key, writes nothing to a
    file whose ring is bound elsewhere or unclaimed, and rekeys a missing
    or foreign key (or keeps a foreign one that signs a segment of the
    log).

    ``expected`` is the fingerprint the store's meta records, ``key`` the
    ring's current key (never in the repr), ``error`` why it could not be
    read, ``bound_to`` the store file the ring belongs to when that is
    another, ``unread`` why that file could not be read to tell whether
    it still holds the store, and ``unfit`` how this file fails to carry
    the history of a ring bound to none (unclaimed)."""

    state: str
    keyring: KeyRing
    store_file: Path
    expected: str
    key: bytes | None = field(default=None, repr=False)
    error: KeyUnreadableError | None = None
    bound_to: Path | None = None
    unread: str | None = None
    unfit: RingFit | None = None

    @property
    def key_path(self) -> Path:
        return self.keyring.current_path()

    @property
    def withheld(self) -> Path | None:
        """What keeps the ring from this file whatever its key is, for
        `Store.open`, which then opens it for reading only and keeps that
        answer for its worker connections (`Store.reopen`): the store file
        the ring is bound to (bound elsewhere), or the ring's binding file,
        which names no store yet (unclaimed). None in the other states."""
        if self.state == KEY_BOUND_ELSEWHERE:
            return self.bound_to
        if self.state == KEY_UNCLAIMED:
            return self.keyring.binding_path()
        return None

    @property
    def held(self) -> str | None:
        """The fingerprint of the key the ring holds, or None."""
        return None if self.key is None else fingerprint(self.key)

    @property
    def summary(self) -> str:
        key_path, expected = self.key_path, self.expected[:16]
        if self.state == KEY_PRESENT:
            return f"the key at {key_path} matches the store (fingerprint {expected})"
        if self.state == KEY_UNREADABLE:
            reason = "unknown" if self.error is None else self.error.reason
            return (
                f"the key at {key_path} is there and this account cannot read it "
                f"({reason}): the log cannot be verified here, and every owner "
                "open (the daemon, a hook, the CLI) fails until it can; nothing "
                "is rekeyed over it"
            )
        if self.state == KEY_MISSING:
            return (
                f"no key for this store at {key_path}: the log cannot be verified "
                "here, and the next owner open (the daemon, a hook, the CLI) "
                "writes a new key and rekeys the log, so the rows signed before "
                "it stay unverifiable"
            )
        if self.state == KEY_FOREIGN:
            return (
                f"the key at {key_path} (fingerprint {(self.held or '')[:16]}) is "
                f"not the store's (fingerprint {expected}): the log cannot be "
                "verified here, and the next owner open keeps it only if it signs "
                "a segment of the log, and otherwise retires it, writes a new key "
                "and rekeys the log"
            )
        if self.state == KEY_UNCLAIMED:
            why = None if self.unfit is None else self.unfit.reason()
            return (
                f"the key ring under {self.keyring.keys_dir} is bound to no store "
                "file yet and holds a history this file does not carry"
                + ("" if why is None else f" ({why})")
                + ": it is bound only to the file that carries it, so every "
                "open of this file is read-only, and nothing is rekeyed or written"
            )
        return self._bound()[0]

    @property
    def remedy(self) -> str | None:
        """What to do, or None when the key is present."""
        if self.state == KEY_PRESENT:
            return None
        if self.state == KEY_UNREADABLE:
            return (
                "make it readable to this account again (owner-only, 0600), and "
                "the volume a link to it points into mounted"
            )
        if self.state == KEY_MISSING:
            return (
                "file the store's key back with `bettermemory key import FILE` "
                "(a `bettermemory key export` of the keys directory that held it, "
                f"or its key file {self.key_path.name} from where it was kept), or "
                "point BETTERMEMORY_KEYS_DIR at the directory that holds it, before "
                "anything opens the store for writing"
            )
        if self.state == KEY_FOREIGN:
            return (
                "file the key whose fingerprint the store records back with "
                "`bettermemory key import FILE`; `bettermemory log verify` shows "
                "which segments verify"
            )
        if self.state == KEY_UNCLAIMED:
            return (
                "this key ring belongs with the store file whose log "
                f"{self._history()} (`bettermemory key show` in a store's "
                "directory says whether it is that file): open that store first, "
                "whose first writing open binds the ring to it, or run "
                "`bettermemory key adopt --yes` with BETTERMEMORY_DIR naming its "
                "directory, which binds the ring to it on the record and refuses "
                "a file that does not carry that history"
            )
        return self._bound()[1]

    def _history(self) -> str:
        """What the store file an unclaimed ring belongs with carries: the
        row at the origin anchor's seq and the head's seq, as the ring
        records them."""
        marks: list[str] = []
        fit = self.unfit
        if fit is not None and fit.origin.seq is not None:
            marks.append(f"carries its origin anchor's row at seq {fit.origin.seq}")
        if fit is not None and fit.head is not None:
            marks.append(f"reaches its head checkpoint at seq {fit.head.seq}")
        return " and ".join(marks) or "carries the history it records"

    def _bound(self) -> tuple[str, str]:
        assert self.bound_to is not None
        return _bound_words(
            self.keyring, self.keyring.store_id, self.bound_to, self.unread
        )

    def refusal(self, path: Path) -> str:
        """Why a connection opened in this state refuses a write: the
        summary and remedy every reader says, and, for a missing or foreign
        key, the API's other way out, an owner's open, which writes a new key
        and starts a new chain segment (an unreadable key fails that open,
        and a ring bound elsewhere keeps it read-only)."""
        remedy = self.remedy or "open it from the process that owns the store"
        owner = (
            " If the key is lost for good, open it with allow_rekey=True from the "
            "process that owns the store, which writes a new key and starts a new "
            "chain segment."
            if self.state in (KEY_MISSING, KEY_FOREIGN)
            else ""
        )
        return (
            f"store {path} is open for reading only: {self.summary}. "
            f"{_sentence(remedy)}.{owner}"
        )

    def details(self) -> dict[str, Any]:
        """The state's facts, for a check's or a finding's details."""
        out: dict[str, Any] = {
            "key_state": self.state,
            "key_path": str(self.key_path),
            "expected_fingerprint": self.expected,
        }
        if self.held is not None:
            out["held_fingerprint"] = self.held
        if self.error is not None:
            out["reason"] = self.error.reason
        if self.bound_to is not None:
            out["bound_to"] = str(self.bound_to)
            out["binding_path"] = str(self.keyring.binding_path())
        if self.unfit is not None:
            out["binding_path"] = str(self.keyring.binding_path())
            out["ring_fit"] = self.unfit.to_dict()
        return out


def key_state(
    keyring: KeyRing,
    store_file: Path,
    expected: str,
    *,
    claim: bool = False,
    warn: bool = True,
    conn: sqlite3.Connection | None = None,
) -> KeyState:
    """The state of the key for the store file ``store_file``, whose meta
    records the fingerprint ``expected``: the one reading `Store.open`,
    doctor and posture share, in this order.

    1. unreadable: the key file is there and cannot be read
       (`log.KeyUnreadableError`). The owner's opener (``claim``) raises
       it, naming the file: read as a missing key it would be replaced
       and the log rekeyed, for a failure that may pass in a moment. A
       reader opens without it.
    2. bound elsewhere: the key ring is bound to another store file that
       still holds this store id, or cannot be read to tell. The owner's
       opener claims the ring first (`_claim_key_ring`), binding it to
       this file when it is bound to none or to a file that no longer
       holds the id; every other reader only reads the binding
       (`_key_ring_elsewhere`). Read whatever the key is: a copy carries
       its original's fingerprint, so its key matches, and every open of
       it is read-only.
    3. unclaimed: the key ring is bound to no store file and records a
       history (an origin anchor or a head) that this file does not carry
       (`_unclaimed_fit`), so neither the owner's opener nor a reader binds
       or signs with it: a copy that borrowed the id, where a ring from
       before 9.0.0rc13 was bound to whichever file opened first. Read
       before the claim, which would bind the ring.
    4. missing: no key file.
    5. foreign: a key whose fingerprint is not ``expected``.
    6. present: the key ``expected`` names.

    ``warn`` says an unreadable key and the binding in the log, as an open
    does; doctor and posture read quietly and render the state. ``conn``
    is the open's connection to the file, which the unclaimed check reads
    its log through; without one it opens a read-only one. An open (one
    with ``conn``) first finishes a `key rotate` that committed and did
    not rename its key (`finish_rotation`)."""
    if conn is not None:
        finish_rotation(keyring, store_file, expected)
    try:
        key = keyring.load_current()
    except KeyUnreadableError as exc:
        if claim:
            raise
        if warn:
            log.warning("store %s opens without its key: %s", store_file, exc)
        return KeyState(KEY_UNREADABLE, keyring, store_file, expected, error=exc)
    unfit = _unclaimed_fit(keyring, store_file, conn, key)
    if unfit is not None:
        unclaimed = KeyState(
            KEY_UNCLAIMED, keyring, store_file, expected, key=key, unfit=unfit
        )
        if warn:
            _warn_unclaimed(unclaimed)
        return unclaimed
    reads = _claim_key_ring if claim else _key_ring_elsewhere
    other, unread = reads(keyring, store_file, keyring.store_id, warn=warn)
    if other is not None:
        state = KEY_BOUND_ELSEWHERE
    elif key is None:
        state = KEY_MISSING
    elif fingerprint(key) != expected:
        state = KEY_FOREIGN
    else:
        state = KEY_PRESENT
    return KeyState(
        state, keyring, store_file, expected, key=key, bound_to=other, unread=unread
    )


# ---------------------------------------------------------------------------
# How a store file's log meets its key ring (ENG-106, ENG-93 item 6)
# ---------------------------------------------------------------------------

#: How the key ring's head checkpoint meets a store file's log, in the
#: words of `log.verify_chain`'s ``head`` status: no head, the last row,
#: an earlier row of the log (behind it, which writes catch up), a seq
#: past the log's end, or a row that carries another MAC.
HEAD_ABSENT = "absent"
HEAD_OK = "ok"
HEAD_STALE = "stale"
HEAD_PAST = "tail_deleted"
HEAD_MISMATCH = "mismatch"


@dataclass(frozen=True)
class RingFit:
    """How the log of one store file meets the key ring's origin anchor and
    head checkpoint (`ring_fit`): whether the file carries the history the
    ring records. ``origin`` is the anchor check (`log.check_origin`),
    ``head`` the checkpoint, ``head_status`` one of the ``HEAD_*`` names
    and ``log_end`` the file's last row.

    It fits when the row at the anchor's seq carries the anchor's MAC
    (and verifies under the anchor's key while that key is held: a broken
    anchor does not fit, an ``unverifiable`` one does, since a lost key
    tells nothing about the file) and its log reaches the head (the row at
    the head's seq is there with the head's MAC). A ring with neither
    anchor nor head records no history, and every file fits it."""

    origin: OriginCheck
    head: Head | None
    head_status: str
    log_end: LogRow | None

    @property
    def carries_origin(self) -> bool:
        return not self.origin.broken

    @property
    def reaches_head(self) -> bool:
        return self.head_status in (HEAD_ABSENT, HEAD_OK, HEAD_STALE)

    @property
    def fits(self) -> bool:
        return self.carries_origin and self.reaches_head

    def reason(self) -> str | None:
        """Why the file does not fit, in one clause, or None when it does."""
        if not self.carries_origin:
            seq = self.origin.seq
            if self.origin.reason == "missing":
                return (
                    f"its log holds no row at seq {seq}, where the key ring's "
                    "origin anchor says this host's history of the store begins"
                )
            if self.origin.reason == "mac":
                return (
                    f"its row at seq {seq} carries another MAC than the key "
                    "ring's origin anchor names"
                )
            return (
                f"its row at seq {seq} carries the origin anchor's MAC and does "
                "not verify under the key the anchor names, which this host holds"
            )
        if self.head is None or self.reaches_head:
            return None
        if self.head_status == HEAD_PAST:
            end = 0 if self.log_end is None else self.log_end.seq
            return (
                f"its log ends at seq {end}, before the key ring's head "
                f"checkpoint at seq {self.head.seq}"
            )
        return (
            f"its row at seq {self.head.seq} carries another MAC than the key "
            "ring's head checkpoint"
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "origin": self.origin.to_dict(),
            "head": {"status": self.head_status},
            "log_end": None if self.log_end is None else self.log_end.seq,
            "fits": self.fits,
        }
        if self.head is not None:
            out["head"]["seq"] = self.head.seq
        reason = self.reason()
        if reason is not None:
            out["reason"] = reason
        return out


def head_status(head: Head | None, conn: sqlite3.Connection) -> str:
    """How ``head`` meets the log on ``conn`` (the ``HEAD_*`` names), read
    as `log.verify_chain` reads it: two lookups by primary key."""
    if head is None:
        return HEAD_ABSENT
    last = last_row(conn)
    if last is None or head.seq > last.seq:
        return HEAD_PAST
    if head.seq == last.seq:
        return HEAD_OK if same_mac(head.mac, last.mac) else HEAD_MISMATCH
    named = next(iter_rows(conn, since_seq=head.seq - 1), None)
    if named is not None and named.seq == head.seq and same_mac(head.mac, named.mac):
        return HEAD_STALE
    return HEAD_MISMATCH


def ring_fit(
    conn: sqlite3.Connection, keyring: KeyRing, *, current_key: bytes | None = None
) -> RingFit:
    """How the store file open on ``conn`` meets ``keyring``'s origin anchor
    and head (`RingFit`): `key adopt` binds the ring only to a file that
    fits, and an owner's open binds a ring bound to no file only to one
    that fits (`_unclaimed_fit`). ``current_key``, a key the caller already
    holds, is tried first against the anchored row."""
    origin = check_origin(conn, keyring=keyring, current_key=current_key)
    head = keyring.read_head()
    return RingFit(origin, head, head_status(head, conn), last_row(conn))


def _fit_read_only(
    store_file: Path, keyring: KeyRing, key: bytes | None
) -> RingFit | None:
    """`ring_fit` of the store file at ``store_file``, read through a
    read-only connection of its own (`store_schema.connect_read_only`),
    for a caller that has none open (posture's read of the key); None
    when the file cannot be read so."""
    try:
        conn, scratch = connect_read_only(store_file, timeout=_FIT_READ_TIMEOUT)
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        return ring_fit(conn, keyring, current_key=key)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def _unclaimed_fit(
    keyring: KeyRing,
    store_file: Path,
    conn: sqlite3.Connection | None,
    key: bytes | None,
) -> RingFit | None:
    """How ``store_file`` fails to fit a key ring that is bound to no store
    file and records a history (an origin anchor or a head), or None when
    the ring is bound, records none, or the file fits (ENG-93 item 6).

    A ring from before 9.0.0rc13 has no binding, and its first owner's
    open after the upgrade bound it to whichever file it opened: a file
    that borrowed the store's id (a repository's ``./.claude-memory/``
    opened before the home store) took the ring, retired the home store's
    key and moved its head past the home log. The ring is now bound only
    to the file that carries its history, the rule `key adopt` binds by
    (`ring_fit`): its row at the anchor's seq carries the anchor's MAC and
    its log reaches the head. Any other file reads ``unclaimed``. A ring
    with neither anchor nor head records nothing to tell a file by, and
    the first owner's open binds it as before. A binding that cannot be
    read is not this rule's: `_claim_key_ring` keeps it, as it keeps a
    ring bound to a file it cannot read, and the open goes on.

    ``conn`` is the open's own connection; without one the file is read
    through a read-only connection (`_fit_read_only`), and a file that
    cannot be read so is not called unclaimed here: its open says why."""
    try:
        if keyring.read_binding() is not None:
            return None
    except OSError:
        return None
    if keyring.read_origin() is None and keyring.read_head() is None:
        return None
    if conn is None:
        fit = _fit_read_only(store_file, keyring, key)
    else:
        try:
            fit = ring_fit(conn, keyring, current_key=key)
        except sqlite3.Error:
            return None
    return None if fit is None or fit.fits else fit


def _warn_unclaimed(state: KeyState) -> None:
    """Say in the log that the key ring is bound to no store file and this
    file does not carry its history: a warning the first time this process
    opens the file so, the same line at DEBUG after, keyed with the
    binding's path in the guard `_warn_bound_elsewhere` keeps."""
    guard_key = (_resolved_for_guard(state.store_file), state.keyring.binding_path())
    with _BOUND_ELSEWHERE_LOCK:
        first = guard_key not in _BOUND_ELSEWHERE_WARNED
        _BOUND_ELSEWHERE_WARNED.add(guard_key)
    level = logging.WARNING if first else logging.DEBUG
    remedy = _sentence(state.remedy or "")
    log.log(level, "store %s: %s. %s", state.store_file, state.summary, remedy)


def pending_key_path(keyring: KeyRing) -> Path:
    """Where `key rotate` writes the new key before its commit
    (`key_lifecycle._rotate`), until the rename that makes it the current
    key: ``<store_id>.key.pending``. Not ``<store_id>.<x>.key``, which reads
    as a retired key."""
    return keyring.keys_dir / f"{keyring.store_id}.key.pending"


def write_key_once(path: Path, key: bytes) -> bool:
    """Create ``path`` holding ``key``, owner-only, only when nothing is
    there: False when a file is, which is never written over (an owner's
    open wrote a key meanwhile, or a copy is there already)."""
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        return False
    try:
        os.write(fd, key)
        fsync_file(fd)
    finally:
        os.close(fd)
    return True


def _bound_here_or_nowhere(keyring: KeyRing, store_file: Path) -> bool:
    try:
        bound = keyring.read_binding()
    except OSError:
        return False
    if bound is None:
        return True
    try:
        return bound == Path(store_file).resolve() or os.path.samefile(
            bound, store_file
        )
    except (OSError, ValueError):
        return False


def finish_rotation(keyring: KeyRing, store_file: Path, recorded: str) -> bytes | None:
    """Finish a `key rotate` that committed and did not rename its new key
    (a crash or a failure between its commit and the rename): when the
    pending key file (`pending_key_path`) holds the key the store's meta
    names (``recorded``) and the ring is bound to ``store_file`` or to no
    file, the current key is kept under its fingerprint first (a copy is
    there already when the rotation wrote it) and the pending key renamed
    over it, so the current key file is never absent. Returns that key, or
    None when there is nothing to finish or it cannot be done now (said in
    the log; the open goes on as it would have, and the next one tries
    again). A pending key meta does not name is a rotation that did not
    commit: it signs nothing and is left alone."""
    pending = pending_key_path(keyring)
    try:
        key = pending.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning(
            "store %s: the pending key %s cannot be read: %s", store_file, pending, exc
        )
        return None
    if len(key) != KEY_BYTES or fingerprint(key) != recorded:
        return None
    if not _bound_here_or_nowhere(keyring, store_file):
        return None
    try:
        current = keyring.load_current()
        if current is not None and fingerprint(current) != recorded:
            retired = keyring.retired_path(fingerprint(current))
            if not write_key_once(retired, current) and retired.read_bytes() != current:
                raise OSError(f"{retired} holds another key under the same prefix")
        replace_atomic(pending, keyring.current_path())
    except (OSError, KeyUnreadableError) as exc:
        try:
            now = keyring.load_current()
        except KeyUnreadableError:
            now = None
        if now is not None and fingerprint(now) == recorded:
            return now
        log.warning(
            "store %s: the rotated key %s could not be made current (%s); the "
            "next open tries again",
            store_file,
            recorded[:16],
            exc,
        )
        return None
    log.info(
        "store %s: the key %s a rotation committed is now the current key",
        store_file,
        recorded[:16],
    )
    return key


def _signing_key(
    conn: sqlite3.Connection, keyring: KeyRing, path: Path, key: bytes
) -> bytes:
    """The key a row appended inside the caller's write transaction signs
    with (`Store._append`, ENG-81). A connection reads its key once, at
    open; another connection's rekey can commit before this one's write
    transaction begins, and a row signed with the old key inside the new
    key's segment reads ``tampered``. So meta's ``key_fingerprint`` is read
    again here, under the write lock, which holds it until the commit:
    ``key`` while meta still names it; the key ring's current key when meta
    names that one (the rekey replaced the key in this ring, which only a
    process that can write the keys directory, and so holds the key, can
    do), after finishing a `key rotate` that committed and has not renamed
    its new key yet (`finish_rotation`). Otherwise still ``key``: meta is not authenticated, so a key meta
    names that this ring does not hold (an edit of the file, or a rekey
    under another keys directory) is not taken at its word. The owner goes
    on signing under its own key into that segment, as it did before this
    check. After such a rekey `log verify` reports the segment as a
    foreign_segment (tampered) from then on, this connection's own rows in
    it included; the next owner's open closes the segment
    (`Store._close_foreign_tail`, `Store._reclaim`), so the rows after it
    verify. Refusing would let anyone who can write the store file stop
    every write of a running daemon."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'key_fingerprint'"
    ).fetchone()
    recorded = None if row is None else str(row[0])
    if recorded is None or recorded == fingerprint(key):
        return key
    try:
        current = keyring.load_current()
    except KeyUnreadableError:
        current = None
    if current is None or fingerprint(current) != recorded:
        current = finish_rotation(keyring, path, recorded) or current
    if current is not None and fingerprint(current) == recorded:
        log.info(
            "store %s was rekeyed since this connection opened; it signs with "
            "the new key %s",
            path,
            recorded[:16],
        )
        return current
    return key


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
