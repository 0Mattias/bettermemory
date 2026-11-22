//! Hostile input for `store::digest_query`: extreme floors, very large
//! token sets and tokens, columns holding what the writer never writes,
//! and the cost of a question against the size of the asked set.

mod timing;

use std::collections::BTreeSet;

use bettermemory_core::store::digest::DIGEST_SCHEMA;
use bettermemory_core::store::digest_query::{digest_candidates, digest_shares, QueryError};
use rusqlite::{params, Connection};

fn store(records: usize, per: usize) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(DIGEST_SCHEMA).unwrap();
    let tx = conn.unchecked_transaction().unwrap();
    for doc in 0..records {
        let tokens: Vec<String> = (0..per)
            .map(|i| format!("t{}", (doc * 7 + i) % 5000))
            .collect();
        tx.execute(
            "INSERT INTO digest_records(doc, id, n, claim, tokens) VALUES (?, ?, ?, ?, ?)",
            params![
                doc as i64,
                format!("r{doc}"),
                per as i64,
                per as i64,
                tokens.join(" ")
            ],
        )
        .unwrap();
        for t in &tokens {
            tx.execute(
                "INSERT OR IGNORE INTO digest_tokens(token, doc) VALUES (?, ?)",
                params![t, doc as i64],
            )
            .unwrap();
        }
    }
    tx.commit().unwrap();
    conn
}

fn query(n: usize) -> BTreeSet<String> {
    (0..n).map(|i| format!("t{i}")).collect()
}

#[test]
fn extreme_floors_answer_or_raise_without_panic() {
    let conn = store(50, 10);
    let q = query(20);
    for floor in [
        0.0,
        -0.0,
        f64::MIN_POSITIVE,
        -f64::MIN_POSITIVE,
        1e-300,
        9_223_372_036_854_775.0,
        -9_223_372_036_854_776.0,
        f64::MAX,
        f64::MIN,
        f64::NEG_INFINITY,
        f64::NAN,
    ] {
        let got = digest_shares(&conn, "S", Some("1:m"), &q, false, floor, false);
        match got {
            Ok(a) => assert!(a.rows.is_some(), "{floor}"),
            Err(QueryError::Value(_)) => assert!(floor.is_nan()),
            Err(QueryError::Overflow(_)) => assert!((floor * 1000.0).abs() >= 9.2e18),
            Err(QueryError::Type(m)) => panic!("{floor}: {m}"),
        }
    }
}

#[test]
fn huge_tokens_and_odd_columns_do_not_panic() {
    let conn = store(5, 5);
    let big = "x".repeat(1 << 20);
    let q: BTreeSet<String> = [big.clone(), format!("{big}-{big}"), "t1".into()].into();
    let a = digest_candidates(&conn, "S", Some("1:m"), &q, false, 0.0, false).unwrap();
    assert!(a.rows.is_some());
    conn.execute_batch(
        "UPDATE digest_records SET tokens = x'ff00', id = x'27' WHERE doc = 1;\
         UPDATE digest_records SET n = 1e300 WHERE doc = 2;",
    )
    .unwrap();
    for floor in [0.0, 0.3] {
        let a =
            digest_candidates(&conn, "S", Some("2:m"), &query(10), false, floor, false).unwrap();
        assert!(a.rows.is_some() || a.warning.is_some());
        let s = digest_shares(&conn, "S", Some("3:m"), &query(10), false, floor, false).unwrap();
        assert!(s.rows.is_some() || s.warning.is_some());
    }
}

#[test]
fn a_question_grows_with_its_tokens() {
    let conn = store(2000, 12);
    let mark = std::cell::Cell::new(0u64);
    let run = |n: usize| {
        let q = query(n);
        mark.set(mark.get() + 1);
        let m = mark.get().to_string();
        timing::once(|| digest_shares(&conn, "S", Some(&m), &q, false, 0.4, false).unwrap())
    };
    timing::grows_within("digest_shares", 30, &mut || run(400), &mut || run(4000));
}
