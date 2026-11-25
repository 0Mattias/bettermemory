//! Inputs on which a plausible wrong port of `restore_rows.py` answers
//! otherwise than the reference does, each named by the mistake it
//! catches: the clock asked where a replaced head decides; MACs compared
//! as bytes; Rust's `==` taken for Python's (1 against 1.0 and True); the
//! decision skipped inside the write lock; a control row counted as a
//! record; the reason said twice; the record written without its lock.
//! The answers are the reference's (`restore_rows.jsonl` pins the same
//! inputs where a single call shows them).

#![cfg(unix)]

mod common;
mod restore_rows;

use std::thread;
use std::time::Duration;

use bettermemory_core::fsutil::flock_excl;
use bettermemory_core::store::restore_rows::{append_for, settle, Warning};
use bettermemory_core::{json, unicode};
use restore_rows::{
    clock, entry, mac1, open_case, record_entries, record_of, row, rows_after, EARLY, LATE,
};
use serde_json::{json, Value};

fn settled(opened: &mut restore_rows::Opened) -> Vec<Warning> {
    let mut log = Vec::new();
    settle(&mut opened.store, &mut clock(), &mut log).expect("settle");
    log
}

/// The created store with `rows` past its first and a record of the
/// entries `entries` makes from that row's MAC.
fn with_record(rows: &[Value], entries: impl Fn(&str) -> Vec<Value>) -> restore_rows::Opened {
    let opened = open_case("kills", rows, &Value::Null, true);
    let mac = mac1(&opened);
    restore_rows::put_record(&opened.record, &record_of(&entries(&mac)));
    opened
}

#[test]
fn a_replaced_head_that_is_not_here_leaves_the_clock_unasked() {
    // Rows past the bundle's head stamped before the restore, and a
    // replaced head past the bundle's head that this log does not hold:
    // the MAC says this is the restored history, so the row is appended.
    // A port asking the clock here withholds it as `replaced`.
    let rows = [
        row(2, "index_rebuilt", EARLY),
        row(3, "capture_open", EARLY),
    ];
    let mut opened = with_record(&rows, |mac| {
        vec![entry(
            mac,
            json!({"head_before": {"seq": 3, "mac": "other"}}),
            &[],
        )]
    });
    let log = settled(&mut opened);
    assert_eq!(
        rows_after(&opened.file, 3),
        vec![(4, "store_restored".to_owned())]
    );
    assert!(
        matches!(log.as_slice(), [Warning::Named { seq: 4, .. }]),
        "{log:?}"
    );
}

#[test]
fn a_replaced_head_on_the_log_withholds_whatever_the_clock_says() {
    // Rows stamped after the restore, the replaced head on the log by its
    // MAC: withheld as `replaced`. A port asking the clock first appends.
    let rows = [row(2, "index_rebuilt", LATE), row(3, "capture_open", LATE)];
    let mut opened = with_record(&rows, |mac| {
        vec![entry(
            mac,
            json!({"head_before": {"seq": 3, "mac": "m3"}}),
            &[],
        )]
    });
    let log = settled(&mut opened);
    assert!(rows_after(&opened.file, 3).is_empty());
    match log.as_slice() {
        [Warning::Withheld {
            row, clock: None, ..
        }] => assert_eq!(row.because, "replaced"),
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_mac_is_compared_as_the_text_str_gives_it() {
    // The row at head_after holds its MAC as a blob; `str()` of it is
    // "b'ab'", which the entry names. A port comparing bytes calls the
    // log another history.
    let rows = [
        json!({"seq": 2, "ts": LATE, "kind": "index_rebuilt", "payload": "{}",
               "mac": {"blob": "6162"}}),
        row(3, "capture_open", LATE),
    ];
    let mut opened = with_record(&rows, |mac| {
        vec![entry(
            mac,
            json!({"head_after": {"seq": 2, "mac": "b'ab'"}}),
            &[],
        )]
    });
    settled(&mut opened);
    assert_eq!(rows_after(&opened.file, 3).len(), 1);
}

#[test]
fn an_entry_is_found_by_python_equality() {
    // The record holds restored_at 1; the restore's own entry says 1.0.
    // Python finds it pending, appends one row and returns it. A port
    // using Rust's equality appends the entry twice and returns the
    // second row.
    let mut opened = with_record(&[], |mac| vec![entry(mac, json!({"restored_at": 1}), &[])]);
    let mac = mac1(&opened);
    let own = entry(&mac, json!({"restored_at": 1.0}), &[]);
    let own = json::loads(&unicode::cps(&own.to_string())).expect("JSON");
    let mut log = Vec::new();
    let row = append_for(&mut opened.store, &mut clock(), &own, &mut log).expect("append_for");
    assert_eq!(row.map(|r| r.seq), Some(2));
    assert_eq!(rows_after(&opened.file, 1).len(), 1);
}

#[test]
fn true_and_one_are_one_place_in_the_record() {
    // Two entries, restored_at true and 1, otherwise apart: the first's
    // row is named in the last place that equals it (the second's), and
    // the second's then in the first's. A port using Rust's equality
    // names each in its own place.
    let mut opened = with_record(&[], |mac| {
        vec![
            entry(mac, json!({"restored_at": true, "bundle": "/b/t"}), &[]),
            entry(mac, json!({"restored_at": 1, "bundle": "/b/1"}), &[]),
        ]
    });
    settled(&mut opened);
    let entries = record_entries(&opened.record);
    assert_eq!(entries[0]["bundle"], "/b/t");
    assert_eq!(entries[0]["row"]["seq"], 3);
    assert_eq!(entries[1]["row"]["seq"], 2);
}

#[test]
fn a_control_row_past_the_head_is_no_record() {
    let rows = [
        row(2, "index_rebuilt", LATE),
        row(3, "capture_close", LATE),
        row(4, "history_accepted", LATE),
        row(5, "key_imported", LATE),
    ];
    let mut opened = with_record(&rows, |mac| vec![entry(mac, json!({}), &[])]);
    settled(&mut opened);
    assert_eq!(
        rows_after(&opened.file, 5),
        vec![(6, "store_restored".to_owned())]
    );
}

#[test]
fn a_reason_is_said_once() {
    // Withheld for a record row; then that row's MAC changes, so the
    // withholding no longer stands and is decided again, for the same
    // reason: written into the record without a word.
    let rows = [row(2, "memory_put", LATE)];
    let mut opened = with_record(&rows, |mac| vec![entry(mac, json!({}), &[])]);
    assert_eq!(settled(&mut opened).len(), 1);
    opened
        .store
        .connection()
        .execute_batch("UPDATE log SET mac = 'm2b' WHERE seq = 2")
        .expect("update");
    assert!(settled(&mut opened).is_empty());
    let entries = record_entries(&opened.record);
    assert_eq!(entries[0]["row_withheld"]["mac"], "m2b");
}

#[test]
fn the_decision_is_taken_again_inside_the_write_lock() {
    // Another connection holds the write lock and, inside it, appends the
    // entry's row (as a racing writing open would). The settle reads
    // outside the lock and finds nothing, waits for the lock, and inside
    // it finds the row and names it. A port that does not decide again
    // appends a second row.
    let mut opened = with_record(&[], |mac| vec![entry(mac, json!({}), &[])]);
    let mac = mac1(&opened);
    let payload = {
        let mut p = entry(&mac, json!({"engine_version": "8.0.0"}), &["row"]);
        p["kind"] = json!("store_restored");
        p.to_string()
    };
    let holder = rusqlite::Connection::open(&opened.file).expect("holder");
    holder.execute_batch("BEGIN IMMEDIATE").expect("lock");
    holder
        .execute(
            "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) \
             VALUES (2, ?, NULL, 'store_restored', ?, 'p', 'raced')",
            rusqlite::params![LATE, payload],
        )
        .expect("insert");
    let releaser = thread::spawn(move || {
        thread::sleep(Duration::from_millis(500));
        holder.execute_batch("COMMIT").expect("commit");
    });
    let log = settled(&mut opened);
    releaser.join().expect("released");
    assert_eq!(
        rows_after(&opened.file, 1),
        vec![(2, "store_restored".to_owned())]
    );
    assert!(
        matches!(log.as_slice(), [Warning::Named { seq: 2, .. }]),
        "{log:?}"
    );
    assert_eq!(record_entries(&opened.record)[0]["row"]["mac"], "raced");
}

#[test]
fn the_record_is_written_under_its_lock() {
    // The record's lock held elsewhere: the settle appends its row, then
    // waits for the lock before it names the row. A port writing without
    // the lock names it while the lock is held.
    let mut opened = with_record(&[], |mac| vec![entry(mac, json!({}), &[])]);
    let before = std::fs::read(&opened.record).expect("record");
    let held = flock_excl(&opened.record, ".lock").expect("lock");
    let record = opened.record.clone();
    let checker = thread::spawn(move || {
        thread::sleep(Duration::from_millis(500));
        let during = std::fs::read(&record).expect("record");
        drop(held);
        during
    });
    settled(&mut opened);
    let during = checker.join().expect("checked");
    assert_eq!(during, before, "the record changed while its lock was held");
    assert_eq!(record_entries(&opened.record)[0]["row"]["seq"], 2);
}
