//! Hostile inputs to the journal reads and the proof: many episodes in
//! one session (the sort grows linearly), TTLs at the integer bounds,
//! a session id stored as a blob, and a very long store path.

mod timing;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::store::journal;
use bettermemory_core::store::upgrade::{prove_exclusive, UpgradeError};
use bettermemory_core::unicode;
use rusqlite::Connection;

const SCHEMA: &str = "CREATE TABLE episodes (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, \
    created TEXT NOT NULL, body TEXT NOT NULL, scopes_json TEXT NOT NULL DEFAULT '[]', \
    takeaway TEXT, origin_json TEXT, is_floor INTEGER NOT NULL DEFAULT 0, swarm_id TEXT, \
    log_mac TEXT); CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, session TEXT, \
    kind TEXT NOT NULL, payload TEXT NOT NULL, prev_mac TEXT NOT NULL, mac TEXT NOT NULL);";

fn store(n: usize) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(SCHEMA).unwrap();
    conn.execute_batch("BEGIN").unwrap();
    for i in 0..n {
        let created = format!(
            "2026-01-01T{:02}:{:02}:{:02}+0{}:00",
            (i * 7) % 24,
            (i * 13) % 60,
            i % 60,
            i % 3
        );
        conn.execute(
            "INSERT INTO episodes (id, session_id, created, body) VALUES (?, 's', ?, 'b')",
            rusqlite::params![format!("01HZZZZZZZZZZZZZZZZ{i:07}"), created],
        )
        .unwrap();
    }
    conn.execute_batch("COMMIT").unwrap();
    conn
}

#[test]
fn a_large_session_sorts_in_linear_time() {
    let small = store(2_000);
    let large = store(20_000);
    let run = |conn: &Connection, n: usize| {
        assert_eq!(journal::episodes_by_session(conn, "s").unwrap().len(), n);
    };
    timing::grows_within(
        "episodes_by_session: 2,000 and 20,000 episodes",
        timing::TENFOLD,
        &mut || timing::once(|| run(&small, 2_000)),
        &mut || timing::once(|| run(&large, 20_000)),
    );
}

#[test]
fn ttls_at_the_integer_bounds_answer_without_panic() {
    let conn = store(3);
    let now =
        Some(DateTime::from_isoformat(&unicode::cps("9999-12-31T23:59:59.999999+00:00")).unwrap());
    for ttl in [i64::MIN, -1, 0, 1, 999_999_999, 1_000_000_000, i64::MAX] {
        let _ = journal::prunable_episode_sessions(&conn, ttl, now);
        let _ = journal::episode_volume(&conn, ttl, now);
    }
    let low = Some(DateTime::from_isoformat(&unicode::cps("0001-01-01T00:00:00")).unwrap());
    assert_eq!(
        journal::prunable_episode_sessions(&conn, 1, low)
            .unwrap_err()
            .class(),
        "OverflowError"
    );
}

#[test]
fn a_blob_session_id_declines_without_panic() {
    let conn = store(1);
    conn.execute(
        "INSERT INTO episodes (id, session_id, created, body) VALUES ('x', X'00ff', '2020-01-01', 'b')",
        [],
    )
    .unwrap();
    assert!(journal::episode_session_ids(&conn).is_err());
    assert!(journal::iter_episodes(&conn, None).is_err());
}

#[test]
fn a_long_path_in_the_refusal() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch("BEGIN").unwrap();
    let path = "\u{1b}\u{202e}x".repeat(100_000);
    let Err(UpgradeError::Version(v)) = prove_exclusive(&conn, &path) else {
        panic!("not refused")
    };
    assert!(v.message.len() > 300_000);
    assert!(conn.is_autocommit());
}
