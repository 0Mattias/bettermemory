//! Cases a plausible wrong reading of `history`'s first unit fails, each
//! the reference's answer (checked against `src/bettermemory/history.py`
//! when written).

use bettermemory_core::history as h;
use bettermemory_core::json;
use bettermemory_core::log::LogRow;
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::store::StoreError;
use bettermemory_core::unicode::cps;
use rusqlite::Connection;

const DDL: &str = "CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, \
    session TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, \
    mac TEXT NOT NULL); CREATE INDEX log_by_kind ON log(kind, seq);";

fn store(rows: &[(i64, &str, &str)]) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(DDL).expect("ddl");
    for (seq, kind, payload) in rows {
        conn.execute(
            "INSERT INTO log VALUES (?, 't', NULL, ?, ?, 'pm', 'm')",
            rusqlite::params![seq, kind, payload],
        )
        .expect("insert");
    }
    conn
}

fn row(seq: i64, kind: &str, payload: &str) -> LogRow {
    LogRow {
        seq,
        ts: "t".into(),
        session: None,
        kind: kind.into(),
        payload: payload.into(),
        prev_mac: "pm".into(),
        mac: "m".into(),
    }
}

fn dict(text: &str) -> PyValue {
    json::loads(&cps(text)).expect("json")
}

fn seqs(found: &[h::Found]) -> Vec<i64> {
    found.iter().map(|(r, _)| r.seq).collect()
}

#[test]
fn the_restore_bound_is_the_first_item_s_seq_not_the_lowest() {
    let conn = store(&[
        (1, "memory_put", "{}"),
        (5, "store_restored", "{}"),
        (9, "memory_put", "{}"),
        (12, "history_accepted", "{}"),
    ]);
    let found = vec![
        (row(9, "memory_put", "{}"), None),
        (row(1, "memory_put", "{}"), None),
    ];
    let got = h::with_restores(&conn, found).expect("restores");
    assert_eq!(seqs(&got), vec![1, 9, 12]);
}

#[test]
fn a_restore_bound_excludes_its_own_seq() {
    let conn = store(&[(5, "store_restored", "{}"), (6, "store_restored", "{}")]);
    let mut got = Vec::new();
    h::replaced_rows(&conn, 5, &mut |r| {
        got.push(r.seq);
        Ok(())
    })
    .expect("rows");
    assert_eq!(got, vec![6]);
}

#[test]
fn a_seq_given_twice_keeps_its_last_item() {
    let conn = store(&[]);
    let found = vec![
        (row(3, "a", "first"), None),
        (row(3, "b", "{}"), Some(dict("{}"))),
    ];
    let got = h::with_restores(&conn, found).expect("restores");
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].0.kind, "b");
    let (got, _) =
        h::with_linking_versions(&conn, &cps("A"), vec![row(3, "a", "x"), row(3, "b", "{}")])
            .expect("links");
    assert_eq!(got[0].0.kind, "b");
    assert!(got[0].1.is_some());
}

#[test]
fn an_empty_id_names_every_row_and_the_test_is_case_sensitive() {
    let conn = store(&[(1, "x", "abc"), (2, "x", "ABC")]);
    assert_eq!(h::rows_naming(&conn, &[], &[]).expect("rows").len(), 2);
    let got = h::rows_naming(&conn, &cps("b"), &[]).expect("rows");
    assert_eq!(got.iter().map(|r| r.seq).collect::<Vec<_>>(), vec![1]);
}

#[test]
fn a_linking_record_s_own_scan_reads_only_versions_and_deletes() {
    let b = r#"{"memory": {"id": "B", "links": [{"type": "t", "target_id": "A"}]}}"#;
    let conn = store(&[
        (1, "memory_put", b),
        (2, "write", r#"{"id": "B"}"#),
        (3, "memory_put", r#"{"memory": {"id": "B"}}"#),
        (4, "episode_put", r#"{"episode": {"id": "B"}}"#),
        (5, "tombstone_delete", r#"{"id": "B"}"#),
    ]);
    let rows = h::rows_naming(&conn, &cps("A"), &[]).expect("rows");
    let (got, sources) = h::with_linking_versions(&conn, &cps("A"), rows).expect("links");
    assert_eq!(seqs(&got), vec![1, 3, 5]);
    assert_eq!(sources.into_iter().collect::<Vec<_>>(), vec![cps("B")]);
}

#[test]
fn a_record_linking_itself_is_no_source() {
    let a = r#"{"memory": {"id": "A", "links": [{"type": "t", "target_id": "A"}]}}"#;
    let conn = store(&[(1, "memory_put", a)]);
    let rows = h::rows_naming(&conn, &cps("A"), &[]).expect("rows");
    let (_, sources) = h::with_linking_versions(&conn, &cps("A"), rows).expect("links");
    assert!(sources.is_empty());
}

#[test]
fn the_subject_reads_each_kind_s_own_place() {
    let p = dict(r#"{"memory": {"id": "A"}, "id": "B", "memory_id": "C"}"#);
    assert_eq!(h::subject("memory_put", &p), Some(&cps("A")));
    assert_eq!(h::subject("memory_delete", &p), Some(&cps("B")));
    assert_eq!(h::subject("verification_put", &p), Some(&cps("C")));
    assert_eq!(h::subject("Memory_put", &p), None);
    assert_eq!(h::subject("write", &p), None);
    assert_eq!(h::subject("episode_put", &p), None);
    let p = dict(r#"{"home": {"record_id": "H"}, "record_id": "R"}"#);
    assert_eq!(h::subject("home_put", &p), Some(&cps("H")));
    assert_eq!(h::subject("home_delete", &p), Some(&cps("R")));
    assert_eq!(
        h::subject("memory_put", &dict(r#"{"memory": {"id": 1}}"#)),
        None
    );
}

#[test]
fn a_standing_list_is_matched_by_member_not_by_substring() {
    let p = dict(r#"{"standing": "xAx"}"#);
    assert!(!h::names_as_event("standing_guard", &p, &cps("A")));
    let p = dict(r#"{"standing": ["xAx", "A"]}"#);
    assert!(h::names_as_event("standing_guard", &p, &cps("A")));
    let p = dict(r#"{"id": "A"}"#);
    assert!(!h::names_as_event("search", &p, &cps("A")));
    assert!(h::names_as_event("restore", &p, &cps("A")));
}

#[test]
fn a_link_type_is_str_of_the_value() {
    let p = dict(
        r#"{"memory": {"links": [{"target_id": "A"}, {"type": true, "target_id": "A"},
        {"type": 1.0, "target_id": "A"}, {"type": {"k": [1, "v"]}, "target_id": "A"},
        {"type": "z", "target_id": "B"}]}}"#,
    );
    let got: Vec<String> = h::link_types("memory_put", &p, &cps("A"))
        .expect("types")
        .into_iter()
        .map(|t| {
            t.iter()
                .map(|&c| char::from_u32(c).expect("char"))
                .collect()
        })
        .collect();
    assert_eq!(got, vec!["1.0", "None", "True", "{'k': [1, 'v']}"]);
    // A tombstone's links sit beside the record, a memory's inside it.
    let beside = dict(r#"{"links": [{"type": "t", "target_id": "A"}]}"#);
    assert_eq!(
        h::link_types("tombstone_put", &beside, &cps("A"))
            .expect("t")
            .len(),
        1
    );
    assert!(h::link_types("memory_put", &beside, &cps("A"))
        .expect("t")
        .is_empty());
}

#[test]
fn a_payload_that_is_not_an_object_is_none_and_deep_nesting_raises() {
    assert!(h::payload(&row(1, "k", "[1]")).expect("read").is_none());
    assert!(h::payload(&row(1, "k", "{} x")).expect("read").is_none());
    let deep = "[".repeat(20_000) + &"]".repeat(20_000);
    assert_eq!(
        h::payload(&row(1, "k", &deep)),
        Err(PyError::RecursionError)
    );
}

#[test]
fn the_first_column_not_utf_8_is_the_one_named() {
    let conn = store(&[]);
    conn.execute(
        "INSERT INTO log VALUES (1, CAST(x'ff' AS TEXT), CAST(x'ff' AS TEXT), 'k', 'A', 'pm', 'm')",
        [],
    )
    .expect("insert");
    match h::rows_naming(&conn, &cps("A"), &[]) {
        Err(StoreError::NotUtf8 { column, .. }) => assert_eq!(column, "ts"),
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_held_transaction_is_kept_and_an_ended_one_raises() {
    let conn = store(&[]);
    conn.execute_batch("BEGIN").expect("begin");
    h::read_snapshot(&conn, || ()).expect("snapshot");
    assert!(!conn.is_autocommit());
    conn.execute_batch("ROLLBACK").expect("rollback");
    let got = h::read_snapshot(&conn, || conn.execute_batch("COMMIT").expect("commit"));
    assert!(got.is_err());
    assert!(conn.is_autocommit());
}

#[test]
fn a_lone_surrogate_id_cannot_be_bound() {
    let conn = store(&[(1, "k", "x")]);
    match h::rows_naming(&conn, &[0xD800], &[]) {
        Err(StoreError::Py(PyError::UnicodeEncodeError)) => {}
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_held_seq_keeps_its_item_over_the_replaced_row() {
    let conn = store(&[(1, "memory_put", "{}"), (5, "store_restored", "{}")]);
    let found = vec![
        (row(1, "memory_put", "{}"), None),
        (row(5, "held", "x"), None),
    ];
    let got = h::with_restores(&conn, found).expect("restores");
    assert_eq!(seqs(&got), vec![1, 5]);
    assert_eq!(got[1].0.kind, "held");
    assert!(got[1].1.is_none());
}

#[test]
fn an_id_at_the_start_of_a_payload_names_it() {
    let conn = store(&[(1, "x", "Ab"), (2, "x", "bA")]);
    let got = h::rows_naming(&conn, &cps("A"), &[]).expect("rows");
    assert_eq!(got.iter().map(|r| r.seq).collect::<Vec<_>>(), vec![1, 2]);
}

#[test]
fn the_link_kinds_are_the_reference_s() {
    let carried: &[(&str, &str)] = &h::LINK_CARRIED_BY;
    assert_eq!(
        carried,
        &[
            ("memory_delete", "tombstone_put"),
            ("tombstone_delete", "memory_put")
        ]
    );
    let mut scan: Vec<&str> = h::LINKING_KINDS.to_vec();
    scan.extend(h::LINK_CARRIED_BY.iter().map(|(delete, _)| *delete));
    scan.sort_unstable();
    scan.dedup();
    assert_eq!(scan, h::LINK_SCAN_KINDS.to_vec());
}

#[test]
fn a_panic_in_the_body_rolls_back() {
    let conn = store(&[]);
    let got = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        h::read_snapshot(&conn, || panic!("body"))
    }));
    assert!(got.is_err());
    assert!(conn.is_autocommit());
}

#[test]
fn the_next_row_is_stepped_to_before_a_row_is_visited() {
    // The reference's cursor steps past a row before the generator yields
    // it: the overflow stepping to seq 2 raises before seq 1's payload,
    // nested past the budget, is parsed (OperationalError "integer
    // overflow" there).
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(
        "CREATE TABLE t (seq INTEGER PRIMARY KEY, ts TEXT, session TEXT, kind TEXT, \
         payload TEXT, prev_mac TEXT, mac TEXT); \
         CREATE VIEW log AS SELECT seq, CASE WHEN seq = 2 \
         THEN abs(-9223372036854775807 - 1) ELSE ts END AS ts, session, kind, payload, \
         prev_mac, mac FROM t;",
    )
    .expect("ddl");
    let deep = "[".repeat(20_000) + &"]".repeat(20_000);
    for (seq, payload) in [(1, deep.as_str()), (2, "{}")] {
        conn.execute(
            "INSERT INTO t VALUES (?, 't', NULL, 'store_restored', ?, 'pm', 'm')",
            rusqlite::params![seq, payload],
        )
        .expect("insert");
    }
    let found = vec![(row(0, "k", "{}"), None)];
    match h::with_restores(&conn, found) {
        Err(StoreError::Sqlite(_)) => {}
        other => panic!("{other:?}"),
    }
}
