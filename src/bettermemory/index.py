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
  bump can detect mismatched indexes and force a rebuild), the
  tokenizer fingerprint (so a tokenizer change respelling the
  persisted FTS stream forces the same rebuild — schema v5), and the
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
fuses these results with the keyword / BM25 scorers from
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
import time
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

from ._fsutil import flock_excl
from .models import Memory
from .origin import Origin
from .search import fts_index_text, fts_match_query, tokenizer_fingerprint

if TYPE_CHECKING:
    from .provenance import Evidence

log = logging.getLogger("bettermemory.index")


# Bump when the schema changes. A reader that sees a version it
# doesn't support drops the file and forces a rebuild rather than
# risk misinterpreting the rows. Migration semantics:
#
#   - on-disk > code SCHEMA_VERSION: raise IndexVersionError. The
#     caller (Store / CLI) should delete the index file and run
#     `bettermemory reindex`. We don't downgrade because we don't
#     know what newer columns the existing rows depend on.
#   - on-disk < code SCHEMA_VERSION: drop the data tables, recreate
#     empty, and set `meta.needs_rebuild = '1'`. The flag is cleared
#     ONLY by a successful `rebuild()` — never by the incremental
#     Store hooks, which repopulate just the memories that happen to
#     get touched. Without the flag, `_handlers.load_search_candidates`
#     would re-engage the FTS prefilter as soon as `indexed_count` crossed
#     its threshold and every untouched pre-upgrade memory would be
#     silently unreachable in `memory_search`; with it, search routes
#     to `load_all` until `Store.__post_init__`'s auto-rebuild (or an
#     explicit `bettermemory reindex`) restores full coverage.
#
# Version 2: adds `memories.filename` for id → path lookup (so
# `_handlers.load_search_candidates` can directly read the candidate
# set instead of walking the whole store with `load_all`), and adds
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
#
# Version 4 (tokenizer v2): the FTS table stops indexing the raw body
# under unicode61 and instead indexes `body_fts` / `scopes_fts` —
# preprocessed columns holding `search.fts_index_text` output, i.e. the
# exact token stream the Python rankers score (diacritic-folded,
# contraction-stripped, symbol-aliased, plural-stemmed, CJK-bigrammed,
# kebab-expanded). Prefilter/ranker parity becomes structural instead
# of hand-mirrored, and CJK bodies — previously ONE giant unicode61
# token per unspaced clause, unmatchable by any realistic query —
# become searchable at all. Raw `body` and `scopes_text` columns stay
# (the LIKE-based scope filter and debuggability read them); only what
# the FTS virtual table indexes changes.
#
# Version 5 (tokenizer fingerprint ratchet): identical DDL to v4. The
# bump exists because v4 PERSISTS tokenize() output, so query/index
# parity requires the persisted stream to match the live tokenizer —
# and four post-3.12.0 tokenizer fixes respelled that stream (stopword
# curation, final-y normalisation, CJK index-side unigrams, the NFKC
# fold), leaving every 3.12.0-built index stale-spelled against live
# queries ('todos' indexed, 'todo' queried). The wipe forces a respell
# through the standard heal path. To keep this class of skew from
# recurring silently, the meta table also records
# `tokenizer_fingerprint` (see `search.tokenizer_fingerprint`) next to
# `schema_version`, stamped in the same transaction; `_ensure_schema`
# treats a fingerprint mismatch at the CURRENT version exactly like an
# older version.
#
# Version 6 (origin columns): `memories` gains `origin_repo` /
# `origin_worktree`, mirroring the origin block already on the .md file.
# They exist so the BM25 corpus-statistics path can decide search
# admission from an index row instead of parsing every body — see
# `corpus_document_frequencies`. NOT a SQL filter: the columns are read
# back out and fed to `search.candidate_admitted`, because `repos_match`
# compares on `(host, owner, name)` AND consults per-process alternate
# spellings, so admission is not expressible in SQL. Storing the raw
# strings and deciding in Python is what keeps the IDF denominator
# provably equal to the ranked set.
#
# Version 7 (provenance column): `memories` gains `provenance`, one of
# `local` / `synced` / `untracked` / `unaccounted` (see `provenance.py`
# for the derivation). Index-resident on purpose: frontmatter is
# attacker-writable, so the label is derived from the event log and the
# sync repo at `rebuild` and stamped `local` at the upsert the Store's
# own creation paths perform, never read from the file. Prior labels
# survive a rebuild (`_read_prior_provenance`) and survive the drop a
# later version or tokenizer bump performs (`_stash_provenance` parks
# them in `meta.provenance_carry`, which `meta` keeps across the drop);
# `meta.provenance_classified` records that a classified rebuild has
# run, the baseline rule 5 of the derivation reads.
#
# Version 8 (verified_locally_at column): `memories` gains
# `verified_locally_at`, the instant THIS host last stamped the memory
# through `Store.mark_verified`, or NULL. A pulled file carries whatever
# `last_verified_at` the other host (or the attacker) wrote, and the
# frontmatter cannot say who wrote it; this column can, because only the
# local stamp path sets it and `sync pull` clears it for every file it
# brought down (`clear_local_verification`). The read surfaces report a
# `synced` row with a stamp but no local verification as
# `verification.status: "remote"` (`verify.remote_verification_status`).
# The column is carried across rebuilds and across the drop a later
# version or tokenizer bump performs exactly as the labels are
# (`_stash_provenance` parks it in `meta.trust_carry`), and the rebuild
# derives it from `verify` and `sync_pull` events as well
# (`provenance.classify_trust`).
#
# Version 10 (verified_head column): `memories` gains `verified_head`,
# the commit the memory's origin checkout stood at when `memory_verify`
# last stamped it (`Memory.verified_head`), or NULL. Straight off the
# record at every upsert and re-read from frontmatter at every rebuild,
# so unlike the three columns above it needs no carry across a drop; it
# is here so a curation pass can read every stamp's anchor from one
# query instead of opening every file.
SCHEMA_VERSION = 10

# Pinned `search.tokenizer_fingerprint()` digest for the current
# SCHEMA_VERSION. Consumed only by the ratchet test
# (test_index.py::test_tokenizer_fingerprint_pinned_constant_is_the_ratchet)
# — the runtime stamp/compare uses the live function, so persisted rows
# and their meta stamp can never disagree. If the test reports a diff,
# the index-side token stream changed: re-pin this constant, and bump
# SCHEMA_VERSION with it iff the current version has shipped in a
# release (the fingerprint mismatch already heals same-version stores;
# the bump is what keeps version semantics honest once a release
# persisted the old stream — pre-release stream amendments fold into
# the pending bump).
TOKENIZER_FINGERPRINT = (
    "70bc8e2452b87298538b81bca1cf9039b867734e2aed1078aba4deceb6be6797"
)

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
    body_fts TEXT NOT NULL DEFAULT '',
    scopes_text TEXT NOT NULL,
    scopes_fts TEXT NOT NULL DEFAULT '',
    scopes_json TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    -- Schema v6. Raw spellings, straight off the memory's origin block;
    -- NULL for a legacy/global write with no origin. Never compared in
    -- SQL — `corpus_document_frequencies` hands them to
    -- `search.candidate_admitted` so the normalising `repos_match`
    -- (and its per-process alternate spellings) stays the single
    -- definition of "belongs to this caller".
    origin_repo TEXT,
    origin_worktree TEXT,
    -- Schema v7. How the memory entered the store, derived at rebuild
    -- (`provenance.classify`) or stamped `local` by the Store's own
    -- creation upserts. NULL on a row an incremental hook wrote without
    -- a label (an update on a memory the index had not classified yet);
    -- the next rebuild classifies it. Read surfaces omit the field when
    -- NULL rather than guess.
    provenance TEXT,
    -- Schema v8. When this host last stamped the memory through its own
    -- verify path (ISO-8601), or NULL: never, or not since a `sync pull`
    -- brought the file down. Read beside `provenance` to tell a local
    -- stamp from one that arrived in the file.
    verified_locally_at TEXT,
    -- Schema v9. SHA-256 of the file's bytes as the store last wrote
    -- them, stamped by every in-process write path (`Store._write_path`
    -- and the direct `_atomic_write_post` callers) and by the rebuild,
    -- which carries a recorded hash FORWARD for a non-pulled file whose
    -- bytes no longer match it — so a hand edit stays visible across
    -- `bettermemory reindex`. `doctor`'s `memory_content_evidence`
    -- compares it with the file. NULL until a write or a rebuild stamps
    -- the row.
    content_sha256 TEXT,
    -- Schema v10. The commit the memory's origin checkout stood at when
    -- `memory_verify` last stamped it, straight off the record
    -- (`Memory.verified_head`); NULL when the stamp carries no anchor.
    -- Re-read from frontmatter at every rebuild, so it needs no carry.
    verified_head TEXT
);

-- The FTS table indexes the PREPROCESSED columns (schema v4): body_fts /
-- scopes_fts carry `search.fts_index_text` output, the same normalised
-- token stream the Python rankers score, so a MATCH built by
-- `search.fts_match_query` agrees with the rankers by construction.
-- unicode61 here only re-splits the space-joined tokens (and the hyphens
-- inside preserved compounds, which is what lets the quoted compound
-- phrase match). Raw body/scopes_text stay on the content table for the
-- LIKE scope filter and debuggability but are NOT indexed.
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


def _root_has_memory_files(root: Path, *, exclude: str | None = None) -> bool:
    """True when the store root holds at least one active memory file
    other than `exclude` (a bare filename). The filter — regular file,
    not a symlink, `.md` suffix — mirrors `Store._iter_active_paths`
    without importing the store module (store.py imports index.py, not
    the reverse). Short-circuits on the first hit; tombstones live in a
    subdirectory and never match. Best-effort: an unlistable root reads
    as empty — nothing could be rebuilt from it either, and first-touch
    stamping must not grow a failure mode `status()`'s never-raises
    contract would otherwise have to absorb."""
    try:
        for entry in root.iterdir():
            if (
                entry.suffix == ".md"
                and entry.name != exclude
                and entry.is_file()
                and not entry.is_symlink()
            ):
                return True
    except OSError:
        return False
    return False


def _ensure_schema(
    conn: sqlite3.Connection, path: Path, *, inflight_filename: str | None = None
) -> None:
    """Apply the schema (CREATE IF NOT EXISTS everywhere) and stamp the
    meta table with the current schema_version and tokenizer
    fingerprint. Idempotent — repeat calls on the same connection are
    safe. `path` is the index file `conn` is open on; the first-touch
    stamp and the migration both serialise on a flock sidecar next to
    it.

    First-touch (no `schema_version` row yet): under the flock, re-read
    and — still no row — stamp version + fingerprint, and, when the
    store root (`path.parent`) already holds memory files, set
    `meta.needs_rebuild = '1'` in the SAME transaction. A concurrent
    creator that loses the stamp race finds the winner's row on the
    re-read and falls through to the version handling below instead.
    A fresh index born inside a populated root (the user deleted
    `.index.sqlite`, historically the recovery advice, or restored a
    backup without the sidecar) is exactly as hollow as a
    post-migration one: the incremental hooks refill only touched
    memories, so without the flag the prefilter would re-engage on
    `indexed_count` alone and every untouched legacy memory would
    silently vanish from `memory_search`. `inflight_filename` names
    the one file whose row the caller is writing in this same
    operation (`upsert` threads it; the Store hooks write the .md
    before upserting): it is excluded from the populated-check so a
    genuinely-new store's first write does not flag itself.

    Version handling:
    - On-disk > code SCHEMA_VERSION: raise `IndexVersionError`. Callers
      (Store / CLI) should drop the file and call `rebuild` from
      scratch.
    - On-disk < code SCHEMA_VERSION, or `meta.tokenizer_fingerprint`
      differing from the live `search.tokenizer_fingerprint()` (the
      persisted `body_fts` / `scopes_fts` streams were spelled by a
      different tokenizer, so query/index parity is broken even though
      the DDL matches — the skew four 3.12.x tokenizer fixes shipped):
      drop the data tables, recreate them at the current schema, stamp
      version + fingerprint, and set `meta.needs_rebuild = '1'` — all
      in ONE transaction, under a cross-process migration lock (the
      inline comments below name the two races that shape closes).
      Memory data lives on disk in the .md files; `Store.__post_init__`
      auto-rebuilds from them on the next construction, and
      `bettermemory reindex` remains the manual path. While the flag is
      set, `_handlers.load_search_candidates` treats the index as unusable
      and routes to `load_all` — the incremental Store hooks only
      repopulate touched memories, so `indexed_count` alone can cross
      the prefilter threshold with most of the corpus still missing.
    """
    # First-touch path: meta table may not exist yet. CREATE IF NOT
    # EXISTS is safe to run before the version check.
    conn.executescript(_SCHEMA)
    live_fp = tokenizer_fingerprint()
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        # Two processes creating the index concurrently can both read
        # no-row here — the Store flock is per-memory-file, so writers
        # of different memories don't serialise — and `meta.key` is a
        # PRIMARY KEY, so an unserialised loser's stamp would die with
        # IntegrityError: the best-effort hooks swallow it at the cost
        # of a dropped index row, `status()` reports the healthy index
        # corrupt, and `_open_for_rebuild` answers by unlinking the
        # index the winner just stamped. Same discipline as the
        # migration branch below: take the flock, re-read, and let the
        # loser fall through to the version handling on whatever the
        # winner wrote (equal build: no-op; newer: raise; older or
        # fingerprint-skewed: migrate). Lock ordering is safe for the
        # reasons given at the migration acquire.
        with flock_excl(path):
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                # All stamps land in one implicit transaction — a
                # reader never sees a version row without its
                # fingerprint sibling (nor, on a populated root,
                # without the rebuild flag below).
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('tokenizer_fingerprint', ?)",
                    (live_fp,),
                )
                # Recall-hole guard, first-touch shape (see the
                # docstring): a fresh index inside an already-populated
                # store must not look trustworthy — flag it
                # rebuild-pending exactly like the migration branch
                # does, cleared only by `rebuild()`. The in-flight
                # upsert's own file doesn't count as missing coverage;
                # a store holding nothing else is genuinely new and
                # stays unflagged.
                if _root_has_memory_files(path.parent, exclude=inflight_filename):
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key, value) "
                        "VALUES ('needs_rebuild', '1')"
                    )
                conn.commit()
                return
    on_disk = int(row[0])
    if on_disk > SCHEMA_VERSION:
        raise _newer_version_error(on_disk)
    # The fingerprint read is ordered AFTER the newer-version raise: a
    # future build's fingerprint always differs, and the wipe below must
    # never claim an index a newer reader owns. Plain TEXT equality — no
    # int() parse, so status()/rebuild()'s corruption tolerance gains no
    # new escape path. An absent row counts as a mismatch: pre-v5
    # indexes never wrote one, and every v5 stamp writes both keys in
    # the same transaction.
    fp_row = conn.execute(
        "SELECT value FROM meta WHERE key = 'tokenizer_fingerprint'"
    ).fetchone()
    fingerprint_stale = fp_row is None or fp_row[0] != live_fp
    if on_disk < SCHEMA_VERSION or fingerprint_stale:
        if on_disk < SCHEMA_VERSION:
            log.warning(
                "index schema version %s is older than current (%s); "
                "dropping and recreating empty. Search bypasses the index "
                "until the next Store construction auto-rebuilds it (or "
                "`bettermemory reindex` is run).",
                on_disk,
                SCHEMA_VERSION,
            )
        else:
            log.warning(
                "index tokenizer fingerprint %s does not match this "
                "build's (%s) — the persisted FTS token stream was "
                "spelled by a different tokenizer; dropping and "
                "recreating empty. Search bypasses the index until the "
                "next Store construction auto-rebuilds it (or "
                "`bettermemory reindex` is run).",
                fp_row[0] if fp_row is not None else None,
                live_fp,
            )
        # The version/fingerprint reads above are deliberately unguarded
        # (the common up-to-date case must not pay a lock), so two
        # upgrading processes can both reach this branch. The flock
        # serialises them; the re-read below turns the loser into a
        # no-op instead of a second wipe destroying rows the winner's
        # caller already started repopulating. Lock ordering is safe:
        # this flock is leaf-level (nothing is acquired under it but
        # the SQLite write lock, and SQLite-lock holders never take
        # this flock), and it releases before `_ensure_schema` returns.
        with flock_excl(path):
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row[0]) if row is not None else on_disk
            if current > SCHEMA_VERSION:
                # A newer-version migrator won the race while we waited.
                # Same contract as the primary check above.
                raise _newer_version_error(current)
            fp_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'tokenizer_fingerprint'"
            ).fetchone()
            if (
                current == SCHEMA_VERSION
                and fp_row is not None
                and fp_row[0] == live_fp
            ):
                # Lost the race to an equal migrator: the swap is
                # already committed, stamped, and flagged. BOTH stamps
                # must match for the no-op — an equal-version winner
                # running a different tokenizer build left a stream
                # this build still cannot query.
                return
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
            #
            # The version stamp, fingerprint stamp, `indexed_count`
            # reset, and `needs_rebuild` flag are string-built into the
            # SAME script (`executescript` takes no bound parameters;
            # everything interpolated is a module-level integer/string
            # literal or a sha256 hexdigest with a fixed `[0-9a-f]`
            # alphabet, so there is no injection surface). Committing
            # them separately after the swap left a window where the new
            # tables were live but meta still carried the old version:
            # a pre-bump process reading in that window passed its own
            # version check and its old-column-list INSERT succeeded
            # against the new table (`body_fts` / `scopes_fts` DEFAULT
            # ''), so the FTS trigger indexed empty strings and the row
            # stuck FTS-invisible once the stamp landed.
            #
            # Recall-hole guard: `needs_rebuild` is cleared ONLY by
            # `rebuild()` — incremental hook upserts must not be able
            # to make a post-migration index look usable while the
            # untouched rest of the corpus is missing from it.
            #
            # Provenance labels would go down with the table. Park them
            # in `meta` first (its own committed transaction, so a crash
            # between the stash and the drop leaves a harmless extra row
            # the next rebuild reconciles against the still-present
            # table); `_read_prior_provenance` reads the stash back.
            _stash_provenance(conn)
            try:
                conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    "DROP TABLE IF EXISTS memory_links;\n"
                    "DROP TABLE IF EXISTS memories_fts;\n"
                    "DROP TABLE IF EXISTS memories;\n"
                    f"{_SCHEMA}\n"
                    f"UPDATE meta SET value = '{SCHEMA_VERSION}' "
                    "WHERE key = 'schema_version';\n"
                    "INSERT OR REPLACE INTO meta(key, value) "
                    f"VALUES ('tokenizer_fingerprint', '{live_fp}');\n"
                    "UPDATE meta SET value = '0' WHERE key = 'indexed_count';\n"
                    "INSERT OR REPLACE INTO meta(key, value) "
                    "VALUES ('needs_rebuild', '1');\n"
                    "COMMIT;"
                )
            except Exception:
                conn.rollback()
                raise
        return
    conn.commit()


class IndexVersionError(RuntimeError):
    """Raised when the on-disk index schema is newer than this code."""


def _newer_version_error(on_disk: int) -> IndexVersionError:
    """Uniform error for an on-disk schema newer than this reader.
    Raised by `_ensure_schema`'s primary version check and by the
    under-lock re-check (a newer-version migrator can win the race
    while an older one waits on the migration flock)."""
    return IndexVersionError(
        f"index schema version {on_disk} is newer than this "
        f"reader supports (max {SCHEMA_VERSION}); delete the "
        f"index file and run `bettermemory reindex`"
    )


def _unlink_index_files(path: Path) -> None:
    """Remove the index file and its -wal/-shm siblings.

    Used by `rebuild` to recover from a corrupt or version-skewed
    index: the canonical .md files are the source of truth, so the
    derived index can always be dropped and recreated. Missing
    siblings are ignored (a recovery may run before some exist).

    The -wal/-shm siblings are removed BEFORE the main .db so that any
    single-point unlink failure leaves a CONSISTENT state — never a
    stale WAL outliving the database it journals (a worse state than
    before, for a primitive whose whole job is repair). On POSIX the
    partial case is implausible (unlink permission is governed by the
    parent dir), but a Windows file lock or a mixed-permission EACCES on
    one sibling could otherwise delete the .db and orphan the WAL. A
    genuine non-FileNotFoundError failure still propagates (the operator
    must fix permissions); it just can no longer half-delete the index."""
    for sibling in (
        path.with_suffix(path.suffix + "-wal"),
        path.with_suffix(path.suffix + "-shm"),
        path,
    ):
        with contextlib.suppress(FileNotFoundError):
            sibling.unlink()


def _open_for_rebuild(path: Path) -> sqlite3.Connection:
    """Open a connection with the schema ensured, recovering from a
    corrupt or version-skewed index by dropping and recreating it.

    `rebuild` is the documented repair primitive, so it must tolerate
    ANY prior on-disk state rather than crash on it. Three unusable
    states are exactly the ones whose recovery instruction is "run
    `bettermemory reindex`":
      - a torn / zero-byte .db -> `sqlite3.DatabaseError`, surfaced
        lazily during `_connect`'s PRAGMA setup (sqlite3.connect only
        validates the header on first use);
      - an on-disk `schema_version` newer than this code ->
        `IndexVersionError` from `_ensure_schema`;
      - a non-integer `schema_version` -> `ValueError` from
        `_ensure_schema`'s `int()` read — unparseable meta IS
        corruption, the same call `status()` makes when it reports
        this state `corrupt=True` and doctor answers with "run
        `bettermemory reindex`".
    In every case, drop the index file (+ siblings) and rebuild from a
    clean slate. The canonical .md files are untouched, so nothing is
    lost. (Before this, the recovery primitive crashed on exactly the
    inputs it exists to repair.) Corruption these open-time reads CAN'T
    see — a torn FTS shadow page first walked by the data sweep — is
    recovered by `rebuild`'s own data-phase fallback, not here."""
    try:
        conn = _connect(path)
    except sqlite3.DatabaseError:
        # `_connect` closes its own connection before re-raising, so
        # there is nothing to clean up here — just nuke and reopen.
        _unlink_index_files(path)
        conn = _connect(path)
    # From here `conn` is open; any failure must close it before
    # propagating, or it leaks as a `ResourceWarning: unclosed database`
    # on GC (the contract `_connect`'s docstring and the corrupt-index
    # tests pin). The compound path — recovery reopen followed by a disk
    # error in the post-unlink `_ensure_schema` — is the one that would
    # otherwise leak.
    try:
        try:
            _ensure_schema(conn, path)
        except (ValueError, sqlite3.DatabaseError, IndexVersionError):
            conn.close()
            _unlink_index_files(path)
            conn = _connect(path)
            _ensure_schema(conn, path)
    except BaseException:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def rebuild(root: Path, items: Iterable[tuple[Path, Memory]]) -> int:
    """Drop and rebuild the entire index from a `(path, memory)` iterable.

    Returns the number of memories indexed. The data tables are
    truncated (not the schema) so triggers and indexes survive.
    `meta.indexed_count` is updated at the end so callers can
    sanity-check against the on-disk count, and the schema-migration
    `needs_rebuild` flag is cleared — this is the ONLY place it clears.

    Idempotent — running twice produces the same final state. Safe
    against partial failures: the data phase runs in a single
    transaction, so a mid-build non-SQLite crash leaves the prior
    index intact.

    Recovery primitive: tolerates ANY prior on-disk index state, in
    BOTH phases. Open-time — a torn .db header, a schema_version newer
    than this code, unparseable meta — is dropped and recreated by
    `_open_for_rebuild`. Data-phase corruption those open-time reads
    cannot see (a torn FTS shadow page passes `_connect`'s PRAGMAs and
    `_ensure_schema`'s meta reads, then first raises when the
    DELETE/INSERT sweep's triggers walk the damaged pages — exactly
    the class doctor's `PRAGMA quick_check` detects and answers with
    "run `bettermemory reindex`") falls back to the same nuclear path:
    drop the file (+ WAL/SHM siblings) and re-run the data phase ONCE
    against a fresh file. A second failure propagates — the retry runs
    on a file this process just created, so it is not prior-state
    corruption. The .md files are canonical throughout; a *valid*
    prior index is still preserved through the transactional build.

    Each entry pairs the on-disk path with its parsed Memory so the
    `filename` column can mirror the real file (collision-suffixed
    names included). Callers typically pass `Store.iter_active()`.
    """
    path = index_path(root)
    # Materialised before anything touches SQLite: the corruption
    # fallback below re-runs the data phase, and callers typically pass
    # a one-shot generator (`Store.iter_active()`) the failed first
    # attempt would have partially drained. Same memory envelope
    # `load_all` already pays on every fallback search.
    entries = list(items)
    # Gathered before the index is touched: the evidence is the event log
    # and the sync repo, neither of which the rebuild changes, and a
    # corruption fallback below must classify from the same evidence the
    # first attempt had. Lazy import: `provenance` reads the event log,
    # which this module otherwise never needs.
    from . import provenance as _provenance

    evidence = _provenance.gather_evidence(root)
    conn = _open_for_rebuild(path)
    try:
        try:
            return _rebuild_data(conn, entries, evidence)
        except sqlite3.IntegrityError:
            # NOT a corruption signal, so it must not reach the nuclear
            # path below. A constraint violation is produced by the
            # ENTRIES, not by the file: re-running the identical feed
            # against a fresh file fails identically, and the only thing
            # the fallback accomplishes is unlinking a perfectly healthy
            # index — turning a recoverable feed problem into a store
            # whose `reindex` can never succeed again, with `doctor`
            # advising the very command that cannot work. Propagate with
            # the real error instead.
            raise
        except sqlite3.DatabaseError as exc:
            log.warning(
                "index rebuild data phase failed on the existing file "
                "(%s); dropping %s and its WAL/SHM siblings, then "
                "rebuilding from scratch",
                exc,
                path,
            )
            conn.close()
            _unlink_index_files(path)
            conn = _open_for_rebuild(path)
            return _rebuild_data(conn, entries, evidence)
    finally:
        # Reassignment-safe: if the recovery `_open_for_rebuild` itself
        # raised, `conn` still names the already-closed first connection
        # and sqlite3's close() is idempotent.
        conn.close()


def _rebuild_data(
    conn: sqlite3.Connection,
    entries: list[tuple[Path, Memory]],
    evidence: Evidence,
) -> int:
    """The transactional data phase of `rebuild`: truncate, refill,
    stamp `indexed_count`, clear `needs_rebuild`. Takes a LIST rather
    than the caller's iterable because `rebuild`'s corruption fallback
    re-runs this phase against a fresh file — a one-shot generator
    would replay empty.

    `evidence` is `provenance.gather_evidence`'s result. The prior
    labels are read BEFORE the truncation, inside the same transaction,
    so a crash mid-build rolls the read and the refill back together
    and the sticky labels cannot be lost to a torn rebuild."""
    from . import provenance as _provenance

    with conn:
        prior, baseline = _read_prior_provenance(conn)
        prior_trust = _read_prior_trust(conn)
        prior_hashes = _read_prior_content_hashes(conn)
        conn.execute("DELETE FROM memories")
        count = 0
        seen: set[str] = set()
        for entry_path, memory in entries:
            # An upsert, not the plain INSERT this loop used to run: two
            # active `.md` files can legitimately carry the same id — an iCloud or
            # Dropbox conflicted copy, a `cp` before a hand-edit, a
            # backup restored by copying files back in. Those are the
            # exact situations `reindex` exists for. A plain INSERT
            # raised `UNIQUE constraint failed: memories.id` on the
            # second file, which used to take the corruption fallback
            # and unlink a healthy index. `scan_active_memory_ids`
            # already documents the contract this restores: "Duplicate
            # ids across two files collapse into one entry (last file in
            # directory order wins)" — which is exactly what the upsert's
            # `ON CONFLICT(id) DO UPDATE` does, and what the incremental
            # hook path has always done.
            label = _provenance.classify(
                memory, entry_path.name, evidence, prior, baseline=baseline
            )
            verified_locally_at = _provenance.classify_trust(
                memory, entry_path.name, evidence, prior_trust.get(memory.id)
            )
            try:
                current_sha: str | None = file_sha256(entry_path)
            except OSError:
                current_sha = None
            content_sha = (
                carried_content_sha256(label, prior_hashes.get(memory.id), current_sha)
                if current_sha is not None
                else prior_hashes.get(memory.id)
            )
            _upsert_memory(
                conn,
                memory,
                entry_path.name,
                provenance=label,
                verified_locally_at=verified_locally_at,
                content_sha256=content_sha,
            )
            if memory.id not in seen:
                seen.add(memory.id)
                count += 1
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('indexed_count', ?)",
            (str(count),),
        )
        # The stash is consumed by this rebuild; the baseline marker
        # records that every row now carries a classified label, which
        # is what lets the next rebuild treat an id it has never seen as
        # new rather than as pre-existing.
        conn.execute("DELETE FROM meta WHERE key = 'provenance_carry'")
        conn.execute("DELETE FROM meta WHERE key = 'trust_carry'")
        conn.execute("DELETE FROM meta WHERE key = 'content_sha_carry'")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) "
            "VALUES ('provenance_classified', '1')"
        )
        # The schema-migration `needs_rebuild` flag clears here and
        # ONLY here, inside the same transaction as the repopulation:
        # a mid-build crash rolls back both, so the flag can never
        # clear without the rows actually landing. (The first-touch
        # stamp a recovery reopen sets on a populated root clears the
        # same way — only when the retry's rows actually land.)
        conn.execute("DELETE FROM meta WHERE key = 'needs_rebuild'")
        # The auto-rebuild failure-backoff marker clears with it: a
        # successful rebuild ends the backoff no matter how recently a
        # prior attempt failed.
        conn.execute("DELETE FROM meta WHERE key = 'last_rebuild_failure'")
    return count


def upsert(
    root: Path,
    memory: Memory,
    *,
    filename: str,
    provenance: str | None = None,
    verified_locally_at: str | None = None,
    content_sha256: str | None = None,
) -> None:
    """Insert or replace one memory in the index. Called by Store hooks
    on write / update. Safe to call before the index file exists — the
    schema is created on demand (and, when the store already holds
    memories beyond this one, flagged `needs_rebuild`: a hook-created
    fresh index in a populated store covers only what gets touched).

    `filename` is the on-disk filename (no leading directory) the
    Store actually wrote. Threading it through — rather than
    re-deriving — is what lets `filenames_for_ids` resolve
    collision-suffixed names back to the correct path.

    `provenance` is the label the caller can vouch for: the Store's own
    creation paths pass `local`. None means "no claim": an update keeps
    whatever label the row already carries, and a row that did not
    exist lands unlabelled until the next rebuild classifies it."""
    path = index_path(root)
    conn = _connect(path)
    try:
        _ensure_schema(conn, path, inflight_filename=filename)
        with conn:
            _upsert_memory(
                conn,
                memory,
                filename,
                provenance=provenance,
                verified_locally_at=verified_locally_at,
                content_sha256=content_sha256,
            )
            _bump_count(conn)
    finally:
        conn.close()


def stamp_content_sha256(root: Path, memory_id: str, sha: str) -> None:
    """Record the bytes a writer that bypasses `upsert` just put on disk
    (`migrate`'s origin backfill and repair rewrite frontmatter through
    `_atomic_write_post` directly). A row that does not exist yet is
    left for the next upsert or rebuild to create; nothing is inserted
    here, because a hash without the row's other columns is not a row."""
    path = index_path(root)
    if not path.exists():
        return
    conn = _connect(path)
    try:
        _ensure_schema(conn, path)
        with conn:
            conn.execute(
                "UPDATE memories SET content_sha256 = ? WHERE id = ?",
                (sha, memory_id),
            )
    finally:
        conn.close()


def content_hashes(root: Path) -> dict[str, tuple[str | None, str | None]] | None:
    """`{id: (content_sha256, provenance)}` over every indexed row, for
    `doctor`'s content-evidence check. None (never raises) when the index
    is absent or unusable, so a caller can tell "nothing changed" from
    "could not look"."""
    path = index_path(root)
    if not path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, path)
        return {
            row["id"]: (row["content_sha256"], row["provenance"])
            for row in conn.execute(
                "SELECT id, content_sha256, provenance FROM memories"
            )
        }
    except (sqlite3.Error, ValueError, IndexVersionError, OSError):
        return None
    finally:
        if conn is not None:
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
        _ensure_schema(conn, path)
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
        _ensure_schema(conn, path)
        # Build the MATCH clause from the SAME tokenisation the Python
        # rankers use (`search.fts_match_query`), not a raw
        # `text.split()`. Since schema v4 the indexed text is itself
        # `search.fts_index_text` output, so both sides of the MATCH
        # speak tokenize()'s normalised tokens (folds, aliases, stems,
        # CJK bigrams) and parity is structural; the one remaining
        # OR-variant is the joined-token conjunctive form (compound <->
        # AND of its components). FTS5 special characters stay inert —
        # every variant is emitted as a quoted phrase with embedded
        # quotes doubled, so `:` / `*` in a user query can't be read
        # as column-prefix or prefix-match syntax.
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

    This is the lookup `_handlers.load_search_candidates` uses to avoid
    parsing every memory's frontmatter when only a handful of
    candidates from the FTS5 pre-filter are actually wanted."""
    if not ids:
        return {}
    path = index_path(root)
    if not path.exists():
        return {}
    conn = _connect(path)
    try:
        _ensure_schema(conn, path)
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


# SQLite's default host-parameter ceiling is 999 on older builds; a
# `memory_list` over a large store hands more ids than that in one call.
_PROVENANCE_BATCH = 500


def provenance_for(root: Path, ids: list[str]) -> dict[str, str]:
    """`{id: label}` for every id whose row carries a classified label.

    Ids without a row, or with a NULL label (a hook-written row the next
    rebuild has not classified yet), are omitted: the read surfaces then
    leave the field out rather than guess. Never raises; an absent or
    unusable index reads as `{}`, the same degrade every other read
    helper here takes."""
    if not ids:
        return {}
    path = index_path(root)
    if not path.exists():
        return {}
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, path)
        out: dict[str, str] = {}
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                "SELECT id, provenance FROM memories "
                f"WHERE id IN ({placeholders}) AND provenance IS NOT NULL",
                batch,
            ).fetchall()
            for row in rows:
                out[row["id"]] = row["provenance"]
        return out
    except (sqlite3.Error, ValueError, IndexVersionError, OSError):
        return {}
    finally:
        if conn is not None:
            conn.close()


class TrustRow(NamedTuple):
    """One row's trust facts the file cannot supply: how it entered the
    store and when this host last verified it (None: never, or not since
    a pull brought the file down)."""

    provenance: str
    verified_locally_at: str | None


def trust_for(root: Path, ids: list[str]) -> dict[str, TrustRow]:
    """`{id: TrustRow}` for every id whose row carries a classified
    label. The read `_response.attach_provenance` pays once per
    response; same omissions and the same never-raises degrade as
    `provenance_for`."""
    if not ids:
        return {}
    path = index_path(root)
    if not path.exists():
        return {}
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, path)
        out: dict[str, TrustRow] = {}
        for start in range(0, len(ids), _PROVENANCE_BATCH):
            batch = ids[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                "SELECT id, provenance, verified_locally_at FROM memories "
                f"WHERE id IN ({placeholders}) AND provenance IS NOT NULL",
                batch,
            ).fetchall()
            for row in rows:
                out[row["id"]] = TrustRow(row["provenance"], row["verified_locally_at"])
        return out
    except (sqlite3.Error, ValueError, IndexVersionError, OSError):
        return {}
    finally:
        if conn is not None:
            conn.close()


def clear_local_verification(root: Path, filenames: Iterable[str]) -> int:
    """Set `verified_locally_at` to NULL on the rows at `filenames`.

    `sync pull` calls this for every file the rebase brought down: the
    bytes changed under this host, so whatever it verified before is no
    longer what is on disk, and whatever stamp the file now carries came
    from elsewhere. Deliberately an UPDATE and not an upsert with None,
    because the upsert's COALESCE treats None as "no claim". Never
    raises; returns the rows changed, 0 when the index is absent or
    unusable (the rebuild that follows a pull derives the column from
    the `sync_pull` event instead)."""
    names = [name for name in filenames if name]
    if not names:
        return 0
    path = index_path(root)
    if not path.exists():
        return 0
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, path)
        changed = 0
        with conn:
            for start in range(0, len(names), _PROVENANCE_BATCH):
                batch = names[start : start + _PROVENANCE_BATCH]
                placeholders = ",".join("?" * len(batch))
                cursor = conn.execute(
                    "UPDATE memories SET verified_locally_at = NULL "
                    f"WHERE filename IN ({placeholders}) "
                    "AND verified_locally_at IS NOT NULL",
                    batch,
                )
                changed += max(cursor.rowcount, 0)
        return changed
    except (sqlite3.Error, ValueError, IndexVersionError, OSError):
        return 0
    finally:
        if conn is not None:
            conn.close()


def provenance_counts(root: Path) -> dict[str, int] | None:
    """`{label: count}` over every indexed row, with `unclassified` for
    NULL labels. The health and doctor surfaces read this. Returns None
    (never raises) when the index is absent or unusable, so a caller can
    tell "no unaccounted memories" from "could not look"."""
    path = index_path(root)
    if not path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, path)
        counts: dict[str, int] = {}
        for row in conn.execute(
            "SELECT provenance, COUNT(*) AS n FROM memories GROUP BY provenance"
        ):
            label = (
                row["provenance"] if row["provenance"] is not None else "unclassified"
            )
            counts[label] = int(row["n"])
        return counts
    except (sqlite3.Error, ValueError, IndexVersionError, OSError):
        return None
    finally:
        if conn is not None:
            conn.close()


def provenance_rows(root: Path, *, label: str) -> list[str] | None:
    """Ids of every row carrying `label`, newest `created` first.

    The health bucket joins these against the memories it has already
    loaded; the doctor check names the first. None (never raises) when
    the index is absent or unusable."""
    path = index_path(root)
    if not path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, path)
        rows = conn.execute(
            "SELECT id FROM memories WHERE provenance = ? ORDER BY created DESC, id",
            (label,),
        ).fetchall()
        return [row["id"] for row in rows]
    except (sqlite3.Error, ValueError, IndexVersionError, OSError):
        return None
    finally:
        if conn is not None:
            conn.close()


def corpus_document_frequencies(
    root: Path,
    terms: Sequence[str],
    *,
    admit: Callable[[list[str], Origin | None], bool],
) -> tuple[int, dict[str, int], dict[str, int]] | None:
    """Document frequencies for `terms` over the collection a search will
    actually rank.

    Returns `(collection_size, body_df, scope_df)` where `body_df` counts
    admitted documents whose BODY carries the term and `scope_df` counts
    admitted documents carrying it in body OR scopes — the two
    denominators `search.compute_idf` builds.

    Why this exists: above `_INDEX_THRESHOLD_DEFAULT` the search path
    hands the ranker at most `_PREFILTER_CAP` candidates, and every one is
    there BECAUSE it matched the query. Deriving df from that pool makes
    the query's own discriminative terms look ubiquitous — df approaches N
    by construction — and Okapi IDF collapses toward zero for exactly the
    terms that should dominate. Measured at 74x on a 608-memory store.

    `admit` is the caller's admission predicate, taking `(scopes, origin)`
    and returning whether that memory survives the search filters. It is
    `search.candidate_admitted` bound to this request's scope / repo /
    worktree filters — the SAME predicate `_filter_candidates` runs.
    Passing it in rather than filtering in SQL is the whole design:
    `repos_match` compares on `(host, owner, name)` and consults
    per-process alternate spellings registered by `origin.capture`, so
    admission is not expressible in SQL, and any index-side approximation
    would quietly disagree with the ranked set for precisely the
    multi-remote stores alternates exist to serve. The columns are read
    OUT and judged in Python; the denominator is provably the ranked set.

    Cost is two FTS lookups per term plus one columnar scan of
    `(id, scopes_json, origin_repo, origin_worktree)`. That scan is
    O(corpus) — measured 3.8 ms at 1K memories, 17 ms at 5K, 71 ms at 20K
    — so it is honestly linear, just with a constant ~3.6 µs/row against
    the per-file open + YAML parse that `load_all` pays for the same
    coverage. 5K is where this module's docstring puts the file-walk
    cliff, so the scan buys the prefilter's coverage back at a small
    fraction of what the prefilter was avoiding.

    If that ever needs to come down: the Python loop exists only because
    `admit` is opaque here. `SELECT DISTINCT origin_repo, origin_worktree`
    is a handful of rows in any real store, so admission could be decided
    once per distinct origin and the row restriction pushed into SQL —
    left undone deliberately, since it only pays above ~20K memories and
    the scope-filter arm would still need the row-wise path.

    Returns None — never raises — when the index is absent, unreadable,
    older than the origin-column schema, or yields an empty collection.
    The caller then keeps pool-derived statistics, which are wrong in the
    way described above but are what shipped, so degradation is never
    worse than the status quo.
    """
    if not terms:
        return None
    path = index_path(root)
    if not path.exists():
        return None
    unique = sorted({t for t in terms if t})
    if not unique:
        return None
    # `_connect` runs `PRAGMA journal_mode = WAL` on open and so raises on
    # a corrupt file — it has to sit INSIDE the guard, or page-level
    # corruption escapes as a DatabaseError to a caller documented never
    # to see one.
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, path)
        admitted: set[str] = set()
        for row in conn.execute(
            "SELECT id, scopes_json, origin_repo, origin_worktree FROM memories"
        ):
            repo = row["origin_repo"]
            worktree = row["origin_worktree"]
            origin = (
                Origin(repo=repo, worktree_root=worktree)
                if (repo is not None or worktree is not None)
                else None
            )
            # An origin carrying only `cwd` reconstructs as None here.
            # That is not a lossy shortcut: `should_include_for_caller`
            # reads only `repo` and `worktree_root`, so the two are
            # indistinguishable to the admission rule.
            try:
                scopes = json.loads(row["scopes_json"])
            except (TypeError, ValueError):
                continue
            if admit(list(scopes), origin):
                admitted.add(row["id"])
        if not admitted:
            return None

        def _ids_matching(match_expr: str) -> set[str]:
            return {
                r["id"]
                for r in conn.execute(
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
            body_hits = _ids_matching(f"body_fts : {quoted}") & admitted
            any_hits = _ids_matching(quoted) & admitted
            if body_hits:
                body_df[term] = len(body_hits)
            if any_hits:
                scope_df[term] = len(any_hits)
        return len(admitted), body_df, scope_df
    except (sqlite3.Error, ValueError, IndexVersionError, OSError):
        # Same posture as every other read helper here: the index is a
        # derived cache, so a failure degrades the ranking rather than
        # failing the search. ValueError is the poisoned-meta class —
        # `_ensure_schema`'s `int()` read of a hand-edited or
        # foreign-tool-written `schema_version` — the same inclusion
        # `status()` and `_open_for_rebuild` carry for it; `scope_counts`
        # and `category_rows` below share this tuple for the same reason.
        return None
    finally:
        if conn is not None:
            conn.close()


def scope_counts(
    root: Path,
    *,
    admit: Callable[[list[str], Origin | None], bool],
) -> tuple[int, dict[str, int]] | None:
    """Per-scope memory counts over the collection a caller can retrieve.

    Returns `(total, {scope: count})` where `total` is the number of
    ADMITTED memories and each count is how many of those carry that
    scope. A memory with three scopes contributes 1 to `total` and 1 to
    each of three scope buckets — the same asymmetry
    `handlers/scope_overview.memory_scope_overview` produces, because
    this is the cheap half of that surface.

    Why it lives here rather than reusing `Store.load_all()`: the
    SessionStart hook (`cli/session_start_cmd.py`) runs on every session
    open and must not pay the per-file open + YAML parse that dominates
    `load_all` (~148 ms on the dogfood store, ~74 % of the equivalent
    handler's cost). This reads the same four columns
    `corpus_document_frequencies` does — `(id, scopes_json, origin_repo,
    origin_worktree)` — in one columnar scan, and never touches a body.

    `admit` is the caller's admission predicate over `(scopes, origin)`,
    bound the same way `corpus_document_frequencies`' is: to
    `search.candidate_admitted` with this caller's repo / worktree
    filters. Judging admission in Python rather than SQL is not an
    optimisation left on the table — `repos_match` consults per-process
    alternate spellings registered by `origin.capture`, so the rule is
    not expressible in SQL, and any index-side approximation would put
    a count on screen that `memory_search` then contradicts. Sharing the
    predicate is what makes "42 memories" here provably the same 42
    `memory_scope_overview` reports.

    UNLIKE `corpus_document_frequencies`, an empty admitted set returns
    `(0, {})` rather than None: there, zero admitted rows means "no
    denominator, fall back"; here it is a true and useful answer ("this
    repo has nothing stored"), and collapsing it into the None channel
    would make the caller unable to tell "nothing here" from "index
    unusable" — the two want opposite handling.

    Returns None — never raises — when the index is absent, unreadable,
    corrupt, or older than the origin-column schema. The caller degrades
    to silence rather than to an expensive `load_all`: a session-start
    hook that stalls the session is worse than one that says nothing.
    """
    path = index_path(root)
    if not path.exists():
        return None
    # `_connect` runs `PRAGMA journal_mode = WAL` on open and so raises
    # on a corrupt file — it sits INSIDE the guard for the same reason
    # `corpus_document_frequencies`' does.
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, path)
        total = 0
        counts: dict[str, int] = {}
        for row in conn.execute(
            "SELECT id, scopes_json, origin_repo, origin_worktree FROM memories"
        ):
            repo = row["origin_repo"]
            worktree = row["origin_worktree"]
            origin = (
                Origin(repo=repo, worktree_root=worktree)
                if (repo is not None or worktree is not None)
                else None
            )
            # An origin carrying only `cwd` reconstructs as None here;
            # `should_include_for_caller` reads only `repo` and
            # `worktree_root`, so the two are indistinguishable to the
            # admission rule.
            try:
                scopes = json.loads(row["scopes_json"])
            except (TypeError, ValueError):
                continue
            scope_list = list(scopes)
            if not admit(scope_list, origin):
                continue
            total += 1
            for scope in scope_list:
                counts[scope] = counts.get(scope, 0) + 1
        return total, counts
    except (sqlite3.Error, ValueError, IndexVersionError, OSError):
        return None
    finally:
        if conn is not None:
            conn.close()


def category_rows(
    root: Path,
    *,
    category: str,
    admit: Callable[[list[str], Origin | None], bool],
) -> list[tuple[str, str]] | None:
    """`(id, filename)` pairs for the ADMITTED rows of one category.

    The standing tier's candidate selector (`cli/session_start_cmd.py`):
    the SessionStart hook needs to know WHICH files hold ambient
    memories for this caller before it pays a parse for any of them, and
    this answers that from the same columnar row `scope_counts` already
    reads plus the `category` and `filename` columns. Bodies stay
    untouched — the caller parses the named files itself, because
    delivery needs fields the index does not carry (`verified_paths`,
    `claims`, the attestation lists that feed the staleness verdict).

    `admit` is the same predicate `scope_counts` takes, bound to
    `search.candidate_admitted` with the caller's repo / worktree
    filters, and judged in Python for the same reason: `repos_match`
    consults per-process alternate spellings, so the rule is not
    expressible in SQL. A row whose `scopes_json` does not parse is
    skipped, exactly as `scope_counts` skips it.

    The category comparison, by contrast, IS done in SQL: `category` is
    a single stored string with no normalisation rule, so there is no
    predicate to share and filtering rows the caller will discard would
    be pure waste. `filename` can be empty on a pre-v2 row — those are
    returned as-is; the caller's index-trust gates (the filename-set
    comparison) run before this and already decline the hint for stores
    where rows cannot name their files.

    Returns None — never raises — when the index is absent, unreadable,
    corrupt, or older than this reader, mirroring `scope_counts`' degrade
    contract: the standing tier goes silent rather than stalling session
    open.
    """
    path = index_path(root)
    if not path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, path)
        out: list[tuple[str, str]] = []
        for row in conn.execute(
            "SELECT id, filename, scopes_json, origin_repo, origin_worktree "
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
            try:
                scopes = json.loads(row["scopes_json"])
            except (TypeError, ValueError):
                continue
            if not admit(list(scopes), origin):
                continue
            out.append((row["id"], row["filename"]))
        return out
    except (sqlite3.Error, ValueError, IndexVersionError, OSError):
        return None
    finally:
        if conn is not None:
            conn.close()


def indexed_ids(root: Path, ids: Sequence[str] | None = None) -> set[str]:
    """Memory ids that currently have a row in the index.

    The identity-level counterpart to `meta.indexed_count`: the count
    answers "how many", this answers "which". The startup divergence
    check (`store._warn_on_index_divergence`) needs the latter, because
    a raw count comparison cannot tell a genuinely-unindexed file from a
    write that is merely in flight — every Store mutator lands the .md
    on disk and commits the index row as two separate steps, so a
    concurrent reader sampling the two counters between them sees a gap
    that does not exist a millisecond later. Comparing id SETS makes the
    gap addressable: the specific ids can be re-checked once the writer
    that owns them has actually finished, instead of blaming the index
    for a snapshot artifact.

    `ids=None` reads every row. Passing a sequence restricts the read to
    that subset (`WHERE id IN (…)`, the same shape `filenames_for_ids`
    uses) and returns the members that have a row — which is what the
    divergence check's per-id recheck wants, since it re-reads one id at
    a time under that memory's file lock and has no use for the rest of
    the table. Unlike `filenames_for_ids` this keeps rows whose
    `filename` column is empty (a pre-v2 row): the question here is
    "does a row exist", not "where does it point".

    Returns an empty set when the index file is absent — the same
    best-effort no-index answer `filenames_for_ids` / `links_for` give.
    SQLite errors propagate; the caller decides how to degrade (the
    divergence check treats an unreadable index as "gap unconfirmed"
    and warns, since it cannot prove the gap is transient)."""
    if ids is not None and not ids:
        return set()
    path = index_path(root)
    if not path.exists():
        return set()
    conn = _connect(path)
    try:
        _ensure_schema(conn, path)
        if ids is None:
            return {row[0] for row in conn.execute("SELECT id FROM memories")}
        placeholders = ",".join("?" * len(ids))
        return {
            row[0]
            for row in conn.execute(
                f"SELECT id FROM memories WHERE id IN ({placeholders})",
                list(ids),
            )
        }
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
    `_handlers.load_search_candidates` uses)."""
    path = index_path(root)
    if not path.exists():
        return [], []
    conn = _connect(path)
    try:
        _ensure_schema(conn, path)
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


def links_for_many(
    root: Path, memory_ids: Iterable[str]
) -> tuple[
    dict[
        str, tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]]
    ],
    bool,
]:
    """Bulk `links_for`: resolve outbound + inbound links for many ids over a
    SINGLE index connection. Returns ``(links_map, needs_rebuild)``.

    `attach_link_annotations` (the supersedes/contradicts search activation)
    runs on every hit-producing search and is NOT config-gated, so a naive
    per-hit `links_for` opens the index file once per hit (up to `max_results`,
    50): connect + PRAGMAs + a chmod-stat of the .db/-wal/-shm siblings + a full
    schema-ensure, times N. This folds all of it into one open and two
    ``IN (...)`` queries — mirroring how the other ``attach_*`` helpers resolve
    everything from a single already-paid load instead of churning the index.

    `links_map` maps every requested id to ``(outbound, inbound)``; an id with
    no links maps to ``([], [])``. Tuple shapes and per-id ordering match
    `links_for` exactly (outbound ``(type, target_id, note)``, inbound
    ``(type, source_id, note)``). An absent / empty index file maps every id to
    ``([], [])`` — the same best-effort no-op `links_for` returns.

    `needs_rebuild` mirrors `links_for_with_status`: the meta flag read on the
    SAME connection the link queries already hold, not via a second
    `status()` open. True between a schema-version migration and the next
    successful `rebuild()` — the window where `memory_links` holds rows only
    for memories touched since the migration, so `links_map` may be silently
    missing edges from every untouched legacy source (including the inbound
    `supersedes` edge the annotation surface exists to warn about). A
    flag-set answer must not be trusted as complete; the caller falls back
    to scanning its already-loaded candidates. Reported False when the index
    file is absent (nothing was migrated; the empty map IS the correct
    no-index answer) and on the empty-ids short-circuit (no connection is
    opened to read it)."""
    ids = list(dict.fromkeys(memory_ids))
    out: dict[
        str,
        tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]],
    ] = {mid: ([], []) for mid in ids}
    if not ids:
        return out, False
    path = index_path(root)
    if not path.exists():
        return out, False
    placeholders = ",".join("?" * len(ids))
    conn = _connect(path)
    try:
        _ensure_schema(conn, path)
        for row in conn.execute(
            "SELECT source_id, type, target_id, note FROM memory_links "
            f"WHERE source_id IN ({placeholders}) ORDER BY source_id, type, target_id",
            ids,
        ).fetchall():
            out[row["source_id"]][0].append(
                (row["type"], row["target_id"], row["note"])
            )
        for row in conn.execute(
            "SELECT target_id, type, source_id, note FROM memory_links "
            f"WHERE target_id IN ({placeholders}) ORDER BY target_id, type, source_id",
            ids,
        ).fetchall():
            out[row["target_id"]][1].append(
                (row["type"], row["source_id"], row["note"])
            )
        rebuild_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'needs_rebuild'"
        ).fetchone()
        return out, bool(rebuild_row and rebuild_row[0] == "1")
    finally:
        conn.close()


def links_for_with_status(
    root: Path, memory_id: str
) -> tuple[
    list[tuple[str, str, str | None]],
    list[tuple[str, str, str | None]],
    int,
    bool,
    str | None,
    str | None,
]:
    """Like `links_for`, but also returns `meta.indexed_count`, the
    `meta.needs_rebuild` flag and the row's trust facts, all read on
    the SAME connection. Returns `(outbound, inbound, indexed_count,
    needs_rebuild, provenance, verified_locally_at)`.

    `provenance` is the row's index-resident label (schema v7), or None
    when the file is absent, the row is missing, or no rebuild has
    classified it yet; `verified_locally_at` (schema v8) is the row's
    local verification stamp on the same terms. They ride this open so
    `memory_show` carries them without a third connection: the two-open
    guard in tests/test_server_links.py counts every `_connect` on the
    no-inbound path, and a separate `provenance_for` call tripped it.

    The handler (`_links_payload`) needs three facts to build
    `reverse_links` correctly: this id's inbound links, whether the
    index is populated at all, AND whether a schema migration left it
    rebuild-pending. The naive shape — `links_for(...)` then a
    separate `status(...)` — opens the index file twice (two
    `_connect` + `_ensure_schema` round-trips on the same DB), and the
    second open fires on the COMMON case: any `memory_show` of a memory
    with no inbound links, even against a perfectly healthy populated
    index. Folding both meta reads into the single open `links_for`
    already holds removes that second connection on the hot path.

    `indexed_count` is 0 when the index is absent, empty (the
    post-`SCHEMA_VERSION`-bump rebuild window), or otherwise can't
    answer — every state where the handler should fall back to
    `load_all`. When the file is absent we short-circuit before opening
    anything and report `(…, 0, False)`, mirroring `status()`'s
    absent-file branch.

    `needs_rebuild` is True between a schema-version migration and the
    next successful `rebuild()`: the incremental Store hooks repopulate
    only touched memories, so `indexed_count` can climb back above zero
    while untouched legacy sources' `memory_links` rows are still
    missing. The handler must treat flag-set exactly like a zero count
    — the same unusable-index routing
    `_handlers.load_search_candidates` applies (via `status()`) on the
    search surface.

    A populated-but-no-inbound id (the common case) returns
    `([], [], n>0, False)`: empty inbound, but a non-zero count with
    the flag clear tells the handler the index IS usable, so it returns
    empty reverse_links with NO fallback scan."""
    path = index_path(root)
    if not path.exists():
        return [], [], 0, False, None, None
    conn = _connect(path)
    try:
        _ensure_schema(conn, path)
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
        count_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'indexed_count'"
        ).fetchone()
        indexed_count = int(count_row[0]) if count_row else 0
        rebuild_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'needs_rebuild'"
        ).fetchone()
        trust_row = conn.execute(
            "SELECT provenance, verified_locally_at FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        return (
            [(row["type"], row["target_id"], row["note"]) for row in outbound],
            [(row["type"], row["source_id"], row["note"]) for row in inbound],
            indexed_count,
            bool(rebuild_row and rebuild_row[0] == "1"),
            trust_row[0] if trust_row else None,
            trust_row[1] if trust_row else None,
        )
    finally:
        conn.close()


def status(root: Path) -> dict[str, Any]:
    """Diagnostic snapshot of the index file. Used by
    `bettermemory doctor` to surface index health and by the reindex
    CLI to report before/after counts. Never raises — a missing index
    file returns `exists=False`; a corrupt, version-skewed, or
    unreadable one returns the degraded `corrupt=True` shape."""
    path = index_path(root)
    if not path.exists():
        return {"exists": False, "path": str(path)}
    try:
        conn = _connect(path)
        try:
            _ensure_schema(conn, path)
            count_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'indexed_count'"
            ).fetchone()
            schema_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            rebuild_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'needs_rebuild'"
            ).fetchone()
            failure_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_rebuild_failure'"
            ).fetchone()
            size_bytes = path.stat().st_size
            return {
                "exists": True,
                "path": str(path),
                "schema_version": int(schema_row[0]) if schema_row else None,
                "indexed_count": int(count_row[0]) if count_row else 0,
                # True between a schema-version migration and the next
                # successful `rebuild()`: the data tables were dropped
                # empty and only incrementally-touched memories are back,
                # so readers must treat the index as unusable no matter
                # what `indexed_count` says.
                "needs_rebuild": bool(rebuild_row and rebuild_row[0] == "1"),
                # Wall-clock (`time.time()`) of the last FAILED
                # construction-time auto-rebuild attempt, or None. Parsed
                # defensively OUTSIDE the corruption tuple below: the
                # marker is advisory backoff state — a garbage value
                # means "no usable marker", never "corrupt index".
                "last_rebuild_failure": _parse_failure_marker(failure_row),
                "size_bytes": size_bytes,
            }
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.DatabaseError, IndexVersionError) as exc:
        # OSError is load-bearing for the never-raises contract:
        # `_connect`'s `path.parent.mkdir` can raise EACCES/EROFS, and
        # `path.stat()` raises FileNotFoundError when a concurrent
        # rebuild-recovery unlinks the file between the exists() check
        # above and the stat. ValueError is too: a hand-edited or
        # foreign-tool-written meta row with a non-integer
        # schema_version / indexed_count fails the `int()` reads (in
        # `_ensure_schema`'s version check and in the dict build above)
        # — unparseable meta IS corruption. Every caller treats this
        # degraded shape as "index unusable — fall back / suggest
        # reindex", which is the right answer mid-recovery too.
        return {
            "exists": True,
            "path": str(path),
            "corrupt": True,
            "error": str(exc),
        }


def flag_needs_rebuild(root: Path) -> bool:
    """Mark the index stale so search routes to `load_all` until a rebuild.

    For bulk paths that rewrite `.md` files directly instead of going
    through a `Store` mutator — `migrate` is the one in tree. Those writes
    leave the index holding pre-migration frontmatter with no signal: the
    S4 divergence check compares COUNTS, and a migration that rewrites
    scopes or an origin block in place does not change the count, so the
    skew is invisible to it.

    Setting the flag is the conservative lever rather than a per-file
    upsert: it is the same mechanism `_ensure_schema` uses for a version
    bump, it fails SAFE (search falls back to the full scan, which is
    slower but correct), and `Store.__post_init__`'s auto-rebuild clears
    it on the next construction without the user doing anything.

    Returns True when the flag landed. Best-effort and never raises: an
    unwritable or absent index is already the degraded case, and a bulk
    migration must not fail because a derived cache could not be marked.
    """
    path = index_path(root)
    if not path.exists():
        return False
    try:
        conn = _connect(path)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) "
                    "VALUES ('needs_rebuild', '1')"
                )
            return True
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def record_rebuild_failure(root: Path) -> None:
    """Best-effort cross-process marker for the construction-time
    auto-rebuild backoff: stamp the wall-clock of a FAILED attempt in
    meta so the NEXT process (every CLI invocation constructs a fresh
    Store) can skip re-running a full-store rebuild that just failed.

    Best-effort by design: the most common persistent failure cause is
    an unwritable index, in which case this write fails too and the
    in-process memo in `store._rebuild_index_if_flagged` remains the
    only (per-process) backoff floor. Never raises — the caller is
    already unwinding the rebuild's real error, which must win.
    Cleared transactionally by `_rebuild_data` on the next SUCCESSFUL
    rebuild, alongside `needs_rebuild`. `bettermemory reindex` calls
    `rebuild()` directly and never consults the marker."""
    try:
        conn = _connect(index_path(root))
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) "
                    "VALUES ('last_rebuild_failure', ?)",
                    (str(time.time()),),
                )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — advisory marker; never mask the rebuild error
        pass


def _parse_failure_marker(row: Any) -> float | None:
    """`meta.last_rebuild_failure` → float timestamp, or None when the
    row is absent or unparseable. Deliberately NOT routed through
    `status()`'s corruption tuple: the marker is advisory, so garbage
    degrades to "no marker" rather than tainting the whole snapshot."""
    if not row:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _upsert_memory(
    conn: sqlite3.Connection,
    memory: Memory,
    filename: str,
    *,
    provenance: str | None = None,
    verified_locally_at: str | None = None,
    content_sha256: str | None = None,
) -> None:
    """INSERT OR REPLACE on the id key. Trigger logic keeps the FTS
    virtual table in sync — the AFTER UPDATE trigger handles the
    delete-then-insert dance internally so callers don't have to.

    The only row writer. A plain-INSERT sibling used to serve `rebuild`
    on the premise that a freshly truncated table cannot collide; that
    premise was false — two active `.md` files can carry the same id
    (a conflicted copy, a `cp` before a hand-edit) — and the resulting
    `IntegrityError` cost the store its index. Upserting everywhere
    means the rebuild collapses duplicate ids exactly as the
    incremental path and `scan_active_memory_ids` already do.

    `filename` is the actual on-disk filename — the caller threads it
    through from the path it just wrote. The store's collision suffix
    (`<slug>-<short_id>.md`) means we can't re-derive this from the
    Memory fields alone, and getting it wrong points `filenames_for_ids`
    at the wrong file.

    `provenance` None is "no claim", and the conflict clause honours it
    with COALESCE: an update on an already-labelled row keeps the label,
    because how a memory entered the store does not change when its
    body is edited. A label the caller does vouch for replaces whatever
    was there (a rebuild reclassifying, or the Store stamping `local`
    on a restore).

    `verified_locally_at` (schema v8) takes the same COALESCE: None keeps
    the row's stamp, a value replaces it. Only `Store.mark_verified`
    passes one on the incremental path; clearing is a separate,
    deliberate UPDATE (`clear_local_verification`), never an upsert with
    None."""
    conn.execute(
        "INSERT INTO memories("
        "id, created, updated, last_verified_at, confidence, category, "
        "body, body_fts, scopes_text, scopes_fts, scopes_json, filename, "
        "origin_repo, origin_worktree, provenance, verified_locally_at, "
        "content_sha256, verified_head) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "created = excluded.created, "
        "updated = excluded.updated, "
        "last_verified_at = excluded.last_verified_at, "
        "confidence = excluded.confidence, "
        "category = excluded.category, "
        "body = excluded.body, "
        "body_fts = excluded.body_fts, "
        "scopes_text = excluded.scopes_text, "
        "scopes_fts = excluded.scopes_fts, "
        "scopes_json = excluded.scopes_json, "
        "filename = excluded.filename, "
        # Must be in the update set, not just the insert: `migrate origin
        # --repair` exists specifically to rewrite origins captured wrong,
        # and an upsert that left these stale would keep filtering search
        # admission on the pre-repair value.
        "origin_repo = excluded.origin_repo, "
        "origin_worktree = excluded.origin_worktree, "
        "provenance = COALESCE(excluded.provenance, memories.provenance), "
        "verified_locally_at = COALESCE(excluded.verified_locally_at, "
        "memories.verified_locally_at), "
        "content_sha256 = COALESCE(excluded.content_sha256, "
        "memories.content_sha256), "
        # Straight off the record, never COALESCEd: a body edit clears the
        # anchor with the stamp, and the row has to say so.
        "verified_head = excluded.verified_head",
        (
            memory.id,
            memory.created.isoformat(),
            memory.updated.isoformat(),
            _isoformat_optional(memory.last_verified_at),
            memory.confidence.value,
            memory.category.value if memory.category is not None else None,
            memory.body,
            fts_index_text(memory.body),
            _scopes_text(memory.scopes),
            fts_index_text(" ".join(memory.scopes)),
            json.dumps(memory.scopes),
            filename,
            memory.origin.repo if memory.origin else None,
            memory.origin.worktree_root if memory.origin else None,
            provenance,
            verified_locally_at,
            content_sha256,
            memory.verified_head,
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
    plain list with no dedup (the model only rejects self-links and
    caps the list length), and the reverse index has to mirror it.

    `INSERT OR IGNORE` cannot be relied on to collapse exact-duplicate
    link lines on its own: SQLite treats NULL as DISTINCT in a primary
    key, and `MemoryLink.note` defaults to None — the common case — so
    two identical note=NULL links would each satisfy the PK and produce
    two identical rows. We therefore pre-dedup over the full key tuple
    `(source_id, type, target_id, note)` with a seen-set before insert:
    exact-duplicate lines (a redundant row on disk too, never a distinct
    note) collapse to one row regardless of whether `note` is NULL,
    while links that share `(type, target_id)` but differ in `note`
    are kept distinct."""
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
        "INSERT OR IGNORE INTO memory_links("
        "source_id, type, target_id, note) VALUES (?, ?, ?, ?)",
        rows,
    )


def _stash_provenance(conn: sqlite3.Connection) -> None:
    """Park every classified label in `meta.provenance_carry` before a
    schema or tokenizer drop empties the table.

    Skipped when the table predates the column (the 6 -> 7 upgrade has
    nothing to carry) or holds no labels. Its own committed transaction:
    the drop that follows runs in a separate `BEGIN IMMEDIATE` script,
    and a stash that outlives an aborted drop is reconciled by
    `_read_prior_provenance` (the table wins over the stash)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    stashed = False
    if "provenance" in columns:
        rows = conn.execute(
            "SELECT id, provenance FROM memories WHERE provenance IS NOT NULL"
        ).fetchall()
        if rows:
            payload = json.dumps(
                {row[0]: row[1] for row in rows}, separators=(",", ":")
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) "
                "VALUES ('provenance_carry', ?)",
                (payload,),
            )
            stashed = True
    # Schema v8: the local-verification stamps ride the same drop under
    # their own key, so a version bump cannot turn every synced memory's
    # local stamp back into a remote one.
    if "verified_locally_at" in columns:
        rows = conn.execute(
            "SELECT id, verified_locally_at FROM memories "
            "WHERE verified_locally_at IS NOT NULL"
        ).fetchall()
        if rows:
            payload = json.dumps(
                {row[0]: row[1] for row in rows}, separators=(",", ":")
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('trust_carry', ?)",
                (payload,),
            )
            stashed = True
    if "content_sha256" in columns:
        rows = conn.execute(
            "SELECT id, content_sha256 FROM memories WHERE content_sha256 IS NOT NULL"
        ).fetchall()
        if rows:
            payload = json.dumps(
                {row[0]: row[1] for row in rows}, separators=(",", ":")
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) "
                "VALUES ('content_sha_carry', ?)",
                (payload,),
            )
            stashed = True
    if stashed:
        conn.commit()


def _read_prior_content_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    """The content hashes this index carried before a rebuild: the
    `meta.content_sha_carry` stash (parked ahead of a drop) under the
    live column, the live table winning. Only well-formed hex survives,
    for the reason `_read_prior_provenance` drops unknown labels."""
    prior: dict[str, str] = {}
    stash = conn.execute(
        "SELECT value FROM meta WHERE key = 'content_sha_carry'"
    ).fetchone()
    if stash is not None:
        try:
            parked = json.loads(stash[0])
        except (TypeError, ValueError):
            parked = None
        if isinstance(parked, dict):
            for memory_id, sha in parked.items():
                if isinstance(memory_id, str) and _is_sha256_hex(sha):
                    prior[memory_id] = sha
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    if "content_sha256" in columns:
        for row in conn.execute(
            "SELECT id, content_sha256 FROM memories WHERE content_sha256 IS NOT NULL"
        ):
            if _is_sha256_hex(row[1]):
                prior[row[0]] = row[1]
    return prior


def _is_sha256_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def file_sha256(path: Path) -> str:
    """SHA-256 of a memory file's bytes — the same digest the store
    records at each write, computed by the doctor and the rebuild."""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def carried_content_sha256(label: str | None, prior: str | None, current: str) -> str:
    """What a rebuild stamps for a file: the hash it computes now, unless
    the index recorded a different one for a file no pull brought down —
    then the RECORDED hash stays, and the mismatch with the file is the
    evidence `doctor` reports. A `synced` row is anchored at the pull:
    the pull is the recorded path by which its bytes changed."""
    if prior is not None and prior != current and label != "synced":
        return prior
    return current


def _read_prior_provenance(conn: sqlite3.Connection) -> tuple[dict[str, str], bool]:
    """The labels this index carried before a rebuild, and whether a
    classified rebuild has completed on it before.

    Merges the `meta.provenance_carry` stash (labels parked ahead of a
    drop) under the live table's labels, so a row the hooks stamped
    after the drop wins over the parked copy. Labels outside the known
    set are dropped rather than carried: the stash is written by this
    module, but `meta` is a plain table and the read must not trust it
    blindly."""
    from . import provenance as _provenance

    prior: dict[str, str] = {}
    stash = conn.execute(
        "SELECT value FROM meta WHERE key = 'provenance_carry'"
    ).fetchone()
    if stash is not None:
        try:
            parked = json.loads(stash[0])
        except (TypeError, ValueError):
            parked = None
        if isinstance(parked, dict):
            for memory_id, label in parked.items():
                if isinstance(memory_id, str) and label in _provenance.LABELS:
                    prior[memory_id] = label
    for row in conn.execute(
        "SELECT id, provenance FROM memories WHERE provenance IS NOT NULL"
    ):
        if row[1] in _provenance.LABELS:
            prior[row[0]] = row[1]
    baseline = (
        conn.execute(
            "SELECT value FROM meta WHERE key = 'provenance_classified'"
        ).fetchone()
        is not None
    )
    return prior, baseline


def _read_prior_trust(conn: sqlite3.Connection) -> dict[str, str]:
    """The `verified_locally_at` stamps this index carried before a
    rebuild: the `meta.trust_carry` stash (parked ahead of a drop) under
    the live column, the live table winning. Values that do not parse
    as timestamps are dropped, for the same reason
    `_read_prior_provenance` drops unknown labels."""
    from .time_utils import parse_event_ts

    prior: dict[str, str] = {}
    stash = conn.execute("SELECT value FROM meta WHERE key = 'trust_carry'").fetchone()
    if stash is not None:
        try:
            parked = json.loads(stash[0])
        except (TypeError, ValueError):
            parked = None
        if isinstance(parked, dict):
            for memory_id, stamp in parked.items():
                if (
                    isinstance(memory_id, str)
                    and isinstance(stamp, str)
                    and parse_event_ts(stamp) is not None
                ):
                    prior[memory_id] = stamp
    for row in conn.execute(
        "SELECT id, verified_locally_at FROM memories "
        "WHERE verified_locally_at IS NOT NULL"
    ):
        if isinstance(row[1], str) and parse_event_ts(row[1]) is not None:
            prior[row[0]] = row[1]
    return prior


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
    "TOKENIZER_FINGERPRINT",
    "IndexVersionError",
    "filenames_for_ids",
    "index_path",
    "indexed_ids",
    "links_for",
    "links_for_with_status",
    "provenance_counts",
    "provenance_for",
    "provenance_rows",
    "query",
    "rebuild",
    "remove",
    "status",
    "upsert",
]
