"""One SQLite file per store: the bettermemory 9 store.

The v8 store was a directory of markdown files with a derived FTS5 index
beside it (``src/bettermemory/store.py`` and ``src/bettermemory/index.py``,
which stay in the tree until the engine port retires them). This store is
one file, ``memory.sqlite``, whose tables ARE the records: memories,
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

Transactions. Each mutating method runs ``BEGIN IMMEDIATE``, changes the
table, appends its log rows and commits, then moves the head checkpoint
forward. A failure anywhere rolls the table change and the rows back
together.

Keys. ``create`` writes the store's key under the keys directory
(``log.default_keys_dir`` unless the caller names one) and records only
its fingerprint in ``meta``. ``open`` finds the key by fingerprint; a
missing or foreign key is retired, a fresh one written, and a ``rekey``
row appended, unless the caller opens with ``allow_rekey=False``, which
leaves the store readable and refuses mutations.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ._fsutil import ensure_owner_only_dir
from .events import _redact_event_fields
from .identity import Actor
from . import log as _chain
from .log import (
    CONTROL_KINDS,
    REKEY,
    STORE_CREATED,
    KeyRing,
    LogRow,
    append_row,
    fingerprint,
    iter_rows,
    verify_chain,
)
from .models import Episode, Memory, MemoryLink, TombstonedMemory
from .origin import Origin
from .search import fts_index_text, fts_match_query, tokenizer_fingerprint
from .time_utils import isoformat_utc

log = logging.getLogger("bettermemory.sqlite_store")

STORE_FILENAME = "memory.sqlite"
SCHEMA_VERSION = 1

# Set on a store's first open, before its first table and before WAL mode
# fixes it. A record row (body, its token stream, the JSON columns) runs
# past the largest payload a 4 KB page keeps in line, so at 4 KB most rows
# spill into an overflow page of their own; 8 KB keeps them in line and
# measured 16 percent smaller on the rank-parity corpus at the same write
# cost, where 16 KB saved nothing more and cost a third on every write
# (bench/parity/sqlite_candidates.py records both sizes beside the parity).
PAGE_SIZE = 8192

# How a record entered the store. `local`: written through this store's
# own code path. `imported`: brought in by the migration or an import.
# `synced`: arrived from another machine (phase 3). `unaccounted`: a row
# the fold does not produce; reported by `log_verify`, never written.
LOCAL = "local"
IMPORTED = "imported"
SYNCED = "synced"
UNACCOUNTED = "unaccounted"
PROVENANCE_LABELS = frozenset({LOCAL, IMPORTED, SYNCED, UNACCOUNTED})
_WRITABLE_PROVENANCE = frozenset({LOCAL, IMPORTED, SYNCED})

# The tables a replay of the log reproduces, and the log kinds that
# mutate them: one put and one delete per table. `memory_links` is
# derived from each memory's link list and folded with it.
FOLDED_TABLES = (
    "memories",
    "memory_links",
    "tombstones",
    "episodes",
    "verifications",
    "conflicts",
    "imports",
    "quarantine",
)
_MUTATION_TABLES = (
    "memory",
    "tombstone",
    "episode",
    "verification",
    "conflict",
    "import",
    "quarantine",
)
MUTATION_KINDS = frozenset(
    f"{table}_{op}" for table in _MUTATION_TABLES for op in ("put", "delete")
)

# Key columns per folded table, for the fold's row comparison.
_TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "memories": ("id",),
    "memory_links": ("source_id", "type", "target_id", "note"),
    "tombstones": ("id",),
    "episodes": ("id",),
    "verifications": ("memory_id", "machine_id"),
    "conflicts": ("id",),
    "imports": ("source",),
    "quarantine": ("name",),
}

_CONFLICT_COLUMNS = (
    "id",
    "a_id",
    "b_id",
    "summary_a",
    "summary_b",
    "similarity",
    "method",
    "detector",
    "created",
    "status",
    "verdict_ts",
    "note",
    "verdict_hash_a",
    "verdict_hash_b",
)
_QUARANTINE_COLUMNS = ("name", "reason", "detail", "remote", "at", "size", "sha256")
_IMPORT_COLUMNS = ("source", "content_hash", "imported_at", "memory_id")
_VERIFICATION_LISTS = (
    "verified_paths",
    "verified_commits",
    "verified_versions",
    "verified_absent_paths",
)

_PROVENANCE_BATCH = 500


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# `memories` opens with the v8 index's columns in the v8 order, minus
# `verified_locally_at` (this host's stamps are `verifications` rows
# now) and `content_sha256` (the log covers integrity), then carries the
# rest of the record. The FTS5 table, its three triggers, the `updated`
# index, `memory_links` and its cleanup trigger are the v8 index's
# statements verbatim; tests/test_sqlite_store.py compares the text.

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    rowid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    last_verified_at TEXT,
    confidence TEXT NOT NULL,
    category TEXT,
    body TEXT NOT NULL,
    body_fts TEXT NOT NULL DEFAULT '',
    scopes_text TEXT NOT NULL,
    scopes_fts TEXT NOT NULL DEFAULT '',
    scopes_json TEXT NOT NULL,
    filename TEXT,
    origin_repo TEXT,
    origin_worktree TEXT,
    provenance TEXT NOT NULL,
    verified_head TEXT,
    actor_client TEXT,
    actor_model TEXT,
    source TEXT NOT NULL,
    origin_json TEXT,
    actor_json TEXT,
    verified_paths_json TEXT NOT NULL DEFAULT '[]',
    verified_commits_json TEXT NOT NULL DEFAULT '[]',
    verified_versions_json TEXT NOT NULL DEFAULT '[]',
    verified_absent_paths_json TEXT NOT NULL DEFAULT '[]',
    claims_json TEXT NOT NULL DEFAULT '[]',
    links_json TEXT NOT NULL DEFAULT '[]',
    corroborations INTEGER NOT NULL DEFAULT 0,
    last_corroborated TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    body_fts, scopes_fts,
    content='memories', content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, body_fts, scopes_fts)
    VALUES (new.rowid, new.body_fts, new.scopes_fts);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body_fts, scopes_fts)
    VALUES ('delete', old.rowid, old.body_fts, old.scopes_fts);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body_fts, scopes_fts)
    VALUES ('delete', old.rowid, old.body_fts, old.scopes_fts);
    INSERT INTO memories_fts(rowid, body_fts, scopes_fts)
    VALUES (new.rowid, new.body_fts, new.scopes_fts);
END;

CREATE INDEX IF NOT EXISTS memories_by_updated ON memories(updated DESC);

CREATE TABLE IF NOT EXISTS memory_links (
    source_id TEXT NOT NULL,
    type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (source_id, type, target_id, note)
);

CREATE INDEX IF NOT EXISTS memory_links_by_target ON memory_links(target_id);

CREATE TRIGGER IF NOT EXISTS memory_links_cleanup AFTER DELETE ON memories BEGIN
    DELETE FROM memory_links
    WHERE source_id = old.id OR target_id = old.id;
END;

CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    last_verified_at TEXT,
    confidence TEXT NOT NULL,
    category TEXT,
    body TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    filename TEXT,
    origin_repo TEXT,
    origin_worktree TEXT,
    provenance TEXT NOT NULL,
    verified_head TEXT,
    actor_client TEXT,
    actor_model TEXT,
    source TEXT NOT NULL,
    origin_json TEXT,
    actor_json TEXT,
    verified_paths_json TEXT NOT NULL DEFAULT '[]',
    verified_commits_json TEXT NOT NULL DEFAULT '[]',
    verified_versions_json TEXT NOT NULL DEFAULT '[]',
    verified_absent_paths_json TEXT NOT NULL DEFAULT '[]',
    claims_json TEXT NOT NULL DEFAULT '[]',
    links_json TEXT NOT NULL DEFAULT '[]',
    corroborations INTEGER NOT NULL DEFAULT 0,
    last_corroborated TEXT,
    removed TEXT NOT NULL,
    removed_reason TEXT NOT NULL,
    removed_session TEXT
);

CREATE INDEX IF NOT EXISTS tombstones_by_removed ON tombstones(removed);

CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created TEXT NOT NULL,
    body TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    takeaway TEXT,
    origin_json TEXT,
    is_floor INTEGER NOT NULL DEFAULT 0,
    swarm_id TEXT
);

CREATE INDEX IF NOT EXISTS episodes_by_session ON episodes(session_id, created);

CREATE TABLE IF NOT EXISTS log (
    seq INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    session TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_mac TEXT NOT NULL,
    mac TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS log_by_kind ON log(kind, seq);

CREATE TABLE IF NOT EXISTS verifications (
    memory_id TEXT NOT NULL,
    machine_id TEXT,
    verified_at TEXT NOT NULL,
    verified_head TEXT,
    verified_paths_json TEXT NOT NULL DEFAULT '[]',
    verified_commits_json TEXT NOT NULL DEFAULT '[]',
    verified_versions_json TEXT NOT NULL DEFAULT '[]',
    verified_absent_paths_json TEXT NOT NULL DEFAULT '[]'
);

CREATE UNIQUE INDEX IF NOT EXISTS verifications_by_memory_machine
    ON verifications(memory_id, COALESCE(machine_id, ''));

CREATE TABLE IF NOT EXISTS conflicts (
    id TEXT PRIMARY KEY,
    a_id TEXT NOT NULL,
    b_id TEXT NOT NULL,
    summary_a TEXT NOT NULL,
    summary_b TEXT NOT NULL,
    similarity REAL NOT NULL,
    method TEXT NOT NULL,
    detector TEXT NOT NULL,
    created TEXT NOT NULL,
    status TEXT NOT NULL,
    verdict_ts TEXT,
    note TEXT,
    verdict_hash_a TEXT,
    verdict_hash_b TEXT
);

CREATE TABLE IF NOT EXISTS pending_writes (
    pending_id TEXT PRIMARY KEY,
    client_key TEXT NOT NULL,
    session TEXT,
    created_at REAL NOT NULL,
    payload_json TEXT NOT NULL,
    gate_flags_json TEXT NOT NULL DEFAULT '{}',
    expired_at REAL
);

CREATE TABLE IF NOT EXISTS imports (
    source TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    memory_id TEXT
);

CREATE TABLE IF NOT EXISTS quarantine (
    name TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    detail TEXT,
    remote TEXT,
    at TEXT NOT NULL,
    size INTEGER,
    sha256 TEXT
);
"""


class NotFoundError(KeyError):
    """No record with that key in the table asked."""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _connect(path: Path) -> sqlite3.Connection:
    """The one way a store file is opened: the page size for a new file,
    WAL, synchronous NORMAL, a 5-second busy timeout, foreign keys on,
    autocommit off so every write is an explicit transaction, and
    owner-only modes on the file and its WAL siblings."""
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
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
        }
    )
    conn.execute(_UPSERT_TOMBSTONE, tuple(columns[c] for c in _TOMBSTONE_COLUMNS))


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
        verified_head=row["verified_head"],
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
        verified_head=row["verified_head"],
        removed=_dt(row["removed"]),
        removed_reason=row["removed_reason"],
        removed_session=row["removed_session"],
    )


def _write_episode_row(conn: sqlite3.Connection, episode: Episode) -> None:
    conn.execute(
        "INSERT INTO episodes(id, session_id, created, body, scopes_json, takeaway, "
        "origin_json, is_floor, swarm_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET session_id = excluded.session_id, "
        "created = excluded.created, body = excluded.body, "
        "scopes_json = excluded.scopes_json, takeaway = excluded.takeaway, "
        "origin_json = excluded.origin_json, is_floor = excluded.is_floor, "
        "swarm_id = excluded.swarm_id",
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


def _apply_mutation(
    conn: sqlite3.Connection, kind: str, payload: Mapping[str, Any]
) -> None:
    """Apply one mutation row to the tables on ``conn``. Raises ValueError,
    KeyError, TypeError or a pydantic ValidationError on a payload that
    does not carry what the kind needs; the fold reports those rows as
    ``payload_invalid``."""
    if kind == "memory_put":
        memory = Memory.model_validate(payload["memory"])
        provenance = str(payload["provenance"])
        if provenance not in _WRITABLE_PROVENANCE:
            raise ValueError(f"unknown provenance {provenance!r}")
        _write_memory_row(
            conn, memory, provenance=provenance, filename=payload.get("filename")
        )
    elif kind == "memory_delete":
        conn.execute("DELETE FROM memories WHERE id = ?", (str(payload["id"]),))
    elif kind == "tombstone_put":
        dead = TombstonedMemory.model_validate(payload["tombstone"])
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
        )
    elif kind == "tombstone_delete":
        conn.execute("DELETE FROM tombstones WHERE id = ?", (str(payload["id"]),))
    elif kind == "episode_put":
        _write_episode_row(conn, Episode.model_validate(payload["episode"]))
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
    else:
        raise ValueError(f"unknown mutation kind {kind!r}")


def _table_rows(
    conn: sqlite3.Connection, table: str
) -> dict[tuple[Any, ...], tuple[Any, ...]]:
    """Every row of a folded table keyed by its key columns, rowid left
    out: the fold compares records, and rowids are the tie order the
    candidate query is graded on elsewhere."""
    columns = [
        str(r[1])
        for r in conn.execute(f"PRAGMA table_info({table})")
        if r[1] != "rowid"
    ]
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


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class SqliteStore:
    """One store, one file, one connection. Construct through ``create``,
    ``open`` or ``open_or_create``."""

    def __init__(
        self,
        path: Path,
        conn: sqlite3.Connection,
        keyring: KeyRing,
        key: bytes | None,
        store_id: str,
    ) -> None:
        self._path = path
        self._conn = conn
        self._keyring = keyring
        self._key = key
        self._store_id = store_id
        self._last_appended: LogRow | None = None
        self._unaccounted_memory_ids: set[str] = set()

    # -- construction ------------------------------------------------------

    @classmethod
    def create(
        cls, path: Path | str, *, keys_dir: Path | str | None = None
    ) -> SqliteStore:
        """A new store at ``path`` with a fresh key. Refuses an existing file."""
        path = Path(path)
        if path.exists():
            raise FileExistsError(f"a store already exists at {path}")
        ensure_owner_only_dir(path.parent, parents=True)
        conn = _connect(path)
        try:
            conn.executescript(SCHEMA)
            store_id = secrets.token_hex(16)
            keyring = KeyRing(
                Path(keys_dir) if keys_dir is not None else _chain.default_keys_dir(),
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
            }
            store = cls(path, conn, keyring, key, store_id)
            with store._transaction() as tx:
                tx.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)", list(meta.items())
                )
                store._append(
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
        allow_rekey: bool = True,
    ) -> SqliteStore:
        """An existing store. With ``allow_rekey`` (the default) a missing
        or foreign key is replaced and a ``rekey`` row appended; without
        it the store opens for reading and refuses to mutate."""
        path = Path(path)
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
            schema = int(meta.get("schema_version", "0"))
            if schema > SCHEMA_VERSION:
                raise ValueError(
                    f"{path} is schema {schema}; this build reads up to {SCHEMA_VERSION}"
                )
            keyring = KeyRing(
                Path(keys_dir) if keys_dir is not None else _chain.default_keys_dir(),
                store_id,
            )
            key = keyring.load_current()
            if key is not None and fingerprint(key) == expected:
                return cls(path, conn, keyring, key, store_id)
            if not allow_rekey:
                return cls(path, conn, keyring, None, store_id)
            if key is not None:
                keyring.retire_current()
            key = keyring.create_key()
            store = cls(path, conn, keyring, key, store_id)
            with store._transaction() as tx:
                tx.execute(
                    "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
                    (fingerprint(key),),
                )
                store._append(
                    tx,
                    REKEY,
                    {"old_fingerprint": expected, "new_fingerprint": fingerprint(key)},
                    session=None,
                )
            log.warning(
                "store %s was opened without its key (fingerprint %s); a new key "
                "%s was written and the log rekeyed",
                path,
                expected[:16],
                fingerprint(key)[:16],
            )
            return store
        except Exception:
            conn.close()
            raise

    @classmethod
    def open_or_create(
        cls, path: Path | str, *, keys_dir: Path | str | None = None
    ) -> SqliteStore:
        path = Path(path)
        if path.is_file():
            return cls.open(path, keys_dir=keys_dir)
        return cls.create(path, keys_dir=keys_dir)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteStore:
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

    def meta(self) -> dict[str, str]:
        return {
            str(r["key"]): str(r["value"])
            for r in self._conn.execute("SELECT key, value FROM meta ORDER BY key")
        }

    # -- transactions -------------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        self._last_appended = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
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
    ) -> LogRow:
        if self._key is None:
            raise RuntimeError(
                f"store {self._path} has no key (opened with allow_rekey=False)"
            )
        row = append_row(
            conn, key=self._key, kind=kind, payload=payload, session=session
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
    ) -> None:
        """Apply a mutation and log it, inside the caller's transaction."""
        _apply_mutation(conn, kind, payload)
        self._append(conn, kind, payload, session=session)

    # -- memories -----------------------------------------------------------

    def put_memory(
        self,
        memory: Memory,
        *,
        provenance: str | None = None,
        filename: str | None = None,
        session: str | None = None,
    ) -> Memory:
        """Insert or replace the record. ``provenance`` None keeps the row's
        label (``local`` for a new row); ``filename`` None keeps the row's
        name. Both are resolved before the row is logged, so the log
        carries what the table holds."""
        if provenance is not None and provenance not in _WRITABLE_PROVENANCE:
            raise ValueError(
                f"provenance must be one of {sorted(_WRITABLE_PROVENANCE)}, got {provenance!r}"
            )
        existing = self._conn.execute(
            "SELECT provenance, filename FROM memories WHERE id = ?", (memory.id,)
        ).fetchone()
        if existing is not None:
            provenance = provenance or str(existing["provenance"])
            filename = filename if filename is not None else existing["filename"]
        payload = {
            "memory": memory.model_dump(mode="json"),
            "provenance": provenance or LOCAL,
            "filename": filename,
        }
        with self._transaction() as tx:
            self._mutate(tx, "memory_put", payload, session=session)
        return memory

    def get_memory(self, memory_id: str) -> Memory:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(memory_id)
        return _row_to_memory(row)

    def has_memory(self, memory_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            is not None
        )

    def get_many(self, memory_ids: Sequence[str]) -> list[Memory]:
        """The records found, in the order asked; unknown ids are skipped."""
        out: list[Memory] = []
        for memory_id in memory_ids:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is not None:
                out.append(_row_to_memory(row))
        return out

    def iter_memories(self) -> Iterator[Memory]:
        for row in self._conn.execute("SELECT * FROM memories ORDER BY rowid"):
            yield _row_to_memory(row)

    def memory_ids(self) -> list[str]:
        return [
            str(r["id"])
            for r in self._conn.execute("SELECT id FROM memories ORDER BY rowid")
        ]

    def count_memories(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def filename_for(self, memory_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT filename FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(memory_id)
        return None if row["filename"] is None else str(row["filename"])

    # -- tombstones ---------------------------------------------------------

    def tombstone(
        self,
        memory_id: str,
        reason: str,
        *,
        session: str | None = None,
        now: datetime | None = None,
    ) -> TombstonedMemory:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
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
        with self._transaction() as tx:
            self._mutate(tx, "memory_delete", {"id": memory_id}, session=session)
            self._mutate(tx, "tombstone_put", payload, session=session)
        return dead

    def get_tombstone(self, memory_id: str) -> TombstonedMemory:
        row = self._conn.execute(
            "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(memory_id)
        return _row_to_tombstone(row)

    def iter_tombstones(self) -> Iterator[TombstonedMemory]:
        for row in self._conn.execute(
            "SELECT * FROM tombstones ORDER BY removed, rowid"
        ):
            yield _row_to_tombstone(row)

    def count_tombstones(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0])

    def restore(self, memory_id: str, *, session: str | None = None) -> Memory:
        """Bring a tombstoned record back as an active one, labelled
        ``local``: this store's own code path put it back."""
        row = self._conn.execute(
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
        payload = {
            "memory": memory.model_dump(mode="json"),
            "provenance": LOCAL,
            "filename": row["filename"],
        }
        with self._transaction() as tx:
            self._mutate(tx, "tombstone_delete", {"id": memory_id}, session=session)
            self._mutate(tx, "memory_put", payload, session=session)
        return memory

    def delete_tombstone(self, memory_id: str, *, session: str | None = None) -> None:
        if (
            self._conn.execute(
                "SELECT 1 FROM tombstones WHERE id = ?", (memory_id,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(memory_id)
        with self._transaction() as tx:
            self._mutate(tx, "tombstone_delete", {"id": memory_id}, session=session)

    # -- episodes -----------------------------------------------------------

    def put_episode(self, episode: Episode, *, session: str | None = None) -> Episode:
        payload = {"episode": episode.model_dump(mode="json")}
        with self._transaction() as tx:
            self._mutate(tx, "episode_put", payload, session=session)
        return episode

    def get_episode(self, episode_id: str) -> Episode:
        row = self._conn.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(episode_id)
        return _row_to_episode(row)

    def iter_episodes(self, *, session_id: str | None = None) -> Iterator[Episode]:
        if session_id is None:
            cursor = self._conn.execute("SELECT * FROM episodes ORDER BY rowid")
        else:
            cursor = self._conn.execute(
                "SELECT * FROM episodes WHERE session_id = ? ORDER BY rowid",
                (session_id,),
            )
        for row in cursor:
            yield _row_to_episode(row)

    def count_episodes(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])

    def delete_episode(self, episode_id: str, *, session: str | None = None) -> None:
        if (
            self._conn.execute(
                "SELECT 1 FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(episode_id)
        with self._transaction() as tx:
            self._mutate(tx, "episode_delete", {"id": episode_id}, session=session)

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
        found = self._conn.execute(
            "SELECT 1 FROM verifications WHERE memory_id = ? "
            "AND COALESCE(machine_id, '') = COALESCE(?, '')",
            (memory_id, machine_id),
        ).fetchone()
        if found is None:
            raise NotFoundError((memory_id, machine_id))
        with self._transaction() as tx:
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
        if (
            self._conn.execute(
                "SELECT 1 FROM conflicts WHERE id = ?", (conflict_id,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(conflict_id)
        with self._transaction() as tx:
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
        if (
            self._conn.execute(
                "SELECT 1 FROM imports WHERE source = ?", (source,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(source)
        with self._transaction() as tx:
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
        if (
            self._conn.execute(
                "SELECT 1 FROM quarantine WHERE name = ?", (name,)
            ).fetchone()
            is None
        ):
            raise NotFoundError(name)
        with self._transaction() as tx:
            self._mutate(tx, "quarantine_delete", {"name": name}, session=session)

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
        if kind in MUTATION_KINDS or kind in CONTROL_KINDS:
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

    def provenance_for(self, memory_ids: Sequence[str]) -> dict[str, str]:
        """``{id: label}`` for the active records asked. A record the last
        ``refold`` or ``log_verify`` in this process found unaccounted for
        reads ``unaccounted`` whatever its row says; a caller that needs a
        fresh verdict runs ``refold`` first, since the replay is the one
        cost this read does not pay."""
        ids = list(dict.fromkeys(memory_ids))
        if not ids:
            return {}
        out: dict[str, str] = {}
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                f"SELECT id, provenance FROM memories WHERE id IN ({placeholders})",
                batch,
            ):
                out[str(row["id"])] = str(row["provenance"])
        for memory_id in out:
            if memory_id in self._unaccounted_memory_ids:
                out[memory_id] = UNACCOUNTED
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
        skipped."""
        scratch = sqlite3.connect(":memory:", isolation_level=None)
        scratch.row_factory = sqlite3.Row
        problems: list[dict[str, Any]] = []
        try:
            scratch.executescript(SCHEMA)
            scratch.execute("BEGIN")
            for row in iter_rows(self._conn):
                if row.kind not in MUTATION_KINDS:
                    continue
                try:
                    payload = json.loads(row.payload)
                    if not isinstance(payload, dict):
                        raise TypeError("payload is not an object")
                    _apply_mutation(scratch, row.kind, payload)
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
            diverged = bool(problems)
            for table in FOLDED_TABLES:
                live = _table_rows(self._conn, table)
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
                tables[table] = {
                    "unaccounted": unaccounted,
                    "missing": missing,
                    "rows": len(live),
                }
            self._unaccounted_memory_ids = set(tables["memories"]["unaccounted"])
            return {
                "status": "diverged" if diverged else "ok",
                "tables": tables,
                "problems": problems,
            }
        finally:
            scratch.close()

    def log_verify(self) -> dict[str, Any]:
        """The chain report merged with the fold: ``tampered`` when a row
        fails its MAC, the chain breaks, the head names a row the log no
        longer has, or a table differs from the fold; ``unverifiable``
        when a segment's key or the head is absent; ``ok`` otherwise."""
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
            "problems": problems,
            "fold": {"status": fold["status"], "tables": fold["tables"]},
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


__all__ = [
    "FOLDED_TABLES",
    "IMPORTED",
    "LOCAL",
    "MUTATION_KINDS",
    "PROVENANCE_LABELS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "STORE_FILENAME",
    "SYNCED",
    "UNACCOUNTED",
    "NotFoundError",
    "SqliteStore",
]
