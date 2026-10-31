"""The records a write brings back into force (ENG-200).

A record is in force under every record when no active record holds a
``supersedes`` link to it (`in_force`). It comes back into force in one
store transaction: the last record superseding it is removed or put
without the link, or it is itself restored with none superseding it. Each
of those writes names it (`candidates`), and the commit of the
transaction marks it (`settle`, `Store._flush_returns`) with a
``returned_put`` row naming the record and ``via``, the record whose
change brought it back (itself for a restore). The marks are written
last, after the transaction's record rows, so a record's own rows stay
next to each other in the log, and each record is judged as the
transaction leaves it. Every door goes through
the store: memory_remove, memory_update's links, memory_admin restore,
the CLI's restore, the capture's closing call, capture undo and
rollback.

A mark says no standing roster has read the record since it came back.
The store marks every record that comes back, standing or not; the
standing layer reads the mark only for a standing record while the write
review is in force (`standing.held_ids`), which holds it out of force
until its own memory_update reads it against the roster. A mark on a
record that is not standing holds nothing; the update that makes it
standing reads the roster, as every such update does. Any memory_update
that reads a marked record against the roster drops the mark in its
commit (`handlers.standing_review.release`). Removing the record drops
its mark, and so does a record that supersedes it again; a later return
marks it again.

A restore also puts back the index rows of the links into the record:
removing it dropped them (the ``memory_links_cleanup`` trigger deletes
the rows into a deleted record as well as out of it), so a restored
record that an active record still supersedes read as in force. Each
active record whose links name it is put again, unchanged, with the
`RELINK_MARK` payload field, a ``memory_put`` row every build folds the
same way; the trigger is left as it is, since changing it would change
the fold of an existing log kind (CONTRIBUTING.md, "Store versions").

`backfill` marks the records an earlier build brought back: the log shows
a ``supersedes`` link into each from another record's put, or its
restore, and none holds it now. It first puts back the index rows an
earlier build's restore left out (`missing_links`), so a restored record
an active record still supersedes reads as history and is not marked.
Step 2 leaves it to the owner's open (`ensure_backfilled`), which holds
the key the rows are written under.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .models import LinkType
from .store_schema import RETURNED_BACKFILL_KEY
from .time_utils import isoformat_utc

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("bettermemory.store")

__all__ = [
    "RELINK_MARK",
    "Before",
    "backfill",
    "before",
    "candidates",
    "ensure_backfilled",
    "linking_sources",
    "missing_links",
    "returned_rows",
    "settle",
]

#: The payload field of a put that only puts a record's link rows back
#: after a restore (`relink`). The fold reads only the record.
RELINK_MARK = "relink"

_SUPERSEDES = LinkType.SUPERSEDES.value


@dataclass(frozen=True)
class Before:
    """What a record mutation looked like before it was applied: the
    record it writes or deletes, whether it was active, and the records
    its ``supersedes`` links held then."""

    record_id: str
    active: bool
    superseded: frozenset[str]


def _targets(conn: sqlite3.Connection, source_id: str) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in conn.execute(
            "SELECT target_id FROM memory_links WHERE source_id = ? AND type = ?",
            (source_id, _SUPERSEDES),
        )
    )


def _is_active(conn: sqlite3.Connection, record_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM memories WHERE id = ?", (record_id,)).fetchone()
        is not None
    )


def _superseded(conn: sqlite3.Connection, record_id: str) -> bool:
    """Whether another active record supersedes `record_id`, as `in_force`
    reads it under every record (a link to itself makes nothing history)."""
    row = conn.execute(
        "SELECT 1 FROM memory_links l JOIN memories m ON m.id = l.source_id "
        "WHERE l.type = ? AND l.target_id = ? AND l.source_id != ? LIMIT 1",
        (_SUPERSEDES, record_id, record_id),
    ).fetchone()
    return row is not None


def _marked(conn: sqlite3.Connection, record_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM returned WHERE record_id = ?", (record_id,)
    ).fetchone()
    return row is not None


def _has_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'returned'"
    ).fetchone()
    return row is not None


def before(
    conn: sqlite3.Connection, kind: str, payload: Mapping[str, Any]
) -> Before | None:
    """The state a ``memory_put`` or ``memory_delete`` changes, read before
    it is applied; None for any other kind."""
    if kind == "memory_put":
        record = payload.get("memory")
        record_id = record.get("id") if isinstance(record, Mapping) else None
    elif kind == "memory_delete":
        record_id = payload.get("id")
    else:
        return None
    if not isinstance(record_id, str):
        return None
    active = _is_active(conn, record_id)
    return Before(
        record_id=record_id,
        active=active,
        superseded=_targets(conn, record_id) if active else frozenset(),
    )


def candidates(conn: sqlite3.Connection, kind: str, state: Before) -> list[str]:
    """After `kind` was applied: the records whose marks it may change,
    for `settle` to decide at the commit. A removal names each record the
    removed one superseded, which may be back in force, and itself, whose
    mark goes with it; a put names each ``supersedes`` target it dropped,
    which may be back in force, and each it added, which is out of force
    again; a record that was not active had none to drop. A new record is
    reviewed by its write, and a restore names itself
    (`store_records._put_restored`)."""
    if kind == "memory_delete":
        if not state.active:
            return []
        return [*sorted(state.superseded), state.record_id]
    return sorted(state.superseded ^ _targets(conn, state.record_id))


def settle(
    conn: sqlite3.Connection,
    pending: Mapping[str, tuple[str, str | None]],
    *,
    now: datetime | None = None,
) -> tuple[
    list[tuple[dict[str, str], str | None]], list[tuple[dict[str, str], str | None]]
]:
    """The ``returned`` rows a transaction's record writes leave due, read
    as the transaction leaves the store: for each named record with the
    record whose change named it and the session, a mark to put when it
    is active, no active record supersedes it and it has none, and its
    mark to delete when it is no longer active or an active record
    supersedes it again (a later return marks it again), the deletion
    saying which (``dropped``: ``removed`` or ``superseded``; a release by
    a roster read says none, `Store.release_returned`). So a mark is only
    ever on an active record in force. A store whose file lacks the table
    (one step 2 could not write) gets none."""
    if not pending or not _has_table(conn):
        return [], []
    stamp = isoformat_utc(now or datetime.now(timezone.utc))
    puts: list[tuple[dict[str, str], str | None]] = []
    deletes: list[tuple[dict[str, str], str | None]] = []
    for record_id, (via, session) in sorted(pending.items()):
        marked = _marked(conn, record_id)
        active = _is_active(conn, record_id)
        if not active or _superseded(conn, record_id):
            if marked:
                dropped = "superseded" if active else "removed"
                deletes.append(({"record_id": record_id, "dropped": dropped}, session))
        elif not marked:
            puts.append(
                ({"record_id": record_id, "via": via, "returned": stamp}, session)
            )
    return puts, deletes


def linking_sources(conn: sqlite3.Connection, record_id: str) -> list[str]:
    """The active records, other than `record_id`, whose stored links name
    it, oldest id first: the rows the restore puts back (`relink`)."""
    out: list[str] = []
    for row in conn.execute(
        "SELECT id, links_json FROM memories WHERE id != ? AND links_json LIKE ? "
        "ORDER BY id",
        (record_id, f'%"{record_id}"%'),
    ):
        try:
            links = json.loads(row[1] or "[]")
        except (TypeError, ValueError):
            continue
        if any(
            isinstance(link, Mapping) and link.get("target_id") == record_id
            for link in links
        ):
            out.append(str(row[0]))
    return out


def missing_links(conn: sqlite3.Connection) -> dict[str, str]:
    """The active records whose stored links name an active record the
    index holds no row for, each with the first such record by id: the
    rows an earlier build's restore did not put back (`linking_sources`).
    Reads every record's links once."""
    active = {str(row[0]) for row in conn.execute("SELECT id FROM memories")}
    indexed = {
        (str(r[0]), str(r[1]), str(r[2]), r[3])
        for r in conn.execute(
            "SELECT source_id, type, target_id, note FROM memory_links"
        )
    }
    out: dict[str, str] = {}
    for record_id, links_json in conn.execute(
        "SELECT id, links_json FROM memories WHERE links_json IS NOT NULL "
        "AND links_json != '[]' ORDER BY id"
    ):
        try:
            links = json.loads(links_json)
        except (TypeError, ValueError):
            continue
        lost = sorted(
            link["target_id"]
            for link in links
            if isinstance(link, Mapping)
            and link.get("target_id") in active
            and (record_id, link.get("type"), link["target_id"], link.get("note"))
            not in indexed
        )
        if lost:
            out[str(record_id)] = lost[0]
    return out


def returned_rows(
    store: Store, ids: Iterable[str] | None = None
) -> dict[str, dict[str, str]]:
    """The marks of `ids` (every mark when None), keyed by record id. A
    store whose file lacks the table (a read-only file step 2 could not
    write) holds none."""
    wanted = None if ids is None else list(dict.fromkeys(ids))
    if wanted is not None and not wanted:
        return {}
    conn = store._conn
    try:
        if wanted is None:
            rows = conn.execute("SELECT record_id, via, returned FROM returned")
            return {str(r[0]): {"via": str(r[1]), "returned": str(r[2])} for r in rows}
        out: dict[str, dict[str, str]] = {}
        for start in range(0, len(wanted), 500):
            batch = wanted[start : start + 500]
            marks_sql = ",".join("?" * len(batch))
            for r in conn.execute(
                "SELECT record_id, via, returned FROM returned "
                f"WHERE record_id IN ({marks_sql})",
                batch,
            ):
                out[str(r[0])] = {"via": str(r[1]), "returned": str(r[2])}
        return out
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {}


def backfill(store: Store, *, session: str | None = None) -> list[str]:
    """Mark the records an earlier build brought back into force, in one
    transaction, and clear meta's backfill key in it: every active record
    a ``memory_put`` row's ``supersedes`` link from another record named,
    or a ``tombstone_delete`` row restored, that no active record
    supersedes now and no mark holds. A record this build has marked
    before is left to this build (a backfill that failed at one open runs
    at the next, after writes). The index rows a restore left out are put
    back first (`missing_links`, `Store._put_relinked`). Returns the ids
    marked. Reads the log's record puts once."""
    named: set[str] = {
        str(row[0])
        for row in store._conn.execute(
            "SELECT json_extract(payload, '$.id') FROM log "
            "WHERE kind = 'tombstone_delete'"
        )
        if isinstance(row[0], str)
    }
    for (payload,) in store._conn.execute(
        "SELECT payload FROM log WHERE kind = 'memory_put'"
    ):
        try:
            record = json.loads(payload).get("memory") or {}
        except (TypeError, ValueError, AttributeError):
            continue
        source = record.get("id")
        for link in record.get("links") or ():
            if (
                isinstance(link, Mapping)
                and link.get("type") == _SUPERSEDES
                and isinstance(link.get("target_id"), str)
                and link["target_id"] != source
            ):
                named.add(link["target_id"])
    named -= {
        str(row[0])
        for row in store._conn.execute(
            "SELECT coalesce(json_extract(payload, '$.returned.record_id'), "
            "json_extract(payload, '$.record_id')) FROM log "
            "WHERE kind IN ('returned_put', 'returned_delete')"
        )
        if isinstance(row[0], str)
    }
    marked: list[str] = []
    with store._transaction() as tx:
        store._put_relinked(tx, missing_links(tx), session=session)
        stamp = isoformat_utc(datetime.now(timezone.utc))
        for record_id in sorted(named):
            if (
                _is_active(tx, record_id)
                and not _superseded(tx, record_id)
                and not _marked(tx, record_id)
            ):
                store._mutate(
                    tx,
                    "returned_put",
                    {
                        "returned": {
                            "record_id": record_id,
                            "via": record_id,
                            "returned": stamp,
                        }
                    },
                    session=session,
                )
                marked.append(record_id)
        tx.execute("DELETE FROM meta WHERE key = ?", (RETURNED_BACKFILL_KEY,))
    return marked


def ensure_backfilled(store: Store) -> list[str]:
    """The run-at-open hook: `backfill` once a step 2 left meta's key and
    the store's key is held; else nothing. A failed backfill is logged and
    leaves the key, so the next open runs it again. Returns the ids
    marked."""
    if not store.has_key:
        return []
    row = store._conn.execute(
        "SELECT value FROM meta WHERE key = ?", (RETURNED_BACKFILL_KEY,)
    ).fetchone()
    if row is None:
        return []
    try:
        marked = backfill(store)
    except (sqlite3.Error, OSError, ValueError) as exc:
        # The key stays, so the next open tries again; the store is
        # served meanwhile with the marks it has.
        log.warning(
            "returned: the backfill failed and will run at the next open: %s", exc
        )
        return []
    if marked:
        log.info(
            "returned: %d records an earlier build brought back into force "
            "marked for a standing roster read",
            len(marked),
        )
    return marked
