//! A native write killed anywhere is whole or absent: the store opens
//! and both builds verify its chain ok, the head never names a row the
//! log lacks, and a further write succeeds and verifies.
//!
//! The starting store: created natively (`store_create.json`'s values),
//! opened, three records written, closed. The test binary runs itself as
//! the writing child (`write_kill_child`, ignored when run alone), which
//! opens it, in WAL or (switched on its connection) rollback-journal
//! mode, and makes four writes: a new record, a re-put of one with a new
//! body, a removal, the removed record's tombstone. The child ends at
//! each [`WriteBoundary`] of each write, by `abort` and by `exit` (no
//! destructors either way); on top, children slowed a millisecond per
//! boundary are killed from outside at pseudo-random delays. After each
//! kill: the record tables equal what the writes the log holds leave (a
//! dry run's snapshot per log length), the FTS5 index passes its
//! `integrity-check`, the head names a row the log holds or is behind,
//! the native check verifies ok, and a further native write succeeds and
//! verifies ok. Copies are then opened, verified and counted by the
//! reference in one child, when `BM_REFERENCE_PYTHON` imports
//! `bettermemory` (else a PARITY set-aside). What each boundary left is
//! printed.

#![cfg(unix)]

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use bettermemory_core::store::create;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::write::WriteBoundary;
use common::created::{self, reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::stores::scratch;
use common::written::{self, instant, ulid};

const ROOT_ENV: &str = "BM_TEST_WRITE_ROOT";
const AT_ENV: &str = "BM_TEST_WRITE_KILL_AT";
const HOW_ENV: &str = "BM_TEST_WRITE_KILL_HOW";
const MODE_ENV: &str = "BM_TEST_WRITE_MODE";

const WRITES: usize = 4;
const MODES: [&str; 2] = ["wal", "delete"];

fn bi(b: WriteBoundary) -> usize {
    WriteBoundary::ALL
        .iter()
        .position(|x| *x == b)
        .expect("boundary")
}

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

/// The child's writes, in order.
fn write(store: &mut WritableStore, w: usize, hook: &mut dyn FnMut(WriteBoundary)) {
    let payload = match w {
        0 => written::memory_payload(&ulid(10, "01J"), "a new record", &["global"], &[]),
        1 => written::memory_payload(&ulid(1, "01J"), "a changed body", &["global"], &[]),
        2 => written::payload(&format!("{{\"id\": \"{}\"}}", ulid(2, "01J"))),
        _ => written::tombstone_payload(&ulid(2, "01J"), "record two"),
    };
    let kind = ["memory_put", "memory_put", "memory_delete", "tombstone_put"][w];
    store
        .mutate_with(kind, &payload, Some("kill"), &mut clock(), hook)
        .expect("write");
}

/// The writing child: `$ROOT/c` opened in `$MODE`, its writes made, ended
/// at `$AT` (`write,boundary`) by `$HOW` (abort, exit), or slowed
/// (`slow`) for a kill from outside. Writes each point passed to
/// `$ROOT/progress`.
#[test]
#[ignore = "run by the kill test as its child"]
fn write_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let at = std::env::var(AT_ENV).ok();
    let how = std::env::var(HOW_ENV).unwrap_or_default();
    let mode = std::env::var(MODE_ENV).unwrap_or_default();
    let progress = root.join("progress");
    let mut store = open(&root.join("c"), &mode);
    for w in 0..WRITES {
        let mut hook = |b: WriteBoundary| {
            let point = format!("{w},{}", bi(b));
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
        write(&mut store, w, &mut hook);
    }
    store.close().expect("closed");
}

/// The starting store, made once and copied for each kill.
fn base(dir: &Path) -> PathBuf {
    let c = dir.join("base");
    let doc = created::fixture();
    create::create(&c, &c.join("keys"), &mut created::fixed(&doc)).expect("create");
    let mut store = open(&c, "wal");
    for n in 1..=3 {
        let p = written::memory_payload(&ulid(n, "01J"), &format!("record {n}"), &["global"], &[]);
        store
            .mutate("memory_put", &p, Some("base"), &mut clock())
            .expect("base write");
    }
    store.close().expect("close");
    c
}

/// The record tables after each number of log rows a dry run reached.
fn snapshots(base: &Path, dir: &Path, id: &str) -> BTreeMap<i64, String> {
    let c = dir.join("dry");
    written::keep_copy(base, &c, id);
    let file = c.join("memory.sqlite");
    let mut out = BTreeMap::new();
    let mut store = open(&c, "wal");
    let (t, n) = written::tables(&file);
    out.insert(n, t);
    for w in 0..WRITES {
        write(&mut store, w, &mut |_| {});
        let (t, n) = written::tables(&file);
        out.insert(n, t);
    }
    store.close().expect("close");
    out
}

/// What a kill left: the store directory's and keys directory's names,
/// the log's length, and the head's seq.
fn left(c: &Path, id: &str) -> String {
    let names = |d: &Path| {
        let mut v: Vec<String> = fs::read_dir(d)
            .map(|r| {
                r.flatten()
                    .map(|e| {
                        let name = e.file_name().to_string_lossy().replace(id, "<id>");
                        // A dead head move's temporary file, by its shape.
                        if name.ends_with(".tmp") {
                            "<id>.head.<tmp>".to_owned()
                        } else {
                            name
                        }
                    })
                    .collect()
            })
            .unwrap_or_default();
        v.sort();
        v.join(" ")
    };
    let dir = names(c).replace("keys", "").trim().to_owned();
    let (_, rows) = written::tables(&c.join("memory.sqlite"));
    let head = written::head_in_log(&c.join("keys"), &c.join("memory.sqlite"), id)
        .map_or("none".to_owned(), |(s, _)| s.to_string());
    format!(
        "dir [{dir}]; keys [{}]; log {rows}; head {head}",
        names(&c.join("keys"))
    )
}

type Job = (usize, Option<(usize, usize)>, &'static str, Option<u64>);

struct Outcome {
    mode: usize,
    at: Option<(usize, usize)>,
    reached: Option<String>,
    left: String,
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
        "write_kill_child",
        "--ignored",
        "--nocapture",
        "--test-threads=1",
    ])
    .env(ROOT_ENV, root)
    .env(HOW_ENV, how)
    .env(MODE_ENV, MODES[mode])
    .stdout(std::process::Stdio::null())
    .stderr(std::process::Stdio::null());
    if let Some((w, b)) = at {
        cmd.env(AT_ENV, format!("{w},{b}"));
    }
    let mut child = cmd.spawn().expect("child");
    if let Some(us) = delay {
        std::thread::sleep(Duration::from_micros(us));
        let _ = child.kill();
    }
    let _ = child.wait();
    let reached = fs::read_to_string(root.join("progress")).ok();
    let file = c.join("memory.sqlite");
    // A kill inside a rollback-journal transaction leaves a hot journal,
    // which the first connection that may write rolls back (any opener
    // does); the read-only checks below cannot.
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
    // The store opens (a hot journal rolls back here), and the record
    // tables are what the log's writes leave: whole or absent.
    let left = left(&c, id);
    let (tables, rows) = written::tables(&file);
    assert_eq!(
        snaps.get(&rows),
        Some(&tables),
        "the tables after a kill ({left}) are not those of {rows} log rows"
    );
    written::fts_intact(&file).unwrap_or_else(|e| panic!("the index after {left}: {e}"));
    if let Some((_, named)) = written::head_in_log(&c.join("keys"), &file, id) {
        assert!(named, "the head names a row the log lacks: {left}");
    }
    let copy = root.join("copy");
    written::keep_copy(&c, &copy, id);
    let (status, records) = written::native_verify(&c, id);
    assert_eq!(status, "ok", "{left}");
    // A further write succeeds and verifies.
    let mut again = open(&c, "wal");
    let p = written::memory_payload(&ulid(99, "01J"), "after the kill", &["global"], &[]);
    again
        .mutate("memory_put", &p, Some("after"), &mut clock())
        .unwrap_or_else(|e| panic!("a further write after {left}: {e}"));
    again.close().expect("close");
    let (status, after) = written::native_verify(&c, id);
    assert_eq!(status, "ok", "after a further write: {left}");
    assert_eq!(after, records + 1);
    Outcome {
        mode,
        at,
        reached,
        left,
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
fn a_killed_write_is_whole_or_absent() {
    let dir = scratch("write-kills");
    let id = common::stores::store_id();
    let base = base(&dir.0);
    let snaps = snapshots(&base, &dir.0, &id);
    assert_eq!(snaps.len(), WRITES + 1, "each write adds one row");
    let mut jobs: Vec<Job> = Vec::new();
    for mode in 0..MODES.len() {
        for w in 0..WRITES {
            for b in 0..WriteBoundary::ALL.len() {
                for how in ["abort", "exit"] {
                    jobs.push((mode, Some((w, b)), how, None));
                }
            }
        }
    }
    let mut seed: u64 = 0x9e37_79b9_7f4a_7c15;
    for k in 0..112 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        jobs.push((k % MODES.len(), None, "slow", Some(20_000 + seed % 150_000)));
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
    let mut table: BTreeMap<String, BTreeMap<String, usize>> = BTreeMap::new();
    for o in &outcomes {
        let mode = MODES[o.mode];
        let label = match (o.at, &o.reached) {
            (Some((w, b)), reached) => {
                assert_eq!(
                    reached.as_deref(),
                    Some(format!("{w},{b}").as_str()),
                    "the child stopped elsewhere"
                );
                format!("{mode} write {w} after {:?}", WriteBoundary::ALL[b])
            }
            (None, Some(r)) => format!("{mode} killed after {r}"),
            (None, None) => format!("{mode} killed before any boundary"),
        };
        *table
            .entry(label)
            .or_default()
            .entry(o.left.clone())
            .or_default() += 1;
    }
    for (label, lefts) in &table {
        for (left, count) in lefts {
            eprintln!("{label} x{count}: {left}");
        }
    }
    eprintln!("{} kills", outcomes.len());
    assert!(outcomes.len() >= 200);
    match reference_python() {
        Err(why) => {
            eprintln!("NOT RUN the reference's half: {why}");
            record(
                "a_killed_write_is_whole_or_absent",
                "reference Python",
                "i",
                0,
                &["the reference's open and verify of each killed write".to_owned()],
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
                assert_eq!(status, "ok", "{}", o.left);
                assert_eq!(*n, o.records, "{}", o.left);
            }
            eprintln!("the reference opened and verified {} stores ok", said.len());
            record(
                "a_killed_write_is_whole_or_absent",
                "reference Python",
                "i",
                said.len(),
                &[],
                &[],
            );
        }
    }
}
