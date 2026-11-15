//! The SQLite version spike: `search.jsonl`'s `fts` field was recorded
//! under the reference's SQLite (3.50.4); the crate bundles its own. Every
//! query's FTS5 prefilter (`fts_sql` over a `memories_fts` table built as
//! the generator builds it) must match: the rows, their order, and each
//! `bm25()` score's bits. A difference is a failure, not a tolerance.

mod common;

use bettermemory_core::lexicon::{fts_index_text, fts_match_query};
use bettermemory_core::store::sqlite_version;
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;

#[test]
fn fts5_prefilter_matches_the_reference_sqlite() {
    let records = jsonl("search.jsonl");
    let header = &records[0];
    let conn = rusqlite::Connection::open_in_memory().expect("memory");
    conn.execute_batch(
        "CREATE VIRTUAL TABLE memories_fts USING fts5(body_fts, scopes_fts, tokenize='unicode61')",
    )
    .expect("fts5");
    for m in header["memories"].as_array().expect("memories") {
        let body = m["body"].as_str().expect("body");
        let scopes: Vec<&str> = m["scopes"]
            .as_array()
            .expect("scopes")
            .iter()
            .map(|s| s.as_str().expect("scope"))
            .collect();
        let index_text = fts_index_text(&unicode::cps(body));
        assert_eq!(index_text, m["index_text"].as_str().expect("index_text"));
        conn.execute(
            "INSERT INTO memories_fts(rowid, body_fts, scopes_fts) VALUES (?, ?, ?)",
            rusqlite::params![
                m["rowid"].as_i64().expect("rowid"),
                index_text,
                fts_index_text(&unicode::cps(&scopes.join(" "))),
            ],
        )
        .expect("insert");
    }
    let sql = header["fts_sql"].as_str().expect("fts_sql");
    let mut checked = 0;
    let mut rows_compared = 0;
    // bm25 bits are one platform's (class iii): set aside, named, where the
    // file was made elsewhere.
    let mut ledger = Ledger::new(
        "fts5_prefilter_matches_the_reference_sqlite",
        "search.jsonl",
    );
    for record in &records[1..] {
        let query = record["query"].as_str().expect("query");
        let match_query = fts_match_query(&unicode::cps(query));
        let mut case_rows = 0;
        ledger.case(query, || {
            assert_eq!(
                match_query,
                record["match_query"].as_str().expect("mq"),
                "{query}"
            );
            let got: Vec<(i64, String)> = if match_query.is_empty() {
                Vec::new()
            } else {
                let mut statement = conn.prepare(sql).expect("prepare");
                statement
                    .query_map([&match_query], |r| {
                        Ok((
                            r.get::<_, i64>(0)?,
                            format!("{:016x}", r.get::<_, f64>(1)?.to_bits()),
                        ))
                    })
                    .expect("query")
                    .map(|r| r.expect("row"))
                    .collect()
            };
            let want: Vec<(i64, String)> = record["fts"]
                .as_array()
                .expect("fts")
                .iter()
                .map(|r| {
                    (
                        r["rowid"].as_i64().expect("rowid"),
                        r["bits"].as_str().expect("bits").to_owned(),
                    )
                })
                .collect();
            assert_eq!(got, want, "{query}: rows, order or score bits differ");
            case_rows = want.len();
        });
        rows_compared += case_rows;
        checked += 1;
    }
    eprintln!(
        "SQLite {}: {checked} queries, {rows_compared} rows, every rowid, order and bm25 bit equal",
        sqlite_version()
    );
    assert_eq!(checked, records.len() - 1, "search.jsonl fts");
    ledger.finish(checked);
}
