//! Hostile input to the restore's hold: a marker field of megabytes, a
//! deeply nested field, a store path SQLite's URI parser would read as
//! syntax, a large file that is not a database, many holds in turn. No
//! panic, and the probe's time grows with nothing but the file's header.
//! POSIX only; `restore_lock_hold_not_posix.rs` records the gap.

#![cfg(unix)]

mod common;
mod timing;

use std::fs;
use std::time::Duration;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::restore_lock::{
    acquire, held_here, lock_path, probe, state_finding, unlock, State, UnlockStatus,
};
use common::stores::scratch;

const WAIT: Duration = Duration::from_millis(200);

fn epoch() -> DateTime {
    DateTime::from_unix_micros_utc(0).expect("time")
}

#[test]
fn a_huge_field_is_written_and_read_back_capped() {
    let root = scratch("hold-robust");
    let store = root.0.join("memory.sqlite");
    fs::write(&store, b"").expect("store");
    let mut held = acquire(&store, WAIT).expect("acquire").expect("held");
    let big = "x".repeat(4 << 20);
    held.mark(&[("bundle", PyValue::str(&big))], &epoch())
        .expect("mark");
    held.release(false);
    // Past the 64 KiB cap the marker reads as cut short: an empty record.
    let finding = state_finding(&store).expect("finding");
    assert_eq!(finding.state, State::Unfinished);
    let report = unlock(&store, true, WAIT).expect("unlock");
    assert_eq!(report.status, UnlockStatus::Cleared);
    assert_eq!(report.marker.map(|m| m.len()), Some(0));
    assert!(!lock_path(&store).exists());
}

#[test]
fn a_deep_field_is_written_or_refused_without_a_panic() {
    let root = scratch("hold-robust");
    let store = root.0.join("memory.sqlite");
    fs::write(&store, b"").expect("store");
    let mut held = acquire(&store, WAIT).expect("acquire").expect("held");
    let mut deep = PyValue::List(Vec::new());
    for _ in 0..5_000 {
        deep = PyValue::List(vec![deep]);
    }
    let _ = held.mark(&[("deep", deep)], &epoch());
    held.release(true);
}

#[test]
fn a_path_sqlite_would_parse_is_probed_by_its_bytes() {
    let root = scratch("hold-robust");
    let dir = root.0.join("a%41 ?#&mode=ro");
    fs::create_dir(&dir).expect("dir");
    let store = dir.join("memory.sqlite");
    let conn = rusqlite::Connection::open(&store).expect("open");
    conn.execute_batch("PRAGMA journal_mode = WAL; CREATE TABLE t (x);")
        .expect("wal");
    let found: String = probe(&store)
        .expect("held")
        .iter()
        .filter_map(|&c| char::from_u32(c))
        .collect();
    assert!(found.contains("a%41 ?#&mode=ro/memory.sqlite"), "{found}");
    drop(conn);
    assert_eq!(probe(&store), None);
    // Nothing was made in a directory the URI could have named instead.
    assert!(!root.0.join("aA").exists());
}

#[test]
fn a_large_file_that_is_not_a_database_is_probed_in_constant_time() {
    let root = scratch("hold-robust");
    let small = root.0.join("small.sqlite");
    let large = root.0.join("large.sqlite");
    fs::write(&small, vec![b'z'; 1 << 16]).expect("small");
    fs::write(&large, vec![b'z'; 10 << 16]).expect("large");
    timing::grows_within(
        "probe of a non-database",
        4,
        &mut || timing::wall(|| assert_eq!(probe(&small), None)),
        &mut || timing::wall(|| assert_eq!(probe(&large), None)),
    );
}

#[test]
fn many_holds_in_turn_leave_nothing_behind() {
    let root = scratch("hold-robust");
    let store = root.0.join("memory.sqlite");
    fs::write(&store, b"").expect("store");
    for i in 0..500 {
        let mut held = acquire(&store, WAIT).expect("acquire").expect("held");
        assert!(held_here(&store).expect("held_here"));
        held.mark(&[("n", PyValue::Int(i.to_string()))], &epoch())
            .expect("mark");
        held.release(true);
        assert!(!held_here(&store).expect("held_here"));
    }
    assert!(!lock_path(&store).exists());
}
