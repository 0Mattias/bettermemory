//! Hostile input for the store's document frequency source, over a store
//! of the reference's admission columns made by hand (no digest, so every
//! read falls back to the memory rows): 20,000 rows and 4,097 terms in
//! time linear in the rows, eight threads on one memo over one file, a
//! scope list nested 100,000 deep, and row numbers at the ends of i64.
//! Runs everywhere.

mod timing;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use bettermemory_core::pyvalue::PyError;
use bettermemory_core::store::docfreq::{DocFreqError, DocFreqMemo};
use bettermemory_core::store::docfreq_store::{self, StoreDocFreq};
use rusqlite::Connection;

const SCHEMA: &str = "
CREATE TABLE memories (id TEXT PRIMARY KEY, scopes_json TEXT NOT NULL,
    actor_client TEXT, actor_model TEXT, body_fts TEXT, scopes_fts TEXT);
CREATE VIRTUAL TABLE memories_fts USING fts5(body_fts, scopes_fts);
CREATE TABLE homes (record_id TEXT PRIMARY KEY, project TEXT NOT NULL);
CREATE TABLE log (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, mac TEXT NOT NULL);
";

fn put(conn: &Connection, rid: i64, scopes: &str, body: &str) {
    conn.execute(
        "INSERT INTO memories (rowid, id, scopes_json, body_fts, scopes_fts) \
         VALUES (?, ?, ?, ?, ?)",
        rusqlite::params![rid, format!("m{rid}"), scopes, body, scopes],
    )
    .expect("put");
    conn.execute(
        "INSERT INTO memories_fts (rowid, body_fts, scopes_fts) VALUES (?, ?, ?)",
        rusqlite::params![rid, body, scopes],
    )
    .expect("fts");
}

fn store(rows: i64) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(SCHEMA).expect("schema");
    conn.execute_batch("BEGIN").expect("begin");
    for rid in 1..=rows {
        put(
            &conn,
            rid,
            "[\"global\"]",
            &format!("word{} common", rid % 97),
        );
    }
    conn.execute(
        "INSERT INTO log (seq, kind, mac) VALUES (1, 'memory_put', 'm')",
        [],
    )
    .expect("log");
    conn.execute_batch("COMMIT").expect("commit");
    conn
}

fn terms(n: usize) -> Vec<String> {
    (0..n).map(|i| format!("word{i}")).collect()
}

fn ranked(conn: &Connection, path: &Path, t: &[String]) -> usize {
    let src = StoreDocFreq::over(conn, path, "id");
    let memo = DocFreqMemo::new();
    let found = docfreq_store::ranking_statistics(&src, &memo, t, &mut |_, _, _| true)
        .expect("ranked")
        .expect("found");
    found.0
}

#[test]
fn twenty_thousand_rows_and_4097_terms_cost_linear_time() {
    let t = terms(4097);
    let small = store(2_000);
    let large = store(20_000);
    let path = Path::new("/nowhere/memory.sqlite");
    assert_eq!(ranked(&large, path, &t), 20_000);
    timing::grows_within(
        "ranking_statistics over 2,000 and 20,000 rows",
        timing::TENFOLD,
        &mut || timing::once(|| ranked(&small, path, &t)),
        &mut || timing::once(|| ranked(&large, path, &t)),
    );
}

static COUNTER: AtomicU64 = AtomicU64::new(0);

fn scratch_file() -> PathBuf {
    let base = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join("docfreq_store");
    std::fs::create_dir_all(&base).expect("base");
    base.join(format!(
        "s{}-{}.sqlite",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    ))
}

#[test]
fn eight_threads_share_one_memo_over_one_file() {
    let file = scratch_file();
    {
        let conn = Connection::open(&file).expect("open");
        conn.execute_batch(SCHEMA).expect("schema");
        conn.execute_batch("BEGIN").expect("begin");
        for rid in 1..=500 {
            put(
                &conn,
                rid,
                "[\"global\"]",
                &format!("word{} common", rid % 7),
            );
        }
        conn.execute("INSERT INTO log VALUES (1, 'memory_put', 'm')", [])
            .expect("log");
        conn.execute_batch("COMMIT").expect("commit");
    }
    let memo = DocFreqMemo::new();
    let t = terms(7);
    let answers: Vec<_> = std::thread::scope(|s| {
        let handles: Vec<_> = (0..8)
            .map(|i| {
                let (memo, t, file) = (&memo, &t, &file);
                s.spawn(move || {
                    let conn = Connection::open(file).expect("open");
                    let src = StoreDocFreq::over(&conn, file, "id");
                    let mut out = Vec::new();
                    for k in 0..20 {
                        let asked = &t[(i + k) % 7..];
                        out.push(
                            docfreq_store::ranking_statistics(&src, memo, asked, &mut |_, _, _| {
                                true
                            })
                            .expect("ranked"),
                        );
                    }
                    out
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|h| h.join().expect("thread"))
            .collect()
    });
    let conn = Connection::open(&file).expect("open");
    let src = StoreDocFreq::over(&conn, &file, "id");
    for (i, got) in answers.iter().enumerate() {
        for (k, found) in got.iter().enumerate() {
            let asked = &t[(i + k) % 7..];
            let want =
                docfreq_store::document_frequencies(&src, &memo, asked, &mut |_, _, _| true, false)
                    .expect("plain");
            let found = found
                .as_ref()
                .map(|(n, b, s, _)| (*n, b.clone(), s.clone()));
            assert_eq!(found, want, "thread {i} call {k}");
        }
    }
    assert_eq!(memo.keys().len(), 1);
    let _ = std::fs::remove_file(&file);
}

#[test]
fn a_scope_list_nested_100000_deep_raises_without_overflowing() {
    let conn = store(3);
    let deep = format!("{}{}", "[".repeat(100_000), "]".repeat(100_000));
    put(&conn, 50, &deep, "word1 deep");
    let path = Path::new("/nowhere/memory.sqlite");
    let src = StoreDocFreq::over(&conn, path, "id");
    let memo = DocFreqMemo::new();
    let t = terms(2);
    let got = docfreq_store::ranking_statistics(&src, &memo, &t, &mut |_, _, _| true);
    assert_eq!(got, Err(DocFreqError::Py(PyError::RecursionError)));
    let plain = docfreq_store::document_frequencies(&src, &memo, &t, &mut |_, _, _| true, false);
    assert!(plain.is_err(), "{plain:?}");
}

#[test]
fn row_numbers_at_the_ends_of_i64() {
    let conn = store(0);
    put(&conn, i64::MIN, "[\"a\"]", "edge low");
    put(&conn, i64::MAX, "[\"a\"]", "edge high");
    put(&conn, 0, "[\"a\"]", "edge zero");
    conn.execute("INSERT INTO log VALUES (2, 'memory_put', 'n')", [])
        .expect("log");
    let path = Path::new("/nowhere/memory.sqlite");
    let src = StoreDocFreq::over(&conn, path, "id");
    let memo = DocFreqMemo::new();
    let t = vec!["edge".to_owned(), "high".to_owned(), "low".to_owned()];
    let memoised = docfreq_store::document_frequencies(&src, &memo, &t, &mut |_, _, _| true, true)
        .expect("memo");
    let plain = docfreq_store::document_frequencies(&src, &memo, &t, &mut |_, _, _| true, false)
        .expect("plain");
    assert_eq!(memoised, plain);
    assert_eq!(memoised.expect("found").0, 3);
    use bettermemory_core::store::docfreq::DocFreqSource;
    assert_eq!(
        src.fts_rowids("\"edge\"", i64::MIN, 65)
            .expect("rows")
            .len(),
        3
    );
    assert_eq!(
        src.fts_rowids("\"edge\"", i64::MAX, usize::MAX)
            .expect("rows"),
        vec![i64::MAX]
    );
}
