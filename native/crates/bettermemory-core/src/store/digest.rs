//! The store's length digest (`store_digest`), the parts both builds share:
//! its tables' schema text, the log kinds it follows, the BM25 length of a
//! record's body, and the build a new store is born with (`digest_mark` on
//! a store with no records: the tables made, the state naming the rules
//! and the log's last record mutation, none).
//!
//! The reading side (bringing a digest forward over the log in memory) is
//! [`super::pool`]'s, which takes each record's length from [`bm25_length`].
//! Not ported: a record's other digest columns (`n`, `claim`, `stamped`,
//! `sha`, `tokens`) and the postings, which read the supersession rules;
//! a native create indexes no record, so it writes none.

use rusqlite::Connection;

use crate::lexicon;

/// `store_digest._KINDS`: the log kinds whose last row the digest follows.
pub const DIGEST_KINDS: [&str; 4] = [
    "memory_put",
    "memory_delete",
    "tombstone_put",
    "tombstone_delete",
];

/// `store_digest._SCHEMA`, byte for byte (pinned to `store_create.json`).
pub const DIGEST_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS digest_records (
    doc INTEGER PRIMARY KEY,
    removed INTEGER NOT NULL DEFAULT 0,
    id TEXT NOT NULL DEFAULT '',
    rid INTEGER NOT NULL DEFAULT 0,
    n INTEGER NOT NULL DEFAULT 0,
    claim INTEGER NOT NULL DEFAULT 0,
    stamped INTEGER NOT NULL DEFAULT 0,
    dl INTEGER NOT NULL DEFAULT 0,
    client TEXT,
    model TEXT,
    category TEXT,
    sha TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '[]',
    cwd TEXT NOT NULL DEFAULT '',
    tokens TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS digest_records_by_id ON digest_records(removed, id);
CREATE INDEX IF NOT EXISTS digest_records_by_category
    ON digest_records(removed, category);
CREATE TABLE IF NOT EXISTS digest_tokens (
    token TEXT NOT NULL DEFAULT '',
    doc INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token, doc)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS digest_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
";

/// `store_digest.fingerprint()` under the reference this crate reproduces
/// (Unicode 15.1.0's tokenizer, the supersession rules of this revision):
/// the rules a digest row was computed under. Not computed here (it reads
/// the supersession rules, which are not ported); pinned to
/// `store_create.json`'s `digest_fingerprint`.
pub const DIGEST_FINGERPRINT: &str =
    "59e8e47cd83bbb95543f0def4de2f4eee3f2c4abaa5cabe2c93b68c5b747687f";

/// `_features(body).dl`: the length of the token stream BM25 scores
/// (compounds beside their parts, stopwords out, repeats counted). The one
/// spelling of a record's digest length, for the reader's catch-up
/// ([`super::pool`]) and any writer.
pub fn bm25_length(body: &[u32]) -> usize {
    let stream = lexicon::tokenize(body);
    lexicon::strip_stopwords(&lexicon::expand_kebab(&stream)).len()
}

/// `_build` and the `digest_mark` that follows it, on a store with no
/// record, inside the caller's transaction: the three tables dropped and
/// made again, then the state `fingerprint`, `seq` and `mac` of the log's
/// last record mutation (none: `0` and the empty MAC). The reference names
/// its build in the state (`build`, a random token) and removes it at the
/// end, in transactions of their own; the rows left are these.
pub fn build_empty(conn: &Connection) -> rusqlite::Result<()> {
    for table in ["digest_records", "digest_tokens", "digest_state"] {
        conn.execute(&format!("DROP TABLE IF EXISTS {table}"), [])?;
    }
    for statement in DIGEST_SCHEMA.split(';') {
        if !statement.trim().is_empty() {
            conn.execute(statement, [])?;
        }
    }
    let mut insert =
        conn.prepare("INSERT OR REPLACE INTO digest_state(key, value) VALUES (?, ?)")?;
    insert.execute(["fingerprint", DIGEST_FINGERPRINT])?;
    insert.execute(["seq", "0"])?;
    insert.execute(["mac", ""])?;
    Ok(())
}
