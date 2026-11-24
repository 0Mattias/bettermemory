//! Cases a plausible wrong `record_history` fails: neighbours paired by
//! list position, the seq or subject test dropped, the branch order, an
//! empty actor, the other side of a self conflict, the order of unlinked
//! rows, carried links, the order of the table lookups, `pruned` against
//! `deleted`, `_table_kind`'s order, the findings' order, `replaced_detail`
//! and the order the errors surface in. Each store is a bare connection
//! with the tables the reader reads, its rows signed under one key.

use bettermemory_core::history_record::{
    record_history, replaced_detail, HistoryError, RecordHistory, ACTOR_FROM_EVENT, EVENT, FORMAT,
    FORMAT_VERSION, STORE,
};
use bettermemory_core::json;
use bettermemory_core::log::{compute_mac, fingerprint, Key, KeyRing, GENESIS_MAC};
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::store::provenance::{ConnSource, Provenance};
use bettermemory_core::store::StoreError;
use bettermemory_core::unicode::cps;
use rusqlite::Connection;

const KEY: [u8; 32] = [7; 32];
const DDL: &str = "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); \
    CREATE TABLE memories (id TEXT PRIMARY KEY, provenance TEXT, log_mac TEXT); \
    CREATE TABLE episodes (id TEXT PRIMARY KEY, log_mac TEXT); \
    CREATE TABLE tombstones (id TEXT PRIMARY KEY); \
    CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, session TEXT, \
    kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, mac TEXT NOT NULL);";

fn store(rows: &[(i64, &str, &str)]) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(DDL).expect("ddl");
    conn.execute(
        "INSERT INTO meta VALUES ('key_fingerprint', ?)",
        [fingerprint(&KEY)],
    )
    .expect("meta");
    for (seq, kind, payload) in rows {
        let mac = compute_mac(&KEY, *seq, "t", Some("s"), kind, payload, GENESIS_MAC);
        conn.execute(
            "INSERT INTO log VALUES (?, 't', 's', ?, ?, ?, ?)",
            rusqlite::params![seq, kind, payload, GENESIS_MAC, mac],
        )
        .expect("row");
    }
    conn
}

fn keyring() -> KeyRing {
    KeyRing {
        current: Some(Key::new(KEY.to_vec())),
        ..KeyRing::default()
    }
}

fn read(conn: &Connection, id: &str) -> Result<RecordHistory, HistoryError> {
    let source = ConnSource(conn);
    let ring = keyring();
    let reader = Provenance::new(&source, &ring, false, false).expect("reader");
    record_history(&source, &ring, &reader, &cps(id))
}

fn history(conn: &Connection, id: &str) -> RecordHistory {
    read(conn, id).expect("history")
}

fn actions(h: &RecordHistory) -> Vec<(i64, String)> {
    h.rows.iter().map(|r| (r.seq, r.action.clone())).collect()
}

fn acts(v: &[(i64, &str)]) -> Vec<(i64, String)> {
    v.iter().map(|(s, a)| (*s, (*a).to_owned())).collect()
}

fn put(id: &str) -> String {
    format!(r#"{{"memory": {{"id": "{id}", "body": "b"}}, "provenance": "local"}}"#)
}

fn put_links(kind: &str, id: &str, links: &str) -> String {
    if kind == "memory_put" {
        format!(r#"{{"memory": {{"id": "{id}", "body": "b", "links": {links}}}}}"#)
    } else {
        format!(r#"{{"tombstone": {{"id": "{id}"}}, "links": {links}}}"#)
    }
}

fn del(id: &str) -> String {
    format!(r#"{{"id": "{id}"}}"#)
}

#[test]
fn neighbours_are_one_seq_apart_not_one_item_apart() {
    // An unrelated row between the delete and the tombstone.
    let conn = store(&[
        (1, "memory_put", &put("X")),
        (2, "memory_delete", &del("X")),
        (3, "remark", "{}"),
        (4, "tombstone_put", r#"{"tombstone": {"id": "X"}}"#),
    ]);
    let h = history(&conn, "X");
    assert_eq!(
        actions(&h),
        acts(&[(1, "created"), (2, "deleted"), (4, "updated")])
    );
    // A gap in seq, nothing between.
    let conn = store(&[
        (1, "memory_put", &put("X")),
        (2, "memory_delete", &del("X")),
        (9, "tombstone_put", r#"{"tombstone": {"id": "X"}}"#),
    ]);
    assert_eq!(
        actions(&history(&conn, "X")),
        acts(&[(1, "created"), (2, "deleted"), (9, "updated")])
    );
}

#[test]
fn a_row_without_a_payload_still_stands_between() {
    let conn = store(&[
        (1, "memory_delete", &del("X")),
        (2, "remark", "not json X"),
        (3, "tombstone_put", r#"{"tombstone": {"id": "X"}}"#),
    ]);
    let h = history(&conn, "X");
    assert_eq!(actions(&h), acts(&[(1, "deleted"), (3, "tombstoned")]));
}

#[test]
fn a_neighbour_of_another_record_is_no_pair() {
    let conn = store(&[
        (1, "memory_put", &put("X")),
        (2, "memory_delete", &del("X")),
        (
            3,
            "tombstone_put",
            r#"{"tombstone": {"id": "Y", "note": "X"}}"#,
        ),
        (4, "tombstone_delete", &del("X")),
        (5, "memory_put", &put("Y")),
    ]);
    let h = history(&conn, "X");
    assert_eq!(
        actions(&h),
        acts(&[(1, "created"), (2, "deleted"), (4, "pruned")])
    );
    assert_eq!(h.state, "pruned");
}

#[test]
fn the_branches_go_in_the_reference_s_order() {
    let conn = store(&[
        (1, "memory_put", &put("X")),
        (
            2,
            "history_accepted",
            r#"{"id": "X", "memory": {"id": "X"}}"#,
        ),
        (3, "write", r#"{"id": "X", "actor": {}}"#),
        (
            4,
            "update",
            r#"{"id": "X", "actor": {"client": "c"}, "reason": "r"}"#,
        ),
        (5, "verify", r#"{"id": "X", "actor": ["c"]}"#),
    ]);
    let h = history(&conn, "X");
    let roles: Vec<&str> = h.rows.iter().map(|r| r.role).collect();
    assert_eq!(roles, ["record", STORE, EVENT, EVENT, EVENT]);
    assert_eq!(h.rows[1].action, "accepted");
    // An empty actor mapping is no actor, and no source.
    assert_eq!(h.rows[2].actor, None);
    assert_eq!(h.rows[2].actor_source, None);
    assert_eq!(h.rows[3].actor_source, Some(ACTOR_FROM_EVENT));
    assert!(matches!(&h.rows[3].actor, Some(PyValue::Dict(items)) if items.len() == 1));
    assert_eq!(h.rows[3].detail.len(), 1);
    assert_eq!(h.rows[4].actor, None);
}

#[test]
fn a_self_conflict_names_itself_as_the_other() {
    let conn = store(&[
        (
            1,
            "conflict_put",
            r#"{"conflict": {"id": "c", "a_id": "X", "b_id": "X"}}"#,
        ),
        (
            2,
            "conflict_put",
            r#"{"conflict": {"id": "d", "a_id": "Z", "b_id": "X"}}"#,
        ),
        (
            3,
            "conflict_put",
            r#"{"conflict": {"id": "e", "a_id": "X", "b_id": null}}"#,
        ),
        (
            4,
            "conflict_put",
            r#"{"conflict": {"a_id": ["X"], "b_id": "Xa"}}"#,
        ),
    ]);
    let h = history(&conn, "X");
    let other = |i: usize| {
        h.rows[i]
            .detail
            .iter()
            .find(|(k, _)| *k == PyValue::str("other"))
            .map(|(_, v)| v.clone())
    };
    assert_eq!(h.rows.len(), 3);
    assert_eq!(other(0), Some(PyValue::str("X")));
    assert_eq!(other(1), Some(PyValue::str("Z")));
    assert_eq!(other(2), None);
}

#[test]
fn links_carried_stay_and_links_pruned_go_sorted() {
    let links = r#"[{"type": "b", "target_id": "X"}, {"type": "a", "target_id": "X"}]"#;
    let conn = store(&[
        (1, "memory_put", &put("X")),
        (2, "memory_put", &put_links("memory_put", "Y", links)),
        (3, "memory_delete", &del("Y")),
        (4, "tombstone_put", &put_links("tombstone_put", "Y", links)),
        (5, "tombstone_delete", &del("Y")),
        (6, "memory_put", &put_links("memory_put", "Y", links)),
        (7, "memory_delete", &del("Y")),
        (8, "tombstone_put", &put_links("tombstone_put", "Y", links)),
        (9, "tombstone_delete", &del("Y")),
    ]);
    let h = history(&conn, "X");
    let got: Vec<(i64, String, PyValue)> = h
        .rows
        .iter()
        .map(|r| {
            let t = r
                .detail
                .last()
                .map(|d| d.1.clone())
                .unwrap_or(PyValue::None);
            (r.seq, r.action.clone(), t)
        })
        .collect();
    let want = |s: i64, a: &str, t: &str| (s, a.to_owned(), PyValue::str(t));
    assert_eq!(
        got,
        vec![
            (1, "created".to_owned(), PyValue::None),
            want(2, "linked_from", "a"),
            want(2, "linked_from", "b"),
            want(9, "unlinked", "a"),
            want(9, "unlinked", "b"),
        ]
    );
}

#[test]
fn a_record_in_two_tables_reads_in_the_reference_s_order() {
    // Episodes before tombstones for the state; memories, tombstones,
    // episodes for the kind.
    let conn = store(&[]);
    conn.execute_batch(
        "INSERT INTO episodes VALUES ('X', NULL); INSERT INTO tombstones VALUES ('X');",
    )
    .expect("plant");
    let h = history(&conn, "X");
    assert_eq!(h.state, "active");
    assert_eq!(h.provenance.as_deref(), Some("local"));
    assert_eq!(h.record_kind, Some("memory"));
    assert!(h.findings().is_empty());
    conn.execute_batch("INSERT INTO memories VALUES ('X', 'synced', NULL);")
        .expect("plant");
    let h = history(&conn, "X");
    assert_eq!(h.provenance.as_deref(), Some("synced"));
}

#[test]
fn each_last_word_gives_its_state() {
    let cases: [(&str, &str, &str); 5] = [
        ("memory_delete", r#"{"id": "X"}"#, "deleted"),
        ("tombstone_delete", r#"{"id": "X"}"#, "pruned"),
        ("episode_delete", r#"{"id": "X"}"#, "pruned"),
        ("memory_put", r#"{"memory": {"id": "X"}}"#, "missing"),
        ("tombstone_put", r#"{"tombstone": {"id": "X"}}"#, "missing"),
    ];
    for (kind, payload, state) in cases {
        let h = history(&store(&[(1, kind, payload)]), "X");
        assert_eq!(h.state, state, "{kind}");
        assert_eq!(h.exit_code(), i32::from(state == "missing"), "{kind}");
        assert!(h.known());
    }
    let h = history(&store(&[(1, "remark", r#"{"id": "X"}"#)]), "X");
    assert_eq!((h.state, h.known(), h.exit_code()), ("unknown", false, 0));
}

#[test]
fn a_tombstone_reads_unaccounted_unless_its_put_is_the_last_word() {
    let tomb = r#"{"tombstone": {"id": "X"}}"#;
    let conn = store(&[
        (1, "tombstone_put", tomb),
        (2, "tombstone_delete", &del("X")),
    ]);
    conn.execute_batch("INSERT INTO tombstones VALUES ('X');")
        .expect("plant");
    let h = history(&conn, "X");
    assert_eq!(h.provenance.as_deref(), Some("unaccounted"));
    let conn = store(&[
        (1, "tombstone_put", tomb),
        (2, "verification_put", r#"{"memory_id": "X"}"#),
    ]);
    conn.execute_batch("INSERT INTO tombstones VALUES ('X');")
        .expect("plant");
    let h = history(&conn, "X");
    assert_eq!(h.provenance, None);
    assert_eq!(h.state, "tombstoned");
}

#[test]
fn findings_come_in_the_reference_s_order() {
    let conn = store(&[
        (1, "tombstone_put", r#"{"tombstone": {"id": "X"}}"#),
        (2, "tombstone_delete", &del("X")),
    ]);
    conn.execute_batch(
        "INSERT INTO tombstones VALUES ('X'); UPDATE log SET mac = 'bad' WHERE seq = 2;",
    )
    .expect("plant");
    let h = history(&conn, "X");
    assert_eq!(
        h.findings(),
        [
            "seq 2 (tombstone_delete) fails its MAC",
            "the tombstoned record reads unaccounted: no log row this store signed put it as it stands",
        ]
    );
    assert_eq!(h.exit_code(), 1);
    let dict = h.to_dict(false);
    let PyValue::Dict(items) = &dict else {
        panic!("a dict")
    };
    let keys: Vec<PyValue> = items.iter().map(|(k, _)| k.clone()).collect();
    let names = [
        "format",
        "version",
        "id",
        "record_kind",
        "state",
        "provenance",
        "key_present",
        "findings",
        "rows",
    ];
    assert_eq!(keys, names.map(PyValue::str));
    assert_eq!(items[0].1, PyValue::str(FORMAT));
    assert_eq!(items[1].1, PyValue::int(FORMAT_VERSION));
}

fn detail(kind: &str, payload: &str) -> Vec<(String, String)> {
    let payload = json::loads(&cps(payload)).expect("json");
    replaced_detail(kind, &payload)
        .into_iter()
        .map(|(k, v)| (format!("{k:?}"), format!("{v:?}")))
        .collect()
}

#[test]
fn replaced_detail_reads_each_seq_as_the_reference() {
    let int = |s: &str| format!("{:?}", PyValue::int_from_decimal(s).expect("int"));
    let key = |s: &str| format!("{:?}", PyValue::str(s));
    // `log_end_before` stands in for `head_after` only past a restore.
    assert_eq!(
        detail("store_restored", r#"{"log_end_before": {"seq": 7}}"#),
        [(key("from_seq"), int("7"))]
    );
    assert_eq!(
        detail("history_accepted", r#"{"log_end_before": {"seq": 7}}"#),
        [(key("to_seq"), int("7")), (key("from_seq"), int("7"))]
    );
    // An empty mapping has no seq; a bool is not an int; any size.
    assert_eq!(
        detail(
            "history_accepted",
            r#"{"head_after": {}, "head_before": {"seq": true}, "log_end_before": {"seq": 5}}"#
        ),
        [(key("from_seq"), int("5"))]
    );
    let big = "100000000000000000000000000000000000000000";
    assert_eq!(
        detail(
            "store_restored",
            &format!(r#"{{"head_before": {{"seq": {big}}}, "log_end_before": {{"seq": 99}}}}"#)
        ),
        [(key("from_seq"), int(big))]
    );
    assert_eq!(
        detail(
            "store_restored",
            r#"{"head_before": {"seq": -2}, "log_end_before": {"seq": -10}, "bundle_created_at": null}"#
        ),
        [(key("from_seq"), int("-2"))]
    );
    assert_eq!(
        detail(
            "store_restored",
            r#"{"head_after": {"seq": 3}, "bundle_created_at": "b"}"#
        ),
        [
            (key("bundle_created_at"), key("b")),
            (key("to_seq"), int("3"))
        ]
    );
}

#[test]
fn errors_surface_in_the_reference_s_order() {
    // The key fingerprint is read before the id is bound.
    let conn = store(&[]);
    conn.execute_batch("DELETE FROM meta").expect("meta");
    let source = ConnSource(&conn);
    let ring = keyring();
    let reader = Provenance::new(&source, &ring, false, false).expect("reader");
    let got = record_history(&source, &ring, &reader, &[0xD800]);
    assert!(
        matches!(got, Err(HistoryError::NoKeyFingerprint)),
        "{got:?}"
    );
    // A lone surrogate is UnicodeEncodeError when bound.
    let conn = store(&[]);
    let source = ConnSource(&conn);
    let got = record_history(&source, &ring, &reader_of(&source, &ring), &[0x41, 0xDC80]);
    assert!(
        matches!(
            got,
            Err(HistoryError::Store(StoreError::Py(
                PyError::UnicodeEncodeError
            )))
        ),
        "{got:?}"
    );
    // The snapshot is rolled back: the connection is in autocommit again.
    assert!(conn.is_autocommit());
}

fn reader_of<'a>(source: &'a ConnSource<'a>, ring: &'a KeyRing) -> Provenance<'a> {
    Provenance::new(source, ring, false, false).expect("reader")
}

#[test]
fn ids_at_the_edges_are_text_as_sqlite_sees_it() {
    // JSON text holds no raw NUL, so no payload names an id with one; a
    // table row does.
    let conn = store(&[(1, "memory_put", &put("a\\u0000b"))]);
    let h = history(&conn, "a\u{0}b");
    assert_eq!((h.state, h.rows.len()), ("unknown", 0));
    conn.execute(
        "INSERT INTO memories VALUES (?, 'local', NULL)",
        ["a\u{0}b"],
    )
    .expect("plant");
    let h = history(&conn, "a\u{0}b");
    assert_eq!((h.state, h.record_kind), ("active", Some("memory")));
    assert_eq!(h.record_id, cps("a\u{0}b"));
    // The empty id is in every payload but is no record's.
    let h = history(&conn, "");
    assert_eq!((h.state, h.rows.len()), ("unknown", 0));
}
