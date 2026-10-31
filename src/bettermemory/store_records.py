"""The `Store` methods that write and read records, answers and tombstones.

The put and its standing answer (`put_memory`, `put_standing`), the reads
of active records (`get_memory`, `load_one`, `load_all`, `list_summaries`
...), tombstones and their restore, and the record surface the handlers
use (`write`, `update`, `mark_verified`, `record_corroboration`,
`rename_scope`, `prune_tombstones`). Each is a function of `self: Store`
that `Store` binds by name (`put_memory = _records.put_memory`), so a patch
of `Store.update` still reaches every `self.update(...)`. They write through
the store's transaction seam (`_transaction`, `_mutate`, `_in_chunks`). A
test that patches `utcnow` or `_row_to_memory` for them patches this module.
It lives apart from `store.py` to keep each module under the package's
2,000-line bound (ENG-96); it imports `store_schema` and `store_rows`, and
`store` only for type checking.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, overload

from pydantic import ValidationError

from .identity import Actor
from .log import canonical_payload
from .models import (
    Category,
    Confidence,
    Memory,
    MemoryLink,
    MemorySummary,
    Source,
    TombstonedMemory,
    TombstonedSummary,
    first_summary_line,
    generate_ulid,
    is_valid_ulid,
    utcnow,
)
from .origin import Origin, is_full_commit_sha
from .time_utils import isoformat_utc
from .store_schema import (
    LOCAL,
    UNACCOUNTED,
    _PROVENANCE_BATCH,
    _WRITABLE_PROVENANCE,
    ConcurrentUpdateError,
    MemoryNotFoundError,
    MemoryRow,
    NotFoundError,
    NotTombstonedError,
    RecordHome,
    StandingAnswer,
    TombstoneRow,
    TombstonedError,
    UnknownFieldsError,
    _scopes_after_rename,
)
from .store_rows import (
    _dt_opt,
    _iso,
    _iso_opt,
    _load_json,
    _row_to_memory,
    _row_to_tombstone,
    _unknown_record_fields,
    body_sha256,
)
from . import store_returned as _returned

if TYPE_CHECKING:
    from .store import Store


# The payload field a corroboration's ``memory_put`` row carries beside
# the record, naming the memory it corroborates
# (`Store.record_corroboration`). The fold reads only the record; the mark
# is what tells a corroboration from an edit in the log.
CORROBORATION_MARK = "corroboration"

# How `Store.record_corroboration` asks whether a session already
# corroborated a memory: a ``memory_put`` row of that session whose payload
# names the memory under `CORROBORATION_MARK`, read through the
# ``log_corroborations`` index (`ADDITIVE_SCHEMA`). One text, so the test
# that reads its query plan reads the query the store runs.
CORROBORATION_CHECK_SQL = (
    "SELECT 1 FROM log WHERE kind = 'memory_put' AND session = ? "
    "AND instr(payload, ?) > 0 LIMIT 1"
)


# -- memories -----------------------------------------------------------


def put_memory(
    self: Store,
    memory: Memory,
    *,
    provenance: str | None = None,
    filename: str | None = None,
    session: str | None = None,
    home: RecordHome | None = None,
    standing: StandingAnswer | None = None,
) -> Memory:
    """Insert or replace the record. ``provenance`` None keeps the row's
    label (``local`` for a new row); ``filename`` None keeps the row's
    name. Both are resolved before the row is logged, so the log
    carries what the table holds. ``home`` (design 8.5) writes the
    record's ``homes`` row in the same transaction; None leaves the
    homes table as it is. ``standing`` (unit RS-F7 part 1) writes the
    record's ``standing`` row for this body in the same transaction;
    None leaves the row as it is, and a row naming another body is
    read as no answer (`standing.py`).

    Refuses an id that holds a tombstone (TombstonedError): a removed
    record comes back only through `restore`, which takes the tombstone
    away in the same transaction, so no id is ever active and removed
    at once. The tombstone, the row's label and name and the write are
    read and written under one write lock (`_transaction`)."""
    if provenance is not None and provenance not in _WRITABLE_PROVENANCE:
        raise ValueError(
            f"provenance must be one of {sorted(_WRITABLE_PROVENANCE)}, got {provenance!r}"
        )
    standing_row = None if standing is None else self._standing_row(memory, standing)
    with self._transaction() as tx:
        self._put_memory(
            tx,
            memory,
            provenance=provenance,
            filename=filename,
            session=session,
            home=home,
            standing_row=standing_row,
        )
    return memory


def _put_memory(
    self: Store,
    tx: sqlite3.Connection,
    memory: Memory,
    *,
    provenance: str | None,
    filename: str | None,
    session: str | None,
    home: RecordHome | None = None,
    standing_row: Mapping[str, Any] | None = None,
    marks: Mapping[str, Any] | None = None,
) -> None:
    """`put_memory`'s write, inside the caller's transaction. `marks`
    are payload fields beside the record that say what kind of put
    this is (`CORROBORATION_MARK`); the fold reads only the record."""
    removed = tx.execute(
        "SELECT removed_reason FROM tombstones WHERE id = ?", (memory.id,)
    ).fetchone()
    if removed is not None:
        raise TombstonedError(
            f"memory {memory.id} was removed: {removed['removed_reason']}; "
            f"restore it to bring it back"
        )
    existing = tx.execute(
        "SELECT provenance, filename FROM memories WHERE id = ?", (memory.id,)
    ).fetchone()
    if existing is not None:
        provenance = provenance or str(existing["provenance"])
        filename = filename if filename is not None else existing["filename"]
    payload = {
        **(marks or {}),
        "memory": memory.model_dump(mode="json"),
        "provenance": provenance or LOCAL,
        "filename": filename,
    }
    home_row = self._home_row(memory.id, "memory", home)
    self._mutate(tx, "memory_put", payload, session=session)
    if home_row is not None:
        self._mutate(tx, "home_put", {"home": home_row}, session=session)
    if standing_row is not None:
        self._mutate(
            tx, "standing_put", {"standing": dict(standing_row)}, session=session
        )


def _standing_row(memory: Memory, answer: StandingAnswer) -> dict[str, Any]:
    """The ``standing`` row an answer for `memory`'s body is written
    as, checked before anything is written."""
    probability = answer.probability
    if (
        isinstance(probability, bool)
        or not isinstance(probability, (int, float))
        or not math.isfinite(float(probability))
        or not 0.0 <= float(probability) <= 1.0
    ):
        raise ValueError("a standing answer is a probability from 0 to 1")
    if not answer.source or not answer.answered_by:
        raise ValueError("a standing answer names its source and who gave it")
    return {
        "record_id": memory.id,
        "probability": float(probability),
        "body_sha256": body_sha256(memory.body),
        "source": answer.source,
        "answered_by": answer.answered_by,
        "answered": isoformat_utc(datetime.now(timezone.utc)),
    }


def put_standing(
    self: Store,
    record_id: str,
    answer: StandingAnswer,
    *,
    session: str | None = None,
) -> dict[str, Any]:
    """Write the standing answer for the active record's current body
    and return the row. Raises NotFoundError for a record that is not
    active. The body the answer names is read under the write lock, so
    it is the body the record has when the row lands."""
    with self._transaction() as tx:
        row = self._standing_row(self.get_memory(record_id), answer)
        self._mutate(tx, "standing_put", {"standing": row}, session=session)
    return row


def standing_rows(
    self: Store, ids: Collection[str] | None = None
) -> list[dict[str, Any]]:
    """Every active record (or those of `ids`) with what its standing
    is read from: id, category, ``updated``, scopes, the project its
    ``homes`` row names (None for none), its row's ``log_mac`` (None
    for a row written without one) and its ``standing`` row's
    probability and body sha (None for no row), newest ``updated``
    first. Bodies are not read: `standing.py` hashes one only when its
    row changed. A store whose file could not take the membership
    tables reads as having no home and no answer."""
    present = {
        str(r[0])
        for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('homes', 'standing')"
        )
    }
    home = "h.project" if "homes" in present else "NULL"
    answer = "s.probability, s.body_sha256" if "standing" in present else "NULL, NULL"
    joins = ""
    if "homes" in present:
        joins += " LEFT JOIN homes h ON h.record_id = m.id"
    if "standing" in present:
        joins += " LEFT JOIN standing s ON s.record_id = m.id"
    select = (
        f"SELECT m.id, m.category, m.updated, m.scopes_json, {home}, "
        f"m.log_mac, {answer} FROM memories m{joins}"
    )
    order = " ORDER BY m.updated DESC, m.id DESC"
    if ids is None:
        batches: list[tuple[str, list[str]]] = [(select + order, [])]
    else:
        wanted = list(dict.fromkeys(ids))
        batches = [
            (
                f"{select} WHERE m.id IN ({','.join('?' * len(chunk))}){order}",
                chunk,
            )
            for chunk in (
                wanted[start : start + _PROVENANCE_BATCH]
                for start in range(0, len(wanted), _PROVENANCE_BATCH)
            )
        ]
    out: list[dict[str, Any]] = [
        {
            "id": str(raw[0]),
            "category": None if raw[1] is None else str(raw[1]),
            "updated": str(raw[2]),
            "scopes": tuple(_load_json(raw[3], [])),
            "recorded": None if raw[4] is None else str(raw[4]),
            "log_mac": None if raw[5] is None else str(raw[5]),
            "probability": None if raw[6] is None else float(raw[6]),
            "body_sha256": None if raw[7] is None else str(raw[7]),
        }
        for sql, params in batches
        for raw in self._conn.execute(sql, params)
    ]
    if len(batches) > 1:
        out.sort(key=lambda row: (row["updated"], row["id"]), reverse=True)
    return out


def body_of(self: Store, record_id: str) -> str | None:
    """The active record's body, or None."""
    row = self._conn.execute(
        "SELECT body FROM memories WHERE id = ?", (record_id,)
    ).fetchone()
    return None if row is None else str(row[0])


def get_memory(self: Store, memory_id: str) -> Memory:
    row = self._conn.execute(
        "SELECT * FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(memory_id)
    return _row_to_memory(row)


def has_memory(self: Store, memory_id: str) -> bool:
    return (
        self._conn.execute(
            "SELECT 1 FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        is not None
    )


def get_many(self: Store, ids: Sequence[str]) -> list[Memory]:
    """The records found, in the order asked; unknown ids are skipped."""
    out: list[Memory] = []
    for memory_id in ids:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is not None:
            out.append(_row_to_memory(row))
    return out


def iter_memories(self: Store) -> Iterator[Memory]:
    for row in self._conn.execute("SELECT * FROM memories ORDER BY rowid"):
        yield _row_to_memory(row)


def iter_memory_rows(self: Store) -> Iterator[MemoryRow]:
    """Every active record with its provenance and filename, in
    rowid order: what the mirror writes."""
    for row in self._conn.execute("SELECT * FROM memories ORDER BY rowid"):
        yield MemoryRow(
            memory=_row_to_memory(row),
            provenance=str(row["provenance"]),
            filename=None if row["filename"] is None else str(row["filename"]),
        )


def memory_ids(self: Store) -> list[str]:
    return [
        str(r["id"])
        for r in self._conn.execute("SELECT id FROM memories ORDER BY rowid")
    ]


def count_memories(self: Store) -> int:
    return int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])


def filename_for(self: Store, memory_id: str) -> str | None:
    row = self._conn.execute(
        "SELECT filename FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(memory_id)
    return None if row["filename"] is None else str(row["filename"])


# -- tombstones ---------------------------------------------------------


def tombstone(
    self: Store,
    memory_id: str,
    reason: str,
    *,
    session: str | None = None,
    now: datetime | None = None,
) -> TombstonedMemory:
    """Move the active record to the tombstones, with what its row
    keeps beside it. The record is read under the write lock, so the
    tombstone keeps the record as it stands when it is removed, an
    update that landed first included. An id another writer removed
    first raises TombstonedError with its reason; one the store does
    not hold, NotFoundError."""
    with self._transaction() as tx:
        row = tx.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            gone = tx.execute(
                "SELECT removed_reason FROM tombstones WHERE id = ?", (memory_id,)
            ).fetchone()
            if gone is not None:
                raise TombstonedError(
                    f"memory {memory_id} was removed: {gone['removed_reason']}"
                )
            raise NotFoundError(memory_id)
        memory = _row_to_memory(row)
        removed = now or datetime.now(timezone.utc)
        dead = TombstonedMemory(
            **{
                name: getattr(memory, name)
                for name in TombstonedMemory.model_fields
                if name not in ("removed", "removed_reason", "removed_session")
            },
            removed=removed,
            removed_reason=reason,
            removed_session=session,
        )
        payload = {
            "tombstone": dead.model_dump(mode="json"),
            "provenance": str(row["provenance"]),
            "filename": row["filename"],
            "links": json.loads(row["links_json"]),
            "corroborations": int(row["corroborations"]),
            "last_corroborated": row["last_corroborated"],
        }
        self._mutate(tx, "memory_delete", {"id": memory_id}, session=session)
        self._mutate(tx, "tombstone_put", payload, session=session)
    return dead


def put_tombstone(
    self: Store,
    dead: TombstonedMemory,
    *,
    provenance: str | None = None,
    filename: str | None = None,
    links: Sequence[MemoryLink | Mapping[str, Any]] = (),
    corroborations: int = 0,
    last_corroborated: datetime | None = None,
    session: str | None = None,
) -> TombstonedMemory:
    """Insert or replace a tombstone row directly, with the link list
    and the corroboration rollup the record carried while active, so
    a later ``restore`` is as lossless as one after ``tombstone``.
    ``provenance`` None keeps the row's label (``local`` for a new
    row) and ``filename`` None the row's name, as ``put_memory`` does.
    Refuses an id that is active: a record is in one table or the
    other."""
    if provenance is not None and provenance not in _WRITABLE_PROVENANCE:
        raise ValueError(
            f"provenance must be one of {sorted(_WRITABLE_PROVENANCE)}, got {provenance!r}"
        )
    link_rows = [
        MemoryLink.model_validate(link).model_dump(mode="json", exclude_none=True)
        for link in links
    ]
    with self._transaction() as tx:
        if self.has_memory(dead.id):
            raise ValueError(f"{dead.id} is active; tombstone it instead")
        existing = tx.execute(
            "SELECT provenance, filename FROM tombstones WHERE id = ?", (dead.id,)
        ).fetchone()
        if existing is not None:
            provenance = provenance or str(existing["provenance"])
            filename = filename if filename is not None else existing["filename"]
        payload = {
            "tombstone": dead.model_dump(mode="json"),
            "provenance": provenance or LOCAL,
            "filename": filename,
            "links": link_rows,
            "corroborations": int(corroborations),
            "last_corroborated": _iso_opt(last_corroborated),
        }
        self._mutate(tx, "tombstone_put", payload, session=session)
    return dead


def get_tombstone(self: Store, memory_id: str) -> TombstonedMemory:
    row = self._conn.execute(
        "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(memory_id)
    return _row_to_tombstone(row)


def tombstone_links(self: Store, memory_id: str) -> list[MemoryLink]:
    """The links a tombstone keeps beside its record, which `restore`
    brings back with it."""
    row = self._conn.execute(
        "SELECT links_json FROM tombstones WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(memory_id)
    return [
        MemoryLink.model_validate(entry) for entry in _load_json(row["links_json"], [])
    ]


def iter_tombstones(self: Store) -> Iterator[TombstonedMemory]:
    for row in self._conn.execute("SELECT * FROM tombstones ORDER BY removed, rowid"):
        yield _row_to_tombstone(row)


def iter_tombstone_rows(self: Store) -> Iterator[TombstoneRow]:
    """Every tombstone with what its row keeps beside the record, in
    removal order: what the mirror writes."""
    for row in self._conn.execute("SELECT * FROM tombstones ORDER BY removed, rowid"):
        yield TombstoneRow(
            tombstone=_row_to_tombstone(row),
            provenance=str(row["provenance"]),
            filename=None if row["filename"] is None else str(row["filename"]),
            links=list(_load_json(row["links_json"], [])),
            corroborations=int(row["corroborations"]),
            last_corroborated=_dt_opt(row["last_corroborated"]),
        )


def count_tombstones(self: Store) -> int:
    return int(self._conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0])


def restore(self: Store, memory_id: str, *, session: str | None = None) -> Memory:
    """Bring a tombstoned record back as an active one, labelled
    ``local``: this store's own code path put it back. The tombstone
    is read under the write lock, so a restore never brings back a
    record another writer pruned or restored meanwhile."""
    with self._transaction() as tx:
        row = tx.execute(
            "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(memory_id)
        dead = _row_to_tombstone(row)
        memory = Memory(
            **{
                name: getattr(dead, name)
                for name in Memory.model_fields
                if name not in ("links", "corroborations", "last_corroborated")
            },
            links=[
                MemoryLink.model_validate(entry)
                for entry in json.loads(row["links_json"])
            ],
            corroborations=int(row["corroborations"]),
            last_corroborated=_dt_opt(row["last_corroborated"]),
        )
        self._put_restored(tx, memory, row["filename"], session=session)
    return memory


def _put_restored(
    self: Store,
    tx: sqlite3.Connection,
    memory: Memory,
    filename: str | None,
    *,
    session: str | None,
) -> None:
    """A restore's writes, inside its transaction: the tombstone taken
    away and the record put back, labelled ``local``; then the index rows
    of the links into it, which removing it dropped, put back by putting
    each active record whose links name it again unchanged (`relink`); then
    the record named for its ``returned`` mark, which the commit writes
    when no active record supersedes it (ENG-200, `store_returned`). The
    record's put follows its tombstone's delete directly, which is how the
    history reads a restore."""
    payload = {
        "memory": memory.model_dump(mode="json"),
        "provenance": LOCAL,
        "filename": filename,
    }
    self._mutate(tx, "tombstone_delete", {"id": memory.id}, session=session)
    self._mutate(tx, "memory_put", payload, session=session)
    self._relink(tx, memory.id, session=session)
    self._tx_returns.setdefault(memory.id, (memory.id, session))


def _relink(
    self: Store, tx: sqlite3.Connection, record_id: str, *, session: str | None
) -> list[str]:
    """Put each active record whose stored links name `record_id` again,
    unchanged, so the fold writes the index rows of its links into it
    (`store_returned.linking_sources`); returns the ids put."""
    sources = _returned.linking_sources(tx, record_id)
    return self._put_relinked(tx, dict.fromkeys(sources, record_id), session=session)


def _put_relinked(
    self: Store,
    tx: sqlite3.Connection,
    sources: Mapping[str, str],
    *,
    session: str | None,
) -> list[str]:
    """Put each active record of `sources` again, unchanged, so the fold
    writes the index rows of its links; returns the ids put. The put
    carries `store_returned.RELINK_MARK` with the record whose rows it
    puts back, which the fold ignores. A record whose row the log does not
    account for is left as it is, since putting it would sign a row that
    entered outside the log; so is one a later build wrote, which this
    build may not rewrite (ENG-107)."""
    if not sources:
        return []
    labels = self.provenance_for(list(sources))
    put: list[str] = []
    for source_id, record_id in sources.items():
        if labels.get(source_id) == UNACCOUNTED or _unknown_record_fields(
            tx, source_id
        ):
            continue
        source = tx.execute(
            "SELECT * FROM memories WHERE id = ?", (source_id,)
        ).fetchone()
        if source is None:
            continue
        self._put_memory(
            tx,
            _row_to_memory(source),
            provenance=None,
            filename=None,
            session=session,
            marks={_returned.RELINK_MARK: record_id},
        )
        put.append(source_id)
    return put


def delete_tombstone(
    self: Store, memory_id: str, *, session: str | None = None
) -> None:
    with self._transaction() as tx:
        self._require_row(tx, "tombstones", "id", memory_id)
        self._mutate(tx, "tombstone_delete", {"id": memory_id}, session=session)


def _require_row(tx: sqlite3.Connection, table: str, column: str, key: str) -> None:
    """NotFoundError unless `table` holds a row whose `column` is `key`,
    read inside the caller's transaction: a delete's existence check
    and its write under one lock."""
    found = tx.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (key,)).fetchone()
    if found is None:
        raise NotFoundError(key)


# -- the record surface the runtime uses -----------------------------------
#
# The v8 store's names, kept where the semantics are unchanged, so the
# handlers, the CLI and the benches port without a rename of their own.


def write(
    self: Store,
    *,
    content: str,
    scopes: list[str],
    confidence: Confidence = Confidence.MEDIUM,
    source: Source = Source.EXPLICIT,
    origin: Origin | None = None,
    category: Category | None = None,
    claims: list[str] | None = None,
    links: list[Any] | None = None,
    actor: Actor | dict[str, Any] | None = None,
    session: str | None = None,
    home: RecordHome | None = None,
    standing: StandingAnswer | None = None,
) -> Memory:
    """Create a new memory: mints the id, stamps created and updated,
    stores the body stripped with one trailing newline. `claims` are
    stored verbatim; parsing and the declare-time check are the write
    handler's job, since they need the origin worktree. `home` and
    `standing` are written with the record (`put_memory`)."""
    now = utcnow()
    memory = Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=list(scopes),
        confidence=confidence,
        source=source,
        body=content.strip() + "\n",
        origin=origin,
        actor=Actor.model_validate(actor) if isinstance(actor, dict) else actor,
        category=category,
        claims=list(claims) if claims else [],
        links=[
            entry if isinstance(entry, MemoryLink) else MemoryLink.model_validate(entry)
            for entry in (links or [])
        ],
    )
    return self.put_memory(
        memory, provenance=LOCAL, session=session, home=home, standing=standing
    )


def update(
    self: Store,
    memory: Memory,
    *,
    force: bool = False,
    preserve_verification: bool = False,
    session: str | None = None,
    standing: StandingAnswer | None = None,
) -> Memory:
    """Replace an active record with the caller's edit. The caller's
    `memory.updated` is the snapshot it read; unless `force`, a stored
    `updated` that differs raises ConcurrentUpdateError so the caller
    re-reads and retries. `preserve_verification` keeps the stored
    verification fields over the snapshot's (a metadata edit must not
    undo a verify that landed in between); a content edit leaves it
    False and resets them on purpose. The corroboration rollup is
    store-owned and always carried over. ``standing`` writes the
    edited body's standing answer in the same transaction.

    The stored record is read, compared and replaced under one write
    lock (`_transaction`): of two updates built on one snapshot exactly
    one lands and the other raises ConcurrentUpdateError, and an update
    that races a removal either lands before it or finds the record
    removed (TombstonedError), never bringing it back."""
    with self._transaction():
        current = self._active_or_raise(memory.id)
        if not force and current.updated != memory.updated:
            raise ConcurrentUpdateError(memory.id, current.updated)
        carried: dict[str, object] = {
            "updated": utcnow(),
            "corroborations": current.corroborations,
            "last_corroborated": current.last_corroborated,
        }
        if preserve_verification:
            carried.update(
                {
                    "last_verified_at": current.last_verified_at,
                    "verified_paths": list(current.verified_paths),
                    "verified_commits": list(current.verified_commits),
                    "verified_versions": list(current.verified_versions),
                    "verified_absent_paths": list(current.verified_absent_paths),
                    "claims": list(current.claims),
                    "verified_head": current.verified_head,
                }
            )
        new_memory = memory.model_copy(update=carried)
        Memory.model_validate(new_memory.model_dump())
        return self.put_memory(new_memory, session=session, standing=standing)


def mark_verified(
    self: Store,
    memory_id: str,
    *,
    verified_paths: list[str] | None = None,
    verified_commits: list[str] | None = None,
    verified_versions: list[str] | None = None,
    verified_absent_paths: list[str] | None = None,
    claims: list[str] | None = None,
    verified_head: str | None = None,
    expected_last_verified_at: datetime | None = None,
    expected_updated: datetime | None = None,
    check_expected: bool = False,
    session: str | None = None,
) -> Memory:
    """Stamp `last_verified_at` now and replace whichever attestation
    lists the caller passed (None leaves a list as it is; `verified_head`
    is always written). Writes the record and this host's verification
    row in one transaction, whose write lock is taken before the record
    is read: the stamp lands on the record as it stands, and a record
    removed meanwhile raises TombstonedError. With `check_expected`, a
    stored stamp or `updated` that differs from the caller's snapshot
    raises ConcurrentUpdateError."""
    if verified_head is not None:
        verified_head = verified_head.strip().lower()
        if not is_full_commit_sha(verified_head):
            raise ValueError(
                "verified_head must be a full commit hash (40 or 64 hex "
                f"characters), got {verified_head!r}"
            )
    with self._transaction() as tx:
        existing = self._active_or_raise(memory_id)
        if check_expected and (
            existing.last_verified_at != expected_last_verified_at
            or (expected_updated is not None and existing.updated != expected_updated)
        ):
            raise ConcurrentUpdateError(memory_id, existing.updated)
        verified_now = utcnow()
        changes: dict[str, object] = {"last_verified_at": verified_now}
        if verified_paths is not None:
            changes["verified_paths"] = list(verified_paths)
        if verified_commits is not None:
            changes["verified_commits"] = list(verified_commits)
        if verified_versions is not None:
            changes["verified_versions"] = list(verified_versions)
        if verified_absent_paths is not None:
            changes["verified_absent_paths"] = list(verified_absent_paths)
        if claims is not None:
            changes["claims"] = list(claims)
        changes["verified_head"] = verified_head
        new_memory = existing.model_copy(update=changes)
        Memory.model_validate(new_memory.model_dump())
        row = tx.execute(
            "SELECT provenance, filename FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        payload = {
            "memory": new_memory.model_dump(mode="json"),
            "provenance": str(row["provenance"]),
            "filename": row["filename"],
        }
        stamp = {
            "memory_id": memory_id,
            "machine_id": None,
            "verified_at": _iso(verified_now),
            "verified_head": verified_head,
            "verified_paths": list(new_memory.verified_paths),
            "verified_commits": list(new_memory.verified_commits),
            "verified_versions": list(new_memory.verified_versions),
            "verified_absent_paths": list(new_memory.verified_absent_paths),
        }
        self._mutate(tx, "memory_put", payload, session=session)
        self._mutate(tx, "verification_put", stamp, session=session)
    return new_memory


@overload
def record_corroboration(
    self: Store, memory_id: str, *, session: None = None
) -> Memory: ...


@overload
def record_corroboration(
    self: Store, memory_id: str, *, session: str
) -> Memory | None: ...


def record_corroboration(
    self: Store, memory_id: str, *, session: str | None = None
) -> Memory | None:
    """Bump the corroboration rollup without touching `updated`: a
    dedup-rejected write is the stored claim recurring, evidence it
    still holds, not a rewrite.

    With a `session`, once per memory and session: a session that has
    already corroborated the memory gets None and nothing is written.
    The check reads the log for this session's corroboration rows
    (``CORROBORATION_MARK``, through the ``log_corroborations``
    index), and the check and the write are one transaction under the
    write lock, so parallel calls of one session, on the daemon's tool
    workers or from another process, corroborate once. Without a
    session every call corroborates."""
    with self._transaction() as tx:
        existing = self._active_or_raise(memory_id)
        if session is not None and self._corroborated(tx, memory_id, session):
            return None
        bumped = existing.model_copy(
            update={
                "corroborations": existing.corroborations + 1,
                "last_corroborated": utcnow(),
            }
        )
        self._put_memory(
            tx,
            bumped,
            provenance=None,
            filename=None,
            session=session,
            marks={CORROBORATION_MARK: memory_id},
        )
    return bumped


def _corroborated(tx: sqlite3.Connection, memory_id: str, session: str) -> bool:
    """Whether `session` has a corroboration row for `memory_id` in the
    log: a ``memory_put`` row of that session whose payload names the
    memory under ``CORROBORATION_MARK``. A body's own quotes are escaped
    in the payload text, so only the mark itself matches."""
    needle = canonical_payload({CORROBORATION_MARK: memory_id})[1:-1]
    return tx.execute(CORROBORATION_CHECK_SQL, (session, needle)).fetchone() is not None


def _active_or_raise(self: Store, memory_id: str) -> Memory:
    """The active record, or the v8 errors: TombstonedError for a
    removed id, MemoryNotFoundError otherwise."""
    if not is_valid_ulid(memory_id):
        raise MemoryNotFoundError(f"invalid id: {memory_id!r}")
    row = self._conn.execute(
        "SELECT * FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is not None:
        return _row_to_memory(row)
    dead = self._conn.execute(
        "SELECT removed_reason FROM tombstones WHERE id = ?", (memory_id,)
    ).fetchone()
    if dead is not None:
        raise TombstonedError(
            f"memory {memory_id} was removed: {dead['removed_reason']}"
        )
    raise MemoryNotFoundError(f"no memory with id {memory_id}")


def load_one(self: Store, memory_id: str) -> Memory:
    """One active memory by id; raises if missing or tombstoned."""
    return self._active_or_raise(memory_id)


def show(self: Store, memory_id: str) -> Memory:
    return self._active_or_raise(memory_id)


def load_many(self: Store, ids: list[str]) -> list[Memory]:
    """The active records among `ids`, in the order asked;
    unknown and tombstoned ids are skipped."""
    return self.get_many(ids)


def load_all(self: Store) -> list[Memory]:
    """Every active memory in row order, which is insertion order."""
    return list(self.iter_memories())


def iter_active(self: Store) -> Iterator[Memory]:
    return self.iter_memories()


def list_summaries(self: Store, scopes: list[str] | None = None) -> list[MemorySummary]:
    """Like `load_all` but body-stripped, filtered to memories carrying
    at least one of `scopes` when given."""
    out: list[MemorySummary] = []
    wanted = set(scopes) if scopes else None
    for memory in self.iter_memories():
        if wanted is not None and not (set(memory.scopes) & wanted):
            continue
        out.append(
            MemorySummary(
                id=memory.id,
                scopes=memory.scopes,
                confidence=memory.confidence,
                summary=first_summary_line(memory.body),
                created=memory.created,
                updated=memory.updated,
                last_verified_at=memory.last_verified_at,
                category=memory.category,
                actor=memory.actor,
            )
        )
    return out


def load_tombstones(self: Store) -> list[TombstonedMemory]:
    """Every tombstone, most recently removed first."""
    return [
        _row_to_tombstone(row)
        for row in self._conn.execute(
            "SELECT * FROM tombstones ORDER BY removed DESC, rowid DESC"
        )
    ]


def list_tombstones(
    self: Store, scopes: list[str] | None = None
) -> list[TombstonedSummary]:
    out: list[TombstonedSummary] = []
    wanted = set(scopes) if scopes else None
    for dead in self.load_tombstones():
        if wanted is not None and not (set(dead.scopes) & wanted):
            continue
        out.append(
            TombstonedSummary(
                id=dead.id,
                scopes=dead.scopes,
                confidence=dead.confidence,
                summary=first_summary_line(dead.body),
                created=dead.created,
                updated=dead.updated,
                last_verified_at=dead.last_verified_at,
                category=dead.category,
                removed=dead.removed,
                removed_reason=dead.removed_reason,
                removed_session=dead.removed_session,
            )
        )
    return out


def load_tombstone(self: Store, memory_id: str) -> TombstonedMemory:
    if not is_valid_ulid(memory_id):
        raise MemoryNotFoundError(f"invalid id: {memory_id!r}")
    row = self._conn.execute(
        "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        raise MemoryNotFoundError(f"no tombstone with id {memory_id}")
    return _row_to_tombstone(row)


def restore_trimmed(
    self: Store,
    memory_id: str,
    *,
    drop_claims: Iterable[str] = (),
    drop_verified_paths: Iterable[str] = (),
    clear_verification: bool = False,
    drop_verified_head: bool = False,
    session: str | None = None,
) -> Memory:
    """`restore`, minus the trust fields the caller found no longer
    hold on this machine (the restore handler's trust strip). Raises
    NotTombstonedError for an active id. The record and its tombstone
    are read under the write lock, as `restore` reads them."""
    if not is_valid_ulid(memory_id):
        raise MemoryNotFoundError(f"invalid id: {memory_id!r}")
    with self._transaction() as tx:
        if self.has_memory(memory_id):
            raise NotTombstonedError(
                f"memory {memory_id} is active; nothing to restore"
            )
        row = tx.execute(
            "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError(f"no tombstone with id {memory_id}")
        dead = _row_to_tombstone(row)
        fields: dict[str, Any] = {
            name: getattr(dead, name)
            for name in Memory.model_fields
            if name not in ("links", "corroborations", "last_corroborated")
        }
        gone_claims = set(drop_claims)
        if gone_claims:
            fields["claims"] = [c for c in dead.claims if c not in gone_claims]
        gone_paths = set(drop_verified_paths)
        if gone_paths:
            fields["verified_paths"] = [
                v for v in dead.verified_paths if v not in gone_paths
            ]
        if clear_verification:
            fields["last_verified_at"] = None
        if drop_verified_head:
            fields["verified_head"] = None
        memory = Memory(
            **fields,
            links=[
                MemoryLink.model_validate(entry)
                for entry in json.loads(row["links_json"])
            ],
            corroborations=int(row["corroborations"]),
            last_corroborated=_dt_opt(row["last_corroborated"]),
        )
        self._put_restored(tx, memory, row["filename"], session=session)
    return memory


def rename_scope(
    self: Store,
    old: str,
    new: str,
    *,
    include_tombstones: bool = True,
    session: str | None = None,
) -> dict[str, list[Any]]:
    """Replace scope `old` with `new` on every record carrying it, the
    list deduplicated in place; active records get a fresh `updated`.
    Returns the ids changed under `active` and `tombstoned`; `failed`
    is present only when a record could not be rewritten, and what it
    tried to write is taken back (`_transaction`'s savepoint).

    The records are rewritten in chunks (`_in_chunks`), so a rename
    over a large store never holds the write lock past
    `CHUNK_HOLD_SECONDS` at a time; each record is read again under
    its chunk's lock and rewritten from what it holds then. A run cut
    short leaves the rest carrying `old`, and running it again renames
    those."""
    result: dict[str, list[Any]] = {"active": [], "tombstoned": []}
    if old == new:
        return result
    failed: list[dict[str, str]] = []

    def rename_active(memory_id: str) -> None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return
        memory = _row_to_memory(row)
        renamed = _scopes_after_rename(memory.scopes, old, new)
        if renamed is None:
            return
        refreshed = memory.model_copy(update={"scopes": renamed, "updated": utcnow()})
        try:
            self.put_memory(refreshed, session=session)
        except (ValueError, ValidationError) as exc:
            failed.append({"id": memory.id, "reason": str(exc)})
            return
        result["active"].append(memory.id)

    def rename_removed(memory_id: str) -> None:
        raw = self._conn.execute(
            "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if raw is None:
            return
        dead = _row_to_tombstone(raw)
        renamed = _scopes_after_rename(dead.scopes, old, new)
        if renamed is None:
            return
        payload = {
            "tombstone": dead.model_copy(update={"scopes": renamed}).model_dump(
                mode="json"
            ),
            "provenance": str(raw["provenance"]),
            "filename": raw["filename"],
            "links": list(_load_json(raw["links_json"], [])),
            "corroborations": int(raw["corroborations"]),
            "last_corroborated": raw["last_corroborated"],
        }
        try:
            with self._transaction() as tx:
                self._mutate(tx, "tombstone_put", payload, session=session)
        except UnknownFieldsError as exc:
            # A later build's removed record: left to that build, and the
            # rename goes on to the rest.
            failed.append({"id": dead.id, "reason": str(exc)})
            return
        result["tombstoned"].append(dead.id)

    self._in_chunks(
        [
            memory.id
            for memory in self.iter_memories()
            if _scopes_after_rename(memory.scopes, old, new) is not None
        ],
        rename_active,
    )
    if include_tombstones:
        self._in_chunks(
            [
                dead.id
                for dead in self.iter_tombstones()
                if _scopes_after_rename(dead.scopes, old, new) is not None
            ],
            rename_removed,
        )
    if failed:
        result["failed"] = failed
    return result


def prune_tombstones(
    self: Store,
    older_than: timedelta,
    *,
    now: datetime | None = None,
    session: str | None = None,
) -> list[str]:
    """Delete tombstones removed longer than `older_than` ago. Returns
    the ids pruned, oldest removal first. The deletes run in chunks
    (`_in_chunks`), each tombstone's age read again under its chunk's
    write lock, so one restored or replaced meanwhile is left alone.

    A tombstone a later build wrote with a field this build does not
    define (`UnknownFieldsError`) is left for that build, and the prune
    goes on to the rest.

    A failure part way raises, and the ids are not returned: the chunks
    committed before it stay committed (those tombstones are gone),
    the failing chunk is rolled back, and the rest are still there.
    Running it again with the same cutoff prunes what is left."""
    cutoff = (now or utcnow()) - older_than
    doomed = sorted(
        (dead.removed, dead.id)
        for dead in self.iter_tombstones()
        if dead.removed < cutoff
    )
    pruned: list[str] = []

    def prune(memory_id: str) -> None:
        raw = self._conn.execute(
            "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if raw is None or _row_to_tombstone(raw).removed >= cutoff:
            return
        try:
            with self._transaction() as tx:
                self._mutate(tx, "tombstone_delete", {"id": memory_id}, session=session)
        except UnknownFieldsError:
            return
        pruned.append(memory_id)

    self._in_chunks([memory_id for _, memory_id in doomed], prune)
    return pruned
