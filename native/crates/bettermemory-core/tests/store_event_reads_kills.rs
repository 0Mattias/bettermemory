//! Cases a plausible wrong port of `store::event_reads` fails: a cut on
//! the whole stamp instead of its nineteen-character prefix, a cut that
//! rounds the instant up, a naming bound off by one, an empty naming read
//! as no filter, a hint without its fallback, the local offset applied
//! with the wrong sign, a floor that trusts the log's order, a payload
//! read raw where the reference reads its `str()`.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::event_reads::{
    capture_rows, events_of_kinds, events_since, iter_events, session_events_of_kinds,
    window_floor, CAPTURE_KINDS, NAMING_BOUND,
};
use bettermemory_core::unicode;
use rusqlite::Connection;

const DDL: &str = "CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, \
    session TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, \
    mac TEXT NOT NULL); CREATE INDEX log_by_kind ON log(kind, seq); \
    CREATE INDEX log_by_ts ON log(ts); CREATE INDEX log_by_mac ON log(mac); \
    CREATE INDEX log_by_session ON log(session, seq);";

fn store(rows: &[(&str, &str, &str)]) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(DDL).unwrap();
    for (ts, kind, payload) in rows {
        conn.execute(
            "INSERT INTO log (ts, session, kind, payload, prev_mac, mac) \
             VALUES (?, 's', ?, ?, 'p', 'm')",
            rusqlite::params![ts, kind, payload],
        )
        .unwrap();
    }
    conn
}

fn at(text: &str) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(text)).unwrap()
}

fn no_local(_: &DateTime) -> Option<i32> {
    None
}

fn ids(events: &[PyValue]) -> Vec<String> {
    events
        .iter()
        .map(|e| match e {
            PyValue::Dict(items) => items.iter().find(|(k, _)| *k == PyValue::str("id")).map_or(
                "-".to_owned(),
                |(_, v)| match v {
                    PyValue::Str(s) => unicode::to_string(s).unwrap(),
                    _ => "?".to_owned(),
                },
            ),
            _ => "!".to_owned(),
        })
        .collect()
}

fn kinds(names: &[&str]) -> Vec<PyStr> {
    names.iter().map(|n| unicode::cps(n)).collect()
}

#[test]
fn the_cut_compares_the_prefix_so_both_spellings_match() {
    let conn = store(&[
        ("2026-01-01T00:00:05+00:00", "use", r#"{"id":"plus"}"#),
        ("2026-01-01T00:00:05Z", "use", r#"{"id":"zulu"}"#),
        ("2026-01-01T00:00:04Z", "use", r#"{"id":"before"}"#),
    ]);
    let read = events_since(&conn, &at("2026-01-01T00:00:05.900000+00:00"), &no_local);
    assert!(read.error.is_none());
    assert_eq!(ids(&read.items), ["plus", "zulu"]);
}

#[test]
fn a_naive_since_takes_the_local_offset_east() {
    let conn = store(&[
        ("2026-01-01T00:00:00Z", "use", r#"{"id":"a"}"#),
        ("2026-01-01T05:30:00Z", "use", r#"{"id":"b"}"#),
    ]);
    let east = |_: &DateTime| Some(19_800);
    let read = events_since(&conn, &at("2026-01-01T05:30:00"), &east);
    assert_eq!(ids(&read.items), ["a", "b"]);
    let read = events_since(&conn, &at("2026-01-01T05:30:00"), &no_local);
    assert_eq!(read.error.unwrap().class(), "native:not_ported");
}

#[test]
fn the_naming_bound_is_two_hundred_needles() {
    assert_eq!(NAMING_BOUND, 200);
    let conn = store(&[
        ("2026-01-01T00:00:00Z", "use", r#"{"id":"m1"}"#),
        ("2026-01-01T00:00:01Z", "use", r#"{"id":"m2"}"#),
    ]);
    let since = at("2025-01-01T00:00:00+00:00");
    let mut naming: Vec<PyStr> = (0..199).map(|i| unicode::cps(&format!("n{i}"))).collect();
    naming.push(unicode::cps("m1"));
    let read = events_of_kinds(&conn, &kinds(&["use"]), &since, Some(&naming), &no_local);
    assert_eq!(ids(&read.items), ["m1"]);
    naming.push(unicode::cps("n_extra"));
    let read = events_of_kinds(&conn, &kinds(&["use"]), &since, Some(&naming), &no_local);
    assert_eq!(ids(&read.items), ["m1", "m2"]);
    naming.push(unicode::cps("m1"));
    let read = events_of_kinds(&conn, &kinds(&["use"]), &since, Some(&naming), &no_local);
    assert_eq!(ids(&read.items), ["m1", "m2"], "duplicates count once");
    let read = events_of_kinds(&conn, &kinds(&["use"]), &since, Some(&[]), &no_local);
    assert!(read.items.is_empty());
    let read = events_of_kinds(&conn, &kinds(&["use"]), &since, None, &no_local);
    assert_eq!(read.items.len(), 2);
}

#[test]
fn a_needle_is_spelled_as_the_log_spells_a_string() {
    let conn = store(&[("2026-01-01T00:00:00Z", "use", r#"{"id":"a\"b"}"#)]);
    let since = at("2025-01-01T00:00:00+00:00");
    let naming = [unicode::cps("a\"b")];
    let read = events_of_kinds(&conn, &kinds(&["use"]), &since, Some(&naming), &no_local);
    assert_eq!(read.items.len(), 1);
}

#[test]
fn a_dropped_index_falls_back_to_the_plain_query() {
    let conn = store(&[
        ("2026-01-02T00:00:00Z", "use", r#"{"id":"late"}"#),
        ("2026-01-01T00:00:00Z", "use", r#"{"id":"early"}"#),
    ]);
    conn.execute_batch("DROP INDEX log_by_ts; DROP INDEX log_by_session")
        .unwrap();
    assert_eq!(
        window_floor(&conn, "2026-01-01T00:00:00", None).unwrap(),
        Some(1)
    );
    assert_eq!(
        window_floor(&conn, "2026-01-01T12:00:00", Some(0)).unwrap(),
        None
    );
    let read = session_events_of_kinds(&conn, &unicode::cps("s"), &kinds(&["use"]));
    assert_eq!(ids(&read.items), ["late", "early"]);
}

#[test]
fn a_row_stamped_late_lowers_the_floor_and_the_cut_still_holds() {
    let conn = store(&[
        ("2026-01-05T00:00:00Z", "use", r#"{"id":"import"}"#),
        ("2026-01-01T00:00:00Z", "use", r#"{"id":"old"}"#),
        ("2026-01-03T00:00:00Z", "use", r#"{"id":"new"}"#),
    ]);
    let read = events_since(&conn, &at("2026-01-02T00:00:00+00:00"), &no_local);
    assert_eq!(ids(&read.items), ["import", "new"]);
}

#[test]
fn iter_events_reads_the_payload_as_its_str() {
    let conn = store(&[]);
    conn.execute(
        "INSERT INTO log (ts, kind, payload, prev_mac, mac) VALUES ('2026', 'use', ?, 'p', 'm')",
        [br#"{"id":"blob"}"#.to_vec()],
    )
    .unwrap();
    assert!(iter_events(&conn, 0).items.is_empty());
    let read = events_since(&conn, &at("2000-01-01T00:00:00+00:00"), &no_local);
    assert_eq!(ids(&read.items), ["blob"]);
}

#[test]
fn capture_rows_read_only_capture_kinds() {
    assert_eq!(
        CAPTURE_KINDS,
        [
            "capture_close",
            "capture_member",
            "capture_open",
            "capture_relations",
            "capture_undo"
        ]
    );
    let conn = store(&[
        ("2026", "capture_open", "{}"),
        ("2026", "use", "{}"),
        ("2026", "capture_undo", "{}"),
    ]);
    let read = capture_rows(&conn, &kinds(&["use", "capture_undo", "capture_open"]), 1);
    assert_eq!(read.items.iter().map(|r| r.seq).collect::<Vec<_>>(), [3]);
}

#[test]
fn a_cut_out_of_range_raises_overflow() {
    let conn = store(&[("2026", "use", "{}")]);
    let read = events_since(&conn, &at("0001-01-01T00:00:00+01:00"), &no_local);
    assert_eq!(read.error.unwrap().class(), "OverflowError");
    let read = events_of_kinds(
        &conn,
        &[],
        &at("0001-01-01T00:00:00+01:00"),
        None,
        &no_local,
    );
    assert!(read.error.is_none(), "no kinds asks nothing");
}

#[test]
fn the_head_bound_of_the_floor_is_inclusive() {
    let conn = store(&[
        ("2025-12-31T00:00:00Z", "use", "{}"),
        ("2025-12-31T00:00:00Z", "use", "{}"),
        ("2026-01-01T00:00:00Z", "use", "{}"),
    ]);
    assert_eq!(
        window_floor(&conn, "2026-01-01T00:00:00", Some(3)).unwrap(),
        Some(3)
    );
    assert_eq!(
        window_floor(&conn, "2026-01-01T00:00:00", Some(2)).unwrap(),
        None
    );
}

#[test]
fn events_of_kinds_keeps_a_row_stamped_at_the_cut() {
    let conn = store(&[("2026-01-01T00:00:00", "use", "{\"id\":\"a\"}")]);
    let read = events_of_kinds(
        &conn,
        &kinds(&["use"]),
        &at("2026-01-01T00:00:00+00:00"),
        None,
        &no_local,
    );
    assert!(read.error.is_none());
    assert_eq!(ids(&read.items), vec!["a"]);
}

#[test]
fn too_many_kinds_is_an_operational_error() {
    let conn = store(&[("2026-01-01T00:00:00Z", "use", "{}")]);
    let many: Vec<PyStr> = (0..40_000)
        .map(|i| unicode::cps(&format!("k{i}")))
        .collect();
    let read = events_of_kinds(
        &conn,
        &many,
        &at("2025-01-01T00:00:00+00:00"),
        None,
        &no_local,
    );
    assert_eq!(read.error.unwrap().class(), "OperationalError");
    let read = session_events_of_kinds(&conn, &unicode::cps("s"), &many);
    assert_eq!(read.error.unwrap().class(), "OperationalError");
}
