//! Hostile input to `store::event_reads`: a long log read at its end, a
//! naming far past the bound, payloads nested past any budget, huge
//! needles. No panic in debug or release; a window at the log's end
//! costs its own rows, not the log's.

mod timing;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::store::event_reads::{
    events_of_kinds, events_since, iter_events, iter_events_backward,
};
use bettermemory_core::unicode;
use rusqlite::Connection;

fn store(n: usize, payload: impl Fn(usize) -> String) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, session TEXT, \
         kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, \
         mac TEXT NOT NULL); CREATE INDEX log_by_kind ON log(kind, seq); \
         CREATE INDEX log_by_ts ON log(ts); BEGIN",
    )
    .unwrap();
    for i in 0..n {
        let ts = format!("2026-01-01T00:00:00.{i:06}Z");
        conn.execute(
            "INSERT INTO log (ts, kind, payload, prev_mac, mac) VALUES (?, 'use', ?, 'p', 'm')",
            rusqlite::params![ts, payload(i)],
        )
        .unwrap();
    }
    conn.execute(
        "INSERT INTO log (ts, kind, payload, prev_mac, mac) \
         VALUES ('2026-06-01T00:00:00Z', 'use', '{\"id\":\"end\"}', 'p', 'm')",
        [],
    )
    .unwrap();
    conn.execute_batch("COMMIT").unwrap();
    conn
}

fn no_local(_: &DateTime) -> Option<i32> {
    None
}

#[test]
fn a_window_at_the_end_costs_its_rows_not_the_log() {
    let small = store(2_000, |i| format!("{{\"id\":\"m{i}\"}}"));
    let large = store(20_000, |i| format!("{{\"id\":\"m{i}\"}}"));
    let since = DateTime::from_isoformat(&unicode::cps("2026-05-01T00:00:00+00:00")).unwrap();
    timing::grows_within(
        "events_since: the last row of 2,000 and of 20,000",
        timing::TENFOLD,
        &mut || timing::once(|| events_since(&small, &since, &no_local).items.len()),
        &mut || timing::once(|| events_since(&large, &since, &no_local).items.len()),
    );
}

#[test]
fn a_whole_log_reads_in_linear_time() {
    let small = store(2_000, |i| format!("{{\"id\":\"m{i}\"}}"));
    let large = store(20_000, |i| format!("{{\"id\":\"m{i}\"}}"));
    timing::grows_within(
        "iter_events: 2,000 and 20,000 rows",
        timing::TENFOLD,
        &mut || timing::once(|| iter_events(&small, 0).items.len()),
        &mut || timing::once(|| iter_events(&large, 0).items.len()),
    );
}

#[test]
fn a_naming_far_past_the_bound_reads_every_row_of_the_kinds() {
    let conn = store(10, |i| format!("{{\"id\":\"m{i}\"}}"));
    let naming: Vec<PyStr> = (0..50_000)
        .map(|i| unicode::cps(&format!("x{i}")))
        .collect();
    let since = DateTime::from_isoformat(&unicode::cps("2000-01-01T00:00:00Z")).unwrap();
    let read = events_of_kinds(
        &conn,
        &[unicode::cps("use")],
        &since,
        Some(&naming),
        &no_local,
    );
    assert!(read.error.is_none());
    assert_eq!(read.items.len(), 11);
    let huge = vec![unicode::cps(&"\"\\".repeat(500_000))];
    let read = events_of_kinds(
        &conn,
        &[unicode::cps("use")],
        &since,
        Some(&huge),
        &no_local,
    );
    assert!(read.error.is_none());
    assert!(read.items.is_empty());
}

#[test]
fn deeply_nested_payloads_do_not_panic() {
    let deep = format!("{{\"a\":{}{}}}", "[".repeat(100_000), "]".repeat(100_000));
    let conn = store(3, |_| deep.clone());
    let read = iter_events(&conn, 0);
    assert!(read.items.len() + usize::from(read.error.is_some()) >= 1);
    let read = iter_events_backward(&conn);
    assert!(!read.items.is_empty() || read.error.is_some());
}

#[test]
fn integer_bounds_of_since_seq() {
    let conn = store(3, |_| "{}".to_owned());
    assert_eq!(iter_events(&conn, i64::MIN).items.len(), 4);
    assert!(iter_events(&conn, i64::MAX).items.is_empty());
}
