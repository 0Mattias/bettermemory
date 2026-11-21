//! Hostile rekey rows for `RowVerifier`: payloads nested past what
//! `json.loads` reads (RecursionError, raised as the reference raises
//! it), very large payloads, seq bounds, and a constructor and lookups
//! that grow linearly with the rekey rows.

mod timing;

use bettermemory_core::history::RowVerifier;
use bettermemory_core::log::{fingerprint, KeyRing, LogRow};
use bettermemory_core::pyvalue::PyError;
use bettermemory_core::store::StoreError;
use rusqlite::Connection;

const DDL: &str = "CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, \
    session TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, \
    mac TEXT NOT NULL);";

fn store(rows: impl IntoIterator<Item = (i64, String)>) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(DDL).expect("ddl");
    conn.execute_batch("BEGIN").expect("begin");
    for (seq, payload) in rows {
        conn.execute(
            "INSERT INTO log VALUES (?, 't', NULL, 'rekey', ?, 'pm', 'm')",
            rusqlite::params![seq, payload],
        )
        .expect("insert");
    }
    conn.execute_batch("COMMIT").expect("commit");
    conn
}

fn row(seq: i64) -> LogRow {
    LogRow {
        seq,
        ts: "t".into(),
        session: None,
        kind: "write".into(),
        payload: "{}".into(),
        prev_mac: "pm".into(),
        mac: "m".into(),
    }
}

#[test]
fn a_payload_nested_past_the_budget_is_recursion_error() {
    let deep = format!("{}{}", "[".repeat(100_000), "]".repeat(100_000));
    let conn = store([(2, deep)]);
    let ring = KeyRing::default();
    match RowVerifier::new(&conn, &ring, "") {
        Err(StoreError::Py(PyError::RecursionError)) => {}
        Err(e) => panic!("{e:?}"),
        Ok(_) => panic!("no error"),
    }
}

#[test]
fn a_value_nested_inside_a_readable_payload_does_not_panic() {
    let inner = format!("{}{}", "[".repeat(5_000), "]".repeat(5_000));
    let payload = format!("{{\"old_fingerprint\": {inner}, \"new_fingerprint\": 1}}");
    let conn = store([(2, payload)]);
    let ring = KeyRing::default();
    let mut v = RowVerifier::new(&conn, &ring, "").expect("verifier");
    assert_eq!(v.verified(&row(1)), None);
}

#[test]
fn large_payloads_and_seq_bounds() {
    let big = "x".repeat(4_000_000);
    let f = fingerprint(b"k");
    let conn = store([
        (
            i64::MIN,
            format!("{{\"old_fingerprint\": \"{big}\", \"new_fingerprint\": \"{f}\"}}"),
        ),
        (i64::MAX, "9".repeat(5_000)),
    ]);
    let ring = KeyRing::default();
    let mut v = RowVerifier::new(&conn, &ring, "").expect("verifier");
    assert_eq!(v.verified(&row(i64::MIN)), None);
    assert_eq!(v.verified(&row(i64::MAX)), None);
}

#[test]
fn the_constructor_grows_linearly_with_the_rekey_rows() {
    let f = fingerprint(b"k");
    let payload = format!("{{\"old_fingerprint\": \"{f}\", \"new_fingerprint\": \"{f}\"}}");
    let small = store((1..=2_000).map(|s| (s, payload.clone())));
    let large = store((1..=20_000).map(|s| (s, payload.clone())));
    let ring = KeyRing::default();
    let run =
        |conn: &Connection| timing::once(|| RowVerifier::new(conn, &ring, "").expect("verifier"));
    timing::grows_within(
        "RowVerifier::new: 2,000 and 20,000 rekey rows",
        timing::TENFOLD,
        &mut || run(&small),
        &mut || run(&large),
    );
}
