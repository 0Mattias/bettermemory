"""One SQLite file per store: the bettermemory 9 store.

The v8 store was a directory of markdown files with a derived FTS5 index
beside it; markdown is now the export and import format (``mirror`` and
``migrate_v8``). This store is one file, ``memory.sqlite``, whose tables
ARE the records: memories,
tombstones, episodes, verifications, conflicts, imports and quarantine,
plus the hash-chained log every mutation and telemetry event is a row of
(``bettermemory.log``). There is no derived index to rebuild or to skew:
the FTS5 table and its triggers are the v8 index's own, verbatim, so the
candidate query and its bm25 order are unchanged.

The fold. A mutation row carries the whole record after the change, and
the live write path and ``refold`` apply a row through the same function,
``_apply_mutation``. Replaying the log into a scratch database therefore
reproduces the tables exactly, and a row the replay does not produce is
one that entered outside the store: ``unaccounted``, the label
``provenance_for`` reports for it. ``log_verify`` merges that fold with
the chain report from ``bettermemory.log``.

Transactions. Each mutating method takes the write lock (``BEGIN
IMMEDIATE``) before it reads what it decides on, so the read, every check
and the write are one transaction, then changes the table, appends its log
rows, commits and moves the head checkpoint forward. A failure anywhere,
the commit's included, rolls the table change and the rows back together
and leaves the connection outside any transaction. A mutating call made
inside another's transaction, or inside a ``batch``, joins it under a
savepoint (``Store._transaction``). A long run of writes commits in chunks
of bounded length (``Store._in_chunks``), so no writer, another process
included, waits on it past its busy timeout.

Keys. ``create`` writes the store's key under the keys directory
(``log.default_keys_dir`` unless the caller names one) and records only
its fingerprint in ``meta``. ``open`` finds the key by fingerprint; a
missing or foreign key leaves the store readable and refuses mutations,
naming the key directory, unless the caller opens with
``allow_rekey=True`` (the owner's openers, `open_or_create` and the
migrations, do), when the key is retired, a fresh one written, and a
``rekey`` row appended. Meta's fingerprint is only what the file says:
a key that signs a segment of the log is this store's own whatever meta
names, and stays (`Store.open`). A key file that cannot be read is not a
missing one, and is never replaced. The key ring is bound to one store
file (``<store_id>.path``, `log.KeyRing.bind_store`): an opener of a
second file with the same store id, while the bound file still holds it
(or cannot be read to tell), writes nothing and refuses writes, the
owner's and a reader whose key matches alike.

Foreign segments. A segment of the log whose key this machine does not
hold, after one whose key it does, is rows a writer without the key put
into history this machine signed (`log.chain_segments`). Every read
labels its records ``unaccounted``, as it labels a row whose MAC fails,
save a row this machine's own key signed into it (a daemon that had the
store open while the forged row went in); the leading segments of a
store copied here keep their labels unless ``[security]
strict_provenance`` is set.

The origin anchor. Where this machine's history of the store begins is
kept in the key ring, outside the file (`log.KeyRing.write_origin`):
`create` anchors seq 1, the owner's opener anchors the ``rekey`` row it
writes when it takes over a store copied here, and on a store from before
the anchor existed it anchors the earliest segment this machine holds,
once (`_settle_origin`). The reads check it with the segment map
(`log.chain_trust`). An anchor the log no longer matches means the
history was rewritten where this machine's begins, so every segment whose
key is not held reads as foreign, leading ones included. An anchor never
moves: one whose row no key here verifies because the key that signed it
was lost reads ``unverifiable``, and the reads keep the rule without an
anchor until the key is back.

The parts. Modules beside this one hold parts of the store, to keep each
module under the package's 2,000-line bound (ENG-96); none of them but
`store_open` imports this one at run time, and this one re-exports every
name a caller imports from it. A method a module beside this one holds
is a function of `self: Store` that `Store` binds by name
(`write = _records.write`):

- `store_schema`: the vocabulary, the table texts, the errors and the
  paths a store opens by.
- `store_rows`: records to rows and back, and `_apply_mutation`, the one
  function the write path and the fold apply a mutation row through.
- `store_keys`: the key checks an open makes, and the key ring's binding
  to one store file.
- `store_records`: the methods that write and read records, standing
  answers and tombstones, and the corroboration check.
- `store_journal`: the episode journal's methods, its TTL prune and the
  health report's gauge.
- `store_tables`: verifications, conflicts, imports and quarantine: a put,
  a list and a delete each.
- `store_membership`: the rows of the membership tables, their control
  rows and id counters, and the store id reissue.
- `store_events`: the telemetry rows, the event reads with their window
  memo, and the log's reads, refold and verification.
- `store_provenance`: each record's provenance label and the trust reads
  built on it.
- `store_index`: the reads behind search (the FTS candidates, the links,
  the declared scopes) and the document frequency memo.
- `store_open`: the steps of `Store.open` once the file is connected. It
  reads the names this module re-exports, and `log`, through this module
  when it runs, so a patch here reaches the open.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import secrets
import shutil
import sqlite3
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeVar


from .store_keys import prepare_store_dir
from .identity import Actor
from .config import STORE_FILENAME
from .log import (
    REKEY,
    STORE_CREATED,
    HistoryOrigin,
    KeyRing,
    LogRow,
    OriginCheck,
    Segment,
    append_row,
    chain_segments as chain_segments,
    check_origin,
    fingerprint,
    last_rekey_name,
    row_verifies,
)

# `fts_index_text` passes through: bench/integrity/adapters.py reads it here.
from .search import fts_index_text as fts_index_text, tokenizer_fingerprint
from .time_utils import isoformat_utc
from .store_schema import (
    ADDITIVE_SCHEMA,
    CREATED_AFTER_ORPHANS as CREATED_AFTER_ORPHANS,
    DEFAULT_EPISODE_TTL_DAYS,
    FOLDED_TABLES,
    IMPORTED,
    IMPORTED_FROM_V8,
    LOCAL,
    MEMBERSHIP_EVENT_KINDS,
    MEMBERSHIP_MUTATION_KINDS,
    MEMBERSHIP_SCHEMA,
    MEMBERSHIP_STORE_ID,
    MEMBERSHIP_TABLES,
    MUTATION_KINDS,
    PAGE_SIZE as PAGE_SIZE,
    PROVENANCE_LABELS,
    SCHEMA,
    SCHEMA_VERSION,
    SYNCED,
    UNACCOUNTED,
    StoreRefusedError as StoreRefusedError,
    admit_store as admit_store,
    connect_read_only as connect_read_only,
    remove_read_only_copy as remove_read_only_copy,
    read_store_meta as read_store_meta,
    _MEMBERSHIP_SPECS as _MEMBERSHIP_SPECS,
    ConcurrentUpdateError,
    EpisodeVolume,
    MemoryNotFoundError,
    MemoryRow,
    NotFoundError,
    NotTombstonedError,
    RecordHome,
    StandingAnswer,
    TombstoneRow,
    TombstonedError,
    TrustRow,
    UnmigratedV8DirectoryError as UnmigratedV8DirectoryError,
    _add_membership_tables as _add_membership_tables,
    holds_v8_files as holds_v8_files,
    orphan_names as orphan_names,
    orphan_order as orphan_order,
    orphans_beside as orphans_beside,
    refuse_unmigrated_v8 as refuse_unmigrated_v8,
    resolve_keys_dir as resolve_keys_dir,
    store_path,
    version_meta,
)
from .store_rows import (
    _MEMORY_COLUMNS as _MEMORY_COLUMNS,
    _UPSERT_MEMORY as _UPSERT_MEMORY,
    _apply_mutation as _apply_mutation,
    _event_from_columns,
    _iso as _iso,
    _load_json,
    _record_columns as _record_columns,
    _scopes_text as _scopes_text,
    _table_rows as _table_rows,
    body_sha256 as body_sha256,
    event_import_payload,
)
from .store_keys import (
    KEY_PRESENT as KEY_PRESENT,
    KeyState as KeyState,
    _claim_key_ring,
    key_state as key_state,
    _signing_key,
    _store_id_at as _store_id_at,
    configured_strict_provenance as configured_strict_provenance,
)
from . import store_records as _records
from .store_records import (
    CORROBORATION_CHECK_SQL as CORROBORATION_CHECK_SQL,
)
from . import store_journal as _journal
from . import store_tables as _tables
from . import store_membership as _membership
from . import store_events as _events
from .store_events import (
    _EVENT_WINDOWS as _EVENT_WINDOWS,
)
from . import store_provenance as _provenance
from . import store_index as _index
from . import store_returned as _returned
from .store_index import (
    _ADMISSION_INPUTS,
    _DOC_FREQS as _DOC_FREQS,
    _DocFreqInputs,
    _row_mask,
)

log = logging.getLogger("bettermemory.store")


# The mutation kinds that change a record: a memory, a tombstone, an
# episode, a verification, a conflict, an import or a quarantined file. A
# write transaction that commits one counts in `Store.commits`; one that
# commits only telemetry or membership rows does not, since every tool
# call writes those before its own write (the caller's resolution, the
# turn's events) and a retry writes them again to no harm.
RECORD_MUTATION_KINDS = MUTATION_KINDS - MEMBERSHIP_MUTATION_KINDS


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


# How long a connection's statements wait for another writer's lock before
# they fail with SQLITE_BUSY, in seconds, by the role the connection plays
# (`Store.set_busy_timeout`). None of the three is measured yet; each is
# the 5.0 s every 9.x connection has opened with. What would set each is
# how long one write transaction holds the lock in use (BEGIN IMMEDIATE to
# COMMIT, logged per transaction with the writer's role): a role's timeout
# belongs above the tail of that distribution, the longest chunk of the
# CLI's long runs (`CHUNK_HOLD_SECONDS`) included, so a waiter never gives
# up on a healthy writer.
#
# The CLI's: every opener that names no other role (`_connect`), the CLI's
# commands and the daemon's own connection.
CLI_BUSY_TIMEOUT_SECONDS = 5.0
# A hook body's, on the daemon's hook workers. A hook's reply is waited for
# under `_daemon_client.HOOK_TIMEOUT_SECONDS` (15 s), so beside the hold
# times what would set it is how many write transactions one hook body
# runs (logged per hook): that count times this has to stay under the
# client's wait.
HOOK_BUSY_TIMEOUT_SECONDS = 5.0
# A tool body's, on the daemon's tool workers and in the stdio server; past
# it the call answers ``store_busy`` (`_handlers.RetryableRefusal`).
TOOL_BUSY_TIMEOUT_SECONDS = 5.0

# How long one chunk of a long write run goes on taking items under the
# write lock before it commits (`Store._in_chunks`), in seconds; the item
# that crosses it and the commit come on top. Measured on this tree
# (scratch run, Apple silicon, WAL with synchronous NORMAL): one
# transaction per `put_memory` cost 0.56 to 0.64 ms an item against 0.10
# to 0.11 ms inside one transaction, about 0.5 ms a commit, so chunks of
# 0.1 s add about half a percent to the run.
CHUNK_HOLD_SECONDS = 0.1
# How long a long write run sleeps between two chunks with the write lock
# free (`Store._yield_between_chunks`), in seconds. SQLite's lock is not
# queued: a writer that finds it held sleeps in its busy handler and tries
# again, up to 100 ms apart once it has waited a third of a second, and it
# takes the lock only when a try lands between a chunk's COMMIT and the
# next chunk's BEGIN IMMEDIATE. Without a gap of its own that window is
# only what happens between two chunks (the head checkpoint's write).
# Measured on this tree (scratch run, Apple silicon): a prune of 90,000
# tombstones with two processes writing beside it, twice at each yield.
# With no yield the writers' worst waits were 0.60 to 1.16 s (p99 up to
# 0.83 s) and the prune took 11.1 to 11.9 s. At 0.05 s the worst wait was
# 0.15 to 0.17 s, a thirtieth of the 5 s busy timeout, and the prune took
# 13.0 to 16.0 s while the writers landed about twice as many writes. At
# 0.025 s the worst wait was 0.30 s; at 0.1 s it was 0.17 s, no better,
# and the prune took 17.2 to 20.3 s. No write failed in any run.
CHUNK_YIELD_SECONDS = 0.05


# What a write the store could not take is answered with at the tool
# boundary (`retryable_code`): the code a client branches on.
STORE_BUSY = "store_busy"
STORE_FULL = "store_full"

# The SQLite primary result codes that mean the store, not the call, was in
# the way: another connection held the lock past the busy timeout
# (SQLITE_BUSY) or a table lock (SQLITE_LOCKED), or the disk or the
# database is full (SQLITE_FULL). The call's transaction was rolled back,
# and the same call can succeed once the lock or the space is free.
_RETRYABLE_CODES: dict[int, str] = {
    sqlite3.SQLITE_BUSY: STORE_BUSY,
    sqlite3.SQLITE_LOCKED: STORE_BUSY,
    sqlite3.SQLITE_FULL: STORE_FULL,
}

_T = TypeVar("_T")

# Whether a connection found inside a transaction nothing owned has been
# logged in this process (`note_residue`): the first is, later ones are
# not, so a store that keeps failing its rollbacks does not flood the log.
_residue_logged = False
_residue_lock = threading.Lock()


def note_residue(connection: str, then: str) -> None:
    """Log, once per process, that `connection` was found inside a
    transaction no call owned (one a failed rollback left open, holding
    the write lock against every other writer) and what was done about it
    (`then`). The store's own recovery (`Store._transaction`) and the
    daemon's worker reconnect (`daemon._Workers`) say it in these words."""
    global _residue_logged
    with _residue_lock:
        if _residue_logged:
            return
        _residue_logged = True
    log.warning("%s was left inside a transaction; %s", connection, then)


def error_code_name(exc: BaseException) -> str:
    """The SQLite result code an error carries, by name (``SQLITE_BUSY``),
    for a log line; the error's class name when it carries none."""
    name = getattr(exc, "sqlite_errorname", None)
    return name if isinstance(name, str) and name else type(exc).__name__


def retryable_code(exc: BaseException) -> str | None:
    """`STORE_BUSY` or `STORE_FULL` when `exc` is SQLite saying the store
    was busy, locked or full, read from its result code (an extended code
    reads as its primary one); None for every other error."""
    if not isinstance(exc, sqlite3.OperationalError):
        return None
    code = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return None
    return _RETRYABLE_CODES.get(code & 0xFF)


def _connect(path: Path) -> sqlite3.Connection:
    """The one way a store file is opened for writing: admitted first
    (`store_schema.admit_store`, which refuses a file whose schema this
    build did not write before anything is written to it), then the page
    size for a new file, WAL, synchronous NORMAL, the CLI's busy timeout (a
    connection opened for another role sets its own,
    `Store.set_busy_timeout`), foreign keys on, autocommit off so every
    write is an explicit transaction, and owner-only modes on the file and
    its WAL siblings."""
    _held = __import__("bettermemory.restore_lock").restore_lock.admit(path)
    conn = sqlite3.connect(
        str(path), timeout=CLI_BUSY_TIMEOUT_SECONDS, isolation_level=None
    )
    try:
        admit_store(conn, path)
        # A no-op on an existing store: the page size is fixed once the
        # file holds a table, and WAL mode fixes it for good.
        conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("PRAGMA journal_mode = WAL; PRAGMA schema_version")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.row_factory = sqlite3.Row
        for sibling in (
            path,
            path.with_suffix(path.suffix + "-wal"),
            path.with_suffix(path.suffix + "-shm"),
        ):
            if sibling.exists():
                with contextlib.suppress(OSError):
                    os.chmod(sibling, 0o600)
    except Exception:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class Store:
    """One store, one file, one connection. ``Store(directory)`` opens the
    store in the directory, creating it on first use, which is what every
    runtime entry point wants; ``create``, ``open`` and ``open_or_create``
    are the explicit forms, and each takes the directory or the file
    itself."""

    _path: Path
    _conn: sqlite3.Connection
    _keyring: KeyRing
    _key: bytes | None
    # A reader's key for checking, never for signing: the key ring's
    # current key, bound by a read-only open whose meta names another key
    # when the key ring holds the store's origin anchor or that key signs
    # a segment of the log (`open`), or by an owner's open refused because
    # the key ring belongs to another store file (`_bound_elsewhere`).
    # None otherwise; `_key` is the one key a mutation signs with.
    _read_key: bytes | None
    # What keeps the key ring from this file (`KeyState.withheld`): the
    # store file it is bound to, or its binding file when the ring is bound
    # to none and this file lacks its history. Writes nothing; None else.
    _bound_elsewhere: Path | None
    # The key's state as this connection's open read it (`key_state`),
    # present once it holds a key to sign with; None only inside `_adopt`.
    _key_state: KeyState | None
    # Opened by `open(..., read_only=True)`, and the private copy's
    # directory it reads from (`connect_read_only`), removed by `close`
    # or, left open, when the store is collected or at exit (`_keep_copy`).
    _read_only: bool
    _scratch: Path | None
    _store_id: str
    _last_appended: LogRow | None
    # How many `_transaction` entries are open on this connection: 0 when
    # none, 1 for the owner of the write transaction, more for the calls
    # nested in it. The one record of who owns the transaction.
    _tx_depth: int
    # Whether the write transaction open on this connection has changed a
    # record (`RECORD_MUTATION_KINDS`); a nested write that fails puts it
    # back to what it was (`_joined`).
    _tx_records: bool
    # The records the open write transaction may have brought back into
    # force, or removed with a ``returned`` mark, each with the record
    # whose change did it: read again and marked once, at the commit
    # (`store_returned`, ENG-200). A nested write that fails puts it back.
    _tx_returns: dict[str, tuple[str, str | None]]
    # How many write transactions this connection has committed that
    # changed a record. The tool boundary reads it before and after a call
    # (`_handlers._kept_or_retryable`): a store error after the count moved
    # is one the call met after its write was kept.
    commits: int
    # The busy timeout this connection was opened or last set with, in
    # seconds (`set_busy_timeout`): kept beside the connection so `reopen`,
    # which runs on another thread, reads it without touching a connection
    # SQLite binds to the thread that opened it.
    _busy_timeout: float
    _unaccounted_memory_ids: set[str]
    # (id, log_mac) pairs whose pointer into the log verified; a row gets
    # a new MAC on every mutation, so a stale entry never matches.
    _pointer_cache: dict[tuple[str, str], bool]
    # The chain's segments (`log.chain_segments`), each with its key when
    # this machine holds it and whether it is foreign: read off the rekey
    # rows once per connection, on the first pointer that needs it.
    _segments: list[Segment] | None
    # The check of the origin anchor the segment map was read under
    # (`log.chain_trust`); None while `_segments` is.
    _origin: OriginCheck | None
    # `[security] strict_provenance` for this connection: None until the
    # first row in a segment whose key is not held needs it, then read
    # once through `load_config` (`strict_provenance`). An opener that
    # passes it explicitly sets it here and nothing is read.
    _strict_provenance: bool | None

    def __init__(
        self,
        path: Path | str,
        *,
        keys_dir: Path | str | None = None,
        strict_provenance: bool | None = None,
    ) -> None:
        opened = self.open_or_create(
            path, keys_dir=keys_dir, strict_provenance=strict_provenance
        )
        self._adopt(
            opened._path,
            opened._conn,
            opened._keyring,
            opened._key,
            opened._store_id,
            strict_provenance=opened._strict_provenance,
        )
        self._read_key = opened._read_key
        self._bound_elsewhere = opened._bound_elsewhere
        self._key_state = opened._key_state

    def _adopt(
        self,
        path: Path,
        conn: sqlite3.Connection,
        keyring: KeyRing,
        key: bytes | None,
        store_id: str,
        *,
        strict_provenance: bool | None = None,
    ) -> None:
        self._path = path
        self._conn = conn
        self._keyring = keyring
        self._key = key
        self._read_key = None
        self._bound_elsewhere = None
        self._key_state = None
        self._read_only = False
        self._scratch = None
        self._store_id = store_id
        self._last_appended = None
        self._tx_depth = 0
        self._tx_records = False
        self._tx_returns = {}
        self.commits = 0
        # What `_connect` opened every connection with.
        self._busy_timeout = CLI_BUSY_TIMEOUT_SECONDS
        self._unaccounted_memory_ids = set()
        self._pointer_cache = {}
        self._segments = None
        self._origin = None
        self._strict_provenance = strict_provenance

    @classmethod
    def _bind(
        cls,
        path: Path,
        conn: sqlite3.Connection,
        keyring: KeyRing,
        key: bytes | None,
        store_id: str,
        *,
        strict_provenance: bool | None = None,
    ) -> Store:
        self = cls.__new__(cls)
        self._adopt(
            path, conn, keyring, key, store_id, strict_provenance=strict_provenance
        )
        if key is not None:
            self._key_state = KeyState(
                KEY_PRESENT, keyring, path, fingerprint(key), key=key
            )
        return self

    @classmethod
    def create(
        cls,
        path: Path | str,
        *,
        keys_dir: Path | str | None = None,
        strict_provenance: bool | None = None,
    ) -> Store:
        """A new store at ``path`` with a fresh key. Refuses an existing file.
        Its ``store_created`` row, seq 1, is where this machine's history
        of it begins, and the origin anchor names it; the key ring is bound
        to this file (`log.KeyRing.bind_store`). ``strict_provenance`` is
        `open`'s."""
        path = store_path(path)
        if path.exists():
            raise FileExistsError(f"a store already exists at {path}")
        prepare_store_dir(path, keys_dir)
        orphans = orphans_beside(path)
        conn = _connect(path)
        try:
            conn.executescript(SCHEMA)
            conn.executescript(MEMBERSHIP_SCHEMA)
            conn.executescript(ADDITIVE_SCHEMA)
            store_id = secrets.token_hex(16)
            keyring = KeyRing(
                Path(keys_dir) if keys_dir is not None else resolve_keys_dir(),
                store_id,
            )
            key = keyring.create_key()
            from . import __version__

            created = isoformat_utc(datetime.now(timezone.utc))
            meta = {
                "store_id": store_id,
                "schema_version": str(SCHEMA_VERSION),
                **version_meta(),
                "tokenizer_fingerprint": tokenizer_fingerprint(),
                "engine_version": __version__,
                "key_fingerprint": fingerprint(key),
                "created": created,
                MEMBERSHIP_STORE_ID: secrets.token_hex(16),
            }
            if orphans:
                meta[CREATED_AFTER_ORPHANS] = json.dumps([p.name for p in orphans])
            store = cls._bind(
                path, conn, keyring, key, store_id, strict_provenance=strict_provenance
            )
            with store._transaction() as tx:
                tx.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)", list(meta.items())
                )
                first = store._append(
                    tx,
                    STORE_CREATED,
                    {
                        "store_id": store_id,
                        "schema_version": SCHEMA_VERSION,
                        "tokenizer_fingerprint": meta["tokenizer_fingerprint"],
                        "engine_version": __version__,
                        "created": created,
                    },
                    session=None,
                )
            store._record_origin(first, key)
            # The new store's id is fresh, so the key ring was bound to no
            # file: this one is the file it belongs to.
            _claim_key_ring(keyring, path, store_id)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
            return store
        except Exception:
            conn.close()
            raise

    @classmethod
    @_index.reconciles_tokenizer
    def open(
        cls,
        path: Path | str,
        *,
        keys_dir: Path | str | None = None,
        allow_rekey: bool = False,
        strict_provenance: bool | None = None,
        read_only: bool = False,
    ) -> Store:
        """An existing store. Without ``allow_rekey`` (the default) a store
        whose key under the key directory is missing or foreign opens for
        reading and refuses to mutate, naming the directory; with it the
        key is replaced and a ``rekey`` row appended, which splits the
        chain. The owner's own openers pass it (`open_or_create`, which
        the hooks, the daemon and the CLI open through, and the
        migrations); a reader, a copy on another machine or a daemon
        under the wrong key directory never rekeys by accident.

        Meta's ``key_fingerprint`` is not authenticated: anyone with the
        file can change it. So the owner's opener does not take a
        mismatch at its word. When the key under the key directory signs
        a segment of this log (`_signs_a_segment`), the store is this
        machine's own with rows in it that it did not sign: the key stays
        current, meta is set back to it, and a ``rekey`` row closes the
        chain's last segment when that segment is not the key's
        (`_reclaim`). Only a key that signs none of the log is retired
        and replaced, as for a store copied here without its key. With
        the key in place, the owner's opener still closes a last segment
        a ``rekey`` row it did not write named (`_close_foreign_tail`),
        so its own rows after that segment verify. Neither repair can
        fail the open: a store that cannot be written logs why and opens.

        The owner's opener also keeps the origin anchor (the module
        docstring): the ``rekey`` row it writes for a store copied here is
        where this machine's history of it begins (`_take_over_origin`),
        and on any other writing open a store with no anchor gets one
        (`_settle_origin`). Nothing moves an anchor that exists, a lost key
        included. A reader never writes one.

        Before any of that, the owner's opener claims the key ring for this
        file (`_claim_key_ring`): the ring is found by the store id the
        file carries, so a second file with the same id (a copy in another
        directory, or one made to borrow a known id) would otherwise retire
        the first one's key, or sign with it, and move its head. When the
        ring is bound to another file that still holds this store id (or
        that cannot be read to tell), this open writes nothing and opens as
        a reader does, refusing writes even when its key matches, with a
        warning naming both files. A reader never binds the ring; a reader
        whose key matches reads the binding, and on such a second file
        opens the same way, since a reader with the key can write.

        A key file that is there and cannot be read fails the owner's open
        (`log.KeyUnreadableError`) instead of being replaced; a reader
        opens without it. Which of these the key is, the open reads once
        through `store_keys.key_state` and keeps (`key_state`), in the
        words doctor and posture say it in, and a write it refuses says.

        ``strict_provenance`` is ``[security] strict_provenance`` for this
        connection; None (the default) reads it from the config the first
        time a row needs it (`strict_provenance`).

        ``read_only`` (the read-only commands: `log verify`, `log export`,
        `log anchor`, `stats`, `memory history`, `doctor`, `security scan`)
        opens a reader that writes nothing to the file or beside it
        (`store_schema.connect_read_only`): no WAL switch, no chmod, no
        additive schema step (a reader finds a missing table empty), no
        ``-wal`` or ``-shm`` left behind, and every write fails in
        SQLite. It never rekeys, so it takes no ``allow_rekey``."""
        from .store_open import _open_connected

        if read_only and allow_rekey:
            raise ValueError("a read-only open never rekeys")
        path = store_path(path)
        if not path.is_file():
            raise FileNotFoundError(f"no store at {path}")
        scratch: Path | None = None
        if read_only:
            conn, scratch = connect_read_only(path, timeout=CLI_BUSY_TIMEOUT_SECONDS)
        else:
            conn = _connect(path)
        try:
            return _open_connected(
                cls,
                path,
                conn,
                scratch,
                keys_dir=keys_dir,
                allow_rekey=allow_rekey,
                strict_provenance=strict_provenance,
                read_only=read_only,
            )
        except Exception:
            conn.close()
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            raise

    @classmethod
    def open_or_create(
        cls,
        path: Path | str,
        *,
        keys_dir: Path | str | None = None,
        strict_provenance: bool | None = None,
    ) -> Store:
        """Open the store at `path`, or create one. Refuses to create one
        in a directory that holds an un-migrated bettermemory 8 store: a
        server pointed at such a directory would otherwise serve an empty
        store and say nothing, and `bettermemory migrate v8` is the
        answer. `create` itself is unchanged. ``strict_provenance`` is
        `open`'s."""
        path = store_path(path)
        if path.is_file():
            # The owner-process opener: a store this machine writes to
            # whose key moved is its owner's to rekey.
            return cls.open(
                path,
                keys_dir=keys_dir,
                allow_rekey=True,
                strict_provenance=strict_provenance,
            )
        refuse_unmigrated_v8(path)
        return cls.create(path, keys_dir=keys_dir, strict_provenance=strict_provenance)

    def _close_foreign_tail(self) -> None:
        """Close the chain's last segment when a ``rekey`` row this key did
        not write named it: the owner's opener, with meta and the key in
        agreement. Rows this store appends after a forged ``rekey`` row
        would otherwise sit in the segment that row opened, under a key
        nobody here holds, where `log verify` never checks them and only
        the reads' fallback to this machine's own key verifies them
        (`_pointer_verified`). One indexed lookup when there is nothing to
        close; the write lock only when there is."""
        assert self._key is not None
        own = fingerprint(self._key)
        named = last_rekey_name(self._conn)
        if named is None or named == own:
            return
        row: LogRow | None = None
        try:
            with self._transaction() as tx:
                # Another opener may have closed it while this one waited
                # for the lock.
                named = last_rekey_name(tx)
                if named is not None and named != own:
                    row = self._append(
                        tx,
                        REKEY,
                        {"old_fingerprint": named, "new_fingerprint": own},
                        session=None,
                    )
        except (sqlite3.Error, OSError) as exc:
            log.warning(
                "store %s: the log's last segment is under key %s, not this "
                "store's key %s, and it could not be closed (%s); rows written "
                "here will not verify until it is",
                self._path,
                (named or "")[:16],
                own[:16],
                exc,
            )
            return
        if row is None or named is None:
            return
        self._forget_segments()
        log.warning(
            "store %s: the log's last segment, opened by a rekey row this host "
            "did not write, is under key %s; its rows read unaccounted and a "
            "rekey row at seq %d returns the chain to this store's key %s. "
            "`bettermemory log verify` reports the segment",
            self._path,
            named[:16],
            row.seq,
            own[:16],
        )

    def _reclaim(self, segments: Sequence[Segment], *, meta_fingerprint: str) -> None:
        """Keep this machine's key for a store whose meta names another key
        although that key signs a segment of the log (`open`): set meta back
        to the key and, when the chain's last segment is not the key's,
        append the ``rekey`` row that returns the chain to it, naming that
        segment's key as the log gives it, never meta's. The foreign
        segments stay in the log, where every read labels their rows
        ``unaccounted`` and `log verify` reports them."""
        assert self._key is not None
        own = fingerprint(self._key)
        tail = segments[-1].key_fingerprint if segments else own
        foreign = ", ".join(
            f"seq {segment.first_seq} under key {segment.key_fingerprint[:16]}"
            for segment in segments
            if segment.foreign
        )
        appended: LogRow | None = None
        try:
            with self._transaction() as tx:
                tx.execute(
                    "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'", (own,)
                )
                # The last rekey row names the tail when there is one: read
                # under the lock, so a rekey row another opener appended
                # while this one waited (closing the same segment) counts.
                named = last_rekey_name(tx)
                if named is not None:
                    tail = named
                if tail != own:
                    appended = self._append(
                        tx,
                        REKEY,
                        {"old_fingerprint": tail, "new_fingerprint": own},
                        session=None,
                    )
        except (sqlite3.Error, OSError) as exc:
            log.warning(
                "store %s: meta names key %s but this host's key %s signs the "
                "log; the store opens with that key, and meta could not be "
                "set back (%s)",
                self._path,
                meta_fingerprint[:16],
                own[:16],
                exc,
            )
            return
        self._forget_segments()
        closing = (
            "meta is set back to it"
            if appended is None
            else f"a rekey row at seq {appended.seq} returns the chain to it"
        )
        log.warning(
            "store %s: meta named key %s, but this host's key %s signs the log, "
            "so it stays the store's key and no new key is written; %s. Rows no "
            "key on this host signed, after rows it did: %s. They read "
            "unaccounted, and `bettermemory log verify` reports them",
            self._path,
            meta_fingerprint[:16],
            own[:16],
            closing,
            foreign or "none",
        )

    def _forget_segments(self) -> None:
        """Drop this connection's segment map and the anchor check it was
        read under, so the next pointer check reads both again: after a
        repair appended a ``rekey`` row, or an anchor was written."""
        self._segments = None
        self._origin = None

    def _record_origin(self, row: LogRow, key: bytes) -> None:
        """Anchor this machine's history of the store at ``row``, signed
        by ``key``, unless an anchor is already there
        (`log.KeyRing.write_origin`). A key ring that cannot be written
        logs why, as the head does: the store opens, and a later writing
        open anchors it (`_settle_origin`)."""
        try:
            written = self._keyring.write_origin(
                seq=row.seq, mac=row.mac, key_fingerprint=fingerprint(key)
            )
        except OSError as exc:
            log.warning("origin anchor for %s not written: %s", self._path, exc)
            return
        if written:
            self._forget_segments()

    def _settle_origin(self) -> None:
        """The owner's opener, on a store with no origin anchor: a store
        from before the anchor existed, or one only read here so far. The
        anchor goes on the first row of the earliest segment whose key this
        machine holds, current or retired, and only when that row verifies
        under the key its segment is held by; with no such segment nothing
        is written. Trust on first use: whatever was done to the file
        before this open is taken as it stands, so a history rewritten
        before it is not caught by the anchor (the rule without one still
        is). On a store whose anchor names no key (written before anchors
        named one) it names the key instead (`_name_origin_key`). Costs one
        file read on a store whose anchor names its key, and never fails
        the open."""
        assert self._key is not None
        existing = self._keyring.read_origin()
        if existing is not None:
            if existing.key_fingerprint is None:
                self._name_origin_key(existing)
            return
        try:
            segments = chain_segments(
                self._conn,
                keyring=self._keyring,
                current_fingerprint=fingerprint(self._key),
                current_key=self._key,
            )
            held = next((segment for segment in segments if segment.held), None)
            if held is None or held.key is None or held.first_row is None:
                return
            verifies = row_verifies(held.key, held.first_row)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            # A row an editor left unreadable (a seq that is not a number;
            # a MAC that is not ASCII reads as one that does not verify,
            # below): no anchor, and the open goes on.
            log.warning(
                "origin anchor for %s not written: the log could not be read (%s)",
                self._path,
                exc,
            )
            return
        if not verifies:
            log.warning(
                "store %s: the first row of the earliest segment this host holds "
                "(seq %d) does not verify under its key; no origin anchor is "
                "written. `bettermemory log verify` reports the row",
                self._path,
                held.first_seq,
            )
            return
        self._record_origin(held.first_row, held.key)

    def _name_origin_key(self, origin: HistoryOrigin) -> None:
        """Name the key that signed the anchored row, on an anchor written
        before anchors named their key, when a key this machine holds still
        verifies the row (the anchor holds): from then on a row rewritten
        under its own MAC reads as the mismatch it is, and one whose key is
        later lost reads ``unverifiable`` rather than as tampering
        (`log.check_origin`). The anchor itself does not move. An anchor
        that does not hold stays as it is and keeps reading as before.
        Never fails the open."""
        try:
            check = check_origin(
                self._conn, keyring=self._keyring, current_key=self._key
            )
        except (sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
            log.warning(
                "store %s: the origin anchor's key not named: the log could not "
                "be read (%s)",
                self._path,
                exc,
            )
            return
        if not check.holds or check.signer is None:
            return
        try:
            self._keyring.name_origin_key(origin, check.signer)
        except OSError as exc:
            log.warning(
                "store %s: the origin anchor's key not named: %s", self._path, exc
            )

    def _take_over_origin(self, row: LogRow) -> None:
        """The owner's opener wrote a new key and the ``rekey`` row ``row``
        for a store whose key it did not hold: where this machine's history
        of the store begins, when no anchor names an earlier row.

        An anchor that exists stays where it is, always. When this machine
        lost the key that signed the anchored row (the key file is gone),
        no key here verifies that row any more; the anchor then reads
        ``unverifiable`` (`log.check_origin`), the store's reads fall back
        to the rule without an anchor, and putting the key file back makes
        it hold again. Moving it to ``row`` instead would accept, for good,
        whatever an editor did to the anchored row before the key went. An
        anchor that names no key is given one when a held key, the one just
        retired among them, still verifies its row (`_settle_origin`)."""
        if self._keyring.read_origin() is None:
            assert self._key is not None
            self._record_origin(row, self._key)
            return
        self._settle_origin()

    def _keep_copy(self, read_only: bool, scratch: Path | None) -> None:
        # A full copy of the store must not outlive a store never closed.
        self._read_only, self._scratch = read_only, scratch
        if scratch is not None:
            weakref.finalize(self, remove_read_only_copy, self._conn, scratch)

    def close(self) -> None:
        self._conn.close()
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None

    @property
    def busy_timeout(self) -> float:
        """How long this connection's statements wait for another writer's
        lock, in seconds: the value it was opened or last set with
        (`set_busy_timeout`), read from any thread."""
        return self._busy_timeout

    def set_busy_timeout(self, seconds: float) -> None:
        """Set how long this connection's statements wait for another
        writer's lock before they fail with SQLITE_BUSY: its role's value,
        set by whoever opened it for that role (the daemon's workers)."""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(
                f"a busy timeout is a finite number of seconds, got {seconds!r}"
            )
        self._conn.execute(f"PRAGMA busy_timeout = {int(seconds * 1000)}")
        self._busy_timeout = seconds

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- identity -----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def keyring(self) -> KeyRing:
        return self._keyring

    @property
    def keys_dir(self) -> Path:
        return self._keyring.keys_dir

    @property
    def store_id(self) -> str:
        return self._store_id

    @property
    def key_fingerprint(self) -> str:
        return self.meta()["key_fingerprint"]

    @property
    def has_key(self) -> bool:
        return self._key is not None

    @property
    def key_state(self) -> KeyState:
        """The state of this file's key (`store_keys.key_state`), as this
        connection's open read it, or present once it holds a key to sign
        with (an owner's open that rekeyed or reclaimed the store). Doctor
        renders it; posture reads the same function."""
        assert self._key_state is not None
        return self._key_state

    @property
    def can_verify(self) -> bool:
        """Whether this connection checks pointers into the log: it holds
        the key it writes with, or, opened for reading with meta naming a
        key this machine does not hold, this machine has a history of the
        store (an origin anchor, or a segment its current key signs; see
        `open`). ``has_key`` is the narrower question, whether it may
        write."""
        return self._verify_key is not None

    @property
    def _verify_key(self) -> bytes | None:
        """The key reads check this machine's own rows under: the one a
        mutation signs with, else a reader's (`_read_key`)."""
        return self._key if self._key is not None else self._read_key

    @property
    def strict_provenance(self) -> bool:
        """``[security] strict_provenance`` for this connection: as the
        opener passed it, or read once through the config the first time a
        row in a segment whose key is not held is labelled. A store under
        one key throughout never reads it."""
        strict = self._strict_provenance
        if strict is None:
            strict = self._strict_provenance = configured_strict_provenance()
        return strict

    def meta(self) -> dict[str, str]:
        return {
            str(r["key"]): str(r["value"])
            for r in self._conn.execute("SELECT key, value FROM meta ORDER BY key")
        }

    def store_orphans(self) -> list[Path]:
        """The saved orphans this store was created after that are still
        beside it, oldest first; empty for a store that existed before,
        including one copied back from an orphan, which carries no such
        names."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (CREATED_AFTER_ORPHANS,)
        ).fetchone()
        if row is None:
            return []
        directory = self._path.parent
        return sorted(
            (
                directory / name
                for name in orphan_names(str(row[0]))
                if (directory / name).is_file()
            ),
            key=orphan_order,
        )

    # -- transactions -------------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One public call's reads and writes as one unit, on this store's
        connection.

        The outermost entry owns the write transaction: it takes the write
        lock (``BEGIN IMMEDIATE``) before the call reads anything, so every
        check the call makes is made on what it then writes over, and it
        commits at the end. A failure anywhere, the ``COMMIT`` itself
        included (a busy, full or failing disk), rolls back while the
        connection is still in the transaction and leaves this store owning
        nothing, so the next call begins its own. A nested entry (a
        mutating call inside another's, or inside a `batch`) joins the
        owner's transaction under a savepoint: what it writes commits with
        the owner, and its own failure takes back only what it wrote, so a
        caller that catches it goes on from where it stood.

        Ownership is this store's depth counter, never the connection's
        ``in_transaction``: a transaction a failed rollback left open would
        read as one to join. The owner finds such a residue at its entry
        and rolls it back first (`_clear_residue`). The head checkpoint
        moves once, after the owner's commit, to the last row that commit
        holds, and a commit that changed a record counts in `commits`."""
        conn = self._conn
        if self._tx_depth:
            yield from self._joined(conn)
            return
        if conn.in_transaction:
            self._clear_residue(conn)
        self._last_appended = None
        self._tx_records = False
        self._tx_returns = {}
        # BEGIN before the depth is set: a BEGIN that fails (the busy wait
        # against another writer ran out) leaves this store owning nothing,
        # and the next call begins its own transaction.
        conn.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        try:
            yield conn
            self._require_open(conn)
            self._flush_returns(conn)
            conn.execute("COMMIT")
        except BaseException:
            self._roll_back(conn)
            raise
        finally:
            self._tx_depth = 0
            self._tx_returns = {}
        if self._tx_records:
            self.commits += 1
        self._move_head()

    def _clear_residue(self, conn: sqlite3.Connection) -> None:
        """Roll back a transaction no call on this store owns: with no
        entry open (depth 0), one the connection is still inside is what a
        failed rollback left (`_roll_back`), and it holds the write lock
        against every other writer. Logged once per process
        (`note_residue`). A rollback that fails again raises, and the call
        that found the residue fails with it."""
        note_residue(f"the connection to store {self._path}", "it was rolled back")
        conn.execute("ROLLBACK")

    def _joined(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """The nested entry of `_transaction`: a savepoint inside the
        owner's transaction, released on success and rolled back to on
        failure, with the last appended row put back to what it was so the
        head never names a row the rollback took away."""
        self._require_open(conn)
        name = f"nested_{self._tx_depth}"
        appended = self._last_appended
        records = self._tx_records
        returns = dict(self._tx_returns)
        conn.execute(f"SAVEPOINT {name}")
        self._tx_depth += 1
        try:
            yield conn
            conn.execute(f"RELEASE {name}")
        except BaseException:
            self._last_appended = appended
            self._tx_records = records
            self._tx_returns = returns
            if conn.in_transaction:
                try:
                    conn.execute(f"ROLLBACK TO {name}")
                    conn.execute(f"RELEASE {name}")
                except sqlite3.Error as exc:
                    log.warning(
                        "store %s: a nested write could not be taken back (%s); "
                        "the owner's transaction is rolled back with it",
                        self._path,
                        exc,
                    )
                    self._roll_back(conn)
            raise
        finally:
            self._tx_depth -= 1

    def _require_open(self, conn: sqlite3.Connection) -> None:
        """Refuse to go on in a transaction SQLite has already rolled back.
        An error inside one (a full disk, an I/O error, an interrupt) can
        end it, and a caller that caught that error would otherwise go on
        writing in autocommit, each statement landing on its own."""
        if not conn.in_transaction:
            raise sqlite3.OperationalError(
                f"the write transaction on store {self._path} was rolled back "
                f"by an error inside it; nothing it wrote was kept"
            )

    def _roll_back(self, conn: sqlite3.Connection) -> None:
        """Roll back the owner's transaction while the connection is still
        in it. One SQLite has already rolled back needs nothing. A rollback
        that fails is logged and the caller's own error goes on: the
        connection then holds a transaction nobody owns, which the next
        call on it rolls back first (`_clear_residue`), the daemon's
        workers close and reopen before their next job (`daemon._Workers`)
        and a process that ends closes."""
        if not conn.in_transaction:
            return
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            log.warning("store %s: rollback failed: %s", self._path, exc)

    @contextlib.contextmanager
    def batch(self) -> Iterator[Store]:
        """One transaction around many mutations: everything put or
        deleted inside the block commits together or not at all, and the
        head checkpoint moves once, at the commit. A migration runs in
        one, so a failure part way leaves the store as it was. A batch
        opened while a transaction is already open on this store joins it
        (`_transaction`), as a mutating call does."""
        with self._transaction():
            yield self

    def _in_chunks(self, items: Sequence[_T], apply: Callable[[_T], None]) -> None:
        """Apply `apply` to each of `items` in write transactions (chunks)
        of their own, so a long run (a prune, a scope rename over every
        record) leaves other writers, the daemon's hooks and tools among
        them, a way in. A chunk takes items until `CHUNK_HOLD_SECONDS`
        have passed, commits, and the run sleeps `CHUNK_YIELD_SECONDS`
        with the lock free before the next (`_yield_between_chunks`).
        `apply` decides each item again under its chunk's lock, since
        another writer may change it between chunks. No items takes no
        lock. Inside a transaction a caller already holds, every item goes
        into that one.

        A chunk that fails rolls back alone: the chunks before it stay
        committed, and the items after it are not tried. What `apply`
        decides on is still in the store for those, so running the same
        call again finishes the run."""
        if self._tx_depth:
            for item in items:
                apply(item)
            return
        done = 0
        while done < len(items):
            with self._transaction():
                started = time.monotonic()
                while done < len(items):
                    apply(items[done])
                    done += 1
                    if time.monotonic() - started >= CHUNK_HOLD_SECONDS:
                        break
            if done < len(items):
                self._yield_between_chunks()

    def _yield_between_chunks(self) -> None:
        """The pause between two chunks of `_in_chunks`, the write lock
        free, so a writer waiting in its busy handler takes it
        (`CHUNK_YIELD_SECONDS`)."""
        time.sleep(CHUNK_YIELD_SECONDS)

    def _move_head(self) -> None:
        appended = self._last_appended
        if appended is not None:
            try:
                self._keyring.write_head(seq=appended.seq, mac=appended.mac)
            except OSError as exc:
                log.warning("head checkpoint for %s not written: %s", self._path, exc)

    def _append(
        self,
        conn: sqlite3.Connection,
        kind: str,
        payload: Mapping[str, Any],
        *,
        session: str | None,
        ts: str | None = None,
    ) -> LogRow:
        if self._key is None:
            # In the words the open read the key's state in (`key_state`).
            raise RuntimeError(self.key_state.refusal(self._path))
        # Meta read again under the write lock: a rekey another connection
        # committed since this one opened names the key to sign with.
        key = _signing_key(conn, self._keyring, self._path, self._key)
        if key != self._key:
            self._key = key
            self._key_state = KeyState(
                KEY_PRESENT, self._keyring, self._path, fingerprint(key), key=key
            )
            self._forget_segments()
        row = append_row(
            conn, key=self._key, kind=kind, payload=payload, session=session, ts=ts
        )
        self._last_appended = row
        return row

    def _mutate(
        self,
        conn: sqlite3.Connection,
        kind: str,
        payload: Mapping[str, Any],
        *,
        session: str | None,
    ) -> LogRow:
        """Log a mutation and apply it, inside the caller's transaction.
        The row is appended first so the record it puts can carry the
        row's MAC. A record put or deleted names each record it may bring
        back into force, and itself when it is removed, for the commit to
        mark or unmark (`_flush_returns`, ENG-200)."""
        state = _returned.before(conn, kind, payload)
        row = self._append(conn, kind, payload, session=session)
        _apply_mutation(conn, kind, payload, log_mac=row.mac)
        if kind in RECORD_MUTATION_KINDS:
            self._tx_records = True
        if state is not None:
            for record_id in _returned.candidates(conn, kind, state):
                self._tx_returns.setdefault(record_id, (state.record_id, session))
            if not self._tx_depth:
                self._flush_returns(conn)
        return row

    def _flush_returns(self, conn: sqlite3.Connection) -> None:
        """Write the ``returned`` rows the transaction's record writes call
        for, once, after them (`store_returned.settle`): each named record
        that is active and that no active record supersedes is marked, and
        each that is no longer active, or that an active record supersedes
        again, loses its mark. Run last, so a
        record's own rows stay next to each other in the log, as the
        history reads them, and each record is judged as the transaction
        leaves it (a restore that puts a link back, say)."""
        pending, self._tx_returns = self._tx_returns, {}
        if not pending:
            return
        puts, deletes = _returned.settle(conn, pending)
        for mark, session in puts:
            self._mutate(conn, "returned_put", {"returned": mark}, session=session)
        for deletion, session in deletes:
            self._mutate(conn, "returned_delete", deletion, session=session)

    def release_returned(self, record_id: str, *, session: str | None = None) -> bool:
        """Drop the record's ``returned`` mark (`store_returned`): a
        standing roster read it. Joins the caller's transaction, so the
        release commits with the change that read it. Returns whether a
        mark was there."""
        with self._transaction() as tx:
            if not _returned.returned_rows(self, [record_id]):
                return False
            self._mutate(
                tx, "returned_delete", {"record_id": record_id}, session=session
            )
        return True

    def returned_rows(
        self, ids: Iterable[str] | None = None
    ) -> dict[str, dict[str, str]]:
        """The ``returned`` marks of `ids` (every mark when None)."""
        return _returned.returned_rows(self, ids)

    def _home_row(
        self, record_id: str, kind: str, home: RecordHome | None
    ) -> dict[str, Any] | None:
        """The ``homes`` row a put writes beside its record, or None: no
        home given, or the record already has this very home. A record's
        home is written once (design 1); a put that names another home for
        a homed record raises ValueError before anything is written."""
        if home is None:
            return None
        project, checkout, rule = home
        if not isinstance(project, str) or not project:
            raise ValueError("a home names a project (or global)")
        if not isinstance(rule, str) or not rule:
            raise ValueError("a home names the rule that decided it")
        existing = self._conn.execute(
            "SELECT project, checkout, rule FROM homes WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if existing is not None:
            if (existing["project"], existing["checkout"], existing["rule"]) == (
                project,
                checkout,
                rule,
            ):
                return None
            raise ValueError(f"{record_id} already has a home; a home is written once")
        return {
            "record_id": record_id,
            "kind": kind,
            "project": project,
            "checkout": checkout,
            "rule": rule,
            "decided": isoformat_utc(datetime.now(timezone.utc)),
        }

    # Records, standing answers and tombstones (store_records.py).
    put_memory = _records.put_memory
    _put_memory = _records._put_memory
    _put_restored = _records._put_restored
    _relink = _records._relink
    _put_relinked = _records._put_relinked
    _standing_row = staticmethod(_records._standing_row)
    put_standing = _records.put_standing
    standing_rows = _records.standing_rows
    body_of = _records.body_of
    get_memory = _records.get_memory
    has_memory = _records.has_memory
    get_many = _records.get_many
    iter_memories = _records.iter_memories
    iter_memory_rows = _records.iter_memory_rows
    memory_ids = _records.memory_ids
    count_memories = _records.count_memories
    filename_for = _records.filename_for
    tombstone = _records.tombstone
    put_tombstone = _records.put_tombstone
    get_tombstone = _records.get_tombstone
    tombstone_links = _records.tombstone_links
    iter_tombstones = _records.iter_tombstones
    iter_tombstone_rows = _records.iter_tombstone_rows
    count_tombstones = _records.count_tombstones
    restore = _records.restore
    delete_tombstone = _records.delete_tombstone
    _require_row = staticmethod(_records._require_row)
    write = _records.write
    update = _records.update
    mark_verified = _records.mark_verified
    record_corroboration = _records.record_corroboration
    _corroborated = staticmethod(_records._corroborated)
    _active_or_raise = _records._active_or_raise
    load_one = _records.load_one
    show = _records.show
    load_many = _records.load_many
    load_all = _records.load_all
    iter_active = _records.iter_active
    list_summaries = _records.list_summaries
    load_tombstones = _records.load_tombstones
    list_tombstones = _records.list_tombstones
    load_tombstone = _records.load_tombstone
    restore_trimmed = _records.restore_trimmed
    rename_scope = _records.rename_scope
    prune_tombstones = _records.prune_tombstones

    # The episode journal (store_journal.py).
    put_episode = _journal.put_episode
    get_episode = _journal.get_episode
    iter_episodes = _journal.iter_episodes
    count_episodes = _journal.count_episodes
    delete_episode = _journal.delete_episode
    write_episode = _journal.write_episode
    write_floor = _journal.write_floor
    episodes_by_session = _journal.episodes_by_session
    episodes_by_swarm = _journal.episodes_by_swarm
    episode_session_ids = _journal.episode_session_ids
    prunable_episode_sessions = _journal.prunable_episode_sessions
    prune_episode_sessions = _journal.prune_episode_sessions
    episode_volume = _journal.episode_volume

    # Verifications, conflicts, imports and quarantine (store_tables.py).
    put_verification = _tables.put_verification
    verifications_for = _tables.verifications_for
    delete_verification = _tables.delete_verification
    put_conflict = _tables.put_conflict
    list_conflicts = _tables.list_conflicts
    delete_conflict = _tables.delete_conflict
    put_import = _tables.put_import
    list_imports = _tables.list_imports
    delete_import = _tables.delete_import
    put_quarantine = _tables.put_quarantine
    list_quarantine = _tables.list_quarantine
    delete_quarantine = _tables.delete_quarantine

    # -- membership (design 8.5) --------------------------------------------
    #
    # The membership tables' rows are `store_membership`'s (bound below);
    # the store id of design 8.7 stays here as a property. Nothing in the
    # store decides membership: the model (`membership.py` and its
    # family) is the rule.

    @property
    def in_batch(self) -> bool:
        """Whether a write transaction is open on this store: a `batch`,
        or a mutating call's own (`_transaction`). Whatever is written
        now commits with it."""
        return bool(self._tx_depth)

    @property
    def membership_store_id(self) -> str:
        """The store id of design 8.7: what exports carry and what, beside
        the machine id, keys a machine at sync and import."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (MEMBERSHIP_STORE_ID,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"store {self._path} carries no membership store id")
        return str(row[0])

    # The membership tables (store_membership.py).
    put_membership = _membership.put_membership
    delete_membership = _membership.delete_membership
    record_membership = _membership.record_membership
    membership_rows = _membership.membership_rows
    next_membership_id = _membership.next_membership_id
    next_membership_number = _membership.next_membership_number
    membership_log = _membership.membership_log
    wrote_record = _membership.wrote_record
    reissue_membership_store_id = _membership.reissue_membership_store_id

    # Telemetry, the event reads and the log (store_events.py).
    record_event = _events.record_event
    import_event = _events.import_event
    imported_event_keys = _events.imported_event_keys
    iter_events = _events.iter_events
    record_migration = _events.record_migration
    record_capture = _events.record_capture
    capture_rows = _events.capture_rows
    events_of_kinds = _events.events_of_kinds
    session_events_of_kinds = _events.session_events_of_kinds
    events_since = _events.events_since
    _window_floor = _events._window_floor
    _window_rows = _events._window_rows
    _gather_window = staticmethod(_events._gather_window)
    iter_events_backward = _events.iter_events_backward
    log_rows = _events.log_rows
    refold = _events.refold
    log_verify = _events.log_verify

    # The search seams and the document frequency memo (store_index.py).
    query_candidates = _index.query_candidates
    links_for_many = _index.links_for_many
    inbound_links = _index.inbound_links
    _memory_head = _index._memory_head
    _memoised_document_frequencies = _index._memoised_document_frequencies
    _keep_doc_freqs = staticmethod(_index._keep_doc_freqs)
    declared_scopes = _index.declared_scopes
    pending_conflict_pairs = _index.pending_conflict_pairs
    contradicted_at = _index.contradicted_at

    # Provenance and trust (store_provenance.py).
    provenance_for = _provenance.provenance_for
    _segment_for = _provenance._segment_for
    _origin_broken = _provenance._origin_broken
    _pointer_verified = _provenance._pointer_verified
    trust_rows = _provenance.trust_rows
    unaccounted_ids = _provenance.unaccounted_ids
    provenance_counts = _provenance.provenance_counts
    links_with_status = _provenance.links_with_status
    episode_provenance = _provenance.episode_provenance

    # -- the reads the index used to serve ----------------------------------------

    def document_frequencies(
        self,
        terms: Sequence[str],
        *,
        admit: Callable[[list[str], str | None, Actor | None], bool],
        memoised: bool = False,
    ) -> tuple[int, dict[str, int], dict[str, int]] | None:
        """Document frequencies for `terms` over the admitted collection,
        the v8 index's `corpus_document_frequencies`: `(size, body_df,
        scope_df)`, where `admit` is the search's own admission predicate
        over `(scopes, home project, actor)` (the project the record's
        ``homes`` row names, read in the same query; None for a record
        with none) so the denominator is the ranked set. None when no term
        or no admitted document. With `memoised`,
        the rows' admission inputs and the terms' hits come from the
        store's document frequency memo (`_DOC_FREQS`), read again only
        after a memory changed; the answer is the same."""
        unique = sorted({t for t in terms if t})
        if not unique:
            return None
        if memoised and not (self._tx_depth or self._conn.in_transaction):
            return self._memoised_document_frequencies(unique, admit)
        admitted: set[str] = set()
        for row in self._conn.execute(_ADMISSION_INPUTS):
            client = row["actor_client"]
            model = row["actor_model"]
            actor = (
                Actor(client=client, model=model)
                if (client is not None or model is not None)
                else None
            )
            scopes = _load_json(row["scopes_json"], None)
            if not isinstance(scopes, list):
                continue
            if admit(list(scopes), row["home_project"], actor):
                admitted.add(str(row["id"]))
        if not admitted:
            return None

        def ids_matching(match_expr: str) -> set[str]:
            return {
                str(r["id"])
                for r in self._conn.execute(
                    "SELECT m.id AS id FROM memories m "
                    "JOIN memories_fts f ON f.rowid = m.rowid "
                    "WHERE memories_fts MATCH ?",
                    (match_expr,),
                )
            }

        body_df: dict[str, int] = {}
        scope_df: dict[str, int] = {}
        for term in unique:
            quoted = '"' + term.replace('"', '""') + '"'
            body_hits = ids_matching(f"body_fts : {quoted}") & admitted
            any_hits = ids_matching(quoted) & admitted
            if body_hits:
                body_df[term] = len(body_hits)
            if any_hits:
                scope_df[term] = len(any_hits)
        return len(admitted), body_df, scope_df

    def _read_doc_freq_inputs(self, head: tuple[int, str]) -> _DocFreqInputs:
        """Every memory row's admission inputs, as the uncached
        `document_frequencies` reads them, grouped by equal inputs."""
        index: dict[str, int] = {}
        grouped: dict[
            tuple[Any, ...], tuple[list[str], str | None, Actor | None, list[int]]
        ] = {}
        for row in self._conn.execute(_ADMISSION_INPUTS):
            scopes = _load_json(row["scopes_json"], None)
            if not isinstance(scopes, list):
                continue
            position = len(index)
            index[str(row["id"])] = position
            home_project = row["home_project"]
            client = row["actor_client"]
            model = row["actor_model"]
            group_key = (row["scopes_json"], home_project, client, model)
            group = grouped.get(group_key)
            if group is None:
                actor = (
                    Actor(client=client, model=model)
                    if (client is not None or model is not None)
                    else None
                )
                group = (list(scopes), home_project, actor, [])
                grouped[group_key] = group
            group[3].append(position)
        return _DocFreqInputs(
            head=head,
            groups=tuple(
                (scopes, home_project, actor, _row_mask(positions, len(index)))
                for scopes, home_project, actor, positions in grouped.values()
            ),
            index=index,
            terms=OrderedDict(),
        )

    def _term_masks(self, term: str, inputs: _DocFreqInputs) -> tuple[int, int]:
        """The rows of `inputs` whose body matches `term`, and whose body
        or scopes do, by the uncached call's two FTS queries."""

        def rows_matching(match_expr: str) -> int:
            positions = []
            for r in self._conn.execute(
                "SELECT m.id AS id FROM memories m "
                "JOIN memories_fts f ON f.rowid = m.rowid "
                "WHERE memories_fts MATCH ?",
                (match_expr,),
            ):
                position = inputs.index.get(str(r["id"]))
                if position is not None:
                    positions.append(position)
            return _row_mask(positions, len(inputs.index))

        quoted = '"' + term.replace('"', '""') + '"'
        return rows_matching(f"body_fts : {quoted}"), rows_matching(quoted)

    def scope_counts(
        self, *, admit: Callable[[list[str], str | None], bool]
    ) -> tuple[int, dict[str, int]]:
        """How many admitted memories there are and how many carry each
        scope, without reading a body. `admit` reads the scopes and the
        project the record's ``homes`` row names (None for none), joined
        in the one query."""
        total = 0
        counts: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT m.scopes_json AS scopes_json, h.project AS home_project "
            "FROM memories m LEFT JOIN homes h ON h.record_id = m.id"
        ):
            scopes = _load_json(row["scopes_json"], None)
            if not isinstance(scopes, list):
                continue
            if not admit(list(scopes), row["home_project"]):
                continue
            total += 1
            for scope in scopes:
                counts[scope] = counts.get(scope, 0) + 1
        return total, counts

    def category_rows(
        self,
        *,
        category: str,
        admit: Callable[[list[str], str | None], bool],
    ) -> list[str]:
        """The ids of admitted memories in `category`, in id order; `admit`
        reads the scopes and the home project as `scope_counts` does."""
        out: list[str] = []
        for row in self._conn.execute(
            "SELECT m.id AS id, m.scopes_json AS scopes_json, "
            "h.project AS home_project "
            "FROM memories m LEFT JOIN homes h ON h.record_id = m.id "
            "WHERE m.category = ? ORDER BY m.id",
            (category,),
        ):
            scopes = _load_json(row["scopes_json"], None)
            if not isinstance(scopes, list):
                continue
            if admit(list(scopes), row["home_project"]):
                out.append(str(row["id"]))
        return out

    def brief_rows(
        self,
        *,
        admit: Callable[[list[str], str | None], bool],
        provenance: Collection[str],
        exclude_category: str | None = None,
    ) -> list[str]:
        """The ids of memories newest ``updated`` first (id breaking a tie):
        stored label in `provenance`, category other than
        `exclude_category` (any category when None), home admitted
        by `admit`, which reads the record's scopes and the project its
        ``homes`` row names (None for a record with none) as `scope_counts`
        does (a global record's admission reads the projects its scopes
        name), joined in the one query. Never reads a body. The stored label is a pre-filter
        only: the caller re-reads the verified one (`trust_rows`), which a
        failed pointer turns into ``unaccounted``."""
        labels = sorted(set(provenance))
        if not labels:
            return []
        placeholders = ",".join("?" * len(labels))
        category = (
            ""
            if exclude_category is None
            else "AND (m.category IS NULL OR m.category != ?) "
        )
        params: tuple[str, ...] = (
            tuple(labels) if exclude_category is None else (*labels, exclude_category)
        )
        out: list[str] = []
        for row in self._conn.execute(
            "SELECT m.id AS id, m.scopes_json AS scopes_json, "
            "h.project AS home_project FROM memories m "
            "LEFT JOIN homes h ON h.record_id = m.id "
            f"WHERE m.provenance IN ({placeholders}) "
            f"{category}"
            "ORDER BY m.updated DESC, m.id DESC",
            params,
        ):
            scopes = _load_json(row["scopes_json"], None)
            if admit(
                list(scopes) if isinstance(scopes, list) else [], row["home_project"]
            ):
                out.append(str(row["id"]))
        return out

    # -- reopen and the event row decoder ------------------------------------

    def reopen(self, *, busy_timeout: float | None = None) -> Store:
        """Another connection to this store, for another thread: SQLite
        binds a connection to the thread that opened it, so a worker
        thread reads and records through its own. The same key directory,
        and never a rekey: a store this process opened without its key
        stays unable to mutate on the new connection too, and so does one
        whose key ring belongs to another store file. The set of
        memory ids a refold found unaccounted is shared, not copied, so a
        refold on either connection reaches the other, and so is
        ``strict_provenance``: a value the opener passed, or one this
        connection already read, is the new one's too. `busy_timeout` is
        the new connection's role's (`set_busy_timeout`); None keeps this
        connection's."""
        wait = self._busy_timeout if busy_timeout is None else busy_timeout
        other = Store.open(
            self._path,
            keys_dir=self._keyring.keys_dir,
            allow_rekey=False,
            strict_provenance=self._strict_provenance,
            read_only=self._read_only,
        )
        try:
            other.set_busy_timeout(wait)
        except BaseException:
            other.close()
            raise
        other._unaccounted_memory_ids = self._unaccounted_memory_ids
        if self._bound_elsewhere is not None:
            # The owner's open found the key ring bound to another store
            # file. A reader's open looks too, but reads the binding again,
            # and what it finds now (the bound file gone a moment, say) is
            # not this connection's owner's answer. Keep the key for
            # checking only, as the owner's connection does.
            if other._key is not None:
                other._read_key, other._key = other._key, None
            other._bound_elsewhere = self._bound_elsewhere
            other._key_state = self._key_state
        return other

    def _event_from_raw(self, raw: sqlite3.Row) -> dict[str, Any] | None:
        return _event_from_columns(
            str(raw["ts"]),
            None if raw["session"] is None else str(raw["session"]),
            str(raw["kind"]),
            raw["payload"],
        )

    # -- status --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        size = 0
        for sibling in (
            self._path,
            self._path.with_suffix(self._path.suffix + "-wal"),
        ):
            with contextlib.suppress(OSError):
                size += sibling.stat().st_size
        head = self._keyring.read_head()
        meta = self.meta()
        return {
            "path": str(self._path),
            "store_id": self._store_id,
            "schema_version": int(meta.get("schema_version", "0")),
            "engine_version": meta.get("engine_version"),
            "memories": self.count_memories(),
            "tombstones": self.count_tombstones(),
            "episodes": self.count_episodes(),
            "log_rows": int(
                self._conn.execute("SELECT COUNT(*) FROM log").fetchone()[0]
            ),
            "size_bytes": size,
            "key_fingerprint": meta.get("key_fingerprint"),
            "key_present": self._key is not None,
            "keys_dir": str(self._keyring.keys_dir),
            "head_seq": None if head is None else head.seq,
        }


# ---------------------------------------------------------------------------
# Per-request store resolution
# ---------------------------------------------------------------------------


class StoreSource(Protocol):
    """Where a request's store comes from. `ToolHandlers.for_request` asks
    this at every tool call; the shipped source returns one store for
    every request. A hosted, multi-tenant deployment replaces the
    policy behind this seam and nothing else."""

    def for_request(self, ctx: Any | None) -> Store: ...


class DefaultStoreSource:
    """The one-store policy: every request is served from the process
    store. `principal_of` exposes the attested caller a later policy
    would key on, resolved through `identity.bind`, the same binder the
    session registry uses."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def for_request(self, ctx: Any | None) -> Store:
        return self._store

    def principal_of(self, ctx: Any | None) -> str | None:
        from . import identity

        return identity.bind(ctx).actor.principal


__all__ = [
    "Store",
    "DEFAULT_EPISODE_TTL_DAYS",
    "EpisodeVolume",
    "TrustRow",
    "FOLDED_TABLES",
    "IMPORTED",
    "IMPORTED_FROM_V8",
    "LOCAL",
    "MEMBERSHIP_EVENT_KINDS",
    "MEMBERSHIP_MUTATION_KINDS",
    "MEMBERSHIP_SCHEMA",
    "MEMBERSHIP_STORE_ID",
    "MEMBERSHIP_TABLES",
    "MUTATION_KINDS",
    "RecordHome",
    "StandingAnswer",
    "PROVENANCE_LABELS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "STORE_FILENAME",
    "SYNCED",
    "UNACCOUNTED",
    "ConcurrentUpdateError",
    "DefaultStoreSource",
    "MemoryNotFoundError",
    "MemoryRow",
    "NotFoundError",
    "NotTombstonedError",
    "StoreSource",
    "store_path",
    "TombstoneRow",
    "TombstonedError",
    "event_import_payload",
]
