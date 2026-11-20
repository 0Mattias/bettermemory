//! Hostile input to `history`'s first unit: large logs, payloads nested
//! far past the reference's budget, integers past `str`'s digit limit and
//! the integer bounds of a seq. No panic, and time that grows linearly.

use std::time::{Duration, Instant};

use bettermemory_core::history as h;
use bettermemory_core::json;
use bettermemory_core::log::LogRow;
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::unicode::cps;
use rusqlite::Connection;

const DDL: &str = "CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, \
    session TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, \
    mac TEXT NOT NULL); CREATE INDEX log_by_kind ON log(kind, seq);";

/// A log of `n` rows: every tenth a version of B linking to A, every
/// hundredth a restore, the rest a version of A.
fn log(n: i64) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(DDL).expect("ddl");
    let tx = conn.unchecked_transaction().expect("tx");
    for seq in 1..=n {
        let (kind, payload) = if seq % 100 == 0 {
            ("store_restored", "{}".to_owned())
        } else if seq % 10 == 0 {
            (
                "memory_put",
                r#"{"memory": {"id": "B", "links": [{"type": "t", "target_id": "A"}]}}"#.to_owned(),
            )
        } else {
            (
                "memory_put",
                format!(r#"{{"memory": {{"id": "A", "n": {seq}}}}}"#),
            )
        };
        tx.execute(
            "INSERT INTO log VALUES (?, 't', NULL, ?, ?, 'pm', 'm')",
            rusqlite::params![seq, kind, payload],
        )
        .expect("insert");
    }
    tx.commit().expect("commit");
    conn
}

fn history(conn: &Connection) -> usize {
    h::read_snapshot(conn, || {
        let rows = h::rows_naming(conn, &cps("A"), &[]).expect("rows");
        let (found, _) = h::with_linking_versions(conn, &cps("A"), rows).expect("links");
        h::with_restores(conn, found).expect("restores").len()
    })
    .expect("snapshot")
}

fn fastest(conn: &Connection) -> Duration {
    (0..3)
        .map(|_| {
            let start = Instant::now();
            std::hint::black_box(history(conn));
            start.elapsed()
        })
        .min()
        .expect("runs")
}

#[test]
fn a_history_grows_linearly_with_the_log() {
    let small = log(20_000);
    let large = log(40_000);
    assert_eq!(history(&small), 20_000);
    let (n, n2) = (fastest(&small), fastest(&large));
    assert!(
        n2 < n * 4 + Duration::from_millis(200),
        "20,000 rows {n:?}, 40,000 rows {n2:?}"
    );
}

#[test]
fn a_type_nested_past_the_budget_raises_and_does_not_overflow() {
    let mut value = PyValue::Str(cps("t"));
    for _ in 0..200_000 {
        value = PyValue::List(vec![value]);
    }
    let link = PyValue::Dict(vec![
        (PyValue::Str(cps("type")), value),
        (PyValue::Str(cps("target_id")), PyValue::Str(cps("A"))),
    ]);
    let payload = PyValue::Dict(vec![(
        PyValue::Str(cps("links")),
        PyValue::List(vec![link]),
    )]);
    assert_eq!(
        h::link_types("tombstone_put", &payload, &cps("A")),
        Err(PyError::RecursionError)
    );
}

#[test]
fn a_type_of_9_998_open_containers_is_written_and_9_999_raise() {
    // The reference's `_link_types` answers the same: 9,998 lists around
    // a str or an empty list or dict are written, 9,999 raise.
    let at = |wrappers: usize, leaf: PyValue| {
        let mut value = leaf;
        for _ in 0..wrappers {
            value = PyValue::List(vec![value]);
        }
        let link = PyValue::Dict(vec![
            (PyValue::Str(cps("type")), value),
            (PyValue::Str(cps("target_id")), PyValue::Str(cps("A"))),
        ]);
        let payload = PyValue::Dict(vec![(
            PyValue::Str(cps("links")),
            PyValue::List(vec![link]),
        )]);
        h::link_types("tombstone_put", &payload, &cps("A")).map(|types| types.len())
    };
    let depth = json::MAX_DEPTH;
    assert_eq!(at(depth, PyValue::Str(cps("t"))), Ok(1));
    assert_eq!(at(depth, PyValue::List(Vec::new())), Ok(1));
    assert_eq!(at(depth, PyValue::Dict(Vec::new())), Ok(1));
    assert_eq!(
        at(depth + 1, PyValue::Str(cps("t"))),
        Err(PyError::RecursionError)
    );
    assert_eq!(
        at(depth, PyValue::List(vec![PyValue::None])),
        Err(PyError::RecursionError)
    );
}

#[test]
fn an_integer_type_past_the_digit_limit_raises() {
    let at = |digits: usize| {
        let link = PyValue::Dict(vec![
            (
                PyValue::Str(cps("type")),
                PyValue::int_from_decimal(&format!("-{}", "7".repeat(digits))).expect("int"),
            ),
            (PyValue::Str(cps("target_id")), PyValue::Str(cps("A"))),
        ]);
        let payload = PyValue::Dict(vec![(
            PyValue::Str(cps("links")),
            PyValue::List(vec![link]),
        )]);
        h::link_types("tombstone_put", &payload, &cps("A"))
    };
    assert_eq!(at(4300).expect("4,300 digits").len(), 1);
    assert_eq!(at(4301), Err(PyError::ValueError));
}

#[test]
fn a_payload_of_megabytes_and_deep_nesting_does_not_panic() {
    let row = |payload: String| LogRow {
        seq: i64::MAX,
        ts: "t".into(),
        session: None,
        kind: "memory_put".into(),
        payload,
        prev_mac: "pm".into(),
        mac: "m".into(),
    };
    let deep = "{\"a\":".repeat(1_000_000) + "1" + &"}".repeat(1_000_000);
    assert_eq!(h::payload(&row(deep)), Err(PyError::RecursionError));
    let wide = format!("{{\"x\": \"{}\"}}", "y".repeat(4_000_000));
    assert!(h::payload(&row(wide)).expect("read").is_some());
}

#[test]
fn the_seq_bounds_are_read_and_compared_whole() {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(DDL).expect("ddl");
    for seq in [i64::MIN, -1, 0, i64::MAX] {
        conn.execute(
            "INSERT INTO log VALUES (?, 't', NULL, 'store_restored', 'A', 'pm', 'm')",
            [seq],
        )
        .expect("insert");
    }
    let mut got = Vec::new();
    h::replaced_rows(&conn, i64::MIN, &mut |r| {
        got.push(r.seq);
        Ok(())
    })
    .expect("rows");
    assert_eq!(got, vec![-1, 0, i64::MAX]);
    let rows = h::rows_naming(&conn, &cps("A"), &[]).expect("rows");
    let found = rows.into_iter().map(|r| (r, None)).collect();
    assert_eq!(h::with_restores(&conn, found).expect("restores").len(), 4);
}
