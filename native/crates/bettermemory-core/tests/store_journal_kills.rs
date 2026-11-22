//! Cases a plausible wrong journal read or proof fails: `created` sorted
//! as text where the reference sorts instants (and the reverse for a
//! swarm), ties not kept in rowid order, a TTL of zero read as no TTL,
//! the cutoff compared as an instant, the not-found text, the bytes
//! counted as characters, the `sqlite3` classes, and the proof's
//! clean-up on a connection with its own transaction open.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::store::journal::{self, JournalError};
use bettermemory_core::store::upgrade::{
    prove_exclusive, refusal, sqlite_class, UpgradeError, TRANSACTION_WRITER_REVISION,
};
use bettermemory_core::unicode;
use rusqlite::Connection;

const SCHEMA: &str = "CREATE TABLE episodes (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, \
    created TEXT NOT NULL, body TEXT NOT NULL, scopes_json TEXT NOT NULL DEFAULT '[]', \
    takeaway TEXT, origin_json TEXT, is_floor INTEGER NOT NULL DEFAULT 0, swarm_id TEXT, \
    log_mac TEXT); CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, session TEXT, \
    kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, mac TEXT NOT NULL);";

fn id(n: usize) -> String {
    format!("01HZZZZZZZZZZZZZZZZZ{n:06}")
}

fn store(rows: &[(&str, &str, Option<&str>)]) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(SCHEMA).unwrap();
    for (i, (session, created, swarm)) in rows.iter().enumerate() {
        conn.execute(
            "INSERT INTO episodes (id, session_id, created, body, takeaway, swarm_id) \
             VALUES (?, ?, ?, 'b\u{e9}\n', '\u{20ac}', ?)",
            rusqlite::params![id(i), session, created, swarm],
        )
        .unwrap();
    }
    conn
}

fn ids(list: &[bettermemory_core::records::Episode]) -> Vec<String> {
    list.iter()
        .map(|e| unicode::to_string(&e.id).unwrap())
        .collect()
}

fn at(text: &str) -> Option<DateTime> {
    Some(DateTime::from_isoformat(&unicode::cps(text)).unwrap())
}

#[test]
fn a_session_sorts_by_instant_and_keeps_ties_in_rowid_order() {
    let conn = store(&[
        ("s", "2026-01-01T11:00:00+00:00", Some("w")),
        ("s", "2026-01-01T12:00:00+02:00", Some("w")),
        ("s", "2026-01-01T10:00:00Z", Some("w")),
        ("s", "2026-01-01T09:00:00-01:00", Some("w")),
    ]);
    let by_session = journal::episodes_by_session(&conn, "s").unwrap();
    assert_eq!(ids(&by_session), [id(1), id(2), id(3), id(0)]);
    let by_swarm = journal::episodes_by_swarm(&conn, "w").unwrap();
    assert_eq!(ids(&by_swarm), [id(3), id(2), id(0), id(1)]);
}

#[test]
fn a_naive_and_an_aware_created_do_not_sort() {
    let conn = store(&[("s", "2026-01-01T11:00:00+00:00", None)]);
    let mut list = journal::iter_episodes(&conn, None).unwrap();
    let mut naive = list[0].clone();
    naive.created.offset_us = None;
    assert!(journal::sort_by_created(&mut list).is_ok());
    list.push(naive.clone());
    assert!(matches!(
        journal::sort_by_created(&mut list),
        Err(JournalError::NaiveAware)
    ));
    let mut alone = vec![naive];
    assert!(journal::sort_by_created(&mut alone).is_ok());
}

#[test]
fn the_ttl_and_the_cutoff_are_the_references() {
    let conn = store(&[
        ("old", "2020-01-01T00:00:00+00:00", None),
        ("edge", "2026-01-30T00:00:00+00:00", None),
    ]);
    let now = at("2026-03-01T00:00:00+00:00");
    for ttl in [0, -1, i64::MIN] {
        assert!(journal::prunable_episode_sessions(&conn, ttl, None)
            .unwrap()
            .is_empty());
    }
    assert_eq!(
        journal::prunable_episode_sessions(&conn, 30, now).unwrap(),
        ["old"]
    );
    // The same instant in another offset: the cutoff
    // "2026-01-30T01:00:00+01:00" sorts after "2026-01-30T00:00:00+00:00"
    // as text, so the edge session goes though the instants are equal.
    assert_eq!(
        journal::prunable_episode_sessions(&conn, 30, at("2026-03-01T01:00:00+01:00")).unwrap(),
        ["old", "edge"]
    );
    assert_eq!(
        journal::prunable_episode_sessions(&conn, 29, at("2026-03-01T00:00:01")).unwrap(),
        ["old", "edge"]
    );
    let over = journal::prunable_episode_sessions(&conn, 1_000_000_000, now).unwrap_err();
    assert_eq!(over.class(), "OverflowError");
    let under = journal::prunable_episode_sessions(&conn, 999_999_999, now).unwrap_err();
    assert_eq!(under.class(), "OverflowError");
}

#[test]
fn the_volume_counts_utf8_bytes_and_the_absent_id_is_named() {
    let conn = store(&[
        ("a", "2026-01-01T00:00:00Z", None),
        ("b", "2026-01-01T00:00:00Z", None),
    ]);
    let v = journal::episode_volume(&conn, 30, at("2026-01-02T00:00:00+00:00")).unwrap();
    assert_eq!(
        (v.sessions, v.episodes, v.bytes, v.prunable_sessions),
        (2, 2, 14, 0)
    );
    let e = journal::get_episode(&conn, "x'y").unwrap_err();
    assert_eq!(e.class(), "NotFoundError");
    let text = unicode::to_string(&e.absent().unwrap()).unwrap();
    assert_eq!(text, "no record x'y in the store");
}

#[test]
fn the_sqlite3_classes_are_cpythons() {
    let fail = |code: i32| rusqlite::Error::SqliteFailure(rusqlite::ffi::Error::new(code), None);
    assert_eq!(
        sqlite_class(&fail(rusqlite::ffi::SQLITE_BUSY)),
        "OperationalError"
    );
    assert_eq!(
        sqlite_class(&fail(rusqlite::ffi::SQLITE_IOERR_READ)),
        "OperationalError"
    );
    assert_eq!(
        sqlite_class(&fail(rusqlite::ffi::SQLITE_CONSTRAINT)),
        "IntegrityError"
    );
    assert_eq!(
        sqlite_class(&fail(rusqlite::ffi::SQLITE_CORRUPT)),
        "DatabaseError"
    );
    assert_eq!(
        sqlite_class(&fail(rusqlite::ffi::SQLITE_NOTADB)),
        "DatabaseError"
    );
    assert_eq!(
        sqlite_class(&fail(rusqlite::ffi::SQLITE_MISUSE)),
        "InterfaceError"
    );
    assert_eq!(
        sqlite_class(&fail(rusqlite::ffi::SQLITE_RANGE)),
        "InterfaceError"
    );
    assert_eq!(
        sqlite_class(&fail(rusqlite::ffi::SQLITE_TOOBIG)),
        "DataError"
    );
}

#[test]
fn the_proof_refuses_inside_a_transaction_and_puts_everything_back() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE t (x); PRAGMA busy_timeout = 77; BEGIN; INSERT INTO t VALUES (1)",
    )
    .unwrap();
    let e = prove_exclusive(&conn, "a\"b").unwrap_err();
    let UpgradeError::Version(v) = e else {
        panic!("{e:?}")
    };
    assert_eq!(v, refusal("a\"b"));
    assert!(v.message.starts_with("\"a\\\"b\" needs a newer writer;"));
    assert!(conn.is_autocommit());
    let rows: i64 = conn
        .query_row("SELECT COUNT(*) FROM t", [], |r| r.get(0))
        .unwrap();
    assert_eq!(rows, 0);
    let timeout: i64 = conn
        .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
        .unwrap();
    assert_eq!(timeout, 77);
    let mode: String = conn
        .query_row("PRAGMA locking_mode", [], |r| r.get(0))
        .unwrap();
    assert_eq!(mode, "normal");
    assert!(prove_exclusive(&conn, "x").is_ok());
}

#[test]
fn the_constants_are_the_references() {
    assert_eq!(journal::DEFAULT_EPISODE_TTL_DAYS, 30);
    assert_eq!(TRANSACTION_WRITER_REVISION, 5);
}

#[test]
fn session_ids_keep_first_appearance_order() {
    let conn = store(&[
        ("s2", "2026-01-01T00:00:00Z", None),
        ("s1", "2026-01-01T00:00:00Z", None),
        ("s2", "2026-01-01T00:00:00Z", None),
    ]);
    assert_eq!(journal::episode_session_ids(&conn).unwrap(), ["s2", "s1"]);
}

#[test]
fn the_cutoff_is_exact_to_the_microsecond() {
    let now = at("2026-03-01T00:00:00+00:00").unwrap();
    assert_eq!(
        journal::cutoff(30, now).unwrap(),
        "2026-01-30T00:00:00+00:00"
    );
    let conn = store(&[("near", "2026-01-29T23:59:59.999980+00:00", None)]);
    assert_eq!(
        journal::prunable_episode_sessions(&conn, 30, Some(now)).unwrap(),
        ["near"]
    );
}

#[test]
fn many_ties_keep_rowid_order() {
    // 96 rows over four instants, each instant spelled in four offsets,
    // laid out so no run is already sorted.
    let offsets = ["+00:00", "+01:00", "-02:00", "+05:30"];
    let mut rows = Vec::new();
    let mut keys = Vec::new();
    for i in 0..96usize {
        let hour = 3 - (i * 7) % 4;
        let text = match offsets[(i / 4) % 4] {
            "+00:00" => format!("2026-01-01T{:02}:00:00+00:00", 10 + hour),
            "+01:00" => format!("2026-01-01T{:02}:00:00+01:00", 11 + hour),
            "-02:00" => format!("2026-01-01T{:02}:00:00-02:00", 8 + hour),
            _ => format!("2026-01-01T{:02}:30:00+05:30", 15 + hour),
        };
        rows.push(text);
        keys.push((hour, i));
    }
    let spec: Vec<(&str, &str, Option<&str>)> =
        rows.iter().map(|t| ("s", t.as_str(), None)).collect();
    let conn = store(&spec);
    keys.sort();
    let want: Vec<String> = keys.iter().map(|&(_, i)| id(i)).collect();
    assert_eq!(
        ids(&journal::episodes_by_session(&conn, "s").unwrap()),
        want
    );
}

#[test]
fn a_blob_session_id_is_a_decline() {
    let conn = store(&[]);
    conn.execute(
        "INSERT INTO episodes (id, session_id, created, body) \
         VALUES ('x', X'00', '2026-01-01T00:00:00Z', 'b')",
        [],
    )
    .unwrap();
    let e = journal::episode_session_ids(&conn).unwrap_err();
    assert!(matches!(e, JournalError::NotPorted("BLOB")), "{e:?}");
    assert_eq!(e.class(), "NotPorted");
}
