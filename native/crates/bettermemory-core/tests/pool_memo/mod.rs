//! Stores for the pool's memo tests: `store_files.jsonl`'s signed
//! ``intact`` base with its memory rows replaced by copies of its first
//! record, each with a row number, an id, a body and scopes of its own,
//! written by a plain connection (the index follows by the table's
//! triggers; no log row is appended), and the digest gone, current over
//! the copies, or current over the base's own records only (so the copies
//! are rows the memo's inputs do not hold).

#![allow(dead_code)]

use std::path::PathBuf;

use bettermemory_core::lexicon;
use bettermemory_core::store::digest;
use bettermemory_core::unicode;
use rusqlite::Connection;

use crate::common::stores;

/// What the digest holds once the copies are written.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Digest {
    /// No digest state: the memo reads the memory rows, with no lengths.
    Gone,
    /// The base's two records only, current to the log: every copy is a
    /// row the memo's inputs do not hold.
    Base,
    /// A row per copy, current to the log, with a BM25 length.
    Copies,
}

pub struct PoolStore {
    pub dir: stores::Scratch,
    pub file: PathBuf,
}

/// The body copy `k` holds: the word every copy shares, and its own.
pub fn body(k: i64) -> String {
    format!("postgres note w{k}")
}

/// Copy `k`'s id: a ULID's 26 characters, ordered as `k`.
pub fn id(k: i64) -> String {
    format!("01M7GGTCEE{k:016}")
}

/// The base with `rows` copies (none: the base's own two records, its
/// digest as `held` says): copy `k` (from 1) at row number `first + k - 1`,
/// id [`id`] (so the newest order, `updated` being equal, is the copies by
/// descending `k`), scopes `scopes(k)` (a list of words).
pub fn store(
    rows: i64,
    first: i64,
    scopes: &dyn Fn(i64) -> Vec<String>,
    held: Digest,
) -> PoolStore {
    let (dir, _) = stores::case_store("intact");
    let file = dir.0.join("memory.sqlite");
    let conn = Connection::open(&file).expect("open");
    conn.execute_batch("BEGIN IMMEDIATE").expect("begin");
    if held != Digest::Gone {
        digest::ingest(&conn, 0).expect("ingest");
    }
    let columns: Vec<String> = conn
        .prepare("SELECT name FROM pragma_table_info('memories') ORDER BY cid")
        .expect("columns")
        .query_map([], |r| r.get::<_, String>(0))
        .expect("columns")
        .map(|c| c.expect("a column"))
        .collect();
    if rows == 0 {
        // No copy: the base's own two records stay.
        if held == Digest::Gone {
            conn.execute_batch("DELETE FROM digest_state")
                .expect("gone");
        }
        conn.execute_batch("COMMIT").expect("commit");
        return PoolStore { dir, file };
    }
    conn.execute_batch(
        "CREATE TEMP TABLE base AS SELECT * FROM memories \
         WHERE rowid = (SELECT MIN(rowid) FROM memories);
         DELETE FROM memories; DELETE FROM homes;",
    )
    .expect("the base");
    let set = [
        "rowid",
        "id",
        "body",
        "body_fts",
        "scopes_json",
        "scopes_text",
        "scopes_fts",
    ];
    let kept: Vec<&String> = columns
        .iter()
        .filter(|c| !set.contains(&c.as_str()))
        .collect();
    let insert = format!(
        "INSERT INTO memories ({set}, {kept}) SELECT ?, ?, ?, ?, ?, ?, ?, {kept} FROM base",
        set = set.join(", "),
        kept = kept
            .iter()
            .map(|c| c.as_str())
            .collect::<Vec<_>>()
            .join(", ")
    );
    let mut statement = conn.prepare(&insert).expect("insert");
    for k in 1..=rows {
        let words = scopes(k);
        let json = serde_json::to_string(&words).expect("scopes");
        let text = format!(" {} ", words.join(" "));
        statement
            .execute(rusqlite::params![
                first + k - 1,
                id(k),
                body(k),
                lexicon::fts_index_text(&unicode::cps(&body(k))),
                json,
                text,
                words.join(" "),
            ])
            .expect("a copy");
    }
    drop(statement);
    match held {
        Digest::Gone => conn
            .execute_batch("DELETE FROM digest_state")
            .expect("gone"),
        Digest::Base => {}
        Digest::Copies => {
            conn.execute_batch("DELETE FROM digest_records")
                .expect("records");
            conn.execute(
                "INSERT INTO digest_records (id, rid, scopes, dl) \
                 SELECT id, rowid, scopes_json, 3 FROM memories",
                [],
            )
            .expect("digest rows");
        }
    }
    conn.execute_batch("COMMIT").expect("commit");
    drop(conn);
    PoolStore { dir, file }
}

/// The row numbers whose scopes hold `wanted`, in the index's order for
/// `query` (its match query; `bm25()`, then row number) or newest first
/// for `None`, read by a plain connection.
pub fn expected(file: &std::path::Path, query: Option<&str>, wanted: &str) -> Vec<i64> {
    let conn = Connection::open(file).expect("open");
    let sql = match query {
        Some(_) => {
            "SELECT m.rowid, m.scopes_json FROM memories_fts f JOIN memories m ON m.rowid = f.rowid \
             WHERE memories_fts MATCH ?1 ORDER BY bm25(memories_fts), f.rowid"
        }
        None => "SELECT rowid, scopes_json FROM memories WHERE ?1 IS NULL \
                 ORDER BY updated DESC, id DESC",
    };
    let mut statement = conn.prepare(sql).expect("rows");
    let query = query.map(|q| lexicon::fts_match_query(&unicode::cps(q)));
    let rows = statement
        .query_map([query], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
        })
        .expect("rows");
    rows.map(|r| r.expect("a row"))
        .filter(|(_, scopes)| {
            serde_json::from_str::<Vec<String>>(scopes)
                .expect("scopes")
                .iter()
                .any(|s| s == wanted)
        })
        .map(|(rid, _)| rid)
        .collect()
}
