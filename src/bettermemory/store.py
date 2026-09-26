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
import re
import hmac
import json
import logging
import os
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from pydantic import ValidationError

from ._fsutil import ensure_owner_only_dir
from .events import _redact_event_fields
from .identity import Actor
from . import log as _chain
from .config import KEYS_DIR_ENV, STORE_FILENAME
from .log import (
    CONTROL_KINDS,
    MIGRATE_V8,
    REKEY,
    STORE_CREATED,
    KeyRing,
    LogRow,
    append_row,
    canonical_payload,
    compute_mac,
    fingerprint,
    iter_rows,
    verify_chain,
)
from .models import (
    Category,
    Confidence,
    Episode,
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
from .search import fts_index_text, fts_match_query, tokenizer_fingerprint
from .time_utils import isoformat_utc

log = logging.getLogger("bettermemory.store")

# `STORE_FILENAME` is defined in `config` so the standard-library-only
# daemon client can name the file without importing this module.
SCHEMA_VERSION = 1

# Set on a store's first open, before its first table and before WAL mode
# fixes it. A record row (body, its token stream, the JSON columns) runs
# past the largest payload a 4 KB page keeps in line, so at 4 KB most rows
# spill into an overflow page of their own; 8 KB keeps them in line and
# measured 16 percent smaller on the rank-parity corpus at the same write
# cost, where 16 KB saved nothing more and cost a third on every write
# (bench/parity/results/sqlite-candidates-8.0.0-2026-09-26.json records
# both sizes beside the parity).
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

# What an imported row's payload says about where it came from. The v8
# migration is the one importer today; a later `import` names itself.
IMPORTED_FROM_V8 = "v8"


class TrustRow(NamedTuple):
    """One record's trust facts the record itself cannot supply: how it
    entered the store (the verified provenance label) and when this host
    last verified it (None: never, or not since it arrived from
    elsewhere)."""

    provenance: str
    verified_locally_at: str | None


@dataclass(frozen=True)
class EpisodeVolume:
    """How big the journal is: the growth gauge the health report
    carries. ``prunable_sessions`` counts the sessions whose newest
    episode is past the TTL, which the next ``prune_episode_sessions``
    takes."""

    sessions: int
    episodes: int
    bytes: int
    prunable_sessions: int
    ttl_days: int

    def to_dict(self) -> dict[str, int]:
        return {
            "sessions": self.sessions,
            "episodes": self.episodes,
            "bytes": self.bytes,
            "prunable_sessions": self.prunable_sessions,
            "ttl_days": self.ttl_days,
        }


# How long a journal session lives before the write path prunes it.
DEFAULT_EPISODE_TTL_DAYS = 30


def _scopes_after_rename(scopes: list[str], old: str, new: str) -> list[str] | None:
    """The scope list with `old` replaced by `new` and duplicates collapsed,
    or None when `old` is not present."""
    if old not in scopes:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for scope in scopes:
        name = new if scope == old else scope
        if name in seen:
            continue
        out.append(name)
        seen.add(name)
    return out


@dataclass(frozen=True)
class MemoryRow:
    """An active record with the two columns the record itself does not
    carry: how it entered the store and the v8 filename it keeps."""

    memory: Memory
    provenance: str
    filename: str | None


@dataclass(frozen=True)
class TombstoneRow:
    """A tombstone with what the record carried while active and the
    tombstone model drops: its links, its corroboration rollup, and the
    active filename the mirror derives the tombstone's name from."""

    tombstone: TombstonedMemory
    provenance: str
    filename: str | None
    links: list[dict[str, Any]]
    corroborations: int
    last_corroborated: datetime | None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# `memories` opens with the v8 index's columns in the v8 order, minus
# `verified_locally_at` (this host's stamps are `verifications` rows
# now) and `content_sha256` (the log covers integrity), then carries the
# rest of the record and, last, `log_mac`: the MAC of the log row that
# produced the row, which `provenance_for` verifies on every read so a
# row that entered outside the log reads `unaccounted` without a refold.
# Tombstones and episodes carry the same column. The FTS5 table, its three triggers, the `updated`
# index, `memory_links` and its cleanup trigger are the v8 index's
# statements verbatim; tests/test_store.py compares the text.

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
    last_corroborated TEXT,
    log_mac TEXT
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
    removed_session TEXT,
    log_mac TEXT
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
    swarm_id TEXT,
    log_mac TEXT
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
CREATE INDEX IF NOT EXISTS log_by_ts ON log(ts);
CREATE INDEX IF NOT EXISTS log_by_mac ON log(mac);

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


class MemoryNotFoundError(KeyError):
    """No active memory with that id."""


class TombstonedError(KeyError):
    """The id exists but the memory is tombstoned."""


class NotTombstonedError(KeyError):
    """`restore` was called on an active memory, not a tombstone."""


class ConcurrentUpdateError(Exception):
    """`Store.update` saw a different `updated` in the store than the
    caller's snapshot. The caller's edit was built on a now-stale read
    and was refused rather than clobbering whoever bumped the record in
    between; `current_updated` is the stored stamp at the moment the
    check failed. Not a KeyError: the record is still findable, the
    failure is a stale snapshot."""

    def __init__(self, memory_id: str, current_updated: datetime) -> None:
        self.memory_id = memory_id
        self.current_updated = current_updated
        super().__init__(
            f"memory {memory_id} was updated concurrently "
            f"(your snapshot is stale; stored updated={current_updated.isoformat()})"
        )


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
    those rows as ``payload_invalid``."""
    if kind == "memory_put":
        memory = Memory.model_validate(payload["memory"])
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
            log_mac=log_mac,
        )
    elif kind == "tombstone_delete":
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


_ROW_COLUMNS = ("ts", "session", "kind")


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
    if kind in MUTATION_KINDS or kind in CONTROL_KINDS:
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


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def resolve_keys_dir() -> Path:
    """The keys directory a store opens with when the caller names none:
    ``BETTERMEMORY_KEYS_DIR`` when set, else the user's config directory
    (`log.default_keys_dir`)."""
    override = os.environ.get(KEYS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return _chain.default_keys_dir()


def store_path(path: Path | str) -> Path:
    """The store file for a path that names either the file or the
    directory it lives in: the file is always ``memory.sqlite``, so a path
    with any other name is the directory."""
    resolved = Path(path).expanduser()
    if resolved.name == STORE_FILENAME:
        return resolved
    return resolved / STORE_FILENAME


class UnmigratedV8DirectoryError(RuntimeError):
    """A directory holds bettermemory 8 files and no store file."""


#: A v8 memory file: `<date>-<slug>-<ULID>.md` (`v8.py`).
_V8_MEMORY_NAME = re.compile(r".*-[0-9A-HJKMNP-TV-Z]{26}\.md$")


def holds_v8_files(directory: Path) -> bool:
    """Whether `directory` carries a bettermemory 8 store: a memory
    file, the event log or a shard of it, or the tombstone directory."""
    try:
        children = list(directory.iterdir())
    except OSError:
        return False
    for child in children:
        name = child.name
        if name == ".events.jsonl" or name == ".tombstones":
            return True
        if name.startswith(".events.") and name.endswith(".jsonl"):
            return True
        if name.endswith(".md") and _V8_MEMORY_NAME.match(name):
            return True
    return False


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
    _store_id: str
    _last_appended: LogRow | None
    _in_batch: bool
    _unaccounted_memory_ids: set[str]
    # (id, log_mac) pairs whose pointer into the log verified; a row gets
    # a new MAC on every mutation, so a stale entry never matches.
    _pointer_cache: dict[tuple[str, str], bool]
    # The chain's segments as (first seq, key fingerprint), read from the
    # rekey rows once; the keys themselves by fingerprint.
    _segments: list[tuple[int, str]] | None
    _keys_by_fingerprint: dict[str, bytes | None]

    def __init__(self, path: Path | str, *, keys_dir: Path | str | None = None) -> None:
        opened = self.open_or_create(path, keys_dir=keys_dir)
        self._adopt(
            opened._path, opened._conn, opened._keyring, opened._key, opened._store_id
        )

    def _adopt(
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
        self._last_appended = None
        self._in_batch = False
        self._unaccounted_memory_ids = set()
        self._pointer_cache = {}
        self._segments = None
        self._keys_by_fingerprint = {}

    @classmethod
    def _bind(
        cls,
        path: Path,
        conn: sqlite3.Connection,
        keyring: KeyRing,
        key: bytes | None,
        store_id: str,
    ) -> Store:
        self = cls.__new__(cls)
        self._adopt(path, conn, keyring, key, store_id)
        return self

    @classmethod
    def create(cls, path: Path | str, *, keys_dir: Path | str | None = None) -> Store:
        """A new store at ``path`` with a fresh key. Refuses an existing file."""
        path = store_path(path)
        if path.exists():
            raise FileExistsError(f"a store already exists at {path}")
        ensure_owner_only_dir(path.parent, parents=True)
        conn = _connect(path)
        try:
            conn.executescript(SCHEMA)
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
            }
            store = cls._bind(path, conn, keyring, key, store_id)
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
    ) -> Store:
        """An existing store. With ``allow_rekey`` (the default) a missing
        or foreign key is replaced and a ``rekey`` row appended; without
        it the store opens for reading and refuses to mutate."""
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
            schema = int(meta.get("schema_version", "0"))
            if schema > SCHEMA_VERSION:
                raise ValueError(
                    f"{path} is schema {schema}; this build reads up to {SCHEMA_VERSION}"
                )
            keyring = KeyRing(
                Path(keys_dir) if keys_dir is not None else resolve_keys_dir(),
                store_id,
            )
            key = keyring.load_current()
            if key is not None and fingerprint(key) == expected:
                return cls._bind(path, conn, keyring, key, store_id)
            if not allow_rekey:
                return cls._bind(path, conn, keyring, None, store_id)
            if key is not None:
                keyring.retire_current()
            key = keyring.create_key()
            store = cls._bind(path, conn, keyring, key, store_id)
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
    ) -> Store:
        """Open the store at `path`, or create one. Refuses to create one
        in a directory that holds an un-migrated bettermemory 8 store: a
        server pointed at such a directory would otherwise serve an empty
        store and say nothing, and `bettermemory migrate v8` is the
        answer. `create` itself is unchanged."""
        path = store_path(path)
        if path.is_file():
            return cls.open(path, keys_dir=keys_dir)
        if path.parent.is_dir() and holds_v8_files(path.parent):
            raise UnmigratedV8DirectoryError(
                f"{path.parent} holds a bettermemory 8 store and no "
                f"{STORE_FILENAME}; run `bettermemory migrate v8` to import it "
                "(the directory is never written)"
            )
        return cls.create(path, keys_dir=keys_dir)

    def close(self) -> None:
        self._conn.close()

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

    def meta(self) -> dict[str, str]:
        return {
            str(r["key"]): str(r["value"])
            for r in self._conn.execute("SELECT key, value FROM meta ORDER BY key")
        }

    # -- transactions -------------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One mutating method's transaction. Inside a ``batch`` the
        batch owns the transaction and the head write, and this yields
        the connection as it is."""
        conn = self._conn
        if self._in_batch:
            yield conn
            return
        self._last_appended = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        self._move_head()

    @contextlib.contextmanager
    def batch(self) -> Iterator[Store]:
        """One transaction around many mutations: everything put or
        deleted inside the block commits together or not at all, and the
        head checkpoint moves once, at the commit. A migration runs in
        one, so a failure part way leaves the store as it was. Batches
        do not nest."""
        if self._in_batch:
            raise RuntimeError(f"a batch is already open on store {self._path}")
        conn = self._conn
        self._last_appended = None
        self._in_batch = True
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            self._in_batch = False
        conn.execute("COMMIT")
        self._move_head()

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
            raise RuntimeError(
                f"store {self._path} has no key (opened with allow_rekey=False)"
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
    ) -> None:
        """Log a mutation and apply it, inside the caller's transaction.
        The row is appended first so the record it puts can carry the
        row's MAC."""
        row = self._append(conn, kind, payload, session=session)
        _apply_mutation(conn, kind, payload, log_mac=row.mac)

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

    def iter_memory_rows(self) -> Iterator[MemoryRow]:
        """Every active record with its provenance and filename, in
        rowid order: what the mirror writes."""
        for row in self._conn.execute("SELECT * FROM memories ORDER BY rowid"):
            yield MemoryRow(
                memory=_row_to_memory(row),
                provenance=str(row["provenance"]),
                filename=None if row["filename"] is None else str(row["filename"]),
            )

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

    def put_tombstone(
        self,
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
        if self.has_memory(dead.id):
            raise ValueError(f"{dead.id} is active; tombstone it instead")
        existing = self._conn.execute(
            "SELECT provenance, filename FROM tombstones WHERE id = ?", (dead.id,)
        ).fetchone()
        if existing is not None:
            provenance = provenance or str(existing["provenance"])
            filename = filename if filename is not None else existing["filename"]
        payload = {
            "tombstone": dead.model_dump(mode="json"),
            "provenance": provenance or LOCAL,
            "filename": filename,
            "links": [
                MemoryLink.model_validate(link).model_dump(
                    mode="json", exclude_none=True
                )
                for link in links
            ],
            "corroborations": int(corroborations),
            "last_corroborated": _iso_opt(last_corroborated),
        }
        with self._transaction() as tx:
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

    def iter_tombstone_rows(self) -> Iterator[TombstoneRow]:
        """Every tombstone with what its row keeps beside the record, in
        removal order: what the mirror writes."""
        for row in self._conn.execute(
            "SELECT * FROM tombstones ORDER BY removed, rowid"
        ):
            yield TombstoneRow(
                tombstone=_row_to_tombstone(row),
                provenance=str(row["provenance"]),
                filename=None if row["filename"] is None else str(row["filename"]),
                links=list(_load_json(row["links_json"], [])),
                corroborations=int(row["corroborations"]),
                last_corroborated=_dt_opt(row["last_corroborated"]),
            )

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
            if kind in MUTATION_KINDS or kind in CONTROL_KINDS:
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
            if row.kind in MUTATION_KINDS or row.kind in CONTROL_KINDS:
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
        """``{id: label}`` for the active records asked. The label is the
        row's, unless the row's pointer into the log fails: the ``log_mac``
        names no row, names a row that put a different record, or names a
        row whose MAC does not verify under the segment's key. Such a row
        reads ``unaccounted``, as does one the last ``refold`` in this
        process found so. A store opened without its key cannot verify
        pointers and returns the row labels as stored; ``trust_rows`` says
        when that is the case."""
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

    def _segment_key(self, seq: int) -> bytes | None:
        """The key that signed the row at ``seq``: the segment's, read
        off the rekey rows. None when that key is not on this machine."""
        if self._segments is None:
            segments: list[tuple[int, str]] = []
            first = self.key_fingerprint
            for row in self._conn.execute(
                "SELECT seq, payload FROM log WHERE kind = ? ORDER BY seq", (REKEY,)
            ):
                try:
                    names = json.loads(row["payload"])
                    old, new = (
                        str(names["old_fingerprint"]),
                        str(names["new_fingerprint"]),
                    )
                except (ValueError, KeyError, TypeError):
                    continue
                if not segments:
                    first = old
                segments.append((int(row["seq"]), new))
            self._segments = [(1, first)] + segments
        segments_known = self._segments
        fingerprint_for_seq = segments_known[0][1]
        for first_seq, name in segments_known:
            if seq >= first_seq:
                fingerprint_for_seq = name
        if fingerprint_for_seq not in self._keys_by_fingerprint:
            key = self._key if fingerprint_for_seq == self.key_fingerprint else None
            if key is None:
                key = self._keyring.key_for_fingerprint(fingerprint_for_seq)
            self._keys_by_fingerprint[fingerprint_for_seq] = key
        return self._keys_by_fingerprint[fingerprint_for_seq]

    def _pointer_verified(self, record_id: str, kind: str, log_mac: Any) -> bool | None:
        """Does the record's ``log_mac`` name a log row of ``kind`` that
        put this very record, and does that row's MAC verify under its
        segment's key? None when the key is not on this machine; the
        other two answers are cached per (id, mac)."""
        if self._key is None:
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
            if named == record_id:
                key = self._segment_key(int(raw["seq"]))
                if key is None:
                    return None
                expected = compute_mac(
                    key,
                    seq=int(raw["seq"]),
                    ts=str(raw["ts"]),
                    session=None if raw["session"] is None else str(raw["session"]),
                    kind=str(raw["kind"]),
                    payload=str(raw["payload"]),
                    prev_mac=str(raw["prev_mac"]),
                )
                verdict = hmac.compare_digest(expected, log_mac)
        self._pointer_cache[cache_key] = verdict
        return verdict

    def trust_rows(self, memory_ids: Sequence[str]) -> dict[str, TrustRow] | None:
        """``{id: TrustRow}`` for the active records asked: the provenance
        verdict ``provenance_for`` gives and this host's verification stamp
        (the ``verifications`` row with no machine id), or None when the
        store was opened without its key, so no pointer can be verified and
        the response must say trust is unavailable."""
        if self._key is None:
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

    # -- the record surface the runtime uses -----------------------------------
    #
    # The v8 store's names, kept where the semantics are unchanged, so the
    # handlers, the CLI and the benches port without a rename of their own.

    def write(
        self,
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
    ) -> Memory:
        """Create a new memory: mints the id, stamps created and updated,
        stores the body stripped with one trailing newline. `claims` are
        stored verbatim; parsing and the declare-time check are the write
        handler's job, since they need the origin worktree."""
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
                entry
                if isinstance(entry, MemoryLink)
                else MemoryLink.model_validate(entry)
                for entry in (links or [])
            ],
        )
        return self.put_memory(memory, provenance=LOCAL, session=session)

    def update(
        self,
        memory: Memory,
        *,
        force: bool = False,
        preserve_verification: bool = False,
        session: str | None = None,
    ) -> Memory:
        """Replace an active record with the caller's edit. The caller's
        `memory.updated` is the snapshot it read; unless `force`, a stored
        `updated` that differs raises ConcurrentUpdateError so the caller
        re-reads and retries. `preserve_verification` keeps the stored
        verification fields over the snapshot's (a metadata edit must not
        undo a verify that landed in between); a content edit leaves it
        False and resets them on purpose. The corroboration rollup is
        store-owned and always carried over."""
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
        return self.put_memory(new_memory, session=session)

    def mark_verified(
        self,
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
        row in one transaction. With `check_expected`, a stored stamp or
        `updated` that differs from the caller's snapshot raises
        ConcurrentUpdateError."""
        if verified_head is not None:
            verified_head = verified_head.strip().lower()
            if not is_full_commit_sha(verified_head):
                raise ValueError(
                    "verified_head must be a full commit hash (40 or 64 hex "
                    f"characters), got {verified_head!r}"
                )
        existing = self._active_or_raise(memory_id)
        if check_expected and (
            existing.last_verified_at != expected_last_verified_at
            or (expected_updated is not None and existing.updated != expected_updated)
        ):
            raise ConcurrentUpdateError(memory_id, existing.updated)
        verified_now = utcnow()
        update: dict[str, object] = {"last_verified_at": verified_now}
        if verified_paths is not None:
            update["verified_paths"] = list(verified_paths)
        if verified_commits is not None:
            update["verified_commits"] = list(verified_commits)
        if verified_versions is not None:
            update["verified_versions"] = list(verified_versions)
        if verified_absent_paths is not None:
            update["verified_absent_paths"] = list(verified_absent_paths)
        if claims is not None:
            update["claims"] = list(claims)
        update["verified_head"] = verified_head
        new_memory = existing.model_copy(update=update)
        Memory.model_validate(new_memory.model_dump())
        row = self._conn.execute(
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
        with self._transaction() as tx:
            self._mutate(tx, "memory_put", payload, session=session)
            self._mutate(tx, "verification_put", stamp, session=session)
        return new_memory

    def record_corroboration(
        self, memory_id: str, *, session: str | None = None
    ) -> Memory:
        """Bump the corroboration rollup without touching `updated`: a
        dedup-rejected write is the stored claim recurring, evidence it
        still holds, not a rewrite."""
        existing = self._active_or_raise(memory_id)
        bumped = existing.model_copy(
            update={
                "corroborations": existing.corroborations + 1,
                "last_corroborated": utcnow(),
            }
        )
        return self.put_memory(bumped, session=session)

    def _active_or_raise(self, memory_id: str) -> Memory:
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

    def load_one(self, memory_id: str) -> Memory:
        """One active memory by id; raises if missing or tombstoned."""
        return self._active_or_raise(memory_id)

    def show(self, memory_id: str) -> Memory:
        return self._active_or_raise(memory_id)

    def load_many(self, memory_ids: list[str]) -> list[Memory]:
        """The active records among `memory_ids`, in the order asked;
        unknown and tombstoned ids are skipped."""
        return self.get_many(memory_ids)

    def load_all(self) -> list[Memory]:
        """Every active memory in row order, which is insertion order."""
        return list(self.iter_memories())

    def iter_active(self) -> Iterator[Memory]:
        return self.iter_memories()

    def list_summaries(self, scopes: list[str] | None = None) -> list[MemorySummary]:
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

    def load_tombstones(self) -> list[TombstonedMemory]:
        """Every tombstone, most recently removed first."""
        return [
            _row_to_tombstone(row)
            for row in self._conn.execute(
                "SELECT * FROM tombstones ORDER BY removed DESC, rowid DESC"
            )
        ]

    def list_tombstones(
        self, scopes: list[str] | None = None
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

    def load_tombstone(self, memory_id: str) -> TombstonedMemory:
        if not is_valid_ulid(memory_id):
            raise MemoryNotFoundError(f"invalid id: {memory_id!r}")
        row = self._conn.execute(
            "SELECT * FROM tombstones WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError(f"no tombstone with id {memory_id}")
        return _row_to_tombstone(row)

    def restore_trimmed(
        self,
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
        NotTombstonedError for an active id."""
        if not is_valid_ulid(memory_id):
            raise MemoryNotFoundError(f"invalid id: {memory_id!r}")
        if self.has_memory(memory_id):
            raise NotTombstonedError(
                f"memory {memory_id} is active; nothing to restore"
            )
        row = self._conn.execute(
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
        payload = {
            "memory": memory.model_dump(mode="json"),
            "provenance": LOCAL,
            "filename": row["filename"],
        }
        with self._transaction() as tx:
            self._mutate(tx, "tombstone_delete", {"id": memory_id}, session=session)
            self._mutate(tx, "memory_put", payload, session=session)
        return memory

    def rename_scope(
        self,
        old: str,
        new: str,
        *,
        include_tombstones: bool = True,
        session: str | None = None,
    ) -> dict[str, list[Any]]:
        """Replace scope `old` with `new` on every record carrying it, the
        list deduplicated in place; active records get a fresh `updated`.
        Returns the ids changed under `active` and `tombstoned`; `failed`
        is present only when a record could not be rewritten."""
        result: dict[str, list[Any]] = {"active": [], "tombstoned": []}
        if old == new:
            return result
        failed: list[dict[str, str]] = []
        with self.batch():
            for memory in self.load_all():
                renamed = _scopes_after_rename(memory.scopes, old, new)
                if renamed is None:
                    continue
                refreshed = memory.model_copy(
                    update={"scopes": renamed, "updated": utcnow()}
                )
                try:
                    self.put_memory(refreshed, session=session)
                except (ValueError, ValidationError) as exc:
                    failed.append({"id": memory.id, "reason": str(exc)})
                    continue
                result["active"].append(memory.id)
            if include_tombstones:
                for tomb_row in list(self.iter_tombstone_rows()):
                    dead = tomb_row.tombstone
                    renamed = _scopes_after_rename(dead.scopes, old, new)
                    if renamed is None:
                        continue
                    payload = {
                        "tombstone": dead.model_copy(
                            update={"scopes": renamed}
                        ).model_dump(mode="json"),
                        "provenance": tomb_row.provenance,
                        "filename": tomb_row.filename,
                        "links": tomb_row.links,
                        "corroborations": tomb_row.corroborations,
                        "last_corroborated": _iso_opt(tomb_row.last_corroborated),
                    }
                    with self._transaction() as tx:
                        self._mutate(tx, "tombstone_put", payload, session=session)
                    result["tombstoned"].append(dead.id)
        if failed:
            result["failed"] = failed
        return result

    def prune_tombstones(
        self,
        older_than: timedelta,
        *,
        now: datetime | None = None,
        session: str | None = None,
    ) -> list[str]:
        """Delete tombstones removed longer than `older_than` ago. Returns
        the ids pruned, oldest removal first."""
        cutoff = (now or utcnow()) - older_than
        doomed = [
            (dead.removed, dead.id)
            for dead in self.iter_tombstones()
            if dead.removed < cutoff
        ]
        doomed.sort()
        with self.batch():
            for _, memory_id in doomed:
                with self._transaction() as tx:
                    self._mutate(
                        tx, "tombstone_delete", {"id": memory_id}, session=session
                    )
        return [memory_id for _, memory_id in doomed]

    # -- the reads the index used to serve ----------------------------------------

    def document_frequencies(
        self,
        terms: Sequence[str],
        *,
        admit: Callable[[list[str], Origin | None, Actor | None], bool],
    ) -> tuple[int, dict[str, int], dict[str, int]] | None:
        """Document frequencies for `terms` over the admitted collection,
        the v8 index's `corpus_document_frequencies`: `(size, body_df,
        scope_df)`, where `admit` is the search's own admission predicate
        over `(scopes, origin, actor)` so the denominator is the ranked
        set. None when no term or no admitted document."""
        unique = sorted({t for t in terms if t})
        if not unique:
            return None
        admitted: set[str] = set()
        for row in self._conn.execute(
            "SELECT id, scopes_json, origin_repo, origin_worktree, "
            "actor_client, actor_model FROM memories"
        ):
            repo = row["origin_repo"]
            worktree = row["origin_worktree"]
            origin = (
                Origin(repo=repo, worktree_root=worktree)
                if (repo is not None or worktree is not None)
                else None
            )
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
            if admit(list(scopes), origin, actor):
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

    def scope_counts(
        self, *, admit: Callable[[list[str], Origin | None], bool]
    ) -> tuple[int, dict[str, int]]:
        """How many admitted memories there are and how many carry each
        scope, without reading a body."""
        total = 0
        counts: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT scopes_json, origin_repo, origin_worktree FROM memories"
        ):
            repo = row["origin_repo"]
            worktree = row["origin_worktree"]
            origin = (
                Origin(repo=repo, worktree_root=worktree)
                if (repo is not None or worktree is not None)
                else None
            )
            scopes = _load_json(row["scopes_json"], None)
            if not isinstance(scopes, list):
                continue
            if not admit(list(scopes), origin):
                continue
            total += 1
            for scope in scopes:
                counts[scope] = counts.get(scope, 0) + 1
        return total, counts

    def category_rows(
        self,
        *,
        category: str,
        admit: Callable[[list[str], Origin | None], bool],
    ) -> list[str]:
        """The ids of admitted memories in `category`, in id order."""
        out: list[str] = []
        for row in self._conn.execute(
            "SELECT id, scopes_json, origin_repo, origin_worktree "
            "FROM memories WHERE category = ? ORDER BY id",
            (category,),
        ):
            repo = row["origin_repo"]
            worktree = row["origin_worktree"]
            origin = (
                Origin(repo=repo, worktree_root=worktree)
                if (repo is not None or worktree is not None)
                else None
            )
            scopes = _load_json(row["scopes_json"], None)
            if not isinstance(scopes, list):
                continue
            if admit(list(scopes), origin):
                out.append(str(row["id"]))
        return out

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

    def events_since(self, since: datetime) -> Iterator[dict[str, Any]]:
        """The telemetry rows stamped at or after `since`, in log order:
        what a window read wants. The `ts` index serves the cut."""
        cut = _iso(since.astimezone(timezone.utc)).replace("+00:00", "Z")
        # Both spellings sort correctly against an ISO instant to the
        # second; a `Z` sorts after `+`, so the cut compares on the prefix.
        prefix = cut[:19]
        for raw in self._conn.execute(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
            "WHERE substr(ts, 1, 19) >= ? ORDER BY seq",
            (prefix,),
        ):
            event = self._event_from_raw(raw)
            if event is not None:
                yield event

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
        kind = str(raw["kind"])
        if kind in MUTATION_KINDS or kind in CONTROL_KINDS:
            return None
        try:
            payload = json.loads(raw["payload"])
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        event: dict[str, Any] = {"ts": str(raw["ts"])}
        if raw["session"] is not None:
            event["session"] = str(raw["session"])
        event["kind"] = kind
        for key, value in payload.items():
            if key not in _ROW_COLUMNS:
                event[key] = value
        return event

    # -- the journal ------------------------------------------------------------

    def write_episode(
        self,
        *,
        session_id: str,
        body: str,
        scopes: list[str] | None = None,
        takeaway: str | None = None,
        swarm_id: str | None = None,
        origin: Origin | None = None,
        now: datetime | None = None,
    ) -> Episode:
        """Append one journal entry for `session_id`."""
        if not body or not body.strip():
            raise ValueError("episode body must be a non-empty string")
        episode = Episode(
            id=generate_ulid(),
            session_id=session_id,
            created=now or utcnow(),
            body=body.strip() + "\n",
            scopes=list(scopes or []),
            takeaway=takeaway.strip() if takeaway else None,
            swarm_id=swarm_id,
            origin=origin,
        )
        return self.put_episode(episode, session=session_id)

    def write_floor(
        self,
        *,
        session_id: str,
        origin: Origin | None = None,
        now: datetime | None = None,
    ) -> Episode:
        """The session-tag floor the handoff writes at its entry, so a
        session that crashes before its first takeaway still marks its
        worktree."""
        episode = Episode(
            id=generate_ulid(),
            session_id=session_id,
            created=now or utcnow(),
            body="(session-tag floor, no takeaway recorded)\n",
            scopes=[],
            takeaway=None,
            origin=origin,
            is_floor=True,
        )
        return self.put_episode(episode, session=session_id)

    def episodes_by_session(self, session_id: str) -> list[Episode]:
        out = list(self.iter_episodes(session_id=session_id))
        out.sort(key=lambda e: e.created)
        return out

    def episodes_by_swarm(self, swarm_id: str) -> list[Episode]:
        out = [
            _row_to_episode(row)
            for row in self._conn.execute(
                "SELECT * FROM episodes WHERE swarm_id = ? ORDER BY created, rowid",
                (swarm_id,),
            )
        ]
        return out

    def episode_session_ids(self) -> list[str]:
        """Every session with at least one episode, oldest first."""
        return [
            str(row["session_id"])
            for row in self._conn.execute(
                "SELECT session_id, MIN(rowid) AS first FROM episodes "
                "GROUP BY session_id ORDER BY first"
            )
        ]

    def prunable_episode_sessions(
        self,
        *,
        ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
        now: datetime | None = None,
    ) -> list[str]:
        """Sessions whose newest episode is older than the TTL."""
        if ttl_days <= 0:
            return []
        cutoff = _iso((now or utcnow()) - timedelta(days=ttl_days))
        return [
            str(row["session_id"])
            for row in self._conn.execute(
                "SELECT session_id, MAX(created) AS newest FROM episodes "
                "GROUP BY session_id HAVING newest < ? ORDER BY newest",
                (cutoff,),
            )
        ]

    def prune_episode_sessions(
        self,
        *,
        ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
        keep_session_id: str | None = None,
        now: datetime | None = None,
        session: str | None = None,
    ) -> list[str]:
        """Delete every episode of the sessions past the TTL, except the
        session named by `keep_session_id`. Returns the pruned session
        ids."""
        pruned = [
            sid
            for sid in self.prunable_episode_sessions(ttl_days=ttl_days, now=now)
            if sid != keep_session_id
        ]
        if not pruned:
            return []
        with self.batch():
            for sid in pruned:
                for episode in self.iter_episodes(session_id=sid):
                    with self._transaction() as tx:
                        self._mutate(
                            tx, "episode_delete", {"id": episode.id}, session=session
                        )
        return pruned

    def episode_provenance(self, episode_ids: Sequence[str]) -> dict[str, str]:
        """``{id: label}`` for the episodes asked: ``local`` when the row's
        pointer into the log verifies, ``unaccounted`` when it does not
        (the planted shape), the row's label as stored when the key is not
        on this machine. Unknown ids are omitted."""
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

    def episode_volume(
        self,
        *,
        ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
        now: datetime | None = None,
    ) -> EpisodeVolume:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS episodes, "
            "COALESCE(SUM(LENGTH(CAST(body AS BLOB)) + "
            "LENGTH(CAST(COALESCE(takeaway, '') AS BLOB))), 0) AS bytes "
            "FROM episodes"
        ).fetchone()
        return EpisodeVolume(
            sessions=int(row["sessions"]),
            episodes=int(row["episodes"]),
            bytes=int(row["bytes"]),
            prunable_sessions=len(
                self.prunable_episode_sessions(ttl_days=ttl_days, now=now)
            ),
            ttl_days=ttl_days,
        )

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
    "MUTATION_KINDS",
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
