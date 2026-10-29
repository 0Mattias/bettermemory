"""Records to rows and back, and the one function a mutation row is applied by.

The column codecs (`_iso`, `_dt`, `_json_text` ...), the memory and
tombstone column lists and their upserts, the row writers and readers
(`_write_memory_row`, `_row_to_memory`, `_row_to_tombstone`,
`_row_to_episode` ...), `_apply_mutation`, which the live write path
(`store._mutate`) and the fold (`store_events.refold`) each apply a row
through by their own binding of it, the membership row values and keys,
the fold's table reader, the event a telemetry row reads as, and
`body_sha256`. `store` re-exports every name a caller reads. A store
module that reads one of these names imports it from here, so a test
patches it on the reader's module, never on this one. It lives apart
from `store.py` to keep each module under the package's 2,000-line bound
(ENG-96); from the family it imports `store_schema` alone.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .events import _redact_event_fields
from .identity import Actor
from .models import Episode, Memory, MemoryLink, TombstonedMemory
from .origin import Origin, is_full_commit_sha
from .search import fts_index_text
from .store_schema import (
    IMPORTED_FROM_V8,
    MEMBERSHIP_SCHEMA,
    SCHEMA,
    _CONFLICT_COLUMNS,
    _IMPORT_COLUMNS,
    _MEMBERSHIP_SPECS,
    _NOT_TELEMETRY,
    _QUARANTINE_COLUMNS,
    _TABLE_KEYS,
    _VERIFICATION_LISTS,
    _WRITABLE_PROVENANCE,
    UnknownFieldsError,
    _MembershipTable,
)


def body_sha256(body: str) -> str:
    """The sha256 of a record's body as stored, which a standing row
    names: an answer holds for that body and no other."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Records to rows and back
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _iso_opt(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _dt(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _dt_opt(text: str | None) -> datetime | None:
    return None if text is None else _dt(text)


def _scopes_text(scopes: Sequence[str]) -> str:
    """The space-padded scope list the LIKE filter matches whole tokens
    in, exactly as the v8 index spelled it."""
    if not scopes:
        return " "
    return " " + " ".join(scopes) + " "


def _json_text(value: Any) -> str:
    """The one JSON spelling a column holds: sorted keys, so the text a
    live write stores equals the text the fold derives from the log row,
    whose payload is canonical (sorted) JSON too."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _json_or_none(value: Mapping[str, Any]) -> str | None:
    return _json_text(value) if value else None


def _load_json(text: str | None, default: Any) -> Any:
    return default if text is None else json.loads(text)


def _origin_json(origin: Origin | None) -> str | None:
    if origin is None:
        return None
    return _json_or_none(origin.model_dump(mode="json", exclude_none=True))


def _actor_json(actor: Actor | None) -> str | None:
    if actor is None:
        return None
    return _json_or_none(actor.to_record())


def _record_columns(
    record: Memory | TombstonedMemory,
) -> dict[str, Any]:
    """The columns a memory and a tombstone share, straight off the record."""
    origin = record.origin
    actor = record.actor
    return {
        "id": record.id,
        "created": _iso(record.created),
        "updated": _iso(record.updated),
        "last_verified_at": _iso_opt(record.last_verified_at),
        "confidence": record.confidence.value,
        "category": record.category.value if record.category is not None else None,
        "body": record.body,
        "scopes_json": _json_text(list(record.scopes)),
        "origin_repo": origin.repo if origin is not None else None,
        "origin_worktree": origin.worktree_root if origin is not None else None,
        "verified_head": record.verified_head,
        "actor_client": actor.client if actor is not None else None,
        "actor_model": actor.model if actor is not None else None,
        "source": record.source.value,
        "origin_json": _origin_json(origin),
        "actor_json": _actor_json(actor),
        "verified_paths_json": _json_text(list(record.verified_paths)),
        "verified_commits_json": _json_text(list(record.verified_commits)),
        "verified_versions_json": _json_text(list(record.verified_versions)),
        "verified_absent_paths_json": _json_text(list(record.verified_absent_paths)),
        "claims_json": _json_text(list(record.claims)),
    }


def _links_json(links: Iterable[MemoryLink]) -> str:
    return _json_text(
        [link.model_dump(mode="json", exclude_none=True) for link in links]
    )


_MEMORY_COLUMNS = (
    "id",
    "created",
    "updated",
    "last_verified_at",
    "confidence",
    "category",
    "body",
    "body_fts",
    "scopes_text",
    "scopes_fts",
    "scopes_json",
    "filename",
    "origin_repo",
    "origin_worktree",
    "provenance",
    "verified_head",
    "actor_client",
    "actor_model",
    "source",
    "origin_json",
    "actor_json",
    "verified_paths_json",
    "verified_commits_json",
    "verified_versions_json",
    "verified_absent_paths_json",
    "claims_json",
    "links_json",
    "corroborations",
    "last_corroborated",
    "log_mac",
)

_TOMBSTONE_COLUMNS = (
    "id",
    "created",
    "updated",
    "last_verified_at",
    "confidence",
    "category",
    "body",
    "scopes_json",
    "filename",
    "origin_repo",
    "origin_worktree",
    "provenance",
    "verified_head",
    "actor_client",
    "actor_model",
    "source",
    "origin_json",
    "actor_json",
    "verified_paths_json",
    "verified_commits_json",
    "verified_versions_json",
    "verified_absent_paths_json",
    "claims_json",
    "links_json",
    "corroborations",
    "last_corroborated",
    "removed",
    "removed_reason",
    "removed_session",
    "log_mac",
)


def _upsert_sql(table: str, columns: Sequence[str], key: str) -> str:
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != key)
    return (
        f"INSERT INTO {table}({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}"
    )


_UPSERT_MEMORY = _upsert_sql("memories", _MEMORY_COLUMNS, "id")
_UPSERT_TOMBSTONE = _upsert_sql("tombstones", _TOMBSTONE_COLUMNS, "id")


def _sync_links(conn: sqlite3.Connection, memory: Memory) -> None:
    """Replace the outbound link rows for the memory, exact duplicates
    collapsed the way the v8 index collapsed them."""
    conn.execute("DELETE FROM memory_links WHERE source_id = ?", (memory.id,))
    if not memory.links:
        return
    seen: set[tuple[str, str, str, str | None]] = set()
    rows: list[tuple[str, str, str, str | None]] = []
    for link in memory.links:
        key = (memory.id, link.type.value, link.target_id, link.note)
        if key in seen:
            continue
        seen.add(key)
        rows.append(key)
    conn.executemany(
        "INSERT OR IGNORE INTO memory_links(source_id, type, target_id, note) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


def _write_memory_row(
    conn: sqlite3.Connection,
    memory: Memory,
    *,
    provenance: str,
    filename: str | None,
    log_mac: str | None,
) -> None:
    columns = _record_columns(memory)
    columns.update(
        {
            "body_fts": fts_index_text(memory.body),
            "scopes_text": _scopes_text(memory.scopes),
            "scopes_fts": fts_index_text(" ".join(memory.scopes)),
            "filename": filename,
            "provenance": provenance,
            "links_json": _links_json(memory.links),
            "corroborations": memory.corroborations,
            "last_corroborated": _iso_opt(memory.last_corroborated),
            "log_mac": log_mac,
        }
    )
    conn.execute(_UPSERT_MEMORY, tuple(columns[c] for c in _MEMORY_COLUMNS))
    _sync_links(conn, memory)


def _write_tombstone_row(
    conn: sqlite3.Connection,
    dead: TombstonedMemory,
    *,
    provenance: str,
    filename: str | None,
    links: list[dict[str, Any]],
    corroborations: int,
    last_corroborated: str | None,
    log_mac: str | None,
) -> None:
    columns = _record_columns(dead)
    columns.update(
        {
            "filename": filename,
            "provenance": provenance,
            "links_json": _json_text(links),
            "corroborations": corroborations,
            "last_corroborated": last_corroborated,
            "removed": _iso(dead.removed),
            "removed_reason": dead.removed_reason,
            "removed_session": dead.removed_session,
            "log_mac": log_mac,
        }
    )
    conn.execute(_UPSERT_TOMBSTONE, tuple(columns[c] for c in _TOMBSTONE_COLUMNS))


def _anchor_or_none(value: Any) -> str | None:
    """The loader drops an anchor the store would have refused (a branch
    name or an abbreviation edited into the row), so a revision string
    planted there never reaches git and never fails a load."""
    if value is None:
        return None
    candidate = str(value).strip().lower()
    return candidate if is_full_commit_sha(candidate) else None


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=row["id"],
        created=_dt(row["created"]),
        updated=_dt(row["updated"]),
        scopes=_load_json(row["scopes_json"], []),
        confidence=row["confidence"],
        source=row["source"],
        body=row["body"],
        origin=(
            Origin.model_validate(_load_json(row["origin_json"], {}))
            if row["origin_json"] is not None
            else None
        ),
        actor=(
            Actor.model_validate(_load_json(row["actor_json"], {}))
            if row["actor_json"] is not None
            else None
        ),
        last_verified_at=_dt_opt(row["last_verified_at"]),
        category=row["category"],
        verified_paths=_load_json(row["verified_paths_json"], []),
        verified_commits=_load_json(row["verified_commits_json"], []),
        verified_versions=_load_json(row["verified_versions_json"], []),
        verified_absent_paths=_load_json(row["verified_absent_paths_json"], []),
        claims=_load_json(row["claims_json"], []),
        verified_head=_anchor_or_none(row["verified_head"]),
        links=[
            MemoryLink.model_validate(entry)
            for entry in _load_json(row["links_json"], [])
        ],
        corroborations=int(row["corroborations"]),
        last_corroborated=_dt_opt(row["last_corroborated"]),
    )


def _row_to_tombstone(row: sqlite3.Row) -> TombstonedMemory:
    return TombstonedMemory(
        id=row["id"],
        created=_dt(row["created"]),
        updated=_dt(row["updated"]),
        scopes=_load_json(row["scopes_json"], []),
        confidence=row["confidence"],
        source=row["source"],
        body=row["body"],
        origin=(
            Origin.model_validate(_load_json(row["origin_json"], {}))
            if row["origin_json"] is not None
            else None
        ),
        actor=(
            Actor.model_validate(_load_json(row["actor_json"], {}))
            if row["actor_json"] is not None
            else None
        ),
        last_verified_at=_dt_opt(row["last_verified_at"]),
        category=row["category"],
        verified_paths=_load_json(row["verified_paths_json"], []),
        verified_commits=_load_json(row["verified_commits_json"], []),
        verified_versions=_load_json(row["verified_versions_json"], []),
        verified_absent_paths=_load_json(row["verified_absent_paths_json"], []),
        claims=_load_json(row["claims_json"], []),
        verified_head=_anchor_or_none(row["verified_head"]),
        removed=_dt(row["removed"]),
        removed_reason=row["removed_reason"],
        removed_session=row["removed_session"],
    )


def _write_episode_row(
    conn: sqlite3.Connection, episode: Episode, *, log_mac: str | None
) -> None:
    conn.execute(
        "INSERT INTO episodes(id, session_id, created, body, scopes_json, takeaway, "
        "origin_json, is_floor, swarm_id, log_mac) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET session_id = excluded.session_id, "
        "created = excluded.created, body = excluded.body, "
        "scopes_json = excluded.scopes_json, takeaway = excluded.takeaway, "
        "origin_json = excluded.origin_json, is_floor = excluded.is_floor, "
        "swarm_id = excluded.swarm_id, log_mac = excluded.log_mac",
        (
            episode.id,
            episode.session_id,
            _iso(episode.created),
            episode.body,
            _json_text(list(episode.scopes)),
            episode.takeaway,
            _origin_json(episode.origin),
            1 if episode.is_floor else 0,
            episode.swarm_id,
            log_mac,
        ),
    )


def _row_to_episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        session_id=row["session_id"],
        created=_dt(row["created"]),
        body=row["body"],
        scopes=_load_json(row["scopes_json"], []),
        takeaway=row["takeaway"],
        origin=(
            Origin.model_validate(_load_json(row["origin_json"], {}))
            if row["origin_json"] is not None
            else None
        ),
        is_floor=bool(row["is_floor"]),
        swarm_id=row["swarm_id"],
    )


def _verification_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "memory_id": row["memory_id"],
        "machine_id": row["machine_id"],
        "verified_at": row["verified_at"],
        "verified_head": row["verified_head"],
        **{name: _load_json(row[f"{name}_json"], []) for name in _VERIFICATION_LISTS},
    }


# ---------------------------------------------------------------------------
# Mutations: one function, used by the live path and by the fold
# ---------------------------------------------------------------------------


#: The record a put of each record kind carries, and the model that
#: defines its fields.
_RECORD_PUTS: dict[str, tuple[str, type[Memory] | type[TombstonedMemory]]] = {
    "memory_put": ("memory", Memory),
    "tombstone_put": ("tombstone", TombstonedMemory),
}


def _unknown_record_fields(conn: sqlite3.Connection, record_id: str) -> list[str]:
    """The fields of the record `record_id` that this build's models do not
    define, as its rows were last put: the payload of the log row each of
    its rows (the active one and the tombstone) points at by ``log_mac``,
    found through ``log_by_mac``. Empty for a new record, for one an
    earlier or this build wrote, and on a connection whose log does not
    hold those rows (the fold's scratch database)."""
    unknown: set[str] = set()
    for kind, payload in conn.execute(
        "SELECT kind, payload FROM log WHERE mac IN ("
        "SELECT log_mac FROM memories WHERE id = ? "
        "UNION ALL SELECT log_mac FROM tombstones WHERE id = ?)",
        (record_id, record_id),
    ):
        carried = _RECORD_PUTS.get(str(kind))
        if carried is None:
            continue
        key, model = carried
        try:
            record = json.loads(payload).get(key)
        except (ValueError, AttributeError):
            continue
        if isinstance(record, dict):
            unknown.update(
                str(name) for name in record if name not in model.model_fields
            )
    return sorted(unknown)


def _refuse_unknown_fields(conn: sqlite3.Connection, record_id: str, kind: str) -> None:
    """Raise `UnknownFieldsError` when the record `record_id` carries fields
    this build's models do not define (`_unknown_record_fields`). Every
    mutation of a memory or a tombstone asks first (ENG-107): this build's
    model would drop such fields on an update, a verify or a corroboration,
    carry the record into a tombstone or back without them on a removal or
    a restore, and keep none of the rules that tie them to the fields it
    edits, so an older writer refuses rather than rewrite a later build's
    record. A prune and a purge are refused too: an older build leaves a
    later build's record to that build. The check runs on the live write
    path and in the fold alike, and finds nothing in the fold, whose
    scratch log is empty: a row the live path wrote replays as it was."""
    unknown = _unknown_record_fields(conn, record_id)
    if unknown:
        raise UnknownFieldsError(record_id, kind, unknown)


def _apply_mutation(
    conn: sqlite3.Connection,
    kind: str,
    payload: Mapping[str, Any],
    *,
    log_mac: str | None,
) -> None:
    """Apply one mutation row to the tables on ``conn``; ``log_mac`` is
    the MAC of the row being applied, stamped on the record it puts. Raises
    ValueError, KeyError, TypeError or a pydantic ValidationError on a
    payload that does not carry what the kind needs; the fold reports
    those rows as ``payload_invalid``. A mutation of a record a later build
    wrote with fields this build does not define raises
    `UnknownFieldsError` (`_refuse_unknown_fields`)."""
    if kind == "memory_put":
        memory = Memory.model_validate(payload["memory"])
        _refuse_unknown_fields(conn, memory.id, kind)
        provenance = str(payload["provenance"])
        if provenance not in _WRITABLE_PROVENANCE:
            raise ValueError(f"unknown provenance {provenance!r}")
        _write_memory_row(
            conn,
            memory,
            provenance=provenance,
            filename=payload.get("filename"),
            log_mac=log_mac,
        )
    elif kind == "memory_delete":
        _refuse_unknown_fields(conn, str(payload["id"]), kind)
        conn.execute("DELETE FROM memories WHERE id = ?", (str(payload["id"]),))
    elif kind == "tombstone_put":
        dead = TombstonedMemory.model_validate(payload["tombstone"])
        _refuse_unknown_fields(conn, dead.id, kind)
        provenance = str(payload["provenance"])
        if provenance not in _WRITABLE_PROVENANCE:
            raise ValueError(f"unknown provenance {provenance!r}")
        links = payload.get("links") or []
        if not isinstance(links, list):
            raise TypeError("links must be a list")
        _write_tombstone_row(
            conn,
            dead,
            provenance=provenance,
            filename=payload.get("filename"),
            links=links,
            corroborations=int(payload.get("corroborations") or 0),
            last_corroborated=payload.get("last_corroborated"),
            log_mac=log_mac,
        )
    elif kind == "tombstone_delete":
        _refuse_unknown_fields(conn, str(payload["id"]), kind)
        conn.execute("DELETE FROM tombstones WHERE id = ?", (str(payload["id"]),))
    elif kind == "episode_put":
        _write_episode_row(
            conn, Episode.model_validate(payload["episode"]), log_mac=log_mac
        )
    elif kind == "episode_delete":
        conn.execute("DELETE FROM episodes WHERE id = ?", (str(payload["id"]),))
    elif kind == "verification_put":
        memory_id = str(payload["memory_id"])
        machine_id = payload.get("machine_id")
        lists = {name: list(payload.get(name) or []) for name in _VERIFICATION_LISTS}
        conn.execute(
            "DELETE FROM verifications WHERE memory_id = ? "
            "AND COALESCE(machine_id, '') = COALESCE(?, '')",
            (memory_id, machine_id),
        )
        conn.execute(
            "INSERT INTO verifications(memory_id, machine_id, verified_at, "
            "verified_head, verified_paths_json, verified_commits_json, "
            "verified_versions_json, verified_absent_paths_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_id,
                machine_id,
                str(payload["verified_at"]),
                payload.get("verified_head"),
                *(_json_text(lists[name]) for name in _VERIFICATION_LISTS),
            ),
        )
    elif kind == "verification_delete":
        conn.execute(
            "DELETE FROM verifications WHERE memory_id = ? "
            "AND COALESCE(machine_id, '') = COALESCE(?, '')",
            (str(payload["memory_id"]), payload.get("machine_id")),
        )
    elif kind == "conflict_put":
        record = payload["conflict"]
        conn.execute(
            f"INSERT OR REPLACE INTO conflicts({', '.join(_CONFLICT_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _CONFLICT_COLUMNS)})",
            tuple(record.get(c) for c in _CONFLICT_COLUMNS),
        )
    elif kind == "conflict_delete":
        conn.execute("DELETE FROM conflicts WHERE id = ?", (str(payload["id"]),))
    elif kind == "import_put":
        conn.execute(
            f"INSERT OR REPLACE INTO imports({', '.join(_IMPORT_COLUMNS)}) "
            "VALUES (?, ?, ?, ?)",
            (
                str(payload["source"]),
                str(payload["content_hash"]),
                str(payload["imported_at"]),
                payload.get("memory_id"),
            ),
        )
    elif kind == "import_delete":
        conn.execute("DELETE FROM imports WHERE source = ?", (str(payload["source"]),))
    elif kind == "quarantine_put":
        conn.execute(
            f"INSERT OR REPLACE INTO quarantine({', '.join(_QUARANTINE_COLUMNS)}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(payload["name"]),
                str(payload["reason"]),
                payload.get("detail"),
                payload.get("remote"),
                str(payload["at"]),
                payload.get("size"),
                payload.get("sha256"),
            ),
        )
    elif kind == "quarantine_delete":
        conn.execute("DELETE FROM quarantine WHERE name = ?", (str(payload["name"]),))
    elif kind.endswith("_put") and kind[: -len("_put")] in _MEMBERSHIP_SPECS:
        spec = _MEMBERSHIP_SPECS[kind[: -len("_put")]]
        conn.execute(
            f"INSERT OR REPLACE INTO {spec.table}({', '.join(spec.columns)}) "
            f"VALUES ({', '.join('?' for _ in spec.columns)})",
            _membership_values(spec, payload[spec.kind]),
        )
    elif kind.endswith("_delete") and kind[: -len("_delete")] in _MEMBERSHIP_SPECS:
        spec = _MEMBERSHIP_SPECS[kind[: -len("_delete")]]
        conn.execute(
            f"DELETE FROM {spec.table} WHERE "
            + " AND ".join(f"{column} = ?" for column in spec.key),
            _membership_key(spec, payload),
        )
    else:
        raise ValueError(f"unknown mutation kind {kind!r}")


def _membership_values(spec: _MembershipTable, row: Any) -> tuple[Any, ...]:
    """The column values of a membership row, in the table's column order:
    every column present and no other, the key columns non-empty text,
    the rest text, integers or null (a finite float in a ``floats``
    column), a ``_json`` column's value as its canonical JSON text (null
    stays null). Raises KeyError, TypeError or
    ValueError on a row that does not fit, which the fold reports as
    ``payload_invalid``."""
    if not isinstance(row, Mapping):
        raise TypeError(f"a {spec.kind} row must be an object")
    unknown = set(row) - set(spec.columns)
    if unknown:
        raise ValueError(f"unknown {spec.kind} columns {sorted(unknown)}")
    values: list[Any] = []
    for column in spec.columns:
        value = row.get(column) if column in spec.optional else row[column]
        if column.endswith("_json"):
            values.append(None if value is None else _json_text(value))
        elif isinstance(value, bool):
            values.append(int(value))
        elif value is None or isinstance(value, (str, int)):
            values.append(value)
        elif (
            isinstance(value, float) and column in spec.floats and math.isfinite(value)
        ):
            values.append(value)
        else:
            raise TypeError(f"{spec.kind}.{column} must be text, an integer or null")
    _membership_key(spec, row)
    return tuple(values)


def _membership_spec(kind: str) -> _MembershipTable:
    spec = _MEMBERSHIP_SPECS.get(kind)
    if spec is None:
        raise ValueError(f"{kind!r} is not a membership row kind")
    return spec


def _membership_column(spec: _MembershipTable, column: str) -> None:
    if column not in spec.columns:
        raise ValueError(f"{spec.table} has no column {column!r}")


def _sort_rows(rows: list[dict[str, Any]], order_by: Sequence[str]) -> None:
    """Sort rows read in several batches as one ``ORDER BY`` would: null
    first ascending, last descending, the first name deciding first."""
    for name in reversed(order_by):
        column = name.lstrip("-")
        rows.sort(
            key=lambda row: (
                row[column] is not None,
                row[column] if row[column] is not None else 0,
            ),
            reverse=name.startswith("-"),
        )


def _membership_key(
    spec: _MembershipTable, payload: Mapping[str, Any]
) -> tuple[str, ...]:
    """The key column values a membership row or deletion names, each
    non-empty text."""
    key: list[str] = []
    for column in spec.key:
        value = payload[column]
        if not isinstance(value, str) or not value:
            raise ValueError(f"{spec.kind}.{column} must be non-empty text")
        key.append(value)
    return tuple(key)


@functools.cache
def _known_columns() -> dict[str, tuple[str, ...]]:
    """The columns this build's schema gives each table, in the schema's
    order: read once from an in-memory database built from `SCHEMA` and
    `MEMBERSHIP_SCHEMA`, the statements the fold replays into."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.executescript(MEMBERSHIP_SCHEMA)
        tables = [
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {
            table: tuple(str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})"))
            for table in tables
        }
    finally:
        conn.close()


def _table_rows(
    conn: sqlite3.Connection, table: str
) -> dict[tuple[Any, ...], tuple[Any, ...]]:
    """Every row of a folded table keyed by its key columns, over the
    columns this build's schema gives the table (`_known_columns`), named
    and in the schema's order, rowid left out: the fold compares records,
    and rowids are the tie order the candidate query is graded on
    elsewhere. A column a later build added to the live table is not read
    (ENG-107): this build's fold cannot produce it, so reading it made
    every row of the table differ and an older `log verify` report the
    store tampered. Reading by name also keeps the two sides aligned when
    a table holds its columns in another order than the schema's."""
    have = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]
    known = _known_columns().get(table)
    order = have if known is None else [c for c in known if c in have]
    columns = [c for c in order if c != "rowid"]
    keys = _TABLE_KEYS[table]
    out: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table}"):
        values = tuple(row[c] for c in columns)
        out[tuple(row[k] for k in keys)] = values
    return out


def _key_label(table: str, key: tuple[Any, ...]) -> Any:
    if len(key) == 1:
        return key[0]
    return list(key)


_ROW_COLUMNS = ("ts", "session", "kind")


def _event_from_columns(
    ts: str, session: str | None, kind: str, payload: str
) -> dict[str, Any] | None:
    """A telemetry row's columns in the v8 event shape: ``ts``,
    ``session`` (when the row has one), ``kind``, then the payload's
    fields. None for a mutation or control row, and for a payload that is
    not a JSON object."""
    if kind in _NOT_TELEMETRY:
        return None
    try:
        fields = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(fields, dict):
        return None
    event: dict[str, Any] = {"ts": ts}
    if session is not None:
        event["session"] = session
    event["kind"] = kind
    for key, value in fields.items():
        if key not in _ROW_COLUMNS:
            event[key] = value
    return event


def event_import_payload(
    event: Mapping[str, Any], *, imported_from: str = IMPORTED_FROM_V8
) -> tuple[str | None, str | None, str, dict[str, Any]]:
    """The row a v8 event becomes: ``(ts, session, kind, payload)``.

    ``ts``, ``session`` and ``kind`` leave the event for the row's own
    columns; every other field stays in the payload, ``query`` and
    ``probe_query`` redacted the way ``record_event`` redacts them, and
    ``imported_from`` names the source. An absent or empty ``ts`` comes
    back as None (the caller stamps the row). Raises ValueError on an
    event without a string kind, or whose kind names a mutation or a
    control row: those are not events in any store.
    """
    kind = event.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("an event needs a string kind")
    if kind in _NOT_TELEMETRY:
        raise ValueError(f"{kind!r} is a mutation or control kind, not an event")
    ts = event.get("ts")
    session = event.get("session")
    payload = _redact_event_fields(
        {k: v for k, v in event.items() if k not in _ROW_COLUMNS}
    )
    payload["imported_from"] = imported_from
    return (
        ts if isinstance(ts, str) and ts else None,
        session if isinstance(session, str) and session else None,
        kind,
        payload,
    )
