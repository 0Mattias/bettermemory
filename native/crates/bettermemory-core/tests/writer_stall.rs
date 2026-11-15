//! What a native verify costs a writer on a rollback-journal store read in
//! place: a writer commits one small row every 5 ms while verifies run
//! back to back, and its commit latencies are printed (median, p99,
//! worst), with the longest single read-lock hold of the batched verify.
//! The store is the `intact` base switched to rollback-journal mode with
//! `BM_STALL_ROWS` (default 300,000) chained telemetry rows appended.

mod common;

use std::fs;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bettermemory_core::log::{self, head_file_text, Key, LogRow};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::StoreFile;
use common::stores::{case_store, store_id};

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

fn append(conn: &rusqlite::Connection, key: &Key, last: &LogRow, n: i64) -> LogRow {
    let payload = PyValue::Dict(vec![(PyValue::str("n"), PyValue::int(i128::from(n)))]);
    let row = log::next_row(
        Some(last),
        key.bytes(),
        "telemetry",
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
    .expect("insert");
    row
}

fn pct(sorted: &[Duration], p: f64) -> Duration {
    sorted[((sorted.len() as f64 - 1.0) * p).round() as usize]
}

#[test]
#[ignore = "a timing; run with --ignored --nocapture in release mode"]
fn writer_latency_under_native_verify() {
    let rows: i64 = std::env::var("BM_STALL_ROWS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(300_000);
    let seconds: u64 = std::env::var("BM_STALL_SECONDS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(20);
    let (dir, _) = case_store("intact");
    let id = store_id();
    let keys = dir.0.join("keys");
    let store = dir.0.join("memory.sqlite");
    let key = Key::new(fs::read(keys.join(format!("{id}.key"))).expect("key"));
    {
        let conn = rusqlite::Connection::open(&store).expect("open");
        let mode: String = conn
            .query_row("PRAGMA journal_mode = DELETE", [], |r| r.get(0))
            .expect("mode");
        assert_eq!(mode, "delete");
        conn.execute_batch("BEGIN").expect("begin");
        let mut last = last_row(&conn);
        for n in 0..rows {
            last = append(&conn, &key, &last, n);
        }
        conn.execute_batch("COMMIT").expect("commit");
        fs::write(
            keys.join(format!("{id}.head")),
            head_file_text(last.seq, &last.mac, "t"),
        )
        .expect("head");
    }
    let stop = Arc::new(AtomicBool::new(false));
    let writer = {
        let (store, keys, stop, id, key) = (
            store.clone(),
            keys.clone(),
            stop.clone(),
            id.clone(),
            Key::new(key.bytes().to_vec()),
        );
        std::thread::spawn(move || {
            let conn = rusqlite::Connection::open(&store).expect("writer");
            conn.busy_timeout(Duration::from_secs(10)).expect("busy");
            let mut latencies = Vec::new();
            let mut n = 0i64;
            while !stop.load(Ordering::Relaxed) {
                let started = Instant::now();
                let last = last_row(&conn);
                let row = append(&conn, &key, &last, -1 - n);
                latencies.push(started.elapsed());
                let tmp = keys.join(format!("{id}.head.tmp"));
                fs::write(&tmp, head_file_text(row.seq, &row.mac, "t")).expect("head");
                fs::rename(&tmp, keys.join(format!("{id}.head"))).expect("rename");
                n += 1;
                std::thread::sleep(Duration::from_millis(5));
            }
            latencies
        })
    };
    let ring = load_key_ring(&keys, &id).expect("ring");
    let started = Instant::now();
    let mut verifies = 0;
    let mut longest_hold = Duration::ZERO;
    let mut statuses = std::collections::BTreeMap::<String, usize>::new();
    while started.elapsed() < Duration::from_secs(seconds) {
        let reader = StoreFile::open(&store).expect("open");
        assert!(reader.reads_in_batches().expect("mode"));
        let fp = reader
            .meta()
            .expect("meta")
            .into_iter()
            .find(|(k, _)| k == "key_fingerprint")
            .expect("fp")
            .1;
        let report = reader.verify_chain(&ring, &fp).expect("verify");
        let bad: Vec<_> = report
            .problems
            .iter()
            .filter(|p| {
                !p.iter()
                    .any(|(k, v)| *k == "problem" && format!("{v:?}").contains("head"))
            })
            .collect();
        assert!(bad.is_empty(), "chain problems under a writer: {bad:?}");
        *statuses
            .entry(format!("{:?}", report.head[0].1))
            .or_default() += 1;
        longest_hold = longest_hold.max(reader.longest_verify_hold());
        verifies += 1;
    }
    stop.store(true, Ordering::Relaxed);
    let mut latencies = writer.join().expect("writer");
    latencies.sort();
    eprintln!(
        "{rows} rows: {verifies} native verifies in {:?}; {} commits: median {:?}, p99 {:?}, \
         worst {:?}; longest read-lock hold {:?}; head statuses {statuses:?}",
        started.elapsed(),
        latencies.len(),
        pct(&latencies, 0.5),
        pct(&latencies, 0.99),
        latencies.last().copied().unwrap_or_default(),
        longest_hold,
    );
}

/// A writer appending at least a batch of rows for every batch read
/// cannot keep a pass going: at one row a batch, with two rows appended
/// after every batch, the pass reads the rows there were when it began and
/// `EXTRA_BATCHES` more, then stops, with no row problem; the command's
/// verify then ends `ok` or "the store changed", never `tampered`.
#[test]
fn a_fast_writer_cannot_keep_a_pass_going() {
    use bettermemory_core::store::EXTRA_BATCHES;
    let (dir, _) = case_store("intact");
    let id = store_id();
    let keys = dir.0.join("keys");
    let store = dir.0.join("memory.sqlite");
    let key = Key::new(fs::read(keys.join(format!("{id}.key"))).expect("key"));
    let writer = rusqlite::Connection::open(&store).expect("writer");
    let _: String = writer
        .query_row("PRAGMA journal_mode = DELETE", [], |r| r.get(0))
        .expect("mode");
    let before: i64 = writer
        .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
        .expect("count");
    let ring = load_key_ring(&keys, &id).expect("ring");
    let reader = StoreFile::open(&store).expect("open");
    let fp = reader
        .meta()
        .expect("meta")
        .into_iter()
        .find(|(k, _)| k == "key_fingerprint")
        .expect("fp")
        .1;
    let mut n = 0i64;
    let mut write_two = || {
        for _ in 0..2 {
            let last = last_row(&writer);
            let row = append(&writer, &key, &last, n);
            fs::write(
                keys.join(format!("{id}.head")),
                head_file_text(row.seq, &row.mac, "t"),
            )
            .expect("head");
            n += 1;
        }
    };
    let report = reader
        .verify_chain_in_batches_with(&ring, &fp, 1, &mut write_two)
        .expect("pass");
    assert_eq!(report.rows as i64, before + EXTRA_BATCHES as i64);
    for p in &report.problems {
        let what = format!("{p:?}");
        assert!(
            !what.contains("mac_mismatch")
                && !what.contains("chain_break")
                && !what.contains("seq_gap"),
            "{what}"
        );
    }
    // The rows past where it stopped are in the log, and the head names
    // the last: the command's verify loads again and reads them.
    let mut load = |s: &StoreFile| {
        let fp = s
            .meta()?
            .into_iter()
            .find(|(k, _)| k == "key_fingerprint")
            .expect("fp")
            .1;
        Ok((load_key_ring(&keys, &id).expect("ring"), fp))
    };
    let report = reader.verify_loading(Some(1), &mut load).expect("verify");
    assert_eq!(report.status, "ok");
    assert_eq!(report.rows as i64, before + n);
}

/// A store that really is tampered (a row's payload changed behind its
/// MAC), read while a writer appends after every batch so its head always
/// runs past the rows read: the verify ends `tampered` on its first pass,
/// not "kept changing" after its tries.
#[test]
fn a_tampered_store_under_a_writer_ends_tampered() {
    let (dir, _) = case_store("intact");
    let id = store_id();
    let keys = dir.0.join("keys");
    let store = dir.0.join("memory.sqlite");
    let key = Key::new(fs::read(keys.join(format!("{id}.key"))).expect("key"));
    let writer = rusqlite::Connection::open(&store).expect("writer");
    let _: String = writer
        .query_row("PRAGMA journal_mode = DELETE", [], |r| r.get(0))
        .expect("mode");
    let changed = writer
        .execute(
            "UPDATE log SET payload = payload || ' ' WHERE seq = (SELECT min(seq) FROM log)",
            [],
        )
        .expect("tamper");
    assert_eq!(changed, 1);
    let reader = StoreFile::open(&store).expect("open");
    let mut n = 0i64;
    let mut write_two = || {
        for _ in 0..2 {
            let last = last_row(&writer);
            let row = append(&writer, &key, &last, n);
            fs::write(
                keys.join(format!("{id}.head")),
                head_file_text(row.seq, &row.mac, "t"),
            )
            .expect("head");
            n += 1;
        }
    };
    let mut load = |s: &StoreFile| {
        let fp = s
            .meta()?
            .into_iter()
            .find(|(k, _)| k == "key_fingerprint")
            .expect("fp")
            .1;
        Ok((load_key_ring(&keys, &id).expect("ring"), fp))
    };
    let started = Instant::now();
    let report = reader
        .verify_loading_with(Some(1), &mut load, &mut write_two)
        .expect("a report, not kept changing");
    assert_eq!(report.status, "tampered");
    assert!(!report.problems.is_empty(), "a row problem");
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "{:?}",
        started.elapsed()
    );
}
