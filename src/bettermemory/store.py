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
module under the package's 2,000-line bound (ENG-96); none of them imports
this one at run time, and this one re-exports every name a caller imports
from it. A method a module beside this one holds is a function of
`self: Store` that `Store` binds by name (`write = _records.write`):

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
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Protocol, TypeVar

from pydantic import ValidationError

from . import _caches
from ._fsutil import ensure_owner_only_dir
from .events import _redact_event_fields
from .identity import Actor
from .config import STORE_FILENAME
from .log import (
    CAPTURE_KINDS,
    MIGRATE_V8,
    REKEY,
    STORE_CREATED,
    HistoryOrigin,
    KeyRing,
    LogRow,
    OriginCheck,
    Segment,
    append_row,
    canonical_payload,
    chain_segments,
    chain_trust,
    check_origin,
    compute_mac,
    fingerprint,
    iter_rows,
    last_rekey_name,
    row_verifies,
    same_mac,
    verify_chain,
)

# `fts_index_text` passes through: bench/integrity/adapters.py reads it here.
from .search import (
    fts_index_text as fts_index_text,
    fts_match_query,
    tokenizer_fingerprint,
)
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
    _CONFLICT_COLUMNS,
    _IMPORT_COLUMNS,
    _MEMBERSHIP_BY_TABLE,
    _MEMBERSHIP_SPECS as _MEMBERSHIP_SPECS,
    _NOT_TELEMETRY,
    _PROVENANCE_BATCH,
    _QUARANTINE_COLUMNS,
    _STORE_ID_PATTERN,
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
)
from .store_rows import (
    _MEMORY_COLUMNS as _MEMORY_COLUMNS,
    _ROW_COLUMNS,
    _UPSERT_MEMORY as _UPSERT_MEMORY,
    _apply_mutation as _apply_mutation,
    _dt,
    _event_from_columns,
    _iso as _iso,
    _key_label,
    _load_json,
    _membership_column,
    _membership_key,
    _membership_spec,
    _membership_values,
    _record_columns as _record_columns,
    _scopes_text as _scopes_text,
    _sort_rows,
    _table_rows as _table_rows,
    _verification_row,
    body_sha256 as body_sha256,
    event_import_payload,
)
from .store_keys import (
    _claim_key_ring,
    _current_key,
    _key_ring_elsewhere,
    _signs_a_segment,
    _store_id_at as _store_id_at,
    configured_strict_provenance as configured_strict_provenance,
)
from . import store_records as _records
from .store_records import (
    CORROBORATION_CHECK_SQL as CORROBORATION_CHECK_SQL,
)
from . import store_journal as _journal

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
# commands, the in-process hook path and the daemon's own connection.
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
    """The one way a store file is opened: the page size for a new file,
    WAL, synchronous NORMAL, the CLI's busy timeout (a connection opened
    for another role sets its own, `Store.set_busy_timeout`), foreign keys
    on, autocommit off so every write is an explicit transaction, and
    owner-only modes on the file and its WAL siblings."""
    conn = sqlite3.connect(
        str(path), timeout=CLI_BUSY_TIMEOUT_SECONDS, isolation_level=None
    )
    try:
        # A no-op on an existing store: the page size is fixed once the
        # file holds a table, and WAL mode fixes it for good.
        conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
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


class _WindowRow(NamedTuple):
    """One telemetry row of an event window, as the log holds it: its
    stamp's first nineteen characters (the window's cut compares on
    them), its columns, and its payload text, parsed again for each
    read so no reader holds another's event."""

    cut: str
    ts: str
    session: str | None
    kind: str
    payload: str


@dataclass(frozen=True)
class _EventWindow:
    """The rows `Store.events_since` read for one store's window: every
    telemetry row whose stamp's first nineteen characters are at or after
    `cut`, in log order, among the rows up to `head_seq`, the log's last
    row when they were read, whose MAC was `head_mac`."""

    cut: str
    head_seq: int
    head_mac: str
    rows: tuple[_WindowRow, ...]


# The event window memo of `Store.events_since(since, memoised=True)`, the
# read `memory_search` makes on every search that returns a hit. The
# uncached read filters the whole log on `substr(ts, 1, 19)`, which no
# index serves, for the few rows inside the window. The log only grows:
# a row is appended at the next sequence number under the write lock and
# never changed, and each row's MAC covers the MAC of the row before it.
# So the memo keeps, per store, the window's rows up to the log's head as
# it read it, keyed on that head: a later read whose cut is at or after the
# memo's checks that the row at the head's sequence number still carries
# the head's MAC, drops the kept rows the new cut excludes, and reads only
# the rows past the head, the new cut applied in SQL. A cut before the
# memo's, a head row gone or carrying another MAC (the tail removed, a row
# appended at its number) or a read inside an open transaction reads the
# whole log as the uncached code does. An edit outside the store that
# leaves the head row's MAC as it was is not seen: a row below the head
# edited with its MAC kept or deleted, and the head row edited with its MAC
# kept (the chain then fails `bettermemory log verify` in each case); the
# memo serves the rows it read. Keyed by the store file and its
# id, so two connections to one store share it; bounded to the most
# recently read stores, not in rows: a store's entry keeps every row in the
# window, its payload text and four short strings, about 1.3 KB a row for
# payloads of about 1 KB (26 MB for a window of 20,000 such rows, measured
# with tracemalloc on Python 3.13), and a window holds whatever the
# lookback's span logged. The lock guards each look-up and each store, the
# reads run outside it. Registered with `_caches`, which empties it before
# each test.
EVENT_WINDOW_STORES = 8

_EVENT_WINDOWS: OrderedDict[tuple[str, str], _EventWindow] = OrderedDict()
_EVENT_WINDOWS_LOCK = threading.Lock()


@_caches.register
def _clear_event_windows() -> None:
    with _EVENT_WINDOWS_LOCK:
        _EVENT_WINDOWS.clear()


# Every memory row's admission inputs for the document frequencies: its
# scopes, the project its ``homes`` row names (null for a row with none)
# and its declared writer, in one query.
_ADMISSION_INPUTS = (
    "SELECT m.id AS id, m.scopes_json AS scopes_json, h.project AS home_project, "
    "m.actor_client AS actor_client, m.actor_model AS actor_model "
    "FROM memories m LEFT JOIN homes h ON h.record_id = m.id"
)


@dataclass
class _DocFreqInputs:
    """What `Store.document_frequencies(..., memoised=True)` keeps for one
    store while its memories stay as they were: the admission inputs of
    every memory row whose scopes parse, grouped by equal inputs, and each
    term's FTS hits. A row is a bit, its position in the scan; `groups`
    maps `(scopes, home project, actor)` to the mask of the rows that carry
    them (the project the row's ``homes`` row names, None for a row with
    none), and `terms` maps a term to the masks of the rows whose body
    matches it and whose body or scopes match it. `head` is the log's last
    memory or home row, `(seq, mac)`, when the inputs were read."""

    head: tuple[int, str]
    groups: tuple[tuple[list[str], str | None, Actor | None, int], ...]
    index: dict[str, int]
    terms: OrderedDict[str, tuple[int, int]]


# The document frequency memo of `Store.document_frequencies(...,
# memoised=True)`, the corpus statistics every search above the prefilter
# threshold prices its BM25 legs with: the per-prompt recall's probe,
# `memory_search` and the review sample. The uncached call reads every
# memory row and runs two FTS queries per term on every search, though the
# rows and the hits change only when a memory does. Every change to the
# memories table is a `memory_put` or `memory_delete` row appended to the
# log in the same transaction (`_mutate`; the fold applies the same rows),
# and each row's MAC covers the rows before it, so the log's last memory
# row names the table's state: the memo keeps a store's admission inputs
# and its terms' hits keyed on that row's sequence number and MAC, and a
# call whose key differs reads them again. A write, an update, a
# tombstone, a restore, a deletion and an import each append such a row; a
# telemetry row does not, so the searches' own events leave the memo warm.
# A record's home is a `home_put` row (beside its memory's, or alone from
# the migration and an import), so the key is the last row of either.
# What is not kept is the admission itself: `admit` is the search's
# predicate (`admission.Admission`), which follows the project table's
# merges and boundaries for the caller, so it runs on every call, once per
# distinct `(scopes, home project, actor)` instead of once per row; the
# counts are then those of the uncached call. A read inside an open
# transaction is served
# uncached, and a result is kept only when the key read after it is the
# key read before it, so a memory committed meanwhile by another
# connection is never kept under the older key. An edit to the memories
# table outside the store, with no log row, is not seen, as with the event
# window memo. Keyed by the store file and its id, so the daemon's worker
# connections share it; bounded to the most recently read stores, and to
# the most recently used terms per store (two masks of one bit per memory
# row each, about 170 bytes a term at 662 memories). The lock guards each
# look-up and each store, the reads run outside it. Registered with
# `_caches`, which empties it before each test.
DOC_FREQ_STORES = 8
DOC_FREQ_TERMS = 4096


def _row_mask(positions: Iterable[int], width: int) -> int:
    """The int whose bit `p` is set for each `p` in `positions`, each
    below `width`; built in a bytearray, linear in `width`."""
    bits = bytearray((width + 7) // 8)
    for position in positions:
        bits[position >> 3] |= 1 << (position & 7)
    return int.from_bytes(bits, "little")


_DOC_FREQS: OrderedDict[tuple[str, str], _DocFreqInputs] = OrderedDict()
_DOC_FREQS_LOCK = threading.Lock()


@_caches.register
def _clear_doc_freqs() -> None:
    with _DOC_FREQS_LOCK:
        _DOC_FREQS.clear()


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
    # The store file the key ring is bound to when it is not this one and
    # still holds this store id (`_claim_key_ring`): this connection then
    # writes nothing, and says why. None otherwise.
    _bound_elsewhere: Path | None
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
        self._store_id = store_id
        self._last_appended = None
        self._tx_depth = 0
        self._tx_records = False
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
        ensure_owner_only_dir(path.parent, parents=True)
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
    def open(
        cls,
        path: Path | str,
        *,
        keys_dir: Path | str | None = None,
        allow_rekey: bool = False,
        strict_provenance: bool | None = None,
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
        opens without it.

        ``strict_provenance`` is ``[security] strict_provenance`` for this
        connection; None (the default) reads it from the config the first
        time a row needs it (`strict_provenance`)."""
        path = store_path(path)
        if not path.is_file():
            raise FileNotFoundError(f"no store at {path}")
        conn = _connect(path)
        try:
            meta = {
                str(r["key"]): str(r["value"])
                for r in conn.execute("SELECT key, value FROM meta")
            }
            store_id = meta.get("store_id")
            expected = meta.get("key_fingerprint")
            if not store_id or not expected:
                raise ValueError(f"{path} carries no store id or key fingerprint")
            if not _STORE_ID_PATTERN.fullmatch(store_id):
                raise ValueError(
                    f"{path} carries a store id this build never writes "
                    f"({store_id[:40]!r}); the key directory is searched by it, "
                    f"so the store is not opened"
                )
            schema = int(meta.get("schema_version", "0"))
            if schema > SCHEMA_VERSION:
                raise ValueError(
                    f"{path} is schema {schema}; this build reads up to {SCHEMA_VERSION}"
                )
            _add_membership_tables(conn, path, meta)
            keyring = KeyRing(
                Path(keys_dir) if keys_dir is not None else resolve_keys_dir(),
                store_id,
            )
            key = _current_key(keyring, path, writing=allow_rekey)
            matches = key is not None and fingerprint(key) == expected
            # The owner's opener first claims the key ring for this file
            # (`_claim_key_ring`): one that is bound to another file still
            # holding this store id is that file's, and this open writes
            # nothing to it or with it. A reader whose key matches would
            # sign with it too, and asks the same, writing no binding
            # (`_key_ring_elsewhere`); one without the key writes nothing.
            if allow_rekey:
                elsewhere = _claim_key_ring(keyring, path, store_id)
            elif matches:
                elsewhere = _key_ring_elsewhere(keyring, path, store_id)
            else:
                elsewhere = None
            if matches and elsewhere is None:
                store = cls._bind(
                    path,
                    conn,
                    keyring,
                    key,
                    store_id,
                    strict_provenance=strict_provenance,
                )
                if allow_rekey:
                    store._close_foreign_tail()
                    store._settle_origin()
                return store
            if not allow_rekey or elsewhere is not None:
                reader = cls._bind(
                    path,
                    conn,
                    keyring,
                    None,
                    store_id,
                    strict_provenance=strict_provenance,
                )
                reader._bound_elsewhere = elsewhere
                # Meta's fingerprint is only what the file says. When this
                # machine has a history of the store (its key ring holds
                # an origin anchor for it, or its key signs a segment of
                # the log, as the owner's opener reads it below), an editor
                # who pointed meta at another key must not thereby switch
                # the reads' checks off, which left a planted record its
                # stored label here while `log verify` read the store
                # tampered. The key is bound for checking pointers, never
                # for writing, and nothing is written to set meta back.
                # Binding it can only take a stored label away: a row
                # still verifies under its own segment's key. An owner's
                # open refused for a key ring bound elsewhere checks under
                # a key meta names too, as the bound store's reads would.
                if key is not None and (
                    matches
                    or keyring.read_origin() is not None
                    or _signs_a_segment(
                        chain_segments(
                            conn,
                            keyring=keyring,
                            current_fingerprint=expected,
                            current_key=key,
                        ),
                        key,
                    )
                ):
                    reader._read_key = key
                return reader
            segments = chain_segments(
                conn, keyring=keyring, current_fingerprint=expected, current_key=key
            )
            if key is not None and _signs_a_segment(segments, key):
                store = cls._bind(
                    path,
                    conn,
                    keyring,
                    key,
                    store_id,
                    strict_provenance=strict_provenance,
                )
                store._reclaim(segments, meta_fingerprint=expected)
                store._settle_origin()
                return store
            # The key that signed the chain's last segment, as the log
            # names it: the rekey row records what it replaced, and meta
            # is only what the file says.
            previous = segments[-1].key_fingerprint if segments else expected
            if key is not None:
                keyring.retire_current()
            key = keyring.create_key()
            store = cls._bind(
                path, conn, keyring, key, store_id, strict_provenance=strict_provenance
            )
            with store._transaction() as tx:
                tx.execute(
                    "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
                    (fingerprint(key),),
                )
                rekeyed = store._append(
                    tx,
                    REKEY,
                    {"old_fingerprint": previous, "new_fingerprint": fingerprint(key)},
                    session=None,
                )
            log.warning(
                "store %s was opened without its key (fingerprint %s); a new key "
                "%s was written and the log rekeyed",
                path,
                previous[:16],
                fingerprint(key)[:16],
            )
            store._take_over_origin(rekeyed)
            return store
        except Exception:
            conn.close()
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

    def close(self) -> None:
        self._conn.close()

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
        # BEGIN before the depth is set: a BEGIN that fails (the busy wait
        # against another writer ran out) leaves this store owning nothing,
        # and the next call begins its own transaction.
        conn.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        try:
            yield conn
            self._require_open(conn)
            conn.execute("COMMIT")
        except BaseException:
            self._roll_back(conn)
            raise
        finally:
            self._tx_depth = 0
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
        conn.execute(f"SAVEPOINT {name}")
        self._tx_depth += 1
        try:
            yield conn
            conn.execute(f"RELEASE {name}")
        except BaseException:
            self._last_appended = appended
            self._tx_records = records
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
            if self._bound_elsewhere is not None:
                raise RuntimeError(
                    f"store {self._path} is open for reading only: the key ring "
                    f"under {self._keyring.keys_dir} belongs to the store at "
                    f"{self._bound_elsewhere}, which still holds store id "
                    f"{self._store_id}. Move or remove one of the two stores, or "
                    f"delete {self._keyring.binding_path()} to bind the key ring "
                    f"to this one."
                )
            raise RuntimeError(
                f"store {self._path} is open for reading only: no key under "
                f"{self._keyring.keys_dir} matches its fingerprint. Put the "
                f"store's key back there, or open it with allow_rekey=True from "
                f"the process that owns the store to write a new key and start a "
                f"new chain segment."
            )
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
        row's MAC."""
        row = self._append(conn, kind, payload, session=session)
        _apply_mutation(conn, kind, payload, log_mac=row.mac)
        if kind in RECORD_MUTATION_KINDS:
            self._tx_records = True
        return row

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

    # -- verifications ------------------------------------------------------

    def put_verification(
        self,
        memory_id: str,
        *,
        verified_at: datetime,
        machine_id: str | None = None,
        verified_head: str | None = None,
        verified_paths: Sequence[str] = (),
        verified_commits: Sequence[str] = (),
        verified_versions: Sequence[str] = (),
        verified_absent_paths: Sequence[str] = (),
        session: str | None = None,
    ) -> None:
        """This host's stamp on a record (``machine_id`` None until the
        per-machine identity of phase 3 fills it). One row per memory and
        machine; a later stamp replaces the earlier."""
        payload = {
            "memory_id": memory_id,
            "machine_id": machine_id,
            "verified_at": _iso(verified_at),
            "verified_head": verified_head,
            "verified_paths": list(verified_paths),
            "verified_commits": list(verified_commits),
            "verified_versions": list(verified_versions),
            "verified_absent_paths": list(verified_absent_paths),
        }
        with self._transaction() as tx:
            self._mutate(tx, "verification_put", payload, session=session)

    def verifications_for(
        self, memory_ids: Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        ids = list(dict.fromkeys(memory_ids))
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                "SELECT * FROM verifications "
                f"WHERE memory_id IN ({placeholders}) ORDER BY memory_id, machine_id",
                batch,
            ):
                out.setdefault(str(row["memory_id"]), []).append(_verification_row(row))
        return out

    def delete_verification(
        self,
        memory_id: str,
        *,
        machine_id: str | None = None,
        session: str | None = None,
    ) -> None:
        with self._transaction() as tx:
            found = tx.execute(
                "SELECT 1 FROM verifications WHERE memory_id = ? "
                "AND COALESCE(machine_id, '') = COALESCE(?, '')",
                (memory_id, machine_id),
            ).fetchone()
            if found is None:
                raise NotFoundError((memory_id, machine_id))
            self._mutate(
                tx,
                "verification_delete",
                {"memory_id": memory_id, "machine_id": machine_id},
                session=session,
            )

    # -- conflicts, imports, quarantine ------------------------------------

    def put_conflict(
        self, record: Mapping[str, Any], *, session: str | None = None
    ) -> None:
        conflict = {c: record.get(c) for c in _CONFLICT_COLUMNS}
        for required in ("id", "a_id", "b_id"):
            if not conflict[required]:
                raise ValueError(f"a conflict needs {required}")
        with self._transaction() as tx:
            self._mutate(tx, "conflict_put", {"conflict": conflict}, session=session)

    def list_conflicts(self) -> list[dict[str, Any]]:
        return [
            {c: row[c] for c in _CONFLICT_COLUMNS}
            for row in self._conn.execute(
                f"SELECT {', '.join(_CONFLICT_COLUMNS)} FROM conflicts ORDER BY created, id"
            )
        ]

    def delete_conflict(self, conflict_id: str, *, session: str | None = None) -> None:
        with self._transaction() as tx:
            self._require_row(tx, "conflicts", "id", conflict_id)
            self._mutate(tx, "conflict_delete", {"id": conflict_id}, session=session)

    def put_import(
        self,
        source: str,
        *,
        content_hash: str,
        imported_at: str,
        memory_id: str | None = None,
        session: str | None = None,
    ) -> None:
        payload = {
            "source": source,
            "content_hash": content_hash,
            "imported_at": imported_at,
            "memory_id": memory_id,
        }
        with self._transaction() as tx:
            self._mutate(tx, "import_put", payload, session=session)

    def list_imports(self) -> list[dict[str, Any]]:
        return [
            {c: row[c] for c in _IMPORT_COLUMNS}
            for row in self._conn.execute(
                f"SELECT {', '.join(_IMPORT_COLUMNS)} FROM imports ORDER BY source"
            )
        ]

    def delete_import(self, source: str, *, session: str | None = None) -> None:
        with self._transaction() as tx:
            self._require_row(tx, "imports", "source", source)
            self._mutate(tx, "import_delete", {"source": source}, session=session)

    def put_quarantine(
        self,
        name: str,
        *,
        reason: str,
        at: str,
        detail: str | None = None,
        remote: str | None = None,
        size: int | None = None,
        sha256: str | None = None,
        session: str | None = None,
    ) -> None:
        payload = {
            "name": name,
            "reason": reason,
            "detail": detail,
            "remote": remote,
            "at": at,
            "size": size,
            "sha256": sha256,
        }
        with self._transaction() as tx:
            self._mutate(tx, "quarantine_put", payload, session=session)

    def list_quarantine(self) -> list[dict[str, Any]]:
        return [
            {c: row[c] for c in _QUARANTINE_COLUMNS}
            for row in self._conn.execute(
                f"SELECT {', '.join(_QUARANTINE_COLUMNS)} FROM quarantine ORDER BY name"
            )
        ]

    def delete_quarantine(self, name: str, *, session: str | None = None) -> None:
        with self._transaction() as tx:
            self._require_row(tx, "quarantine", "name", name)
            self._mutate(tx, "quarantine_delete", {"name": name}, session=session)

    # -- membership (design 8.5) --------------------------------------------
    #
    # The tables `membership.py` reads and writes. Every change is a
    # mutation row of the table's own kind, applied by `_apply_mutation`
    # as the fold applies it; the membership rows of design 8.10 are
    # control rows written by `record_membership`. Nothing here decides
    # membership: these are the rows, `membership.py` is the rule.

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

    def put_membership(
        self, kind: str, row: Mapping[str, Any], *, session: str | None = None
    ) -> LogRow:
        """Insert or replace one row of the membership table whose mutation
        kind prefix is `kind` (``home``, ``checkout``, ...): every column
        given, a ``_json`` column as its JSON value. The row is checked
        before it is logged, so a malformed one raises and writes
        nothing."""
        spec = _membership_spec(kind)
        payload = {spec.kind: dict(row)}
        _membership_values(spec, payload[spec.kind])
        with self._transaction() as tx:
            return self._mutate(tx, f"{spec.kind}_put", payload, session=session)

    def delete_membership(
        self, kind: str, key: Mapping[str, Any], *, session: str | None = None
    ) -> LogRow:
        """Delete the membership row `key` names (its key columns)."""
        spec = _membership_spec(kind)
        payload = {column: key[column] for column in spec.key}
        _membership_key(spec, payload)
        with self._transaction() as tx:
            return self._mutate(tx, f"{spec.kind}_delete", payload, session=session)

    def record_membership(
        self, kind: str, payload: Mapping[str, Any], *, session: str | None = None
    ) -> LogRow:
        """One membership row of design 8.10 (`MEMBERSHIP_EVENT_KINDS`),
        written whether or not telemetry is on. Inside a `batch` it commits
        with the batch."""
        if kind not in MEMBERSHIP_EVENT_KINDS:
            raise ValueError(f"{kind!r} is not a membership row kind")
        with self._transaction() as tx:
            return self._append(tx, kind, dict(payload), session=session)

    def membership_rows(
        self,
        table: str,
        *,
        where: Mapping[str, Any] | None = None,
        column_in: tuple[str, Collection[Any]] | None = None,
        order_by: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Rows of a membership table as dicts, a ``_json`` column parsed
        back to its value: those whose columns equal `where` (None matches
        null) and whose `column_in` column holds one of its values, ordered
        by `order_by` (a leading ``-`` sorts that column descending), else
        by the key. Column names are checked against the table."""
        spec = _MEMBERSHIP_BY_TABLE.get(table)
        if spec is None:
            raise ValueError(f"{table!r} is not a membership table")
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (where or {}).items():
            _membership_column(spec, column)
            if value is None:
                clauses.append(f"{column} IS NULL")
            else:
                clauses.append(f"{column} = ?")
                params.append(value)
        order: list[str] = []
        for name in order_by or spec.key:
            column = name.lstrip("-")
            _membership_column(spec, column)
            order.append(f"{column} DESC" if name.startswith("-") else column)
        select = f"SELECT {', '.join(spec.columns)} FROM {table}"
        batches: list[list[Any]] = [[]]
        if column_in is not None:
            column, values = column_in
            _membership_column(spec, column)
            wanted = list(dict.fromkeys(values))
            if not wanted:
                return []
            batches = [
                wanted[start : start + _PROVENANCE_BATCH]
                for start in range(0, len(wanted), _PROVENANCE_BATCH)
            ]
        out: list[dict[str, Any]] = []
        for batch in batches:
            batch_clauses = list(clauses)
            if column_in is not None:
                batch_clauses.append(
                    f"{column_in[0]} IN ({','.join('?' * len(batch))})"
                )
            sql = select
            if batch_clauses:
                sql += " WHERE " + " AND ".join(batch_clauses)
            sql += " ORDER BY " + ", ".join(order)
            for raw in self._conn.execute(sql, (*params, *batch)):
                out.append(
                    {
                        column: (
                            _load_json(raw[column], None)
                            if column.endswith("_json")
                            else raw[column]
                        )
                        for column in spec.columns
                    }
                )
        if len(batches) > 1:
            _sort_rows(out, list(order_by or spec.key))
        return out

    def next_membership_id(self, table: str, prefix: str) -> str:
        """The next local id of a membership table: `prefix` and one more
        than the highest number its ids carry, at least three digits
        (``P001``). Read inside the caller's transaction, so two writers
        never mint one id."""
        spec = _MEMBERSHIP_BY_TABLE.get(table)
        if spec is None or spec.key != ("id",):
            raise ValueError(f"{table!r} has no minted id")
        row = self._conn.execute(
            f"SELECT MAX(CAST(SUBSTR(id, ?) AS INTEGER)) FROM {table} WHERE id LIKE ?",
            (len(prefix) + 1, f"{prefix}%"),
        ).fetchone()
        number = int(row[0] or 0) + 1
        return f"{prefix}{number:03d}"

    def next_membership_number(self, table: str, column: str) -> int:
        """One more than the highest value of an integer column of a
        membership table (a witness's arrival, a view's sighting order)."""
        spec = _MEMBERSHIP_BY_TABLE.get(table)
        if spec is None:
            raise ValueError(f"{table!r} is not a membership table")
        _membership_column(spec, column)
        row = self._conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
        return int(row[0] or 0) + 1

    def membership_log(
        self, kinds: Collection[str] | None = None, *, since_seq: int = 0
    ) -> Iterator[LogRow]:
        """The membership rows after `since_seq`, in log order: the
        mutation rows of the membership tables and the rows of design
        8.10, or only those of `kinds`, read through the log's kind
        index."""
        allowed = MEMBERSHIP_MUTATION_KINDS | MEMBERSHIP_EVENT_KINDS
        names = sorted(allowed if kinds is None else set(kinds) & allowed)
        if not names:
            return
        placeholders = ",".join("?" * len(names))
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            f"WHERE kind IN ({placeholders}) AND seq > ? ORDER BY seq",
            (*names, int(since_seq)),
        ):
            yield LogRow(
                int(raw[0]),
                str(raw[1]),
                None if raw[2] is None else str(raw[2]),
                str(raw[3]),
                str(raw[4]),
                str(raw[5]),
                str(raw[6]),
            )

    def wrote_record(self, record_id: str) -> bool:
        """Whether this store's own log ever put the record `record_id`: a
        memory, tombstone or episode row naming it, or its home."""
        if (
            self._conn.execute(
                "SELECT 1 FROM homes WHERE record_id = ?", (record_id,)
            ).fetchone()
            is not None
        ):
            return True
        needle = canonical_payload({"id": record_id})[1:-1]
        return (
            self._conn.execute(
                "SELECT 1 FROM log WHERE kind IN "
                "('memory_put', 'tombstone_put', 'episode_put') "
                "AND instr(payload, ?) > 0 LIMIT 1",
                (needle,),
            ).fetchone()
            is not None
        )

    def reissue_membership_store_id(
        self, *, record_id: str, source: str, session: str | None = None
    ) -> tuple[str, str]:
        """A new store id after a fork (design 8.7): the meta value
        replaced and a ``store_forked`` row naming the old and new ids,
        the record that showed the fork and where it came from, in one
        transaction, the old id read under its write lock. Returns
        ``(old, new)``."""
        new = secrets.token_hex(16)
        with self._transaction() as tx:
            old = self.membership_store_id
            tx.execute(
                "UPDATE meta SET value = ? WHERE key = ?", (new, MEMBERSHIP_STORE_ID)
            )
            self._append(
                tx,
                "store_forked",
                {
                    "old_store": old,
                    "new_store": new,
                    "record": record_id,
                    "source": source,
                },
                session=session,
            )
        return old, new

    # -- telemetry ----------------------------------------------------------

    def record_event(
        self,
        kind: str,
        *,
        session: str | None = None,
        actor: Actor | Mapping[str, Any] | None = None,
        best_effort: bool = True,
        **fields: Any,
    ) -> LogRow | None:
        """One telemetry row. ``query`` and ``probe_query`` are redacted to
        a hash, a short preview and a length before the row is signed, as
        the v8 recorder redacted them. With ``best_effort`` a storage
        failure is logged and swallowed, so telemetry never fails the tool
        call it describes; a kind that names a mutation or a control row
        is a programming error and always raises."""
        if kind in _NOT_TELEMETRY:
            raise ValueError(f"{kind!r} is a mutation or control kind, not an event")
        payload: dict[str, Any] = _redact_event_fields(dict(fields))
        if actor is not None:
            record = actor.to_record() if isinstance(actor, Actor) else dict(actor)
            if record:
                payload.setdefault("actor", record)
        try:
            with self._transaction() as tx:
                return self._append(tx, kind, payload, session=session)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            if not best_effort:
                raise
            log.warning("event %s not recorded: %s", kind, exc)
            return None

    def import_event(
        self, event: Mapping[str, Any], *, imported_from: str = IMPORTED_FROM_V8
    ) -> LogRow:
        """One v8 event as a telemetry row under its original ``ts`` and
        ``session``, the rest of its fields as the payload plus
        ``imported_from``, query fields redacted (``event_import_payload``
        says exactly what). An event without a ``ts`` is stamped now.
        Raises rather than swallowing: a migration wants to know."""
        ts, session, kind, payload = event_import_payload(
            event, imported_from=imported_from
        )
        with self._transaction() as tx:
            return self._append(tx, kind, payload, session=session, ts=ts)

    def imported_event_keys(
        self, imported_from: str = IMPORTED_FROM_V8
    ) -> set[tuple[str, str | None, str, str]]:
        """``(ts, session, kind, payload text)`` of every telemetry row
        that names ``imported_from``: what a re-run of the importer skips.
        The payload text is the canonical one the row stores, so the
        caller compares ``canonical_payload`` of what it would write."""
        needle = canonical_payload({"imported_from": imported_from})[1:-1]
        out: set[tuple[str, str | None, str, str]] = set()
        for row in self._conn.execute(
            "SELECT ts, session, kind, payload FROM log WHERE payload LIKE ? "
            "ORDER BY seq",
            (f"%{needle}%",),
        ):
            kind = str(row["kind"])
            if kind in _NOT_TELEMETRY:
                continue
            out.add(
                (
                    str(row["ts"]),
                    None if row["session"] is None else str(row["session"]),
                    kind,
                    str(row["payload"]),
                )
            )
        return out

    def iter_events(self, since_seq: int = 0) -> Iterator[dict[str, Any]]:
        """The telemetry rows in the v8 event shape, seq order: ``ts``,
        ``session`` (when the row has one), ``kind``, then the payload's
        fields. Mutation and control rows are not events and are
        skipped; so is a row whose payload is not an object."""
        for row in iter_rows(self._conn, since_seq):
            if row.kind in _NOT_TELEMETRY:
                continue
            try:
                payload = json.loads(row.payload)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            event: dict[str, Any] = {"ts": row.ts}
            if row.session is not None:
                event["session"] = row.session
            event["kind"] = row.kind
            for key, value in payload.items():
                if key not in _ROW_COLUMNS:
                    event[key] = value
            yield event

    def record_migration(
        self,
        *,
        source: str,
        counts: Mapping[str, Any],
        session: str | None = None,
    ) -> LogRow:
        """The ``migrate_v8`` control row: which v8 directory was read and
        how much of it this run imports, written before the imports it
        counts so the log says what was attested when."""
        from . import __version__

        payload = {
            "imported_from": IMPORTED_FROM_V8,
            "source": source,
            "counts": dict(counts),
            "engine_version": __version__,
        }
        with self._transaction() as tx:
            return self._append(tx, MIGRATE_V8, payload, session=session)

    def record_capture(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        session: str | None = None,
        ts: str | None = None,
        guard: Callable[[], None] | None = None,
    ) -> LogRow:
        """One capture control row (`log.CAPTURE_KINDS`), written whether or
        not telemetry is on: `record_event` refuses control kinds, and the
        event readers skip them, so capture has this writer and
        `capture_rows` as its reader. `guard` runs inside the transaction,
        under the write lock, before the row is appended; whatever it
        raises aborts the write, which is how opening a batch is atomic
        per session. Inside a `batch` the row commits with the batch."""
        if kind not in CAPTURE_KINDS:
            raise ValueError(f"{kind!r} is not a capture kind")
        with self._transaction() as tx:
            if guard is not None:
                guard()
            return self._append(tx, kind, dict(payload), session=session, ts=ts)

    def capture_rows(
        self, kinds: Collection[str], *, since_seq: int = 0
    ) -> Iterator[LogRow]:
        """The capture rows of `kinds` after `since_seq`, in log order,
        read through the log's kind index."""
        names = sorted(set(kinds) & CAPTURE_KINDS)
        if not names:
            return
        placeholders = ",".join("?" * len(names))
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            f"WHERE kind IN ({placeholders}) AND seq > ? ORDER BY seq",
            (*names, int(since_seq)),
        ):
            yield LogRow(
                int(raw[0]),
                str(raw[1]),
                None if raw[2] is None else str(raw[2]),
                str(raw[3]),
                str(raw[4]),
                str(raw[5]),
                str(raw[6]),
            )

    # -- search seams ---------------------------------------------------------

    def query_candidates(
        self,
        text: str,
        *,
        scopes: Sequence[str] | None = None,
        client: str | None = None,
        model: str | None = None,
        max_results: int = 100,
    ) -> list[tuple[str, float]]:
        """``[(memory_id, bm25), ...]`` ascending, the v8 index query
        verbatim: the MATCH from ``fts_match_query``, scopes as an OR over
        the space-padded list, the actor filters as exact equality that an
        undeclared row never matches, and the cap applied in SQL."""
        if not text.strip():
            return []
        match_query = fts_match_query(text)
        if not match_query:
            return []
        sql = (
            "SELECT m.id, bm25(memories_fts) AS score "
            "FROM memories_fts "
            "JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE memories_fts MATCH ? "
        )
        params: list[Any] = [match_query]
        if scopes:
            sql += "AND (" + " OR ".join(["m.scopes_text LIKE ?"] * len(scopes)) + ") "
            params.extend(f"% {s} %" for s in scopes)
        if client is not None:
            sql += "AND m.actor_client IS NOT NULL AND m.actor_client = ? "
            params.append(client)
        if model is not None:
            sql += "AND m.actor_model IS NOT NULL AND m.actor_model = ? "
            params.append(model)
        sql += "ORDER BY score ASC LIMIT ?"
        params.append(int(max_results))
        rows = self._conn.execute(sql, params).fetchall()
        return [(str(row["id"]), float(row["score"])) for row in rows]

    def links_for_many(
        self, memory_ids: Iterable[str]
    ) -> dict[
        str, tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]]
    ]:
        """Outbound and inbound link rows per id, the v8 shape: outbound
        ``(type, target_id, note)``, inbound ``(type, source_id, note)``."""
        ids = list(dict.fromkeys(memory_ids))
        out: dict[
            str,
            tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]],
        ] = {mid: ([], []) for mid in ids}
        if not ids:
            return out
        placeholders = ",".join("?" * len(ids))
        for row in self._conn.execute(
            "SELECT source_id, type, target_id, note FROM memory_links "
            f"WHERE source_id IN ({placeholders}) ORDER BY source_id, type, target_id",
            ids,
        ):
            out[row["source_id"]][0].append(
                (row["type"], row["target_id"], row["note"])
            )
        for row in self._conn.execute(
            "SELECT target_id, type, source_id, note FROM memory_links "
            f"WHERE target_id IN ({placeholders}) ORDER BY target_id, type, source_id",
            ids,
        ):
            out[row["target_id"]][1].append(
                (row["type"], row["source_id"], row["note"])
            )
        return out

    def inbound_links(
        self, target_ids: Iterable[str], types: Collection[str]
    ) -> dict[str, list[tuple[str, str, str]]]:
        """``{target: [(type, source_id, source updated)]}`` for the links of
        `types` into each of `target_ids`, oldest source first (id breaking
        a tie). A link's source is always an active memory: the cleanup
        trigger drops a removed one's rows. What `in_force` walks."""
        ids = list(dict.fromkeys(target_ids))
        kinds = sorted(set(types))
        out: dict[str, list[tuple[str, str, str]]] = {}
        if not ids or not kinds:
            return out
        marks = ",".join("?" * len(kinds))
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                "SELECT l.target_id AS target_id, l.type AS type, "
                "l.source_id AS source_id, m.updated AS updated "
                "FROM memory_links l JOIN memories m ON m.id = l.source_id "
                f"WHERE l.type IN ({marks}) AND l.target_id IN ({placeholders}) "
                "ORDER BY l.target_id, m.updated, l.source_id",
                (*kinds, *batch),
            ):
                out.setdefault(str(row["target_id"]), []).append(
                    (str(row["type"]), str(row["source_id"]), str(row["updated"]))
                )
        return out

    def provenance_for(self, memory_ids: Sequence[str]) -> dict[str, str]:
        """``{id: label}`` for the active records asked. The label is the
        row's, unless the row's pointer into the log fails: the ``log_mac``
        names no row, names a row that put a different record, names a row
        whose MAC does not verify under the segment's key, or names a row
        in a foreign segment (one whose key this machine does not hold,
        after one whose key it does, or at or after the origin anchor),
        or, with ``strict_provenance`` or an origin anchor the log no
        longer matches, in any segment whose key is not held, that this
        machine's own key did not sign either. Such a row reads
        ``unaccounted``, as does one the last ``refold`` in this process
        found so. A row in a leading segment whose key is not held (a
        store copied here without its key) keeps its label. A store
        opened without its key cannot verify pointers and returns the row
        labels as stored; ``trust_rows`` says when that is the case."""
        ids = list(dict.fromkeys(memory_ids))
        if not ids:
            return {}
        out: dict[str, str] = {}
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                "SELECT id, provenance, log_mac FROM memories "
                f"WHERE id IN ({placeholders})",
                batch,
            ):
                memory_id = str(row["id"])
                label = str(row["provenance"])
                if memory_id in self._unaccounted_memory_ids:
                    label = UNACCOUNTED
                elif (
                    self._pointer_verified(memory_id, "memory_put", row["log_mac"])
                    is False
                ):
                    label = UNACCOUNTED
                out[memory_id] = label
        return out

    def _segment_for(self, seq: int) -> Segment | None:
        """The segment that holds the row at ``seq``, with its key when
        this machine holds it and whether it is foreign. The map is read
        off the rekey rows once per connection (`log.chain_segments`), so a
        row costs this walk over a handful of segments and nothing more.
        A connection that was open before a rekey row was appended keeps
        its map, and verifies a later row under the last segment it knows,
        which a row signed by another key fails. The origin anchor is
        checked with it (`log.chain_trust`): one more row and one HMAC."""
        if self._segments is None:
            trust = chain_trust(
                self._conn,
                keyring=self._keyring,
                current_fingerprint=self.key_fingerprint,
                current_key=self._verify_key,
            )
            self._segments, self._origin = trust.segments, trust.origin
        found: Segment | None = None
        for segment in self._segments:
            if found is not None and seq < segment.first_seq:
                break
            found = segment
        return found

    def _origin_broken(self) -> bool:
        """Does the origin anchor this connection's segment map was read
        under no longer match the log (`log.OriginCheck.broken`)? False
        before the map is read, and for a store with no anchor."""
        return self._origin is not None and self._origin.broken

    def _pointer_verified(self, record_id: str, kind: str, log_mac: Any) -> bool | None:
        """Does the record's ``log_mac`` name a log row of ``kind`` that
        put this very record, and does that row's MAC verify under its
        segment's key? A row in a foreign segment is checked against this
        connection's own key instead, since nobody here holds the
        segment's: it verifies only when this machine appended it, and
        anything else is False. A row in a leading segment whose key is
        not held answers None (its label stands), or is checked the same
        way under ``strict_provenance`` or when the origin anchor no
        longer matches the log, since which segments lead is then not
        known. A row below seq 1 is False whatever its segment. None too
        when the store was opened without its key. The True and False
        answers are cached per (id, mac)."""
        own_key = self._verify_key
        if own_key is None:
            return None
        if not isinstance(log_mac, str) or not log_mac:
            return False
        cache_key = (record_id, log_mac)
        cached = self._pointer_cache.get(cache_key)
        if cached is not None:
            return cached
        raw = self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            "WHERE mac = ?",
            (log_mac,),
        ).fetchone()
        verdict = False
        if raw is not None and str(raw["kind"]) == kind:
            try:
                payload = json.loads(raw["payload"])
                record = payload[kind.split("_", 1)[0]]
                named = str(record["id"])
            except (ValueError, KeyError, TypeError):
                named = ""
            # No store writes a row below seq 1 (`log.append_row`), and no
            # segment holds one (`log.chain_trust`): a record that names
            # one was put by an editor of the file, whatever its label.
            if named == record_id and int(raw["seq"]) > 0:
                segment = self._segment_for(int(raw["seq"]))
                if segment is None:
                    return None
                key = segment.key
                if key is None:
                    if (
                        not segment.foreign
                        and not self._origin_broken()
                        and not self.strict_provenance
                    ):
                        # A leading segment: signed elsewhere, before this
                        # machine first signed the store. Its label stands.
                        return None
                    # No key here signed the segment, but this machine
                    # may have appended rows to it: a daemon that had the
                    # store open while a forged rekey row went in, or an
                    # opener whose repair could not be written, writes
                    # under its own key into the segment that row opened.
                    # Those rows verify under that key; a forged one
                    # cannot, so it still reads unaccounted.
                    key = own_key
                expected = compute_mac(
                    key,
                    seq=int(raw["seq"]),
                    ts=str(raw["ts"]),
                    session=None if raw["session"] is None else str(raw["session"]),
                    kind=str(raw["kind"]),
                    payload=str(raw["payload"]),
                    prev_mac=str(raw["prev_mac"]),
                )
                verdict = same_mac(expected, log_mac)
        self._pointer_cache[cache_key] = verdict
        return verdict

    def trust_rows(self, memory_ids: Sequence[str]) -> dict[str, TrustRow] | None:
        """``{id: TrustRow}`` for the active records asked: the provenance
        verdict ``provenance_for`` gives and this host's verification stamp
        (the ``verifications`` row with no machine id), or None when the
        store was opened without its key, so no pointer can be verified and
        the response must say trust is unavailable."""
        if self._verify_key is None:
            return None
        labels = self.provenance_for(memory_ids)
        if not labels:
            return {}
        stamps: dict[str, str] = {}
        ids = list(labels)
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                "SELECT memory_id, verified_at FROM verifications "
                f"WHERE machine_id IS NULL AND memory_id IN ({placeholders})",
                batch,
            ):
                stamps[str(row["memory_id"])] = str(row["verified_at"])
        return {
            memory_id: TrustRow(label, stamps.get(memory_id))
            for memory_id, label in labels.items()
        }

    def unaccounted_ids(self) -> list[str]:
        """Every active record whose pointer into the log fails, newest
        first: the planted shape, found by one pass over the rows rather
        than by a refold."""
        out: list[tuple[str, str]] = []
        for row in self._conn.execute(
            "SELECT id, created, log_mac FROM memories ORDER BY created DESC, id"
        ):
            memory_id = str(row["id"])
            if (
                memory_id in self._unaccounted_memory_ids
                or self._pointer_verified(memory_id, "memory_put", row["log_mac"])
                is False
            ):
                out.append((str(row["created"]), memory_id))
        return [memory_id for _, memory_id in out]

    def provenance_counts(self) -> dict[str, int]:
        """How many active records carry each label, with ``unaccounted``
        counted by the pointer check rather than by any stored label."""
        counts: dict[str, int] = {}
        unaccounted = set(self.unaccounted_ids())
        for row in self._conn.execute("SELECT id, provenance FROM memories"):
            label = (
                UNACCOUNTED if str(row["id"]) in unaccounted else str(row["provenance"])
            )
            counts[label] = counts.get(label, 0) + 1
        return counts

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

    def _memory_head(self) -> tuple[int, str]:
        """The log's last memory or home row, `(seq, mac)`: the key of the
        document frequency memo. `(0, "")` on a log with none. Each MAX is
        one probe of `log_by_kind`."""
        row = self._conn.execute(
            "SELECT seq, mac FROM log WHERE seq = (SELECT MAX(s) FROM ("
            "SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_put' "
            "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_delete' "
            "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_put' "
            "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_delete'))"
        ).fetchone()
        if row is None:
            return 0, ""
        return int(row["seq"]), str(row["mac"])

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

    def _memoised_document_frequencies(
        self,
        unique: list[str],
        admit: Callable[[list[str], str | None, Actor | None], bool],
    ) -> tuple[int, dict[str, int], dict[str, int]] | None:
        """`document_frequencies` through the document frequency memo."""
        key = (str(self._path), self._store_id)
        head = self._memory_head()
        known: dict[str, tuple[int, int]] = {}
        with _DOC_FREQS_LOCK:
            entry = _DOC_FREQS.get(key)
            if entry is not None and entry.head != head:
                entry = None
            if entry is not None:
                _DOC_FREQS.move_to_end(key)
                for term in unique:
                    masks = entry.terms.get(term)
                    if masks is not None:
                        entry.terms.move_to_end(term)
                        known[term] = masks
        fresh = entry is None
        if entry is None:
            entry = self._read_doc_freq_inputs(head)
        # The predicate runs on every call, once per group: it is not a
        # function of the store alone.
        admitted = 0
        for scopes, home_project, actor, mask in entry.groups:
            if admit(list(scopes), home_project, actor):
                admitted |= mask
        found: dict[str, tuple[int, int]] = {}
        if admitted:
            found = {t: self._term_masks(t, entry) for t in unique if t not in known}
        if (fresh or found) and self._memory_head() == head:
            self._keep_doc_freqs(key, entry, found, fresh=fresh)
        if not admitted:
            return None
        body_df: dict[str, int] = {}
        scope_df: dict[str, int] = {}
        for term in unique:
            body_mask, any_mask = known.get(term) or found[term]
            body_count = (body_mask & admitted).bit_count()
            any_count = (any_mask & admitted).bit_count()
            if body_count:
                body_df[term] = body_count
            if any_count:
                scope_df[term] = any_count
        return admitted.bit_count(), body_df, scope_df

    @staticmethod
    def _keep_doc_freqs(
        key: tuple[str, str],
        entry: _DocFreqInputs,
        found: dict[str, tuple[int, int]],
        *,
        fresh: bool,
    ) -> None:
        """Store `entry`, read at the store's current memory head, or add
        `found` to the entry already kept at that head."""
        with _DOC_FREQS_LOCK:
            current = _DOC_FREQS.get(key)
            if current is not entry:
                # A kept entry was replaced or dropped since this call read
                # it, or another connection kept one at this head first:
                # its masks index its own scan, so `found` is not merged.
                if not fresh or (current is not None and current.head == entry.head):
                    return
                _DOC_FREQS[key] = entry
            entry.terms.update(found)
            for term in found:
                entry.terms.move_to_end(term)
            while len(entry.terms) > DOC_FREQ_TERMS:
                entry.terms.popitem(last=False)
            _DOC_FREQS.move_to_end(key)
            while len(_DOC_FREQS) > DOC_FREQ_STORES:
                _DOC_FREQS.popitem(last=False)

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

    def declared_scopes(self, record_ids: Sequence[str]) -> dict[str, list[str]]:
        """``{id: scopes}`` for the memories and episodes asked, as written;
        an id that is neither (a removed memory) is absent. What
        `membership.Membership.homes` reads a global record's named
        projects from."""
        wanted = list(dict.fromkeys(record_ids))
        out: dict[str, list[str]] = {}
        for table in ("memories", "episodes"):
            for start in range(0, len(wanted), _PROVENANCE_BATCH):
                batch = wanted[start : start + _PROVENANCE_BATCH]
                placeholders = ",".join("?" * len(batch))
                for row in self._conn.execute(
                    f"SELECT id, scopes_json FROM {table} WHERE id IN ({placeholders})",
                    batch,
                ):
                    scopes = _load_json(row["scopes_json"], None)
                    if isinstance(scopes, list):
                        out[str(row["id"])] = [str(scope) for scope in scopes]
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

    def pending_conflict_pairs(self) -> list[tuple[str, str, str]]:
        """``(a_id, b_id, pair id)`` for every conflict row whose status is
        ``pending``, whatever the detector: the recall surface's disputed
        mark, one query per brief and per prompt."""
        return [
            (str(row["a_id"]), str(row["b_id"]), str(row["id"]))
            for row in self._conn.execute(
                "SELECT a_id, b_id, id FROM conflicts WHERE status = 'pending' "
                "ORDER BY id"
            )
        ]

    def contradicted_at(self, memory_ids: Sequence[str]) -> dict[str, datetime]:
        """The newest ``use`` event with outcome ``contradicted`` naming
        each of `memory_ids`, by id; ids never contradicted are absent.
        Reads the ``use`` rows through the log's kind index, every
        session and the whole log, since a contradiction stays open until
        the record is updated or verified, however long ago it was
        recorded. The outcome is filtered in SQL, so the rows of every
        other outcome (nearly all of them) are never parsed here."""
        wanted = set(memory_ids)
        if not wanted:
            return {}
        out: dict[str, datetime] = {}
        for row in self._conn.execute(
            "SELECT ts, payload FROM log WHERE kind = 'use' "
            "AND json_valid(payload) "
            "AND json_extract(payload, '$.outcome') = 'contradicted' ORDER BY seq"
        ):
            try:
                fields = json.loads(row["payload"])
            except ValueError:
                continue
            if not isinstance(fields, dict) or fields.get("outcome") != "contradicted":
                continue
            ids = fields.get("ids") or fields.get("memory_ids")
            if not isinstance(ids, list):
                continue
            try:
                stamp = _dt(str(row["ts"]))
            except ValueError:
                continue
            for memory_id in ids:
                if isinstance(memory_id, str) and memory_id in wanted:
                    previous = out.get(memory_id)
                    if previous is None or stamp > previous:
                        out[memory_id] = stamp
        return out

    def events_of_kinds(
        self, kinds: Collection[str], *, since: datetime
    ) -> Iterator[dict[str, Any]]:
        """The telemetry rows of `kinds` stamped at or after `since`, in
        log order, read through the log's kind index rather than the
        whole log `events_since` walks. The cut compares the stamp's
        first nineteen characters, as `events_since` does."""
        names = sorted(set(kinds))
        if not names:
            return
        cut = _iso(since.astimezone(timezone.utc)).replace("+00:00", "Z")[:19]
        placeholders = ",".join("?" * len(names))
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            f"WHERE kind IN ({placeholders}) AND substr(ts, 1, 19) >= ? "
            "ORDER BY seq",
            (*names, cut),
        ):
            event = self._event_from_raw(raw)
            if event is not None:
                yield event

    def session_events_of_kinds(
        self, session: str, kinds: Collection[str]
    ) -> Iterator[dict[str, Any]]:
        """The telemetry rows of `kinds` recorded under `session`, in log
        order, whenever they were stamped: what a session's recall state
        is rebuilt from. Read through the log's kind index, the session
        compared on each row of those kinds."""
        names = sorted(set(kinds))
        if not names:
            return
        placeholders = ",".join("?" * len(names))
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            f"WHERE kind IN ({placeholders}) AND session = ? ORDER BY seq",
            (*names, session),
        ):
            event = self._event_from_raw(raw)
            if event is not None:
                yield event

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
        return other

    def links_with_status(
        self, memory_id: str
    ) -> tuple[
        list[tuple[str, str, str | None]],
        list[tuple[str, str, str | None]],
        TrustRow | None,
    ]:
        """One record's outbound and inbound links and its trust row
        (None for an id the store does not hold, or when trust is
        unavailable)."""
        links = self.links_for_many([memory_id])[memory_id]
        trust = self.trust_rows([memory_id])
        return links[0], links[1], None if trust is None else trust.get(memory_id)

    # -- the event reads --------------------------------------------------------

    def events_since(
        self, since: datetime, *, memoised: bool = False
    ) -> Iterator[dict[str, Any]]:
        """The telemetry rows stamped at or after `since`, in log order:
        what a window read wants. The cut is the instant to the second,
        nineteen characters, compared against the whole stamp: both
        stamp spellings (``Z`` and ``+00:00``) share that prefix and a
        nineteen-character string sorts below any longer one that starts
        with it, so ``ts >= cut`` reads as a cut on the prefix. The read
        starts at the window's floor, the first row so stamped, found
        through the `log_by_ts` index (`_window_floor`), and walks the rows
        from there in log order, so a window at the log's end costs its
        own rows and not the log's (measured 0.07 ms against 2.1 ms for a
        100-row window on 34,000 rows). With `memoised`, the rows come from the store's event
        window memo (`_EVENT_WINDOWS`), which reads only the rows past the
        log's head as it last read it; the answer is the same, and each
        call gets events of its own. For a caller whose `since` only moves
        forward, as `memory_search`'s lookback does; a `since` earlier than
        the last memoised one reads the whole log again."""
        cut = _iso(since.astimezone(timezone.utc)).replace("+00:00", "Z")
        # Both spellings sort correctly against an ISO instant to the
        # second; a `Z` sorts after `+`, so the cut compares on the prefix.
        prefix = cut[:19]
        if memoised:
            for row in self._window_rows(prefix):
                event = _event_from_columns(row.ts, row.session, row.kind, row.payload)
                if event is not None:
                    yield event
            return
        floor = self._window_floor(prefix)
        if floor is None:
            return
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            "WHERE seq >= ? AND ts >= ? ORDER BY seq",
            (floor, prefix),
        ):
            event = self._event_from_raw(raw)
            if event is not None:
                yield event

    def _window_floor(self, prefix: str, *, head_seq: int | None = None) -> int | None:
        """The sequence number of the first row stamped at or after `prefix`
        (at or before `head_seq` when given): where a window read starts.
        Found through `log_by_ts`, named by ``INDEXED BY`` because the
        planner otherwise takes the rows in sequence order for the MIN and
        reads most of the log for a window at its end (1.4 ms against
        4 µs on 34,000 rows). None when no row is stamped in the window.
        A row stamped later than the rows after it (an import, a clock set
        back) lowers the floor, and the read from it is then the scan it
        was before, never more. A store whose index is gone (it has been
        in the schema since 9.0.0rc1, and `open` re-creates nothing, so
        only a hand-altered one) refuses the hint; the floor is then read
        without it, by the scan every window read was before."""
        hinted = "SELECT MIN(seq) FROM log INDEXED BY log_by_ts WHERE ts >= ?"
        params: tuple[Any, ...] = (prefix,)
        if head_seq is not None:
            hinted += " AND seq <= ?"
            params = (prefix, head_seq)
        try:
            row = self._conn.execute(hinted, params).fetchone()
        except sqlite3.OperationalError:
            plain = hinted.replace(" INDEXED BY log_by_ts", "")
            row = self._conn.execute(plain, params).fetchone()
        value = None if row is None else row[0]
        return None if value is None else int(value)

    def _window_rows(self, prefix: str) -> tuple[_WindowRow, ...]:
        """The telemetry rows whose stamp's first nineteen characters are at
        or after `prefix`, in log order, through the event window memo."""
        key = (str(self._path), self._store_id)
        in_transaction = bool(self._tx_depth) or self._conn.in_transaction
        with _EVENT_WINDOWS_LOCK:
            window = _EVENT_WINDOWS.get(key)
            if window is not None:
                _EVENT_WINDOWS.move_to_end(key)
        rows: list[_WindowRow] | None = None
        head_seq, head_mac = 0, ""
        if window is not None and window.cut <= prefix and not in_transaction:
            head = self._conn.execute(
                "SELECT mac FROM log WHERE seq = ?", (window.head_seq,)
            ).fetchone()
            if head is not None and str(head["mac"]) == window.head_mac:
                rows = [row for row in window.rows if row.cut >= prefix]
                head_seq, head_mac = window.head_seq, window.head_mac
                # Past the head, the cut is applied by SQLite, as the
                # uncached read applies it.
                read = self._conn.execute(
                    "SELECT seq, ts, session, kind, payload, mac, "
                    "substr(ts, 1, 19) AS cut, substr(ts, 1, 19) >= ? AS inside "
                    "FROM log WHERE seq > ? ORDER BY seq",
                    (prefix, head_seq),
                )
                head_seq, head_mac = self._gather_window(read, rows, head_seq, head_mac)
        if rows is None:
            rows = []
            # The head first, then the window's rows at or before it, so the
            # memo's head and its rows are one snapshot without a
            # transaction: a row appended between the two reads has a higher
            # sequence number, and the next read, from the head, picks it
            # up. The floor comes through `log_by_ts` (`_window_floor`), so
            # the read touches the window's rows where one statement
            # scanned the log.
            head = self._conn.execute(
                "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if head is not None:
                head_seq, head_mac = int(head["seq"]), str(head["mac"])
                floor = self._window_floor(prefix, head_seq=head_seq)
                if floor is not None:
                    read = self._conn.execute(
                        "SELECT seq, ts, session, kind, payload, mac, "
                        "substr(ts, 1, 19) AS cut, substr(ts, 1, 19) >= ? AS inside "
                        "FROM log WHERE seq >= ? AND seq <= ? AND ts >= ? "
                        "ORDER BY seq",
                        (prefix, floor, head_seq, prefix),
                    )
                    # The head stays the log's last row, not the window's.
                    self._gather_window(read, rows, head_seq, head_mac)
        kept = tuple(rows)
        if not in_transaction and all(isinstance(row.cut, str) for row in kept):
            with _EVENT_WINDOWS_LOCK:
                _EVENT_WINDOWS[key] = _EventWindow(
                    cut=prefix, head_seq=head_seq, head_mac=head_mac, rows=kept
                )
                _EVENT_WINDOWS.move_to_end(key)
                while len(_EVENT_WINDOWS) > EVENT_WINDOW_STORES:
                    _EVENT_WINDOWS.popitem(last=False)
        return kept

    @staticmethod
    def _gather_window(
        read: sqlite3.Cursor, rows: list[_WindowRow], head_seq: int, head_mac: str
    ) -> tuple[int, str]:
        """Append the in-window telemetry rows of `read` to `rows`; return
        the last row's sequence number and MAC, the new head."""
        for raw in read:
            head_seq, head_mac = int(raw["seq"]), str(raw["mac"])
            kind = str(raw["kind"])
            if not raw["inside"] or kind in _NOT_TELEMETRY:
                continue
            rows.append(
                _WindowRow(
                    cut=raw["cut"],
                    ts=str(raw["ts"]),
                    session=None if raw["session"] is None else str(raw["session"]),
                    kind=kind,
                    payload=raw["payload"],
                )
            )
        return head_seq, head_mac

    def iter_events_backward(self) -> Iterator[dict[str, Any]]:
        """The telemetry rows newest first."""
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            "ORDER BY seq DESC"
        ):
            event = self._event_from_raw(raw)
            if event is not None:
                yield event

    def _event_from_raw(self, raw: sqlite3.Row) -> dict[str, Any] | None:
        return _event_from_columns(
            str(raw["ts"]),
            None if raw["session"] is None else str(raw["session"]),
            str(raw["kind"]),
            raw["payload"],
        )

    def episode_provenance(self, episode_ids: Sequence[str]) -> dict[str, str]:
        """``{id: label}`` for the episodes asked: ``local`` when the row's
        pointer into the log verifies, ``unaccounted`` when it does not
        (the planted shape, a row in a foreign segment, or one in any
        segment whose key is not held under ``strict_provenance``, that
        this machine's own key did not sign either; `_pointer_verified`), the
        row's label as stored when the key is not on this machine. Unknown
        ids are omitted."""
        ids = list(dict.fromkeys(episode_ids))
        if not ids:
            return {}
        out: dict[str, str] = {}
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                f"SELECT id, log_mac FROM episodes WHERE id IN ({placeholders})", batch
            ):
                episode_id = str(row["id"])
                verified = self._pointer_verified(
                    episode_id, "episode_put", row["log_mac"]
                )
                out[episode_id] = UNACCOUNTED if verified is False else LOCAL
        return out

    # -- the log ------------------------------------------------------------

    def log_rows(self, since_seq: int = 0) -> list[LogRow]:
        return list(iter_rows(self._conn, since_seq))

    def refold(self) -> dict[str, Any]:
        """Replay every mutation row into a scratch database and compare
        each folded table with the live one. ``unaccounted`` lists live
        rows the replay does not produce (planted or edited), ``missing``
        the replayed rows the live table lacks (deleted). A row whose
        payload cannot be applied is reported as ``payload_invalid`` and
        skipped. The membership tables are folded by the same replay and
        reported under ``membership``, table by table; a membership table
        the live store lacks (a read-only store an earlier build created)
        reads as empty."""
        scratch = sqlite3.connect(":memory:", isolation_level=None)
        scratch.row_factory = sqlite3.Row
        problems: list[dict[str, Any]] = []
        try:
            scratch.executescript(SCHEMA)
            scratch.executescript(MEMBERSHIP_SCHEMA)
            scratch.execute("BEGIN")
            for row in iter_rows(self._conn):
                if row.kind not in MUTATION_KINDS:
                    continue
                try:
                    payload = json.loads(row.payload)
                    if not isinstance(payload, dict):
                        raise TypeError("payload is not an object")
                    _apply_mutation(scratch, row.kind, payload, log_mac=row.mac)
                except (ValueError, KeyError, TypeError, ValidationError) as exc:
                    problems.append(
                        {
                            "seq": row.seq,
                            "kind": row.kind,
                            "problem": "payload_invalid",
                            "detail": str(exc).splitlines()[0][:200],
                        }
                    )
            scratch.execute("COMMIT")
            tables: dict[str, Any] = {}
            membership: dict[str, Any] = {}
            diverged = bool(problems)
            present = {
                str(r[0])
                for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            for table in (*FOLDED_TABLES, *MEMBERSHIP_TABLES):
                live = _table_rows(self._conn, table) if table in present else {}
                folded = _table_rows(scratch, table)
                unaccounted = sorted(
                    (
                        _key_label(table, k)
                        for k, v in live.items()
                        if folded.get(k) != v
                    ),
                    key=str,
                )
                missing = sorted(
                    (_key_label(table, k) for k in folded if k not in live), key=str
                )
                if unaccounted or missing:
                    diverged = True
                report = tables if table in FOLDED_TABLES else membership
                report[table] = {
                    "unaccounted": unaccounted,
                    "missing": missing,
                    "rows": len(live),
                }
            # In place: a connection `reopen` made shares this set, and a
            # worker may read it meanwhile. Add first, then drop the rest:
            # each step is one call on the set, and between them it holds
            # every id still unaccounted, never fewer.
            found = set(tables["memories"]["unaccounted"])
            self._unaccounted_memory_ids.update(found)
            self._unaccounted_memory_ids.intersection_update(found)
            return {
                "status": "diverged" if diverged else "ok",
                "tables": tables,
                "membership": membership,
                "problems": problems,
            }
        finally:
            scratch.close()

    def log_verify(self) -> dict[str, Any]:
        """The chain report merged with the fold: ``tampered`` when a row
        fails its MAC, the chain breaks, the head names a row the log no
        longer has, the origin anchor no longer matches it, or a table
        differs from the fold; ``unverifiable`` when a segment's key or
        the head is absent; ``ok`` otherwise. ``origin`` is the anchor
        check (`log.OriginCheck.to_dict`)."""
        chain = verify_chain(
            self._conn, keyring=self._keyring, current_fingerprint=self.key_fingerprint
        ).to_dict()
        fold = self.refold()
        problems = list(chain["problems"]) + list(fold["problems"])
        problems.sort(key=lambda p: (int(p["seq"]), str(p["problem"])))
        status = chain["status"]
        if fold["status"] != "ok":
            status = "tampered"
        return {
            "status": status,
            "path": str(self._path),
            "store_id": self._store_id,
            "key_fingerprint": self.key_fingerprint,
            "rows": chain["rows"],
            "segments": chain["segments"],
            "head": chain["head"],
            "origin": chain["origin"],
            "problems": problems,
            "fold": {
                "status": fold["status"],
                "tables": fold["tables"],
                "membership": fold["membership"],
            },
            "unaccounted_ids": list(fold["tables"]["memories"]["unaccounted"]),
        }

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
