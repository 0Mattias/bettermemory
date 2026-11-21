//! Hostile input for `store::reads`: many rows and many distinct scopes,
//! scope lists nested past the JSON recursion budget, huge and tiny busy
//! timeouts, terms full of quotes. No panic, and the counts grow linearly.

mod timing;

use bettermemory_core::store::reads::{
    brief_rows, document_frequencies, retryable_code, scope_counts, scopes_for, set_busy_timeout,
};
use rusqlite::Connection;

fn store(n: usize, scopes: impl Fn(usize) -> String) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE memories (id, scopes_json, category, provenance, updated, \
         actor_client, actor_model);
         CREATE TABLE homes (record_id, project);
         CREATE VIRTUAL TABLE memories_fts USING fts5(body_fts, scopes_fts);
         BEGIN;",
    )
    .unwrap();
    for i in 0..n {
        let s = scopes(i);
        conn.execute(
            "INSERT INTO memories (rowid, id, scopes_json, category, provenance, updated) \
             VALUES (?, ?, ?, 'fact', 'stated', ?)",
            rusqlite::params![i as i64 + 1, format!("r{i}"), s, i as i64],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO memories_fts (rowid, body_fts, scopes_fts) VALUES (?, 'body word', ?)",
            rusqlite::params![i as i64 + 1, s],
        )
        .unwrap();
    }
    conn.execute_batch("COMMIT").unwrap();
    conn
}

#[test]
fn distinct_scopes_count_in_linear_time() {
    let small = store(2_000, |i| format!("[\"s{i}\", {i}, {i}.5]"));
    let large = store(20_000, |i| format!("[\"s{i}\", {i}, {i}.5]"));
    timing::grows_within(
        "scope_counts: 2,000 and 20,000 rows of distinct scopes",
        timing::TENFOLD,
        &mut || timing::once(|| scope_counts(&small, &mut |_, _| true).unwrap().1.len()),
        &mut || timing::once(|| scope_counts(&large, &mut |_, _| true).unwrap().1.len()),
    );
}

#[test]
fn a_scope_list_nested_past_the_budget_raises() {
    let deep = format!("[{}{}]", "[".repeat(100_000), "]".repeat(100_000));
    let conn = store(1, |_| deep.clone());
    let e = scope_counts(&conn, &mut |_, _| true).unwrap_err();
    assert_eq!(e.class(), "RecursionError");
    assert!(brief_rows(&conn, &["stated".into()], Some("x"), &mut |_, _| true).is_err());
    assert!(scopes_for(&conn, &["r0".into()]).is_err());
}

#[test]
fn many_ids_go_in_chunks() {
    let conn = store(1_500, |i| format!("[\"s{i}\"]"));
    let ids: Vec<String> = (0..3_000).map(|i| format!("r{i}")).collect();
    assert_eq!(scopes_for(&conn, &ids).unwrap().len(), 1_500);
}

#[test]
fn quoted_and_odd_terms_never_panic() {
    let conn = store(50, |_| "[\"a\"]".into());
    let terms: Vec<String> = [
        "\"",
        "\"\"\"",
        "*",
        "NEAR(",
        "body_fts:",
        "\u{0}",
        "a\"b",
        "x".repeat(10_000).as_str(),
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    let _ = document_frequencies(&conn, &terms, &mut |_, _, _| true);
}

#[test]
fn busy_timeouts_at_the_bounds() {
    let conn = Connection::open_in_memory().unwrap();
    for s in [
        f64::MAX,
        f64::MIN_POSITIVE,
        1e306,
        1e305,
        9.2e15,
        2147483.6475,
    ] {
        let _ = set_busy_timeout(&conn, s);
    }
    assert_eq!(
        set_busy_timeout(&conn, f64::MAX).unwrap_err().class(),
        "OverflowError"
    );
    for code in [i64::MIN, i64::MAX, -1, 0] {
        let _ = retryable_code("OperationalError", Some(code));
    }
}
