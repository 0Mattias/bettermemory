"""The store's compatibility contract at an open (ENG-107).

The meta keys that say which builds may read and write a store, the
`StoreVersionError` an open below a store's reader minimum raises, what a
new store carries (`version_meta`), the check every open makes
(`check_versions`, which runs the schema steps of `store_steps`) and the
rolled-back transaction a read on a store open for reading only runs in
(`writes_rolled_back`). It lives apart from `store_schema`, which imports
and re-exports every name here, to keep that module under the package's
2,000-line bound (ENG-96). It imports no other store module at module
level: the revisions it compares (`store_schema.SCHEMA_REVISION`,
`MIN_READER_REVISION`, `MIN_WRITER_REVISION`) are read from `store_schema`
when a check runs, so a test that patches them there reaches it.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("bettermemory.store")

#: meta's keys for the contract (`store_schema`'s comment on the versions).
SCHEMA_REVISION_KEY = "schema_revision"
MIN_READER_KEY = "min_reader_revision"
MIN_WRITER_KEY = "min_writer_revision"
ENGINE_VERSION_KEY = "engine_version"


class StoreVersionError(ValueError):
    """The store needs a newer build to open it: its minimum reader
    revision is above this build's `SCHEMA_REVISION`, or a version key in
    its meta does not read as a whole number. `key` is the meta key that
    refused the open and `needed` the schema revision a build must read to
    open the store (None when a revision is not what it needs), which
    `doctor` names in its remedy."""

    def __init__(
        self, message: str, *, key: str | None = None, needed: int | None = None
    ) -> None:
        super().__init__(message)
        self.key = key
        self.needed = needed


# ---------------------------------------------------------------------------
# The versions an open checks (ENG-107)
# ---------------------------------------------------------------------------


class VersionVerdict(NamedTuple):
    """What `check_versions` decided for one open. `writes`: the open goes
    on as the owner's writing open (``allow_rekey``); False for a reader's
    open and for a store this build may only read. `read_only`: the store's
    minimum writer revision is above this build's, and the connection
    refuses every write (``PRAGMA query_only``); `reason` says why.
    `revision`, `min_reader` and `min_writer` are the store's as the open
    read them, 0 for a key the store does not carry."""

    writes: bool
    read_only: bool
    reason: str | None
    revision: int
    min_reader: int
    min_writer: int


# The stores this process has said it opens for reading only, each with the
# minimum writer revision that decided it: said once, not at every
# connection a daemon's workers reopen.
_READ_ONLY_TOLD: set[tuple[str, int]] = set()

_WHOLE = re.compile(r"[0-9]{1,9}")


def version_meta() -> dict[str, str]:
    """The version keys `Store.create` writes into a new store's meta: this
    build's revision and the minimums a store it creates carries."""
    from .store_schema import MIN_READER_REVISION, MIN_WRITER_REVISION, SCHEMA_REVISION

    return {
        SCHEMA_REVISION_KEY: str(SCHEMA_REVISION),
        MIN_READER_KEY: str(MIN_READER_REVISION),
        MIN_WRITER_KEY: str(MIN_WRITER_REVISION),
    }


def _shown(text: str) -> str:
    """A value read from meta, fit for one line of a message: meta holds
    whatever an editor of the file wrote, so control and format characters
    are escaped (`repr`) and the text is cut at 64 characters."""
    escaped = repr(text)[1:-1]
    return escaped if len(escaped) <= 64 else escaped[:61] + "..."


def _stored_revision(meta: Mapping[str, str], key: str, path: Path) -> int:
    """meta's revision under `key`, 0 when the store carries none. A value
    that is not a whole number raises `StoreVersionError`: meta is not
    authenticated, and an open that guessed would admit a build the store's
    writer kept out, or keep out one it admitted."""
    raw = meta.get(key)
    if raw is None:
        return 0
    if not _WHOLE.fullmatch(raw):
        raise StoreVersionError(
            f"{path} carries a {key} that is not a whole number "
            f"('{_shown(raw)}'); the store is not opened: set meta's {key} "
            "back to a whole number, or restore the store from a backup taken "
            "before the edit",
            key=key,
        )
    return int(raw)


def _release_order(version: str | None) -> tuple[int, ...] | None:
    """`version`'s place in release order (`_daemon_client.version_key`,
    any local ``+...`` part left off), None when it has none."""
    if version is None:
        return None
    from ._daemon_client import version_key

    return version_key(version.split("+", 1)[0])


def _newer_engine(stored: str | None, version: str) -> bool:
    """Whether `version` names a newer build than ``engine_version``'s
    `stored`: later in release order, or the stored one has no place in it
    and `version` has."""
    ours = _release_order(version)
    theirs = _release_order(stored)
    return ours is not None and (theirs is None or ours > theirs)


def _record_engine(
    conn: sqlite3.Connection, path: Path, meta: dict[str, str], version: str
) -> None:
    """Name this build in ``engine_version`` at a writing open when it is
    newer than the stored one (`_newer_engine`), read again under the write
    lock so two builds opening at once leave the newer. An open of a store
    that already names this build or a newer one takes no lock. A store
    that cannot be written now (a read-only file, or one another writer
    holds past the busy timeout) is left as it is with one line logged; the
    next writing open records it. `meta` gains what was written."""
    if not _newer_engine(meta.get(ENGINE_VERSION_KEY), version):
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (ENGINE_VERSION_KEY,)
            ).fetchone()
            newer = _newer_engine(None if row is None else str(row[0]), version)
            if newer:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (ENGINE_VERSION_KEY, version),
                )
            conn.execute("COMMIT")
        except BaseException:
            # The COMMIT is inside, as in `_add_membership_tables`.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.OperationalError as exc:
        log.info(
            "store %s: engine_version not recorded (%s); the next writing open "
            "records it",
            path,
            exc,
        )
        return
    if newer:
        meta[ENGINE_VERSION_KEY] = version
    elif row is not None:
        meta[ENGINE_VERSION_KEY] = str(row[0])


def check_versions(
    conn: sqlite3.Connection,
    path: Path,
    meta: dict[str, str],
    writing: bool,
    keys_dir: Path | str | None = None,
) -> VersionVerdict:
    """The compatibility contract at an open (ENG-107), run by `Store.open`
    once the store id and the schema version have passed. A store whose
    minimum reader revision is above this build's `SCHEMA_REVISION` is
    refused (`StoreVersionError`, naming the newest build that wrote it).
    One whose minimum writer revision is above it opens for reading only:
    the connection is set ``PRAGMA query_only``, so every write fails as on
    read-only media, nothing is recorded, an owner's open goes on as a
    reader's (no rekey, no key ring claim, no origin anchor), and the first
    such open in a process logs one warning saying why. Otherwise the store's
    schema steps run (`store_steps.run_steps`): at a writing open (`writing`,
    the owner's ``allow_rekey``) each step above the store's revision is
    applied and recorded, a step that is not additive after a backup signed
    with a key from `keys_dir`, the opener's keys directory; a reader's open
    applies none, and while a step that is not additive is pending it is
    set ``PRAGMA query_only`` and reads the store as it stands. A writing
    open then names this build in ``engine_version``
    when it is the newest (`_record_engine`). The verdict's `writes` is
    what the open goes on with."""
    from . import __version__
    from .store_schema import SCHEMA_REVISION

    revision = _stored_revision(meta, SCHEMA_REVISION_KEY, path)
    min_reader = _stored_revision(meta, MIN_READER_KEY, path)
    min_writer = _stored_revision(meta, MIN_WRITER_KEY, path)
    engine = meta.get(ENGINE_VERSION_KEY)
    newest = f" (its newest writer was bettermemory {_shown(engine)})" if engine else ""
    if min_reader > SCHEMA_REVISION:
        raise StoreVersionError(
            f"{path} needs a reader at schema revision {min_reader} or later "
            f"(meta's {MIN_READER_KEY}){newest}; this build, bettermemory "
            f"{__version__}, reads revision {SCHEMA_REVISION}: upgrade "
            "bettermemory to open it",
            key=MIN_READER_KEY,
            needed=min_reader,
        )
    if min_writer > SCHEMA_REVISION:
        reason = (
            f"{path} is open for reading only: it needs a writer at schema "
            f"revision {min_writer} or later{newest}, and this build, "
            f"bettermemory {__version__}, writes revision {SCHEMA_REVISION}: "
            "upgrade bettermemory to write to it"
        )
        conn.execute("PRAGMA query_only = ON")
        told = (str(path), min_writer)
        if told not in _READ_ONLY_TOLD:
            _READ_ONLY_TOLD.add(told)
            log.warning("%s", reason)
        return VersionVerdict(False, True, reason, revision, min_reader, min_writer)
    from .store_steps import run_steps

    run_steps(conn, path, meta, writing=writing, keys_dir=keys_dir)
    if writing:
        _record_engine(conn, path, meta, __version__)
    return VersionVerdict(writing, False, None, revision, min_reader, min_writer)


class _RolledBack(Exception):
    """Ends `writes_rolled_back`'s transaction, which then rolls back."""


@contextlib.contextmanager
def writes_rolled_back(store: Store) -> Iterator[bool]:
    """Run a read on `store` that records as it goes, whether or not its
    connection may write. On a connection that refuses writes (``PRAGMA
    query_only``, a store open for reading only by its versions,
    `check_versions`) the block runs in one write transaction that is
    rolled back at its end, the block's failure included, with
    ``query_only`` lifted for its length and set again after: the caller's
    membership resolution, which writes its events and, at first sight of
    a checkout, its rows, reads what it would read on a store it may
    write, and the store keeps none of it. Yields whether it does so,
    and the block then writes nothing outside the store either (the
    caller turns the checkout token's write off). On any other connection
    the block runs as it is and the yield is False.

    The block must not await: `store`'s connection serves one call at a
    time, and the lifted ``query_only`` holds for nothing but the block.
    The transaction takes the store's write lock (``BEGIN IMMEDIATE``)
    for the block's length, which another build's writer waits for within
    its busy timeout."""
    conn = store.conn
    if not conn.execute("PRAGMA query_only").fetchone()[0]:
        yield False
        return
    conn.execute("PRAGMA query_only = OFF")
    try:
        with contextlib.suppress(_RolledBack), store.batch():
            yield True
            raise _RolledBack
    finally:
        conn.execute("PRAGMA query_only = ON")
