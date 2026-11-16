//! A native `mark_verified` or `record_corroboration` killed anywhere is
//! whole or absent: the store opens and both builds verify its chain ok,
//! the head never names a row the log lacks, and a further write succeeds
//! and verifies.
//!
//! The starting store: created natively, two records put, closed. The test
//! binary runs itself as the writing child (`records_c_kill_child`,
//! ignored when run alone), which opens it in WAL or rollback-journal mode
//! and makes three calls: (0) `mark_verified` of the first record with
//! paths, a target map and a head (`memory_put`, `verification_put`: two
//! rows); (1) `record_corroboration` of the second (one row); (2)
//! `mark_verified` of the second (two rows). The child ends at each
//! boundary a call passes, by `abort` and by `exit`; on top, slowed
//! children are killed from outside at pseudo-random delays. After each
//! kill, read first through a read-write connection: the record tables
//! and the marks equal those a dry run left at that log length, the
//! verifications table holds the dry run's rows at that length, every column,
//! the FTS5 index is intact, the head names a row the log holds or is
//! behind it, the native check verifies ok, and a further write succeeds
//! and verifies ok. Copies are then opened, verified and counted by the
//! reference in one child (else a PARITY set-aside).

#![cfg(unix)]

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Memory;
use bettermemory_core::store::create;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::write::WriteBoundary;
use bettermemory_core::store::writers::{Put, Verify};
use common::created::{self, reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::record_calls::Minting;
use common::stores::scratch;
use common::written::{self, instant, ulid};

const ROOT_ENV: &str = "BM_TEST_RECORDS_C_ROOT";
const AT_ENV: &str = "BM_TEST_RECORDS_C_KILL_AT";
const HOW_ENV: &str = "BM_TEST_RECORDS_C_KILL_HOW";
const MODE_ENV: &str = "BM_TEST_RECORDS_C_MODE";

const MODES: [&str; 2] = ["wal", "delete"];
const CALLS: usize = 3;

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

fn rec(n: u64, sup: &[u64]) -> Memory {
    let sup: Vec<String> = sup.iter().map(|t| ulid(*t, "01J")).collect();
    let refs: Vec<&str> = sup.iter().map(String::as_str).collect();
    let p = written::memory_payload(&ulid(n, "01J"), &format!("record {n}"), &["global"], &refs);
    Memory::from_payload(p.get("memory").expect("memory")).expect("valid")
}

fn provider(next: u64) -> Minting<create::Fixed> {
    Minting {
        inner: clock(),
        next,
    }
}

/// The child's calls, in order.
fn call(store: &mut WritableStore, t: usize, hook: &mut dyn FnMut(WriteBoundary)) {
    let mut p = provider(100 + t as u64);
    let s = Some("kill");
    let paths = written::payload("[\"a.py\", \"b.py\"]");
    let targets = written::payload("{\"a.py\": null, \"b.py\": \"t\"}");
    let head = PyValue::str(&"ab".repeat(20));
    let verify = Verify {
        verified_paths: Some(&paths),
        verified_path_targets: Some(&targets),
        verified_head: Some(&head),
        session: s,
        ..Verify::default()
    };
    match t {
        0 => {
            store
                .mark_verified_hooked(&mut p, &ulid(1, "01J"), verify, Some(hook))
                .expect("verify");
        }
        1 => {
            let done = store
                .record_corroboration_hooked(&mut p, &ulid(2, "01J"), s, Some(hook))
                .expect("corroborate");
            assert!(done.value.is_some());
        }
        _ => {
            store
                .mark_verified_hooked(&mut p, &ulid(2, "01J"), verify, Some(hook))
                .expect("verify");
        }
    }
}

/// The writing child (module docs). Writes each point passed to
/// `$ROOT/progress`.
#[test]
#[ignore = "run by the kill test as its child"]
fn records_c_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let at = std::env::var(AT_ENV).ok();
    let how = std::env::var(HOW_ENV).unwrap_or_default();
    let mode = std::env::var(MODE_ENV).unwrap_or_default();
    let progress = root.join("progress");
    let mut store = open(&root.join("c"), &mode);
    for t in 0..CALLS {
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
        call(&mut store, t, &mut hook);
    }
    store.close().expect("closed");
}

fn base(dir: &Path) -> PathBuf {
    let c = dir.join("base");
    let doc = created::fixture();
    create::create(&c, &c.join("keys"), &mut created::fixed(&doc)).expect("create");
    let mut store = open(&c, "wal");
    for m in [rec(1, &[]), rec(2, &[])] {
        store
            .put_memory(&mut clock(), &m, Put::default())
            .expect("base write");
    }
    store.close().expect("close");
    c
}

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
    let verified: Vec<String> = conn
        .prepare(
            "SELECT json_array(quote(memory_id), quote(machine_id), quote(verified_at), \
             quote(verified_head), quote(verified_paths_json), \
             quote(verified_commits_json), quote(verified_versions_json), \
             quote(verified_absent_paths_json)) \
             FROM verifications ORDER BY rowid",
        )
        .expect("verifications")
        .query_map([], |r| r.get(0))
        .expect("query")
        .collect::<rusqlite::Result<_>>()
        .expect("verifications");
    (
        format!("{t} [{}] [{}]", marks.join(", "), verified.join(", ")),
        n,
    )
}

fn dry_run(base: &Path, dir: &Path, id: &str) -> (BTreeMap<i64, String>, Vec<usize>) {
    let c = dir.join("dry");
    written::keep_copy(base, &c, id);
    let file = c.join("memory.sqlite");
    let mut out = BTreeMap::new();
    let mut counts = Vec::new();
    let mut store = open(&c, "wal");
    let (t, n) = tables(&file);
    out.insert(n, t);
    for t in 0..CALLS {
        let mut count = 0;
        call(&mut store, t, &mut |_| count += 1);
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
        "records_c_kill_child",
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
    let mut again = open(&c, "wal");
    again
        .put_memory(&mut provider(900), &rec(99, &[]), Put::default())
        .unwrap_or_else(|e| panic!("a further write after {label}: {e}"));
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
fn a_killed_verification_is_whole_or_absent() {
    let dir = scratch("records-c-kills");
    let id = common::stores::store_id();
    let base = base(&dir.0);
    let (snaps, counts) = dry_run(&base, &dir.0, &id);
    assert_eq!(snaps.len(), CALLS + 1, "each call adds rows");
    let lengths: Vec<i64> = snaps.keys().copied().collect();
    assert_eq!(
        lengths.windows(2).map(|w| w[1] - w[0]).collect::<Vec<_>>(),
        [2, 1, 2]
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
    let mut seed: u64 = 0x9e37_79b9_7f4a_7c15;
    for k in 0..32 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        jobs.push((k % MODES.len(), None, "slow", Some(20_000 + seed % 80_000)));
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
            o.at.map_or("killed".to_owned(), |(t, n)| format!("call {t} point {n}"));
        *table
            .entry(format!("{} {at}: log {}", MODES[o.mode], o.rows))
            .or_default() += 1;
    }
    for (label, count) in &table {
        eprintln!("{label} x{count}");
    }
    eprintln!("{} kills; boundaries per call {counts:?}", outcomes.len());
    assert!(outcomes.len() >= 110);
    match reference_python() {
        Err(why) => {
            eprintln!("NOT RUN the reference's half: {why}");
            record(
                "a_killed_verification_is_whole_or_absent",
                "reference Python",
                "i",
                0,
                &["the reference's open and verify of each killed call".to_owned()],
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
                "a_killed_verification_is_whole_or_absent",
                "reference Python",
                "i",
                said.len(),
                &[],
                &[],
            );
        }
    }
}
