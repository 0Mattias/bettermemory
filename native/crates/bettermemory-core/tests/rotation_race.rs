//! A key rotation by the reference (`key_lifecycle.rotate_key(apply=True)`)
//! racing a native batched verify on a large rollback-journal store: the
//! verify must end `ok`, or `StoreError::KeptChanging` ("the store kept changing, try
//! again"), never `tampered`. Each run copies a pristine store (the
//! `intact` base in DELETE mode with `BM_RACE_ROWS` rows appended, default
//! 300,000), starts the reference with its imports done, starts the
//! verify, and has the reference rotate 0 to 1 s in. Runs both the
//! command's path (`verify_loading`, which loads meta and the ring again)
//! and, for the count before the fix, one pass with the ring loaded once.
//! Needs `BM_REFERENCE_PYTHON`; every reference path (HOME, the store's
//! directories, the state directory) is in the test's scratch.

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Command, Stdio};

use bettermemory_core::log::{self, head_file_text, Key, KeyRing, LogRow};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::{StoreError, StoreFile};
use common::stores::{case_store, scratch, store_id};

const ROTATE: &str = r#"
import json, sys, time
from pathlib import Path
from bettermemory.key_lifecycle import rotate_key
store, keys, state = (Path(a) for a in sys.argv[1:4])
print("ready", flush=True)
delay = float(sys.stdin.readline().split()[1])
time.sleep(delay)
report = rotate_key(store, apply=True, keys_dir=keys, state_dir=state)
print(json.dumps({"status": report.get("status"), "detail": report.get("detail")}), flush=True)
"#;

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

fn copy_dir(from: &Path, to: &Path) {
    fs::create_dir_all(to).expect("dir");
    for entry in fs::read_dir(from).expect("read") {
        let entry = entry.expect("entry");
        let target = to.join(entry.file_name());
        if entry.file_type().expect("type").is_dir() {
            copy_dir(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), target).expect("copy");
        }
    }
}

fn loader(keys: &Path) -> impl FnMut(&StoreFile) -> Result<(KeyRing, String), StoreError> + '_ {
    move |s: &StoreFile| {
        let meta = s.meta()?;
        let get = |k: &str| meta.iter().find(|(key, _)| key == k).expect(k).1.clone();
        let ring = load_key_ring(keys, &get("store_id"))
            .map_err(|e| StoreError::Refused(format!("{e:?}")))?;
        Ok((ring, get("key_fingerprint")))
    }
}

fn pristine(rows: i64) -> common::stores::Scratch {
    let (pristine, _) = case_store("intact");
    let id = store_id();
    {
        let store = pristine.0.join("memory.sqlite");
        let keys = pristine.0.join("keys");
        let key = Key::new(fs::read(keys.join(format!("{id}.key"))).expect("key"));
        let conn = rusqlite::Connection::open(&store).expect("open");
        let mode: String = conn
            .query_row("PRAGMA journal_mode = DELETE", [], |r| r.get(0))
            .expect("mode");
        assert_eq!(mode, "delete");
        conn.execute_batch("BEGIN").expect("begin");
        let mut last = last_row(&conn);
        for n in 0..rows {
            let payload = PyValue::Dict(vec![(PyValue::str("n"), PyValue::int(i128::from(n)))]);
            let row = log::next_row(
                Some(&last),
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
            last = row;
        }
        conn.execute_batch("COMMIT").expect("commit");
        fs::write(
            keys.join(format!("{id}.head")),
            head_file_text(last.seq, &last.mac, "t"),
        )
        .expect("head");
    }
    pristine
}

#[test]
#[ignore = "a race against the reference; run with --ignored --nocapture in release mode"]
fn a_rotation_during_a_batched_verify_is_never_tampered() {
    let python = std::env::var("BM_REFERENCE_PYTHON").expect("BM_REFERENCE_PYTHON");
    let rows: i64 = std::env::var("BM_RACE_ROWS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(300_000);
    let runs: usize = std::env::var("BM_RACE_RUNS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(20);
    let pristine = pristine(rows);
    let mut outcomes: BTreeMap<(&str, String), usize> = BTreeMap::new();
    let mut rotated = 0;
    for run in 0..runs * 2 {
        let mode = if run % 2 == 0 {
            "reloading"
        } else {
            "same_ring_retry"
        };
        let work = scratch("race");
        let dir = work.0.join("store");
        copy_dir(&pristine.0, &dir);
        for sub in ["home", "state", "mem"] {
            fs::create_dir_all(work.0.join(sub)).expect("sub");
        }
        let store = dir.join("memory.sqlite");
        let keys = dir.join("keys");
        let mut child = Command::new(&python)
            .arg("-c")
            .arg(ROTATE)
            .arg(&store)
            .arg(&keys)
            .arg(work.0.join("state"))
            .current_dir(&work.0)
            .env("HOME", work.0.join("home"))
            .env("BETTERMEMORY_DIR", work.0.join("mem"))
            .env("BETTERMEMORY_KEYS_DIR", &keys)
            .env("BETTERMEMORY_STATE_DIR", work.0.join("state"))
            .env("XDG_CONFIG_HOME", work.0.join("home"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("reference");
        let mut out = BufReader::new(child.stdout.take().expect("stdout"));
        let mut line = String::new();
        out.read_line(&mut line).expect("ready");
        assert_eq!(line.trim(), "ready");
        // Spread over 0..1 s, without a random crate.
        let delay = (run as f64 * 0.618_034).fract();
        writeln!(child.stdin.as_mut().expect("stdin"), "go {delay}").expect("go");
        let reader = StoreFile::open(&store).expect("open");
        assert!(reader.reads_in_batches().expect("mode"));
        let mut load = loader(&keys);
        let outcome = if mode == "reloading" {
            reader.verify_loading(None, &mut load)
        } else {
            // N1b's retry: the same ring again, as before the fix.
            let (ring, fp) = load(&reader).expect("load");
            match reader.verify_chain(&ring, &fp) {
                Err(log::VerifyError::Source(StoreError::Changed)) => {
                    reader.verify_chain(&ring, &fp)
                }
                other => other,
            }
        };
        let label = match outcome {
            Ok(report) => report.status.to_owned(),
            Err(log::VerifyError::Source(StoreError::Changed | StoreError::KeptChanging)) => {
                "changed".into()
            }
            Err(e) => format!("error {e:?}"),
        };
        drop(reader);
        line.clear();
        out.read_line(&mut line).expect("report");
        child.wait().expect("wait");
        if line.contains("\"done\"") {
            rotated += 1;
        }
        *outcomes.entry((mode, label)).or_default() += 1;
    }
    eprintln!("{rows} rows, {runs} runs each, {rotated} rotations done: {outcomes:?}");
    for ((mode, label), n) in &outcomes {
        if *mode == "reloading" {
            assert!(label == "ok" || label == "changed", "{mode}: {n} {label}");
        }
    }
}

const FIRST_OPEN: &str = r#"
import sys, time
from pathlib import Path
from bettermemory.store import Store
store, keys = (Path(a) for a in sys.argv[1:3])
print("ready", flush=True)
delay = float(sys.stdin.readline().split()[1])
time.sleep(delay)
started = time.monotonic()
s = Store.open(store, keys_dir=keys)
took = time.monotonic() - started
s.close()
print(f"{took * 1000:.3f}", flush=True)
"#;

/// The reference writer's first open of a rollback-journal store (a
/// restored file: `backup` writes it in DELETE mode) switches it to WAL,
/// which needs the reader's SHARED lock gone. Timed while a native verify
/// reads it in one snapshot and in batches; prints both.
#[test]
#[ignore = "times the reference against a native verify; run with --ignored --nocapture"]
fn a_reference_writers_first_open_under_a_native_verify() {
    let python = std::env::var("BM_REFERENCE_PYTHON").expect("BM_REFERENCE_PYTHON");
    let rows: i64 = std::env::var("BM_RACE_ROWS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(300_000);
    let runs: usize = std::env::var("BM_RACE_RUNS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(10);
    let pristine = pristine(rows);
    let mut waits: BTreeMap<&str, Vec<f64>> = BTreeMap::new();
    for run in 0..runs * 2 {
        let mode = if run % 2 == 0 {
            "batched"
        } else {
            "one_snapshot"
        };
        let work = scratch("first-open");
        let dir = work.0.join("store");
        copy_dir(&pristine.0, &dir);
        for sub in ["home", "state", "mem"] {
            fs::create_dir_all(work.0.join(sub)).expect("sub");
        }
        let store = dir.join("memory.sqlite");
        let keys = dir.join("keys");
        let mut child = Command::new(&python)
            .arg("-c")
            .arg(FIRST_OPEN)
            .arg(&store)
            .arg(&keys)
            .current_dir(&work.0)
            .env("HOME", work.0.join("home"))
            .env("BETTERMEMORY_DIR", work.0.join("mem"))
            .env("BETTERMEMORY_KEYS_DIR", &keys)
            .env("BETTERMEMORY_STATE_DIR", work.0.join("state"))
            .env("XDG_CONFIG_HOME", work.0.join("home"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("reference");
        let mut out = BufReader::new(child.stdout.take().expect("stdout"));
        let mut line = String::new();
        out.read_line(&mut line).expect("ready");
        let delay = 0.1 + 0.3 * (run as f64 * 0.618_034).fract();
        writeln!(child.stdin.as_mut().expect("stdin"), "go {delay}").expect("go");
        let reader = StoreFile::open(&store).expect("open");
        let mut load = loader(&keys);
        if mode == "batched" {
            reader.verify_loading(None, &mut load).expect("verify");
        } else {
            reader
                .read_transaction(|r| {
                    let (ring, fp) = load(r).expect("load");
                    r.verify_chain(&ring, &fp).expect("verify")
                })
                .expect("transaction");
        }
        drop(reader);
        line.clear();
        out.read_line(&mut line).expect("timing");
        child.wait().expect("wait");
        if let Ok(ms) = line.trim().parse::<f64>() {
            waits.entry(mode).or_default().push(ms);
        }
    }
    for (mode, mut ms) in waits {
        ms.sort_by(f64::total_cmp);
        eprintln!(
            "{mode}: {} first opens, median {:.1} ms, worst {:.1} ms",
            ms.len(),
            ms[ms.len() / 2],
            ms[ms.len() - 1]
        );
    }
}
