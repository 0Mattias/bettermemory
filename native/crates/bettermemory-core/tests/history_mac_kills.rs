//! Cases a plausible wrong `RowVerifier` or `set` fails, each with the
//! reference's answer (`history._RowVerifier`, `history._set`).

use bettermemory_core::history::{set, RowVerifier};
use bettermemory_core::log::{compute_mac, fingerprint, Key, KeyRing, LogRow};
use bettermemory_core::pyvalue::PyValue;
use rusqlite::Connection;

const DDL: &str = "CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, \
    session TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, \
    mac TEXT NOT NULL);";

fn key(n: u8) -> Vec<u8> {
    vec![n; 32]
}

fn fp(n: u8) -> String {
    fingerprint(&key(n))
}

fn rekey(old: &str, new: &str) -> String {
    format!("{{\"old_fingerprint\": \"{old}\", \"new_fingerprint\": \"{new}\"}}")
}

fn store(rekeys: &[(i64, String)]) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(DDL).expect("ddl");
    for (seq, payload) in rekeys {
        conn.execute(
            "INSERT INTO log VALUES (?, 't', NULL, 'rekey', ?, 'pm', 'm')",
            rusqlite::params![seq, payload],
        )
        .expect("insert");
    }
    conn
}

fn row(seq: i64, under: u8) -> LogRow {
    let mac = compute_mac(&key(under), seq, "t", None, "write", "{}", "pm");
    LogRow {
        seq,
        ts: "t".into(),
        session: None,
        kind: "write".into(),
        payload: "{}".into(),
        prev_mac: "pm".into(),
        mac,
    }
}

fn ring(current: u8, retired: &[u8]) -> KeyRing {
    let mut ring = KeyRing {
        current: Some(Key::new(key(current))),
        ..KeyRing::default()
    };
    for n in retired {
        ring.retire(Key::new(key(*n)));
    }
    ring
}

#[test]
fn the_earliest_rekey_names_the_first_segment_not_the_store() {
    let conn = store(&[(5, rekey(&fp(1), &fp(2)))]);
    let ring = ring(2, &[1]);
    let mut v = RowVerifier::new(&conn, &ring, &fp(3)).expect("verifier");
    assert_eq!(v.verified(&row(1, 1)), Some(true));
    assert_eq!(v.verified(&row(5, 2)), Some(true));
    assert_eq!(v.verified(&row(4, 2)), Some(false));
}

#[test]
fn a_later_segment_at_an_equal_seq_wins() {
    let conn = store(&[(1, rekey(&fp(1), &fp(2)))]);
    let ring = ring(2, &[1]);
    let mut v = RowVerifier::new(&conn, &ring, &fp(1)).expect("verifier");
    assert_eq!(v.verified(&row(1, 2)), Some(true));
}

#[test]
fn a_row_before_every_segment_is_the_first_segment_s() {
    let conn = store(&[(0, rekey(&fp(1), &fp(2)))]);
    let ring = ring(2, &[1]);
    let mut v = RowVerifier::new(&conn, &ring, &fp(2)).expect("verifier");
    assert_eq!(v.verified(&row(-3, 1)), Some(true));
    assert_eq!(v.verified(&row(1, 2)), Some(true));
}

#[test]
fn a_row_missing_one_name_is_skipped_whole() {
    let conn = store(&[
        (2, format!("{{\"old_fingerprint\": \"{}\"}}", fp(9))),
        (3, rekey(&fp(1), &fp(2))),
    ]);
    let ring = ring(2, &[1]);
    let mut v = RowVerifier::new(&conn, &ring, &fp(2)).expect("verifier");
    assert_eq!(v.verified(&row(1, 1)), Some(true));
}

#[test]
fn a_key_not_held_is_none_not_false() {
    let conn = store(&[]);
    let ring = ring(2, &[]);
    let mut v = RowVerifier::new(&conn, &ring, &fp(1)).expect("verifier");
    assert_eq!(v.verified(&row(1, 1)), None);
    assert_eq!(v.verified(&row(1, 2)), None);
}

#[test]
fn a_name_is_str_of_the_value() {
    // `str(True)` is "True", no fingerprint: the first segment's key is
    // not held.
    let conn = store(&[(
        4,
        format!(
            "{{\"old_fingerprint\": true, \"new_fingerprint\": \"{}\"}}",
            fp(2)
        ),
    )]);
    let ring = ring(2, &[1]);
    let mut v = RowVerifier::new(&conn, &ring, &fp(1)).expect("verifier");
    assert_eq!(v.verified(&row(1, 1)), None);
    assert_eq!(v.verified(&row(4, 2)), Some(true));
}

#[test]
fn set_keeps_a_present_name_in_its_place() {
    let mut detail = vec![
        ("a".to_owned(), PyValue::None),
        ("b".to_owned(), PyValue::Bool(true)),
    ];
    set(
        &mut detail,
        vec![
            ("c", Some(PyValue::Bool(false))),
            ("a", Some(PyValue::Bool(true))),
            ("b", None),
        ],
    );
    assert_eq!(
        detail,
        vec![
            ("a".to_owned(), PyValue::Bool(true)),
            ("b".to_owned(), PyValue::Bool(true)),
            ("c".to_owned(), PyValue::Bool(false)),
        ]
    );
}
