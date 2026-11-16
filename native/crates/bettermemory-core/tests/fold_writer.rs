//! The fold check under a writer. A writer that appends signed
//! `returned_put` rows and applies them in the same transaction, then moves
//! the head file, as the reference's writers do, runs while native
//! `log_verify_loading` runs again and again: on a WAL store and on a
//! rollback-journal store read in place, every report is `ok` (each row is
//! read once for the chain and the fold, so an append during a pass needs
//! no second pass and cannot end in "kept changing"). A store tampered
//! behind the log while the writer runs reports `tampered` every time.
//! Each test stops after a fixed number of verifies, whatever the
//! machine's speed. The verifies start once the writer has committed, so
//! the store is read in place; a reader that took a private copy of a
//! closed store before a writer started has its own test: its report is
//! "kept changing", never tampered, and ok when the store is opened again. And timings (ignored): what the fold's final snapshot
//! costs a writer on a rollback-journal store.

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bettermemory_core::log::{self, head_file_text, Key, KeyRing, LogRow};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::fold::FoldError;
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::{StoreError, StoreFile};
use common::stores::{materialize, scratch, Scratch};
use common::{jsonl, unhex};
use serde_json::json;

const STORE_ID: &str = "0123456789abcdef0123456789abcdef";

/// The `populated` case of `fold.jsonl`, rebuilt.
fn populated() -> Scratch {
    let mut lines = jsonl("fold.jsonl");
    let header = lines.remove(0);
    let mut recipe = Vec::new();
    for op in header["preludes"]["populated"].as_array().expect("prelude") {
        recipe.push(op.clone());
    }
    let dir = scratch("fold-writer");
    materialize(&json!({ "base": "intact", "recipe": recipe }), &dir.0);
    dir
}

fn last_row(conn: &rusqlite::Connection) -> LogRow {
    conn.query_row(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log ORDER BY seq DESC LIMIT 1",
        [],
        |r| {
            Ok(LogRow {
                seq: r.get(0)?,
                ts: r.get(1)?,
                session: r.get(2)?,
                kind: r.get(3)?,
                payload: r.get(4)?,
                prev_mac: r.get(5)?,
                mac: r.get(6)?,
            })
        },
    )
    .expect("last")
}

/// One honest write: the row and its table row in one transaction, then
/// the head file. Returns the commit's latency.
fn write_one(
    conn: &rusqlite::Connection,
    key: &Key,
    keys: &Path,
    n: usize,
    per: usize,
) -> Duration {
    let started = Instant::now();
    conn.execute_batch("BEGIN IMMEDIATE").expect("begin");
    let mut row = last_row(conn);
    for i in 0..per {
        let last = row;
        let record = format!("w{n}-{i}");
        let payload = PyValue::Dict(vec![(
            PyValue::str("returned"),
            PyValue::Dict(vec![
                (PyValue::str("record_id"), PyValue::str(&record)),
                (PyValue::str("returned"), PyValue::str("t")),
                (PyValue::str("via"), PyValue::str("writer")),
            ]),
        )]);
        row = log::next_row(
            Some(&last),
            key.bytes(),
            "returned_put",
            &payload,
            None,
            "2026-01-01T00:00:00+00:00",
        )
        .expect("row");
        conn.execute(
            "INSERT INTO log VALUES (?, ?, ?, ?, ?, ?, ?)",
            rusqlite::params![
                row.seq,
                row.ts,
                row.session,
                row.kind,
                row.payload,
                row.prev_mac,
                row.mac
            ],
        )
        .expect("log row");
        conn.execute(
            "INSERT OR REPLACE INTO returned(record_id, via, returned) VALUES (?, 'writer', 't')",
            [&record],
        )
        .expect("table row");
    }
    conn.execute_batch("COMMIT").expect("commit");
    let took = started.elapsed();
    let head = keys.join(format!("{STORE_ID}.head"));
    let tmp = keys.join(format!("{STORE_ID}.head.tmp"));
    fs::write(&tmp, head_file_text(row.seq, &row.mac, "t")).expect("head");
    // A reader may hold the head open for a moment where a rename cannot
    // replace an open file.
    for _ in 0..100 {
        if fs::rename(&tmp, &head).is_ok() {
            break;
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    took
}

fn loader(keys: PathBuf) -> impl FnMut(&StoreFile) -> Result<(KeyRing, String), StoreError> {
    move |s: &StoreFile| {
        let fp = s
            .meta()?
            .into_iter()
            .find(|(k, _)| k == "key_fingerprint")
            .map(|(_, v)| v)
            .unwrap_or_default();
        let ring =
            load_key_ring(&keys, STORE_ID).map_err(|e| StoreError::Refused(format!("{e:?}")))?;
        Ok((ring, fp))
    }
}

struct Run {
    outcomes: BTreeMap<String, usize>,
    /// The longest a verify held the read lock at once.
    hold: Duration,
    written: usize,
    latencies: Vec<Duration>,
}

/// `reads` native log verifies while a writer commits `per` rows every
/// `every` (back to back for zero).
fn under_writer(dir: &Path, reads: usize, every: Duration, per: usize) -> Run {
    let keys = dir.join("keys");
    let key = Key::new(fs::read(keys.join(format!("{STORE_ID}.key"))).expect("key"));
    let stop = Arc::new(AtomicBool::new(false));
    let commits = Arc::new(AtomicUsize::new(0));
    let writer = {
        let (store, keys, stop) = (dir.join("memory.sqlite"), keys.clone(), stop.clone());
        let commits = commits.clone();
        std::thread::spawn(move || {
            let conn = rusqlite::Connection::open(&store).expect("writer");
            conn.busy_timeout(Duration::from_secs(30)).expect("busy");
            // A scratch store: a commit that syncs holds the write lock
            // for the length of the sync, and on a rollback-journal store
            // on Windows that starved the reader's open (CI run
            // 43797351206).
            conn.execute_batch("PRAGMA synchronous = OFF")
                .expect("synchronous");
            let mut latencies = Vec::new();
            let mut n = 0;
            while !stop.load(Ordering::Relaxed) {
                latencies.push(write_one(&conn, &key, &keys, n, per));
                commits.fetch_add(1, Ordering::Relaxed);
                n += 1;
                if !every.is_zero() {
                    std::thread::sleep(every);
                }
            }
            (n * per, latencies)
        })
    };
    // The writer has the store open and has committed: a WAL store has its
    // `-wal` and `-shm`, so every open below reads it in place.
    while commits.load(Ordering::Relaxed) == 0 {
        std::thread::sleep(Duration::from_millis(1));
    }
    let mut outcomes = BTreeMap::new();
    let mut hold = Duration::ZERO;
    for _ in 0..reads {
        let store = open_under_writer(&dir.join("memory.sqlite"));
        let outcome = match store.log_verify_loading(&mut loader(keys.clone())) {
            Ok(r) => {
                if r.status != "ok" {
                    // What a run on another machine needs to be read by.
                    eprintln!(
                        "not ok: {} | read from a copy: {} | {} problems | fold {}",
                        r.status,
                        store.reads_a_copy(),
                        r.problems.len(),
                        r.fold.status
                    );
                    for t in r.fold.tables.iter().chain(&r.fold.membership) {
                        if !t.unaccounted.is_empty() || !t.missing.is_empty() {
                            eprintln!(
                                "  {}: {} unaccounted, {} missing, {} rows",
                                t.table,
                                t.unaccounted.len(),
                                t.missing.len(),
                                t.rows
                            );
                        }
                    }
                }
                r.status.to_owned()
            }
            Err(FoldError::Store(StoreError::KeptChanging)) => "kept_changing".to_owned(),
            Err(e) => format!("error: {e}"),
        };
        *outcomes.entry(outcome).or_default() += 1;
        hold = hold.max(store.longest_verify_hold());
    }
    stop.store(true, Ordering::Relaxed);
    let (written, latencies) = writer.join().expect("writer");
    Run {
        outcomes,
        hold,
        written,
        latencies,
    }
}

/// Open the store while a writer commits. On a rollback-journal store a
/// reader waits for the writer's lock, and the wait counts toward the 2 s
/// a store file is given to be admitted: an open that ran out of them is
/// tried again (a reader kept out, not a report).
fn open_under_writer(store: &Path) -> StoreFile {
    let mut last = None;
    for _ in 0..20 {
        match StoreFile::open(store) {
            Ok(store) => return store,
            Err(StoreError::Admission(r)) if r.timed_out => last = Some(r.why),
            Err(e) => panic!("open: {e}"),
        }
    }
    panic!("open: kept out 20 times: {last:?}");
}

fn set_mode(dir: &Path, mode: &str) {
    let conn = rusqlite::Connection::open(dir.join("memory.sqlite")).expect("open");
    conn.query_row(&format!("PRAGMA journal_mode = {mode}"), [], |_| Ok(()))
        .expect("journal mode");
}

fn honest(mode: &str, every: Duration, per: usize) {
    let dir = populated();
    set_mode(&dir.0, mode);
    let run = under_writer(&dir.0, 8, every, per);
    eprintln!(
        "{mode}: 8 native log verifies while {} rows were written: {:?}",
        run.written, run.outcomes
    );
    assert!(run.written > 0, "the writer wrote nothing");
    assert_eq!(
        run.outcomes.get("ok"),
        Some(&8),
        "{mode}: an appending writer changed the report: {:?}",
        run.outcomes
    );
    // Quiet again: ok.
    let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
    let report = store
        .log_verify_loading(&mut loader(dir.0.join("keys")))
        .expect("report");
    assert_eq!(report.status, "ok");
}

#[test]
fn an_honest_writer_never_makes_a_wal_store_read_tampered() {
    honest("wal", Duration::from_millis(2), 1);
}

#[test]
fn an_honest_writer_never_makes_a_rollback_journal_store_read_tampered() {
    honest("delete", Duration::from_millis(2), 1);
}

/// 20 rows a transaction every 2 ms.
#[test]
fn a_busy_writer_still_lets_every_verify_end_ok() {
    for mode in ["wal", "delete"] {
        honest(mode, Duration::from_millis(2), 20);
    }
}

/// 20 rows a transaction back to back: the log grows as fast as the
/// writer can sign, so a verify reads ever more rows; a timing.
#[test]
#[ignore = "a timing; run with --ignored --nocapture in release mode"]
fn a_back_to_back_writer_lets_every_verify_end_ok() {
    for mode in ["wal", "delete"] {
        let started = Instant::now();
        honest(mode, Duration::ZERO, 20);
        eprintln!("{mode}: {:?}", started.elapsed());
    }
}

#[test]
fn a_store_tampered_under_a_writer_ends_tampered() {
    for mode in ["wal", "delete"] {
        let dir = populated();
        set_mode(&dir.0, mode);
        let conn = rusqlite::Connection::open(dir.0.join("memory.sqlite")).expect("open");
        conn.execute(
            "INSERT INTO quarantine(name, reason, at) VALUES ('planted', 'r', 't')",
            [],
        )
        .expect("plant");
        drop(conn);
        let run = under_writer(&dir.0, 5, Duration::from_millis(2), 1);
        eprintln!("{mode}, tampered: {:?}", run.outcomes);
        assert_eq!(
            run.outcomes.get("tampered"),
            Some(&5),
            "{mode}: {:?}",
            run.outcomes
        );
    }
}

/// A reader that copied a closed WAL store before a writer started: the
/// head file then names a row the copy lacks. That is no tampering: the
/// check ends "kept changing", and the store opened again reads ok. (On
/// the Linux runner this was read as tampered once in eight verifies, CI
/// run 43797351206.)
#[test]
fn a_copy_taken_before_a_writer_started_is_not_called_tampered() {
    let dir = populated();
    set_mode(&dir.0, "wal");
    let keys = dir.0.join("keys");
    let key = Key::new(fs::read(keys.join(format!("{STORE_ID}.key"))).expect("key"));
    let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
    assert!(store.reads_a_copy(), "a closed WAL store is read as a copy");
    assert!(!store.written_since_copied());
    let conn = rusqlite::Connection::open(dir.0.join("memory.sqlite")).expect("writer");
    write_one(&conn, &key, &keys, 0, 3);
    assert!(store.written_since_copied());
    match store.log_verify_loading(&mut loader(keys.clone())) {
        Err(FoldError::Store(StoreError::KeptChanging)) => {}
        other => panic!("a copy behind the head: {:?}", other.map(|r| r.status)),
    }
    match store.verify_loading(None, &mut loader(keys.clone())) {
        Err(bettermemory_core::log::VerifyError::Source(StoreError::KeptChanging)) => {}
        other => panic!("a copy behind the head, chain only: {:?}", other.is_ok()),
    }
    drop(store);
    // Read in place while the writer has it open, and as a copy once the
    // writer is gone: ok both times.
    let again = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open again");
    assert!(
        !again.reads_a_copy(),
        "the writer's -wal and -shm are there"
    );
    let report = again
        .log_verify_loading(&mut loader(keys.clone()))
        .expect("report");
    assert_eq!(report.status, "ok");
    drop(again);
    drop(conn);
    let closed = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open closed");
    assert!(closed.reads_a_copy());
    let report = closed
        .log_verify_loading(&mut loader(keys.clone()))
        .expect("report");
    assert_eq!(report.status, "ok");
}

/// The other side: a copy whose source nobody wrote since keeps its
/// report. The log's last row removed from a closed store reads tampered.
#[test]
fn a_copy_of_a_store_whose_tail_was_removed_reads_tampered() {
    let dir = populated();
    set_mode(&dir.0, "wal");
    let keys = dir.0.join("keys");
    let conn = rusqlite::Connection::open(dir.0.join("memory.sqlite")).expect("open");
    conn.execute("DELETE FROM log WHERE seq = (SELECT max(seq) FROM log)", [])
        .expect("remove the tail");
    drop(conn);
    let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
    assert!(store.reads_a_copy());
    assert!(!store.written_since_copied());
    let report = store
        .log_verify_loading(&mut loader(keys.clone()))
        .expect("report");
    assert_eq!(report.status, "tampered");
}

/// The writer's commit latency (median, p99, worst) while native log
/// verifies run back to back on a rollback-journal store read in place:
/// the store `BM_FOLD_STALL_STORE` names (a directory holding
/// `memory.sqlite` and `keys`, copied first), else the populated case;
/// `BM_FOLD_STALL_READS` verifies (5), a commit every
/// `BM_FOLD_STALL_EVERY_MS` (5; 0 for back to back).
#[test]
#[ignore = "a timing; run with --ignored --nocapture in release mode"]
fn writer_latency_under_native_log_verify() {
    let dir = match std::env::var_os("BM_FOLD_STALL_STORE") {
        Some(source) => {
            let dir = scratch("fold-stall");
            let source = PathBuf::from(source);
            fs::copy(source.join("memory.sqlite"), dir.0.join("memory.sqlite")).expect("copy");
            fs::create_dir_all(dir.0.join("keys")).expect("keys");
            for entry in fs::read_dir(source.join("keys")).expect("keys") {
                let entry = entry.expect("entry");
                fs::copy(entry.path(), dir.0.join("keys").join(entry.file_name())).expect("key");
            }
            dir
        }
        None => populated(),
    };
    let _ = unhex;
    set_mode(&dir.0, "delete");
    let reads = std::env::var("BM_FOLD_STALL_READS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(5);
    let started = Instant::now();
    // `BM_FOLD_STALL_EVERY_MS`: the writer's pause between commits (0:
    // back to back); 5 ms by default.
    let every = std::env::var("BM_FOLD_STALL_EVERY_MS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(5);
    let run = under_writer(&dir.0, reads, Duration::from_millis(every), 1);
    let mut l = run.latencies.clone();
    l.sort();
    let pct = |p: f64| l[((l.len() - 1) as f64 * p) as usize];
    eprintln!(
        "{reads} verifies in {:?}, {} commits: median {:?}, p99 {:?}, worst {:?}; \
         longest hold {:?}; outcomes {:?}",
        started.elapsed(),
        run.written,
        pct(0.5),
        pct(0.99),
        l.last().copied().unwrap_or_default(),
        run.hold,
        run.outcomes
    );
}

/// The populated case with 3,000 `returned_put` rows appended (three
/// batches of the verify and more), in `mode`; returns the first and a
/// late appended row's seq.
fn long_store(mode: &str) -> (Scratch, i64, i64) {
    let dir = populated();
    set_mode(&dir.0, mode);
    let keys = dir.0.join("keys");
    let key = Key::new(fs::read(keys.join(format!("{STORE_ID}.key"))).expect("key"));
    let conn = rusqlite::Connection::open(dir.0.join("memory.sqlite")).expect("open");
    let first = last_row(&conn).seq + 1;
    for n in 0..30 {
        write_one(&conn, &key, &keys, n, 100);
    }
    let late = last_row(&conn).seq - 200;
    (dir, first, late)
}

/// The `returned` record a writer row at `seq` put.
fn record_of(conn: &rusqlite::Connection, seq: i64) -> String {
    let payload: String = conn
        .query_row("SELECT payload FROM log WHERE seq = ?", [seq], |r| r.get(0))
        .expect("payload");
    let value: serde_json::Value = serde_json::from_str(&payload).expect("json");
    value["returned"]["record_id"]
        .as_str()
        .expect("record")
        .to_owned()
}

/// Each edit a writer or an attacker could make while a pass reads, made
/// from the pass's `between` hook after its first batch (1,024 rows read;
/// `early` is behind the read position, `late` ahead of it).
fn edit(conn: &rusqlite::Connection, kind: &str, early: i64, late: i64) {
    let forge = |seq: i64, row: bool| {
        conn.execute(
            "UPDATE log SET payload = replace(payload, 'writer', 'forged') WHERE seq = ?",
            [seq],
        )
        .expect("payload");
        if row {
            conn.execute(
                "UPDATE returned SET via = 'forged' WHERE record_id = ?",
                [record_of(conn, seq)],
            )
            .expect("row");
        }
    };
    match kind {
        "payload_and_row_early" => forge(early, true),
        "payload_and_row_late" => forge(late, true),
        "row_only" => {
            conn.execute(
                "UPDATE returned SET via = 'forged' WHERE record_id = ?",
                [record_of(conn, early)],
            )
            .expect("row");
        }
        "tail_deleted" => {
            conn.execute(
                "DELETE FROM log WHERE seq > (SELECT max(seq) - 50 FROM log)",
                [],
            )
            .expect("tail");
        }
        "row_planted" => {
            conn.execute(
                "INSERT INTO returned(record_id, via, returned) VALUES ('planted', 'x', 't')",
                [],
            )
            .expect("plant");
        }
        "payload_only_early" => forge(early, false),
        "middle_row_deleted" => {
            conn.execute("DELETE FROM log WHERE seq = ?", [early + 10])
                .expect("delete");
        }
        other => panic!("no edit {other}"),
    }
}

const EDITS: [&str; 7] = [
    "payload_and_row_early",
    "payload_and_row_late",
    "row_only",
    "tail_deleted",
    "row_planted",
    "payload_only_early",
    "middle_row_deleted",
];

/// An edit made during a pass, deterministically (from the `between`
/// hook, not by timing). Rollback journal: every edit that touches a table
/// row, a row not yet read, or removes rows reads `tampered` in that pass;
/// a log payload edited behind the read position, tables untouched, reads
/// `ok` in that pass (the report describes the log as it was read) and
/// `tampered` on the next. WAL: the pass reads the snapshot it began with,
/// so every edit reads `ok` in that pass and `tampered` on the next.
#[test]
fn an_edit_during_a_pass() {
    let mut seen = Vec::new();
    for mode in ["delete", "wal"] {
        for kind in EDITS {
            let (dir, early, late) = long_store(mode);
            let path = dir.0.join("memory.sqlite");
            let editor = rusqlite::Connection::open(&path).expect("editor");
            editor.busy_timeout(Duration::from_secs(10)).expect("busy");
            let mut done = false;
            let store = StoreFile::open(&path).expect("open");
            let during = store
                .log_verify_loading_with(&mut loader(dir.0.join("keys")), &mut || {
                    if !done {
                        done = true;
                        edit(&editor, kind, early, late);
                    }
                })
                .expect("report")
                .status;
            drop(store);
            assert!(done, "{mode} {kind}: the hook never ran");
            let store = StoreFile::open(&path).expect("open");
            let after = store
                .log_verify_loading(&mut loader(dir.0.join("keys")))
                .expect("report")
                .status;
            seen.push(format!("{mode} {kind}: {during}, then {after}"));
            let want = if mode == "wal" || kind == "payload_only_early" {
                "ok"
            } else {
                "tampered"
            };
            assert_eq!(during, want, "{mode} {kind}");
            assert_eq!(after, "tampered", "{mode} {kind}, the next verify");
        }
    }
    eprintln!("{}", seen.join("\n"));
}
