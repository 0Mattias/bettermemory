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
than fail. As of the audit H1 fix, the index upsert lives *inside*
the per-memory ``fcntl.flock`` critical section in the Store
(``_index_upsert_quietly`` / ``_index_remove_quietly`` are called
under the file lock in every mutator). Earlier the upsert ran after
the lock release on perf grounds; that lost ordering across
concurrent updates to the same id (file lock could release in order
A→B, but the two SQLite upserts could land B→A, leaving the index
with A's body while disk had B's). Stale FTS5 ranking quietly
misled ``memory_search`` and made the index harder to trust than
just rebuilding from disk; pulling the upsert under the lock
trades a tiny extra hold time for an actually-consistent index.

The hook is still best-effort: if a SQLite upsert fails inside the
lock (corrupt index, missing FTS5 extension, ENOSPC), the Store
logs a warning and lets the canonical file write succeed. The
recovery path remains ``bettermemory reindex``, which rebuilds the
index from the on-disk truth. Files are canonical, the index is
regenerable.

This module is intentionally narrow: schema, lifecycle, and a thin
``query`` surface returning ranked memory IDs. The search ranker
fuses these results with the keyword / BM25 / semantic scorers from
``search.py`` — the index is the *candidate set*, not the final
ranking. That split keeps the existing rankers as the source of
truth for scoring semantics and lets the index be added or removed
without changing the result shape.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Memory

log = logging.getLogger("bettermemory.index")


# Bump when the schema changes. A reader that sees a version it
# doesn't support drops the file and forces a rebuild rather than
# risk misinterpreting the rows. Migration semantics:
#
#   - on-disk > code SCHEMA_VERSION: raise IndexVersionError. The
#     caller (Store / CLI) should delete the index file and run
#     `bettermemory reindex`. We don't downgrade because we don't
#     know what newer columns the existing rows depend on.
#   - on-disk < code SCHEMA_VERSION: drop the data tables and
#     recreate empty. The Store hooks repopulate gradually as
#     writes land; `bettermemory reindex` does the explicit
#     full rebuild. The fallback path in `_load_search_candidates`
#     handles the empty-index case by routing to `load_all`, so
#     search keeps working while the index repopulates.
#
# Version 2: adds `memories.filename` for id → path lookup (so
# `_load_search_candidates` can directly read the candidate set
# instead of walking the whole store with `load_all`), and adds
# the `memory_links` table so `_links_payload`'s reverse-link
# scan stops being O(N) per `memory_show`.
#
# Version 3: widens the `memory_links` primary key to include `note`.
# Under v2 the key was `(source_id, type, target_id)`, so two on-disk
# links sharing a `(type, target_id)` but carrying different notes
# collapsed to one row via `INSERT OR IGNORE` — the in-memory reverse
# index silently lost a note the canonical file keeps (the link list
# on disk is a plain list with no dedup). Files are canonical; the
# index must mirror them, so `note` joins the key and both rows
# survive. A pure schema change to a derived cache — the bump forces
# a one-time rebuild via `_ensure_schema`'s older-version path.
SCHEMA_VERSION = 3

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
    scopes_json TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT ''
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

-- Inter-memory links. Keeps `_links_payload`'s reverse-link scan
-- out of `load_all` — that path was O(N) per `memory_show` because
-- finding "everyone who links AT this id" required walking every
-- memory's `links` field on disk. Now it's an index lookup.
CREATE TABLE IF NOT EXISTS memory_links (
    source_id TEXT NOT NULL,
    type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (source_id, type, target_id, note)
);

CREATE INDEX IF NOT EXISTS memory_links_by_target ON memory_links(target_id);

-- Cascade link cleanup when a memory is removed. Both source-side
-- (this memory's outbound links) and target-side (other memories
-- linking AT this id) get dropped. The target-side cleanup keeps
-- the reverse-link query honest: a hit against `target_id = X`
-- after X is tombstoned would otherwise dangle.
CREATE TRIGGER IF NOT EXISTS memory_links_cleanup AFTER DELETE ON memories BEGIN
    DELETE FROM memory_links
    WHERE source_id = old.id OR target_id = old.id;
END;
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

    PRAGMA execution can raise on a corrupt or zero-byte DB file
    (`sqlite3.connect` itself only validates the header lazily). If
    any of the setup steps raise, close the connection before
    re-raising — otherwise the caller never sees `conn` and the
    object lingers until GC, surfacing as a `ResourceWarning` in
    tests like `test_search_falls_back_when_index_corrupt`.

    Each connect tightens permissions to 0o600 on the .db and any
    existing -wal / -shm siblings. The index mirrors body content for
    full-text search, so the same privacy bar applies as to the source
    memory files. The WAL/SHM siblings are created lazily by SQLite on
    first write, so a one-shot chmod-on-creation could miss them;
    chmod-on-every-connect is idempotent and cheap (one stat + one
    chmod per file). No-op on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
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


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply the schema (CREATE IF NOT EXISTS everywhere) and stamp the
    meta table with the current schema_version. Idempotent — repeat
    calls on the same connection are safe.

    Version handling:
    - On-disk > code SCHEMA_VERSION: raise `IndexVersionError`. Callers
      (Store / CLI) should drop the file and call `rebuild` from
      scratch.
    - On-disk < code SCHEMA_VERSION: drop the data tables and recreate
      them at the current schema. Memory data lives on disk in the .md
      files; the Store hooks repopulate as writes happen, and
      `bettermemory reindex` does the explicit full rebuild. The
      `_load_search_candidates` fallback routes to `load_all` while
      the index is empty, so search keeps working through the
      transition.
    """
    # First-touch path: meta table may not exist yet. CREATE IF NOT
    # EXISTS is safe to run before the version check.
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        return
    on_disk = int(row[0])
    if on_disk > SCHEMA_VERSION:
        raise IndexVersionError(
            f"index schema version {on_disk} is newer than this "
            f"reader supports (max {SCHEMA_VERSION}); delete the "
            f"index file and run `bettermemory reindex`"
        )
    if on_disk < SCHEMA_VERSION:
        log.warning(
            "index schema version %s is older than current (%s); "
            "dropping and recreating empty. Run `bettermemory reindex` "
            "to fully repopulate, or let it fill incrementally as "
            "memories are written.",
            on_disk,
            SCHEMA_VERSION,
        )
        # Drop + re-create as a single atomic transaction. A parallel
        # reader opening the index between the DROP and the CREATE
        # would otherwise see an inconsistent schema (no `memories`
        # table) and its SELECT would fail — the SQLite busy timeout
        # doesn't help, no BUSY is raised when the table simply isn't
        # there yet.
        #
        # The transaction control (`BEGIN IMMEDIATE` … `COMMIT`) lives
        # *inside* the executescript string, deliberately. A
        # `conn.execute("BEGIN IMMEDIATE")` followed by a separate
        # `conn.executescript(...)` does NOT wrap: `executescript`
        # implicitly COMMITs any pending transaction before it runs
        # (documented behaviour), so the BEGIN is committed away and
        # the DROP/CREATE run unprotected. Keeping BEGIN/COMMIT in the
        # script body holds the whole drop+recreate in one
        # transaction; a concurrent reader sees the old schema or the
        # new one, never the gap. Verified on CPython 3.11–3.13.
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                "DROP TABLE IF EXISTS memory_links;\n"
                "DROP TABLE IF EXISTS memories_fts;\n"
                "DROP TABLE IF EXISTS memories;\n"
                f"{_SCHEMA}\n"
                "COMMIT;"
            )
            # The `meta` table is never dropped, so a reader can't race
            # these the way it can the schema swap above — run them
            # after the atomic CREATE rather than inside it (which
            # would mean string-building the version into the script,
            # since executescript takes no bound parameters).
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("UPDATE meta SET value = '0' WHERE key = 'indexed_count'")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return
    conn.commit()


class IndexVersionError(RuntimeError):
    """Raised when the on-disk index schema is newer than this code."""


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def rebuild(root: Path, items: Iterable[tuple[Path, Memory]]) -> int:
    """Drop and rebuild the entire index from a `(path, memory)` iterable.

    Returns the number of memories indexed. The data tables are
    truncated (not the schema) so triggers and indexes survive.
    `meta.indexed_count` is updated at the end so callers can
    sanity-check against the on-disk count.

    Idempotent — running twice produces the same final state. Safe
    against partial failures: the rebuild runs in a single
    transaction, so a mid-build crash leaves the prior index intact.

    Each entry pairs the on-disk path with its parsed Memory so the
    `filename` column can mirror the real file (collision-suffixed
    names included). Callers typically pass `Store.iter_active()`.
    """
    path = index_path(root)
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        with conn:
            conn.execute("DELETE FROM memories")
            count = 0
            for entry_path, memory in items:
                _insert_memory(conn, memory, entry_path.name)
                count += 1
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('indexed_count', ?)",
                (str(count),),
            )
        return count
    finally:
        conn.close()


def upsert(root: Path, memory: Memory, *, filename: str) -> None:
    """Insert or replace one memory in the index. Called by Store hooks
    on write / update. Safe to call before the index file exists — the
    schema is created on demand.

    `filename` is the on-disk filename (no leading directory) the
    Store actually wrote. Threading it through — rather than
    re-deriving — is what lets `filenames_for_ids` resolve
    collision-suffixed names back to the correct path."""
    path = index_path(root)
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        with conn:
            _upsert_memory(conn, memory, filename)
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


def filenames_for_ids(root: Path, ids: list[str]) -> dict[str, str]:
    """Resolve a batch of memory ids to their on-disk filenames via
    the index. Returns `{id: filename}` for every id that has a row.
    Ids without a row (newly written and not yet indexed, dropped on
    a schema upgrade pending reindex) are omitted — the caller falls
    back to the `load_all` path for those.

    This is the lookup `_load_search_candidates` uses to avoid
    parsing every memory's frontmatter when only a handful of
    candidates from the FTS5 pre-filter are actually wanted."""
    if not ids:
        return {}
    path = index_path(root)
    if not path.exists():
        return {}
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, filename FROM memories WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        # An empty filename column means the row was written by a
        # pre-v2 schema. Drop those — the caller treats them as
        # "fall back to load_all" without trying to construct a
        # bogus path.
        return {row["id"]: row["filename"] for row in rows if row["filename"]}
    finally:
        conn.close()


def links_for(
    root: Path, memory_id: str
) -> tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]]:
    """Resolve outbound and inbound links for `memory_id`.

    Returns `(outbound, inbound)` where each entry is
    `(type, other_id, note)`:

    - outbound: `(link.type, link.target_id, link.note)` — what this
      memory points at, mirroring `memory.links` on disk.
    - inbound: `(link.type, source.id, link.note)` — every memory
      that links AT this id. The reverse direction the
      `_links_payload` reverse-scan used to compute via a full
      `load_all`.

    Returns empty lists when the index file doesn't exist or has
    no rows for either direction. The handler falls back to
    `load_all` in that case (the same fallback shape the rest of
    `_load_search_candidates` uses)."""
    path = index_path(root)
    if not path.exists():
        return [], []
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        outbound = conn.execute(
            "SELECT type, target_id, note FROM memory_links "
            "WHERE source_id = ? ORDER BY type, target_id",
            (memory_id,),
        ).fetchall()
        inbound = conn.execute(
            "SELECT type, source_id, note FROM memory_links "
            "WHERE target_id = ? ORDER BY type, source_id",
            (memory_id,),
        ).fetchall()
        return (
            [(row["type"], row["target_id"], row["note"]) for row in outbound],
            [(row["type"], row["source_id"], row["note"]) for row in inbound],
        )
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


def _insert_memory(conn: sqlite3.Connection, memory: Memory, filename: str) -> None:
    """Insert a new row. Caller has already cleared the table or
    confirmed no row with this id exists.

    `filename` is the actual on-disk filename — the caller threads it
    through from the path it just wrote. The store's collision suffix
    (`<slug>-<short_id>.md`) means we can't re-derive this from the
    Memory fields alone, and getting it wrong points `filenames_for_ids`
    at the wrong file."""
    conn.execute(
        "INSERT INTO memories("
        "id, created, updated, last_verified_at, confidence, category, "
        "body, scopes_text, scopes_json, filename) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            filename,
        ),
    )
    _sync_links(conn, memory)


def _upsert_memory(conn: sqlite3.Connection, memory: Memory, filename: str) -> None:
    """INSERT OR REPLACE on the id key. Trigger logic keeps the FTS
    virtual table in sync — the AFTER UPDATE trigger handles the
    delete-then-insert dance internally so callers don't have to.

    See `_insert_memory` for the `filename` contract."""
    conn.execute(
        "INSERT INTO memories("
        "id, created, updated, last_verified_at, confidence, category, "
        "body, scopes_text, scopes_json, filename) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "created = excluded.created, "
        "updated = excluded.updated, "
        "last_verified_at = excluded.last_verified_at, "
        "confidence = excluded.confidence, "
        "category = excluded.category, "
        "body = excluded.body, "
        "scopes_text = excluded.scopes_text, "
        "scopes_json = excluded.scopes_json, "
        "filename = excluded.filename",
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
            filename,
        ),
    )
    _sync_links(conn, memory)


def _sync_links(conn: sqlite3.Connection, memory: Memory) -> None:
    """Replace the outbound link rows for `memory.id`. Inter-memory
    links use REPLACE semantics at the model layer (`memory_update`
    overwrites the full list), so the index mirror is the same: drop
    every row where this memory is the source, then insert the new
    list.

    `note` is part of the `memory_links` primary key (schema v3), so
    two on-disk links sharing a `(type, target_id)` but differing in
    their note both survive — the canonical link list on disk is a
    plain list with no dedup, and the reverse index has to mirror it.
    `INSERT OR IGNORE` therefore only collapses an exact-duplicate
    link line (same source, type, target, and note), which is a
    redundant row on disk too, never a distinct note."""
    conn.execute("DELETE FROM memory_links WHERE source_id = ?", (memory.id,))
    if not memory.links:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO memory_links("
        "source_id, type, target_id, note) VALUES (?, ?, ?, ?)",
        [
            (memory.id, link.type.value, link.target_id, link.note)
            for link in memory.links
        ],
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
    "filenames_for_ids",
    "index_path",
    "links_for",
    "query",
    "rebuild",
    "remove",
    "status",
    "upsert",
]
