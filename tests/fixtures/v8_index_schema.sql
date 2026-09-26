
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
    verified_head TEXT,
    -- Schema v11. The writing request's declared identity, straight off
    -- `Memory.actor`; NULL when the writer declared nothing. NULL is the
    -- honest answer and never an empty string: an `Actor` with nothing
    -- set serialises to `{}` and the writers drop the block entirely, so
    -- "wrote no name" and "wrote the empty name" are the same state and
    -- neither one matches a filter.
    actor_client TEXT,
    actor_model TEXT
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
