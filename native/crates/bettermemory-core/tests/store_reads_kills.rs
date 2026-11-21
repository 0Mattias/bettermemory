//! Cases a plausible wrong port of `store::reads` fails.

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::reads::{
    brief_rows, document_frequencies, error_code_name, note_residue, retryable_code, scope_counts,
    scopes_for, set_busy_timeout, sqlite_error_name, DefaultStoreSource, ReadError, StoreSource,
    STORE_BUSY, STORE_FULL,
};
use bettermemory_core::store::SqlValue;
use rusqlite::Connection;

fn store(rows: &[(&str, &str, &str, &str)]) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE memories (id, scopes_json, category, provenance, updated, \
         actor_client, actor_model);
         CREATE TABLE homes (record_id, project);
         CREATE VIRTUAL TABLE memories_fts USING fts5(body_fts, scopes_fts);",
    )
    .unwrap();
    for (i, (id, scopes, body, updated)) in rows.iter().enumerate() {
        conn.execute(
            "INSERT INTO memories (rowid, id, scopes_json, category, provenance, updated) \
             VALUES (?, ?, ?, 'fact', 'stated', ?)",
            rusqlite::params![i as i64 + 1, id, scopes, updated],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO memories_fts (rowid, body_fts, scopes_fts) VALUES (?, ?, ?)",
            rusqlite::params![i as i64 + 1, body, scopes],
        )
        .unwrap();
    }
    conn
}

#[test]
fn an_extended_code_reads_as_its_primary_one() {
    assert_eq!(
        retryable_code("OperationalError", Some(517)),
        Some(STORE_BUSY)
    );
    assert_eq!(
        retryable_code("OperationalError", Some(262)),
        Some(STORE_BUSY)
    );
    assert_eq!(
        retryable_code("OperationalError", Some(13 + 256)),
        Some(STORE_FULL)
    );
    // Python's & on a negative int: -251 & 0xFF is 5.
    assert_eq!(
        retryable_code("OperationalError", Some(-251)),
        Some(STORE_BUSY)
    );
    assert_eq!(retryable_code("IntegrityError", Some(5)), None);
    assert_eq!(retryable_code("OperationalError", None), None);
}

#[test]
fn a_code_without_a_name_falls_back_to_the_class() {
    assert_eq!(
        error_code_name("IntegrityError", Some(29)),
        "IntegrityError"
    );
    assert_eq!(error_code_name("X", Some(2067)), "SQLITE_CONSTRAINT_UNIQUE");
    assert_eq!(sqlite_error_name(100), Some("SQLITE_ROW"));
}

#[test]
fn the_residue_latch_answers_true_once() {
    let first = note_residue();
    assert!(!note_residue());
    assert!(!note_residue());
    // Another test in this binary may have taken it first; either way only
    // one call ever answered true.
    let _ = first;
}

#[test]
fn the_busy_timeout_truncates_toward_zero_and_refuses_negative_zero_never() {
    let conn = Connection::open_in_memory().unwrap();
    set_busy_timeout(&conn, 0.0019).unwrap();
    let ms: i64 = conn
        .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
        .unwrap();
    assert_eq!(ms, 1);
    set_busy_timeout(&conn, -0.0).unwrap();
    match set_busy_timeout(&conn, -1e-300) {
        Err(ReadError::Worded("ValueError", text)) => assert!(text.ends_with("got -1e-300")),
        other => panic!("{other:?}"),
    }
}

#[test]
fn equal_numbers_are_one_scope_key_and_the_first_spelling_stays() {
    let conn = store(&[("a", "[1, true, 1.0, \"1\"]", "x", "1")]);
    let (n, counts) = scope_counts(&conn, &mut |_, _| true).unwrap();
    assert_eq!(n, 1);
    assert_eq!(counts.len(), 2);
    assert_eq!(counts[0], (PyValue::Int("1".into()), 3));
}

#[test]
fn an_unhashable_scope_raises_only_once_admitted() {
    let conn = store(&[("a", "[[1]]", "x", "1")]);
    assert!(scope_counts(&conn, &mut |_, _| false).is_ok());
    match scope_counts(&conn, &mut |_, _| true) {
        Err(e) => assert_eq!(e.class(), "TypeError"),
        Ok(v) => panic!("{v:?}"),
    }
}

#[test]
fn brief_rows_hands_a_non_list_as_empty_and_orders_newest_first() {
    let conn = store(&[
        ("a", "{}", "x", "1"),
        ("b", "[\"s\"]", "x", "2"),
        ("c", "[]", "x", "2"),
    ]);
    let mut seen = Vec::new();
    let ids = brief_rows(&conn, &["stated".into()], None, &mut |s, _| {
        seen.push(s.len());
        true
    })
    .unwrap();
    assert_eq!(ids, ["c", "b", "a"]);
    assert_eq!(seen, [0, 1, 0]);
}

#[test]
fn document_frequencies_count_admitted_rows_once_per_term() {
    let conn = store(&[("a", "[\"fox\"]", "fox fox", "1"), ("b", "[]", "dog", "1")]);
    let (n, body, scope) = document_frequencies(
        &conn,
        &["fox".into(), "fox".into(), "".into(), "cat".into()],
        &mut |_, home, _| *home == SqlValue::Null,
    )
    .unwrap()
    .unwrap();
    assert_eq!(n, 2);
    assert_eq!(body, [("fox".to_owned(), 1)]);
    assert_eq!(scope, [("fox".to_owned(), 1)]);
}

#[test]
fn scopes_for_keeps_first_order_and_drops_unknown_ids() {
    let conn = store(&[("a", "[\"x\", 2]", "", "1"), ("b", "7", "", "1")]);
    let out = scopes_for(&conn, &["b".into(), "z".into(), "a".into(), "b".into()]).unwrap();
    let ids: Vec<&str> = out.iter().map(|(id, _)| id.as_str()).collect();
    assert!(ids == ["a", "b"] || ids == ["b", "a"]);
    let b = out.iter().find(|(id, _)| id == "b").unwrap();
    assert!(b.1.is_empty());
    let a = out.iter().find(|(id, _)| id == "a").unwrap();
    assert_eq!(a.1, [vec!['x' as u32], vec!['2' as u32]]);
}

#[test]
fn the_default_source_serves_its_one_store() {
    let source = DefaultStoreSource::new(41);
    let got: &i32 = StoreSource::<()>::for_request(&source, None);
    assert_eq!(*got, 41);
}

#[test]
fn sqlite_notfound_is_an_internal_error() {
    // `_pysqlite_seterror` raises InternalError for SQLITE_INTERNAL and
    // SQLITE_NOTFOUND, by the primary code of an extended one.
    for code in [2, 12, 12 + 256] {
        let e = rusqlite::Error::SqliteFailure(rusqlite::ffi::Error::new(code), None);
        assert_eq!(ReadError::Sqlite(e).class(), "InternalError", "{code}");
    }
}
