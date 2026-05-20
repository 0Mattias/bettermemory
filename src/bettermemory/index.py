"""SQLite FTS5 inverted index over the memory store (T3.1 of the v1.6 plan).

The index is a *derived cache*. Files on disk remain canonical: markdown
+ YAML frontmatter, greppable, git-trackable, hand-editable. The index
exists to remove the linear-scan ceiling on ``Store.load_all`` — for
corpora over ~5K memories, walking every file on every ``memory_search``
call becomes the dominant cost.

Layout: one SQLite database per store root at
``<root>/.index.sqlite``. A ``memories`` table mirrors the on-disk
records; a ``memories_fts`` FTS5 virtual table provides the inverted
index. Three triggers keep them in sync so callers only have to talk
to the SQL table.

Lifecycle:

- The index is built on demand by ``rebuild(...)`` or by Store hooks
  that call ``upsert(...)`` / ``remove(...)`` on every successful
  write / update / tombstone. The first call to ``rebuild`` on a
  fresh database creates the schema; subsequent calls drop and
  refill the data tables but keep the schema.
- A small ``meta`` table records the schema version (so a future
  bump can detect mismatched indexes and force a rebuild) and the
  number of memories indexed (so callers can sanity-check against
  the on-disk count without a full scan).
- The CLI exposes ``bettermemory reindex`` as the explicit rebuild
  entrypoint. The hooks keep the index live; reindex is the recovery
  path for "I edited files outside the runtime".

Concurrency: SQLite's WAL mode plus a 5-second busy timeout (set in
``_connect``) lets multiple processes share the index — readers don't
block writers and vice versa, and contending writers retry rather
than fail. The index upsert is deliberately *outside* the per-memory
``fcntl.flock`` critical section in the Store — pulling SQLite I/O
inside a file lock would serialize one writer's index write against
every other writer's file write, with no consistency win because the
index is a derived cache. The trade-off: if two writers race on the
same memory id and one SQLite upsert fails past the busy timeout,
the index drifts from the canonical file. Hooks log a warning and
let the canonical write proceed (`_index_upsert_quietly` in
``store.py``); the recovery path is ``bettermemory reindex``, which
rebuilds the index from the on-disk truth. Files are canonical, the
index is regenerable.

This module is intentionally narrow: schema, lifecycle, and a thin
``query`` surface returning ranked memory IDs. The search ranker
fuses these results with the keyword / BM25 / semantic scorers from
``search.py`` — the index is the *candidate set*, not the final
ranking. That split keeps the existing rankers as the source of
truth for scoring semantics and lets the index be added or removed
without changing the result shape.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Memory

log = logging.getLogger("bettermemory.index")


# Bump when the schema changes. A reader that sees a version it
# doesn't support drops the file and forces a rebuild rather than
# risk misinterpreting the rows.
SCHEMA_VERSION = 1

INDEX_FILENAME = ".index.sqlite"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_SCHEMA = """
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
    scopes_text TEXT NOT NULL,
    scopes_json TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    body, scopes_text,
    content='memories', content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, body, scopes_text)
    VALUES (new.rowid, new.body, new.scopes_text);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body, scopes_text)
    VALUES ('delete', old.rowid, old.body, old.scopes_text);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body, scopes_text)
    VALUES ('delete', old.rowid, old.body, old.scopes_text);
    INSERT INTO memories_fts(rowid, body, scopes_text)
    VALUES (new.rowid, new.body, new.scopes_text);
END;

CREATE INDEX IF NOT EXISTS memories_by_updated ON memories(updated DESC);
"""


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


def index_path(root: Path) -> Path:
    """Resolve the index file path for a given store root. Adjacent to
    the active memories so the index shares the trust boundary —
    same directory, same file ownership."""
    return Path(root) / INDEX_FILENAME


def _connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with the defaults we want everywhere:
    foreign keys on (currently no FKs but cheap to enable for future),
    WAL mode for concurrent read+write, and a 5-second busy timeout so
    a momentarily-locked database retries rather than failing fast.

    Each connect tightens permissions to 0o600 on the .db and any
    existing -wal / -shm siblings. The index mirrors body content for
    full-text search, so the same privacy bar applies as to the source
    memory files. The WAL/SHM siblings are created lazily by SQLite on
    first write, so a one-shot chmod-on-creation could miss them;
    chmod-on-every-connect is idempotent and cheap (one stat + one
    chmod per file). No-op on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    import contextlib
    import os as _os

    for sibling in (
        path,
        path.with_suffix(path.suffix + "-wal"),
        path.with_suffix(path.suffix + "-shm"),
    ):
        if sibling.exists():
            with contextlib.suppress(OSError):
                _os.chmod(sibling, 0o600)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply the schema (CREATE IF NOT EXISTS everywhere) and stamp the
    meta table with the current schema_version. Idempotent — repeat
    calls on the same connection are safe.

    If the database carries a schema_version greater than this code's
    `SCHEMA_VERSION`, raises `IndexVersionError`. Callers (Store / CLI)
    should drop the file and call `rebuild` from scratch.
    """
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    else:
        on_disk = int(row[0])
        if on_disk > SCHEMA_VERSION:
            raise IndexVersionError(
                f"index schema version {on_disk} is newer than this "
                f"reader supports (max {SCHEMA_VERSION}); delete the "
                f"index file and run `bettermemory reindex`"
            )
    conn.commit()


class IndexVersionError(RuntimeError):
    """Raised when the on-disk index schema is newer than this code."""


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def rebuild(root: Path, memories: Iterable[Memory]) -> int:
    """Drop and rebuild the entire index from a memories iterable.

    Returns the number of memories indexed. The data tables are
    truncated (not the schema) so triggers and indexes survive.
    `meta.indexed_count` is updated at the end so callers can
    sanity-check against the on-disk count.

    Idempotent — running twice produces the same final state. Safe
    against partial failures: the rebuild runs in a single
    transaction, so a mid-build crash leaves the prior index intact.
    """
    path = index_path(root)
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        with conn:
            conn.execute("DELETE FROM memories")
            count = 0
            for memory in memories:
                _insert_memory(conn, memory)
                count += 1
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('indexed_count', ?)",
                (str(count),),
            )
        return count
    finally:
        conn.close()


def upsert(root: Path, memory: Memory) -> None:
    """Insert or replace one memory in the index. Called by Store hooks
    on write / update. Safe to call before the index file exists — the
    schema is created on demand."""
    path = index_path(root)
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        with conn:
            _upsert_memory(conn, memory)
            _bump_count(conn)
    finally:
        conn.close()


def remove(root: Path, memory_id: str) -> None:
    """Drop one memory from the index. Called by Store hooks on
    tombstone. No-op when the index doesn't exist (no rebuild needed
    just to delete from an empty index)."""
    path = index_path(root)
    if not path.exists():
        return
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        with conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            _bump_count(conn)
    finally:
        conn.close()


def query(
    root: Path,
    text: str,
    *,
    scopes: list[str] | None = None,
    max_results: int = 100,
) -> list[tuple[str, float]]:
    """FTS5 query returning ``[(memory_id, bm25_score), ...]`` sorted
    by score ascending (lower BM25 = more relevant in SQLite's
    convention).

    Returns at most `max_results` rows. `scopes`, when given, is an
    OR filter — at least one matching scope must be present on the
    memory. An empty `text` returns an empty list (FTS5 can't match
    on nothing).

    Caller is expected to layer additional scoring on top: this is
    the candidate set, not the final ranking. The single source of
    truth for ranking semantics is `search.py`.
    """
    if not text.strip():
        return []
    path = index_path(root)
    if not path.exists():
        return []

    conn = _connect(path)
    try:
        _ensure_schema(conn)
        # Build the MATCH clause. FTS5 special characters get escaped
        # by wrapping each term in quotes — protects against a user
        # query containing `:` or `*` from being interpreted as
        # column-prefix or prefix-match syntax.
        terms = [f'"{_escape_fts(t)}"' for t in text.split() if t.strip()]
        if not terms:
            return []
        match_query = " OR ".join(terms)

        sql = (
            "SELECT m.id, bm25(memories_fts) AS score "
            "FROM memories_fts "
            "JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE memories_fts MATCH ? "
        )
        params: list[Any] = [match_query]

        if scopes:
            # The scopes_text column is the space-separated scope list.
            # OR over each requested scope. We use LIKE with explicit
            # space-padding rather than = to handle the multi-scope
            # case (a memory tagged with ['tools', 'projects:foo']
            # has scopes_text=' tools projects:foo ').
            sql += "AND (" + " OR ".join(["m.scopes_text LIKE ?"] * len(scopes)) + ") "
            params.extend(f"% {s} %" for s in scopes)

        sql += "ORDER BY score ASC LIMIT ?"
        params.append(int(max_results))

        rows = conn.execute(sql, params).fetchall()
        return [(row["id"], float(row["score"])) for row in rows]
    finally:
        conn.close()


def status(root: Path) -> dict[str, Any]:
    """Diagnostic snapshot of the index file. Used by
    `bettermemory doctor` to surface index health and by the reindex
    CLI to report before/after counts. Never raises — a missing or
    corrupt index file returns a status dict with `exists=False`."""
    path = index_path(root)
    if not path.exists():
        return {"exists": False, "path": str(path)}
    try:
        conn = _connect(path)
        try:
            _ensure_schema(conn)
            count_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'indexed_count'"
            ).fetchone()
            schema_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            size_bytes = path.stat().st_size
            return {
                "exists": True,
                "path": str(path),
                "schema_version": int(schema_row[0]) if schema_row else None,
                "indexed_count": int(count_row[0]) if count_row else 0,
                "size_bytes": size_bytes,
            }
        finally:
            conn.close()
    except (sqlite3.DatabaseError, IndexVersionError) as exc:
        return {
            "exists": True,
            "path": str(path),
            "corrupt": True,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_fts(term: str) -> str:
    """Escape a token for use inside an FTS5 MATCH expression. FTS5
    treats most special chars as syntax — `"foo"` is the safest form
    (a literal phrase), so we double-quote-escape any existing
    quotes inside the term."""
    return term.replace('"', '""')


def _scopes_text(scopes: list[str]) -> str:
    """Serialize the scope list as space-padded text for LIKE-based
    OR filtering. Leading and trailing spaces let the LIKE pattern
    `% scope %` match exact tokens without false matches against
    substrings (e.g. `projects:foo` shouldn't match `projects:foobar`)."""
    if not scopes:
        return " "
    return " " + " ".join(scopes) + " "


def _isoformat_optional(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _insert_memory(conn: sqlite3.Connection, memory: Memory) -> None:
    """Insert a new row. Caller has already cleared the table or
    confirmed no row with this id exists."""
    conn.execute(
        "INSERT INTO memories("
        "id, created, updated, last_verified_at, confidence, category, "
        "body, scopes_text, scopes_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            memory.id,
            memory.created.isoformat(),
            memory.updated.isoformat(),
            _isoformat_optional(memory.last_verified_at),
            memory.confidence.value,
            memory.category.value if memory.category is not None else None,
            memory.body,
            _scopes_text(memory.scopes),
            json.dumps(memory.scopes),
        ),
    )


def _upsert_memory(conn: sqlite3.Connection, memory: Memory) -> None:
    """INSERT OR REPLACE on the id key. Trigger logic keeps the FTS
    virtual table in sync — the AFTER UPDATE trigger handles the
    delete-then-insert dance internally so callers don't have to."""
    conn.execute(
        "INSERT INTO memories("
        "id, created, updated, last_verified_at, confidence, category, "
        "body, scopes_text, scopes_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "created = excluded.created, "
        "updated = excluded.updated, "
        "last_verified_at = excluded.last_verified_at, "
        "confidence = excluded.confidence, "
        "category = excluded.category, "
        "body = excluded.body, "
        "scopes_text = excluded.scopes_text, "
        "scopes_json = excluded.scopes_json",
        (
            memory.id,
            memory.created.isoformat(),
            memory.updated.isoformat(),
            _isoformat_optional(memory.last_verified_at),
            memory.confidence.value,
            memory.category.value if memory.category is not None else None,
            memory.body,
            _scopes_text(memory.scopes),
            json.dumps(memory.scopes),
        ),
    )


def _bump_count(conn: sqlite3.Connection) -> None:
    """Refresh the meta.indexed_count after a single-row mutation.
    Called inside the same transaction as the change so a crash
    between can't leave the count out of sync."""
    n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('indexed_count', ?)",
        (str(n),),
    )


__all__ = [
    "INDEX_FILENAME",
    "SCHEMA_VERSION",
    "IndexVersionError",
    "index_path",
    "query",
    "rebuild",
    "remove",
    "status",
    "upsert",
]
