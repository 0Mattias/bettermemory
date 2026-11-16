//! A native transaction killed anywhere is whole or absent: the store
//! opens and both builds verify its chain ok, the head never names a row
//! the log lacks, and a further transaction succeeds and verifies.
//!
//! The starting store: created natively (`store_create.json`'s values),
//! opened, three records written (the second superseding the first),
//! closed. The test binary runs itself as the writing child
//! (`tx_kill_child`, ignored when run alone), which opens it, in WAL or
//! (switched on its connection) rollback-journal mode, and commits two
//! transactions: (0) the superseding record removed, a record put, a
//! nested scope that writes a record and fails, a record put superseding
//! the third, and the commit's `returned_put` for the first (four rows);
//! (1) the mark released (`returned_delete`), a record put, a record
//! removed (three rows). The child ends at each [`WriteBoundary`] it
//! passes (counted in order: each row's `Appended` and `Applied`, the
//! nested scope's rolled-back row's among them, `Committed`, the head's
//! lock and move), by `abort` and by `exit`; on top, children slowed a
//! millisecond per boundary are killed from outside at pseudo-random
//! delays. After each kill, read first through a read-write connection (a
//! hot journal rolls back there): the record tables and the marks equal
//! those a dry run left at that log length (so only 0, 4 or 7 new rows),
//! the FTS5 index passes its `integrity-check`, the head names a row the
//! log holds or is behind it, the native check verifies ok, and a further
//! native transaction succeeds and verifies ok. Copies are then opened,
//! verified and counted by the reference in one child, when
//! `BM_REFERENCE_PYTHON` imports `bettermemory` (else a PARITY set-aside).

#![cfg(unix)]

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use bettermemory_core::store::create;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::write::{WriteBoundary, WriteError};
use common::created::{self, reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::stores::scratch;
use common::written::{self, instant, ulid};

const ROOT_ENV: &str = "BM_TEST_TX_ROOT";
const AT_ENV: &str = "BM_TEST_TX_KILL_AT";
const HOW_ENV: &str = "BM_TEST_TX_KILL_HOW";
const MODE_ENV: &str = "BM_TEST_TX_MODE";

const MODES: [&str; 2] = ["wal", "delete"];
const TXS: usize = 2;

fn clock() -> create::Fixed {
    written::clock(&[instant("2001-02-03T06:00:00.000001Z")])
}

fn open(c: &Path, mode: &str) -> WritableStore {
    let store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("opened");
    if mode == "delete" {
        store
            .connection()
            .query_row("PRAGMA journal_mode = DELETE", [], |_| Ok(()))
            .expect("journal mode");
    }
    store
}

fn put(n: u64, sup: &[&str]) -> bettermemory_core::pyvalue::PyValue {
    let sup: Vec<String> = sup.iter().map(|s| (*s).to_owned()).collect();
    let refs: Vec<&str> = sup.iter().map(String::as_str).collect();
    written::memory_payload(&ulid(n, "01J"), &format!("record {n}"), &["global"], &refs)
}

fn delete(n: u64) -> bettermemory_core::pyvalue::PyValue {
    written::payload(&format!("{{\"id\": \"{}\"}}", ulid(n, "01J")))
}

/// The child's transactions, in order.
fn transact(store: &mut WritableStore, t: usize, hook: &mut dyn FnMut(WriteBoundary)) {
    let mut clock = clock();
    let mut tx = store.begin_with(&mut clock, hook).expect("begin");
    let s = Some("kill");
    if t == 0 {
        tx.mutate("memory_delete", &delete(2), s).expect("delete");
        tx.mutate("memory_put", &put(10, &[]), s).expect("put");
        let failed: Result<(), WriteError> = tx.scope(|tx| {
            tx.mutate("memory_put", &put(11, &[]), s)?;
            Err(WriteError::NotAnObject)
        });
        assert!(failed.is_err());
        tx.mutate("memory_put", &put(12, &[&ulid(3, "01J")]), s)
            .expect("put");
    } else {
        assert!(tx.release_returned(&ulid(1, "01J"), s).expect("release"));
        tx.mutate("memory_put", &put(13, &[]), s).expect("put");
        tx.mutate("memory_delete", &delete(10), s).expect("delete");
    }
    tx.commit().expect("commit");
}

/// The writing child: `$ROOT/c` opened in `$MODE`, its transactions
/// committed, ended at `$AT` (`tx,n`: the n-th boundary of a transaction)
/// by `$HOW` (abort, exit), or slowed (`slow`) for a kill from outside.
/// Writes each point passed to `$ROOT/progress`.
#[test]
#[ignore = "run by the kill test as its child"]
fn tx_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let at = std::env::var(AT_ENV).ok();
    let how = std::env::var(HOW_ENV).unwrap_or_default();
    let mode = std::env::var(MODE_ENV).unwrap_or_default();
    let progress = root.join("progress");
    let mut store = open(&root.join("c"), &mode);
    for t in 0..TXS {
        let mut n = 0;
        let mut hook = |_: WriteBoundary| {
            let point = format!("{t},{n}");
            n += 1;
            let _ = fs::write(&progress, &point);
            if how == "slow" {
                std::thread::sleep(Duration::from_millis(1));
            }
            if at.as_deref() == Some(point.as_str()) {
                if how == "abort" {
                    std::process::abort();
                }
                std::process::exit(9);
            }
        };
        transact(&mut store, t, &mut hook);
    }
    store.close().expect("closed");
}

/// The starting store, made once and copied for each kill.
fn base(dir: &Path) -> PathBuf {
    let c = dir.join("base");
    let doc = created::fixture();
    create::create(&c, &c.join("keys"), &mut created::fixed(&doc)).expect("create");
    let mut store = open(&c, "wal");
    let one = ulid(1, "01J");
    for p in [put(1, &[]), put(2, &[&one]), put(3, &[])] {
        store
            .mutate("memory_put", &p, Some("base"), &mut clock())
            .expect("base write");
    }
    store.close().expect("close");
    c
}

/// The record tables and the marks as one text, and the log's length.
fn tables(file: &Path) -> (String, i64) {
    let (t, n) = written::tables(file);
    let conn =
        rusqlite::Connection::open_with_flags(file, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .expect("read");
    let marks: Vec<String> = conn
        .prepare("SELECT record_id || ' ' || via FROM returned ORDER BY record_id")
        .expect("marks")
        .query_map([], |r| r.get(0))
        .expect("query")
        .collect::<rusqlite::Result<_>>()
        .expect("marks");
    (format!("{t} [{}]", marks.join(", ")), n)
}

/// The tables after each transaction of a dry run, by log length, and
/// how many boundaries each transaction passes.
fn dry_run(base: &Path, dir: &Path, id: &str) -> (BTreeMap<i64, String>, Vec<usize>) {
    let c = dir.join("dry");
    written::keep_copy(base, &c, id);
    let file = c.join("memory.sqlite");
    let mut out = BTreeMap::new();
    let mut counts = Vec::new();
    let mut store = open(&c, "wal");
    let (t, n) = tables(&file);
    out.insert(n, t);
    for t in 0..TXS {
        let mut count = 0;
        transact(&mut store, t, &mut |_| count += 1);
        counts.push(count);
        let (t, n) = tables(&file);
        out.insert(n, t);
    }
    store.close().expect("close");
    (out, counts)
}

type Job = (usize, Option<(usize, usize)>, &'static str, Option<u64>);

struct Outcome {
    at: Option<(usize, usize)>,
    mode: usize,
    reached: Option<String>,
    rows: i64,
    copy: PathBuf,
    records: usize,
}

fn one_kill(
    root: &Path,
    base: &Path,
    snaps: &BTreeMap<i64, String>,
    job: Job,
    id: &str,
) -> Outcome {
    let (mode, at, how, delay) = job;
    let c = root.join("c");
    written::keep_copy(base, &c, id);
    let mut cmd = Command::new(std::env::current_exe().expect("exe"));
    cmd.args([
        "--exact",
        "tx_kill_child",
        "--ignored",
        "--nocapture",
        "--test-threads=1",
    ])
    .env(ROOT_ENV, root)
    .env(HOW_ENV, how)
    .env(MODE_ENV, MODES[mode])
    .stdout(std::process::Stdio::null())
    .stderr(std::process::Stdio::null());
    if let Some((t, n)) = at {
        cmd.env(AT_ENV, format!("{t},{n}"));
    }
    let mut child = cmd.spawn().expect("child");
    if let Some(us) = delay {
        std::thread::sleep(Duration::from_micros(us));
        let _ = child.kill();
    }
    let _ = child.wait();
    let reached = fs::read_to_string(root.join("progress")).ok();
    let file = c.join("memory.sqlite");
    // A hot journal is rolled back by the first connection that may write.
    {
        let conn = rusqlite::Connection::open_with_flags(
            &file,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
        )
        .expect("open read-write, creating nothing");
        conn.busy_timeout(Duration::from_secs(10)).expect("busy");
        conn.query_row("SELECT count(*) FROM sqlite_master", [], |_| Ok(()))
            .expect("first read");
    }
    let (got, rows) = tables(&file);
    let label = format!("{} {at:?} {how} reached {reached:?}", MODES[mode]);
    assert_eq!(
        snaps.get(&rows),
        Some(&got),
        "after a kill ({label}) the tables are not those of {rows} log rows"
    );
    written::fts_intact(&file).unwrap_or_else(|e| panic!("the index after {label}: {e}"));
    if let Some((seq, named)) = written::head_in_log(&c.join("keys"), &file, id) {
        assert!(named, "the head names a row the log lacks: {label}");
        assert!(seq <= rows, "the head is ahead of the log: {label}");
    }
    let copy = root.join("copy");
    written::keep_copy(&c, &copy, id);
    let (status, records) = written::native_verify(&c, id);
    assert_eq!(status, "ok", "{label}");
    // A further transaction succeeds and verifies.
    let mut again = open(&c, "wal");
    let mut clock = clock();
    let mut tx = again.begin(&mut clock).expect("begin after");
    tx.mutate("memory_put", &put(99, &[]), Some("after"))
        .unwrap_or_else(|e| panic!("a further write after {label}: {e}"));
    tx.commit().expect("commit after");
    again.close().expect("close");
    let (status, after) = written::native_verify(&c, id);
    assert_eq!(status, "ok", "after a further write: {label}");
    assert_eq!(after, records + 1);
    Outcome {
        at,
        mode,
        reached,
        rows,
        copy,
        records,
    }
}

const REFERENCE: &str = r#"
import json, sys
from bettermemory.store import Store
out = []
args = sys.argv[1:]
for i in range(0, len(args), 2):
    s = Store.open(args[i], keys_dir=args[i + 1], allow_rekey=True)
    try:
        out.append([s.log_verify()["status"], sum(1 for _ in s.iter_memories())])
    finally:
        s.close()
print(json.dumps(out))
"#;

#[test]
fn a_killed_transaction_is_whole_or_absent() {
    let dir = scratch("tx-kills");
    let id = common::stores::store_id();
    let base = base(&dir.0);
    let (snaps, counts) = dry_run(&base, &dir.0, &id);
    assert_eq!(snaps.len(), TXS + 1, "each transaction adds rows");
    let lengths: Vec<i64> = snaps.keys().copied().collect();
    assert_eq!(
        lengths.windows(2).map(|w| w[1] - w[0]).collect::<Vec<_>>(),
        [4, 3]
    );
    let mut jobs: Vec<Job> = Vec::new();
    for mode in 0..MODES.len() {
        for (t, &count) in counts.iter().enumerate() {
            for n in 0..count {
                for how in ["abort", "exit"] {
                    jobs.push((mode, Some((t, n)), how, None));
                }
            }
        }
    }
    let mut seed: u64 = 0x2545_f491_4f6c_dd1d;
    for k in 0..60 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        jobs.push((k % MODES.len(), None, "slow", Some(20_000 + seed % 120_000)));
    }
    let jobs: Vec<(usize, Job)> = jobs.into_iter().enumerate().collect();
    let outcomes: Vec<Outcome> = std::thread::scope(|scope| {
        let workers: Vec<_> = jobs
            .chunks(jobs.len().div_ceil(6))
            .map(|chunk| {
                let (dir, base, snaps, id) = (&dir.0, &base, &snaps, &id);
                scope.spawn(move || {
                    chunk
                        .iter()
                        .map(|&(k, job)| {
                            let root = dir.join(format!("k{k}"));
                            fs::create_dir(&root).expect("root");
                            one_kill(&root, base, snaps, job, id)
                        })
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        workers
            .into_iter()
            .flat_map(|w| w.join().expect("worker"))
            .collect()
    });
    let mut table: BTreeMap<String, usize> = BTreeMap::new();
    for o in &outcomes {
        if let Some((t, n)) = o.at {
            assert_eq!(
                o.reached.as_deref(),
                Some(format!("{t},{n}").as_str()),
                "the child stopped elsewhere"
            );
        }
        let at =
            o.at.map_or("killed".to_owned(), |(t, n)| format!("tx {t} point {n}"));
        *table
            .entry(format!("{} {at}: log {}", MODES[o.mode], o.rows))
            .or_default() += 1;
    }
    for (label, count) in &table {
        eprintln!("{label} x{count}");
    }
    eprintln!(
        "{} kills; boundaries per transaction {counts:?}",
        outcomes.len()
    );
    assert!(outcomes.len() >= 100);
    match reference_python() {
        Err(why) => {
            eprintln!("NOT RUN the reference's half: {why}");
            record(
                "a_killed_transaction_is_whole_or_absent",
                "reference Python",
                "i",
                0,
                &["the reference's open and verify of each killed transaction".to_owned()],
                &[why],
            );
        }
        Ok(python) => {
            let args: Vec<PathBuf> = outcomes
                .iter()
                .flat_map(|o| [o.copy.join("memory.sqlite"), o.copy.join("keys")])
                .collect();
            let refs: Vec<&Path> = args.iter().map(PathBuf::as_path).collect();
            let out = run_reference(&python, &dir.0.join("home"), REFERENCE, &refs);
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            let said: Vec<(String, usize)> =
                serde_json::from_slice(out.stdout.trim_ascii()).expect("JSON");
            assert_eq!(said.len(), outcomes.len());
            for (o, (status, n)) in outcomes.iter().zip(&said) {
                assert_eq!(status, "ok");
                assert_eq!(*n, o.records);
            }
            eprintln!("the reference opened and verified {} stores ok", said.len());
            record(
                "a_killed_transaction_is_whole_or_absent",
                "reference Python",
                "i",
                said.len(),
                &[],
                &[],
            );
        }
    }
}
