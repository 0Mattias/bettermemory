"""The key checks a store open makes, and the key ring's binding to one file.

A store file's key is in one of five states (`key_state`, `KeyState`):
present, unreadable, missing, foreign, or bound elsewhere (the key ring
belongs to another store file that still holds the store id). `Store.open`,
doctor's `key` check and posture's `key.match` finding all read it through
`key_state`, and all three say it in its words (`KeyState.summary`,
`KeyState.remedy`). The owner's opener claims the ring for its file
(`_claim_key_ring`); every other reader asks whether the ring is bound to
another file still holding the store id (`_key_ring_elsewhere`), read
without opening it as a store (`_store_id_at`), warned about once per pair
of files (`_warn_bound_elsewhere`). `Store.open` also checks whether a key
signs a segment of the log (`_signs_a_segment`). Also
`configured_strict_provenance`. It logs as `bettermemory.store`. `store`
re-exports every name a caller reads; a test that patches
`_BOUND_ELSEWHERE_WARNED` patches this module. It lives apart from
`store.py` to keep each module under the package's 2,000-line bound
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
        f"move or remove one of the two stores, or delete {keyring.binding_path()} "
        "to bind the key ring to this one"
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
    (the session-start hook records the brief it shows, and a worker
    connection writes as its owner does), so on a second file with the
    bound store's id it would sign rows with that store's key and move its
    head past that store's log; a reader without the key is told the same,
    so every reader of the file says one state. A binding that cannot be
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

#: The five states of a store file's key, as `key_state` reads them.
KEY_PRESENT = "present"
KEY_UNREADABLE = "unreadable"
KEY_MISSING = "missing"
KEY_FOREIGN = "foreign"
KEY_BOUND_ELSEWHERE = "bound_elsewhere"
KEY_STATES = (
    KEY_PRESENT,
    KEY_UNREADABLE,
    KEY_MISSING,
    KEY_FOREIGN,
    KEY_BOUND_ELSEWHERE,
)


@dataclass(frozen=True)
class KeyState:
    """A store file's key, as `key_state` read it, and the words every
    reader says it in (`summary`, `remedy`): `Store.open` keeps it
    (`Store.key_state`) and refuses a write in them (`refusal`); doctor's
    ``key`` check and posture's ``key.match`` finding render them. Only
    ``present`` signs. A reader opens the other four for reading only;
    the owner's opener fails on an unreadable key, writes nothing to a
    file whose ring is bound elsewhere, and rekeys a missing or foreign
    key (or keeps a foreign one that signs a segment of the log).

    ``expected`` is the fingerprint the store's meta records, ``key`` the
    ring's current key (never in the repr), ``error`` why it could not be
    read, ``bound_to`` the store file the ring belongs to when that is
    another, and ``unread`` why that file could not be read to tell
    whether it still holds the store."""

    state: str
    keyring: KeyRing
    store_file: Path
    expected: str
    key: bytes | None = field(default=None, repr=False)
    error: KeyUnreadableError | None = None
    bound_to: Path | None = None
    unread: str | None = None

    @property
    def key_path(self) -> Path:
        return self.keyring.current_path()

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
                f"restore {self.key_path.name} into {self.keyring.keys_dir} from "
                "where it was kept, or point BETTERMEMORY_KEYS_DIR at the "
                "directory that holds it, before anything opens the store for "
                "writing"
            )
        if self.state == KEY_FOREIGN:
            return (
                "find the key whose fingerprint the store records and put it "
                "back; `bettermemory log verify` shows which segments verify"
            )
        return self._bound()[1]

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
        return out


def key_state(
    keyring: KeyRing,
    store_file: Path,
    expected: str,
    *,
    claim: bool = False,
    warn: bool = True,
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
    3. missing: no key file.
    4. foreign: a key whose fingerprint is not ``expected``.
    5. present: the key ``expected`` names.

    ``warn`` says an unreadable key and the binding in the log, as an open
    does; doctor and posture read quietly and render the state."""
    try:
        key = keyring.load_current()
    except KeyUnreadableError as exc:
        if claim:
            raise
        if warn:
            log.warning("store %s opens without its key: %s", store_file, exc)
        return KeyState(KEY_UNREADABLE, keyring, store_file, expected, error=exc)
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
