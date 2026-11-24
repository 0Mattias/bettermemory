//! Hostile input to `record_history`: a log growing large around a
//! record, a record of 20,000 versions, a payload nested 100,000 deep, a
//! body of a megabyte changed by one line, ids at the edges. No panic,
//! and time that grows linearly with the log.

mod timing;

use bettermemory_core::history_record::{record_history, HistoryError, RecordHistory};
use bettermemory_core::log::KeyRing;
use bettermemory_core::pyvalue::PyError;
use bettermemory_core::store::provenance::{ConnSource, Provenance};
use bettermemory_core::store::StoreError;
use bettermemory_core::unicode::cps;
use rusqlite::Connection;

const DDL: &str = "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); \
    CREATE TABLE memories (id TEXT PRIMARY KEY, provenance TEXT, log_mac TEXT); \
    CREATE TABLE episodes (id TEXT PRIMARY KEY, log_mac TEXT); \
    CREATE TABLE tombstones (id TEXT PRIMARY KEY); \
    CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, session TEXT, \
    kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, mac TEXT NOT NULL); \
    CREATE INDEX log_by_kind ON log(kind, seq); \
    INSERT INTO meta VALUES ('key_fingerprint', 'f');";

/// A store of `n` log rows, each `(kind, payload)` from `row(seq)`.
fn store(n: i64, row: impl Fn(i64) -> (&'static str, String)) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(DDL).expect("ddl");
    let tx = conn.unchecked_transaction().expect("tx");
    {
        let mut insert = tx
            .prepare("INSERT INTO log VALUES (?, 't', NULL, ?, ?, 'pm', 'm')")
            .expect("insert");
        for seq in 1..=n {
            let (kind, payload) = row(seq);
            insert
                .execute(rusqlite::params![seq, kind, payload])
                .expect("row");
        }
    }
    tx.commit().expect("commit");
    conn
}

fn read(conn: &Connection, id: &[u32]) -> Result<RecordHistory, HistoryError> {
    let source = ConnSource(conn);
    let ring = KeyRing::default();
    let reader = Provenance::new(&source, &ring, false, false).expect("reader");
    record_history(&source, &ring, &reader, id)
}

/// Every fourth row a version of A, every fortieth a version of B
/// linking to A, the rest another record's.
fn mixed(seq: i64) -> (&'static str, String) {
    let payload = if seq % 40 == 0 {
        r#"{"memory": {"id": "B", "links": [{"type": "t", "target_id": "A"}]}}"#.to_owned()
    } else if seq % 4 == 0 {
        format!(r#"{{"memory": {{"id": "A", "body": "b", "n": {seq}}}}}"#)
    } else {
        format!(r#"{{"memory": {{"id": "C{seq}", "body": "b"}}}}"#)
    };
    ("memory_put", payload)
}

#[test]
fn a_history_read_grows_linearly_with_the_log() {
    let small = store(20_000, mixed);
    let large = store(40_000, mixed);
    let h = read(&large, &cps("A")).expect("history");
    assert_eq!(h.rows.len(), 9_000 + 1);
    assert_eq!(h.state, "missing");
    timing::grows_within(
        "a history over 40,000 rows against 20,000",
        6,
        &mut || timing::once(|| read(&small, &cps("A")).expect("small")),
        &mut || timing::once(|| read(&large, &cps("A")).expect("large")),
    );
}

#[test]
fn a_record_of_twenty_thousand_versions() {
    let conn = store(20_000, |seq| {
        let payload = format!(r#"{{"memory": {{"id": "A", "body": "v{}"}}}}"#, seq % 3);
        ("memory_put", payload)
    });
    let h = read(&conn, &cps("A")).expect("history");
    assert_eq!(h.rows.len(), 20_000);
    assert!(h.rows.iter().all(|r| r.diff.is_some()));
    assert_eq!(h.rows[1].action, "updated");
}

/// The port's budget, `json::MAX_DEPTH`, raises RecursionError here on
/// every host. The reference's answer at this depth follows the Python
/// release and the system: CPython 3.13 and macOS raise RecursionError,
/// CPython 3.14 on Linux reads the payload and the history continues. So
/// this row matches the reference only where it raises (3.13.13 on
/// macOS was seen), and no golden file holds it.
#[test]
fn a_payload_nested_past_the_budget_is_recursion_error_where_the_reference_raises() {
    let deep = format!(
        r#"{{"id": "A", "x": {}0{}}}"#,
        "[".repeat(100_000),
        "]".repeat(100_000)
    );
    let conn = store(2, |seq| {
        if seq == 1 {
            ("memory_put", r#"{"memory": {"id": "A"}}"#.to_owned())
        } else {
            ("memory_delete", deep.clone())
        }
    });
    let got = read(&conn, &cps("A"));
    assert!(
        matches!(
            got,
            Err(HistoryError::Store(StoreError::Py(PyError::RecursionError)))
        ),
        "{got:?}"
    );
    assert!(conn.is_autocommit());
}

#[test]
fn a_megabyte_body_changed_by_one_line() {
    let body = |at: usize| -> String {
        (0..20_000)
            .map(|i| {
                if i == at {
                    "changed line here\\n".to_owned()
                } else {
                    format!("line {i:05} of the long body here and more text to fill it\\n")
                }
            })
            .collect()
    };
    let (a, b) = (body(usize::MAX), body(10_000));
    assert!(a.len() > 1_000_000);
    let conn = store(2, |seq| {
        let text = if seq == 1 { &a } else { &b };
        (
            "memory_put",
            format!(r#"{{"memory": {{"id": "A", "body": "{text}"}}}}"#),
        )
    });
    let h = read(&conn, &cps("A")).expect("history");
    let diff = h.rows[1].diff.as_ref().expect("a diff");
    let diff = String::from_iter(diff.iter().map(|c| char::from_u32(*c).expect("char")));
    assert!(diff.contains("+changed line here"), "{diff}");
    assert!(diff.contains("-line 10000 of the long body"));
    assert!(diff.len() < 2_000, "{}", diff.len());
}

#[test]
fn ids_at_the_edges() {
    let conn = store(3, |seq| {
        ("memory_put", format!(r#"{{"memory": {{"id": "A{seq}"}}}}"#))
    });
    for id in [
        Vec::new(),
        vec![0],
        vec![0x41, 0],
        vec![0x41; 10_000],
        vec![0x10FFFF; 10_000],
    ] {
        let h = read(&conn, &id).expect("history");
        assert_eq!(h.state, "unknown", "{}", id.len());
        assert!(h.rows.is_empty());
        assert_eq!(h.record_id, id);
    }
    let got = read(&conn, &[0xDFFF; 10_000]);
    assert!(matches!(
        got,
        Err(HistoryError::Store(StoreError::Py(
            PyError::UnicodeEncodeError
        )))
    ));
}
