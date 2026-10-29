"""The steps of ``store.Store.open`` once the store's file is connected.

``Store.open`` connects to the file, read-only for the read-only commands,
and hands the connection to ``_open_connected``. That reads meta and the
key's state and binds the store one of four ways: as its owner when the
key is present; as a reader (``_open_reader``) when the open may not rekey
or the key ring is bound to another file; as its owner reclaiming the log
when the key signs a segment of it; or under a new key, with a ``rekey``
row (``_open_rekeyed``). Why each, and what the owner's opener repairs, is
in ``Store.open``'s docstring. ``store`` imports this when a store is
opened, since this module reads its names. The two that tests patch on
``store`` (``chain_segments`` and ``resolve_keys_dir``) are read from it
at call time.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from .log import REKEY, KeyRing, Segment, fingerprint
from .store import log
from .store_keys import KEY_PRESENT, KeyState, _signs_a_segment, key_state
from .store_schema import (
    SCHEMA_VERSION,
    _STORE_ID_PATTERN,
    _add_membership_tables,
    check_versions,
    read_store_meta,
)

if TYPE_CHECKING:
    from .store import Store


def _open_connected(
    cls: type[Store],
    path: Path,
    conn: sqlite3.Connection,
    scratch: Path | None,
    keys_dir: Path | str | None,
    allow_rekey: bool,
    strict_provenance: bool | None,
    read_only: bool,
) -> Store:
    """Read the connected store's meta and key and bind it as its owner, a reader or under a new key; takes the open's arguments and returns the store."""
    from .store import chain_segments, resolve_keys_dir

    meta = read_store_meta(conn, path)
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
    allow_rekey = check_versions(conn, path, meta, allow_rekey, keys_dir).writes
    if not read_only:
        _add_membership_tables(conn, path, meta)
    keyring = KeyRing(
        Path(keys_dir) if keys_dir is not None else resolve_keys_dir(),
        store_id,
    )
    # The owner's opener first claims the key ring for this file
    # (`store_keys._claim_key_ring`): one that is bound to another
    # file still holding this store id is that file's, and this
    # open writes nothing to it or with it. A reader asks the same,
    # writing no binding, since a reader whose key matches would
    # sign with it too (`store_keys.key_state`).
    state = key_state(keyring, path, expected, claim=allow_rekey, conn=conn)
    key, elsewhere = state.key, state.withheld
    matches = key is not None and fingerprint(key) == expected
    if state.state == KEY_PRESENT:
        store = cls._bind(
            path,
            conn,
            keyring,
            key,
            store_id,
            strict_provenance=strict_provenance,
        )
        store._keep_copy(read_only, scratch)
        if allow_rekey:
            store._close_foreign_tail()
            store._settle_origin()
        return store
    if not allow_rekey or elsewhere is not None:
        return _open_reader(
            cls,
            path,
            conn,
            keyring,
            key,
            store_id,
            expected,
            state,
            elsewhere=elsewhere,
            matches=matches,
            strict_provenance=strict_provenance,
            read_only=read_only,
            scratch=scratch,
        )
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
    return _open_rekeyed(
        cls,
        path,
        conn,
        keyring,
        key,
        store_id,
        expected,
        segments,
        strict_provenance=strict_provenance,
    )


def _open_reader(
    cls: type[Store],
    path: Path,
    conn: sqlite3.Connection,
    keyring: KeyRing,
    key: bytes | None,
    store_id: str,
    expected: str,
    state: KeyState,
    elsewhere: Path | None,
    matches: bool,
    strict_provenance: bool | None,
    read_only: bool,
    scratch: Path | None,
) -> Store:
    """Bind the store for reading, keeping its key for checks where this machine has a history of it; returns the reader."""
    from .store import chain_segments

    reader = cls._bind(
        path,
        conn,
        keyring,
        None,
        store_id,
        strict_provenance=strict_provenance,
    )
    reader._bound_elsewhere = elsewhere
    reader._key_state = state
    reader._keep_copy(read_only, scratch)
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


def _open_rekeyed(
    cls: type[Store],
    path: Path,
    conn: sqlite3.Connection,
    keyring: KeyRing,
    key: bytes | None,
    store_id: str,
    expected: str,
    segments: list[Segment],
    strict_provenance: bool | None,
) -> Store:
    """Retire the store's key for a new one and append the rekey row naming both; returns the store bound under the new key."""
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
