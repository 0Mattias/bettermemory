//! A chunked run killed anywhere leaves every chunk whole or absent, every
//! chunk before the kill present, and the head never ahead of the log;
//! both builds verify the store and continue it.
//!
//! The starting store: created natively, 12 records under `project:a` and
//! 6 removed ones under it. The test binary runs itself as the child
//! (`bulk_kill_child`, ignored when run alone), which opens it in WAL or
//! (switched on its connection) rollback-journal mode and runs
//! `rename_scope("project:a", "project:b")` (three chunks of active
//! records, two of removed ones) then `prune_tombstones(0)` (two chunks),
//! with a monotonic clock that closes a chunk after its fourth item and no
//! pause, so the chunks fall where a dry run finds them. The child ends at
//! each boundary it passes (each chunk's begin, each row appended and
//! applied, each commit, the head's lock and move), by `abort` in WAL mode
//! and by `exit` in rollback-journal mode. After each kill, read first
//! through a read-write connection (a hot journal rolls back there): the
//! log holds exactly the rows of the chunks committed before the kill
//! point, the record tables equal the dry run's at that length, FTS5
//! passes its `integrity-check`, the head names a row the log holds, the
//! native check verifies ok and a further native rename succeeds. Then the
//! reference opens, verifies and continues (a put) every killed store, when
//! `BM_REFERENCE_PYTHON` imports `bettermemory` (else a PARITY set-aside),
//! and runs the same calls on a copy of the starting store under the same
//! monotonic schedule: at a sample of the kill points (every third, and
//! each commit) the log a kill left is as long as the reference's after as
//! many commits.

#![cfg(unix)]

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::store::create::{self, Provider};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::write::WriteBoundary;
use bettermemory_core::unicode;
use common::bulk::put_records;
use common::created::{self, reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::stores::scratch;
use common::written::{self, instant, ulid};
use serde_json::{json, Value};

const ROOT_ENV: &str = "BM_TEST_BULK_ROOT";
const AT_ENV: &str = "BM_TEST_BULK_KILL_AT";
const HOW_ENV: &str = "BM_TEST_BULK_KILL_HOW";
const MODE_ENV: &str = "BM_TEST_BULK_MODE";

/// One stamp on both clocks; a monotonic clock stepping 1/32 s; no pause.
struct Steady {
    at: DateTime,
    mono: f64,
}

impl Provider for Steady {
    fn store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("none"))
    }
    fn key(&mut self) -> io::Result<Key> {
        Err(io::Error::other("none"))
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("none"))
    }
    fn now(&mut self) -> io::Result<DateTime> {
        Ok(self.at)
    }
    fn engine_version(&self) -> String {
        opened::ENGINE.to_owned()
    }
    fn monotonic(&mut self) -> io::Result<f64> {
        self.mono += 1.0 / 32.0;
        Ok(self.mono)
    }
    fn pause(&mut self, _seconds: f64) {}
}

fn steady() -> Steady {
    Steady {
        at: instant("2001-02-03T06:00:00.000001Z"),
        mono: 100.0,
    }
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

/// The child's run: the rename, then the prune.
fn run(store: &mut WritableStore, hook: &mut dyn FnMut(WriteBoundary)) {
    let mut p = steady();
    store
        .rename_scope_hooked(
            &mut p,
            &unicode::cps("project:a"),
            &unicode::cps("project:b"),
            true,
            Some("kill"),
            Some(&mut *hook),
        )
        .expect("rename");
    store
        .prune_tombstones_hooked(&mut p, 0, None, Some("kill"), Some(hook))
        .expect("prune");
}

#[test]
#[ignore = "run by the kill test as its child"]
fn bulk_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let at: Option<usize> = std::env::var(AT_ENV).ok().and_then(|v| v.parse().ok());
    let how = std::env::var(HOW_ENV).unwrap_or_default();
    let mode = std::env::var(MODE_ENV).unwrap_or_default();
    let mut store = open(&root.join("c"), &mode);
    let mut n = 0;
    let mut hook = |_: WriteBoundary| {
        if at == Some(n) {
            if how == "abort" {
                std::process::abort();
            }
            std::process::exit(9);
        }
        n += 1;
    };
    run(&mut store, &mut hook);
    store.close().expect("closed");
}

fn base(dir: &Path) -> PathBuf {
    let c = dir.join("base");
    create::create(
        &c,
        &c.join("keys"),
        &mut created::fixed(&created::fixture()),
    )
    .expect("create");
    let rec = |n: u64| {
        json!({
            "id": ulid(n, "01J"), "created": "2001-02-03T04:05:06.000000Z",
            "updated": "2001-02-03T04:05:06.000000Z", "scopes": ["project:a"],
            "confidence": "high", "source": "explicit-statement",
            "body": format!("record {n} about sqlite"),
        })
    };
    let spec = json!({
        "active": (1..=12).map(rec).collect::<Vec<_>>(),
        "removed": (20..26).map(|n| json!([rec(n), "2001-01-01T00:00:00+00:00"])).collect::<Vec<_>>(),
    });
    let mut store = open(&c, "wal");
    put_records(&mut store, &spec);
    store.close().expect("close");
    c
}

fn rows(file: &Path) -> i64 {
    written::tables(file).1
}

/// A dry run: for each boundary index, the log's length a kill there must
/// leave (that of the last commit before it), and the tables at each
/// committed length.
fn dry(base: &Path, dir: &Path, id: &str) -> (Vec<i64>, BTreeMap<i64, String>, Vec<bool>) {
    let c = dir.join("dry");
    written::keep_copy(base, &c, id);
    let file = c.join("memory.sqlite");
    let mut store = open(&c, "wal");
    let mut snaps = BTreeMap::new();
    let (t, n) = written::tables(&file);
    snaps.insert(n, t);
    let first = n;
    let mut points: Vec<WriteBoundary> = Vec::new();
    {
        let mut hook = |b: WriteBoundary| points.push(b);
        run(&mut store, &mut hook);
    }
    store.close().expect("close");
    // Replay the points against a second dry copy, reading the log at each
    // commit: a fresh copy, so the reads see each chunk as it commits.
    let c2 = dir.join("dry2");
    written::keep_copy(base, &c2, id);
    let file2 = c2.join("memory.sqlite");
    let mut store = open(&c2, "wal");
    let mut lengths = Vec::new();
    {
        let mut hook = |b: WriteBoundary| {
            if b == WriteBoundary::Committed {
                let (t, n) = written::tables(&file2);
                snaps.insert(n, t);
                lengths.push(n);
            }
        };
        run(&mut store, &mut hook);
    }
    store.close().expect("close");
    let commits: Vec<bool> = points
        .iter()
        .map(|b| *b == WriteBoundary::Committed)
        .collect();
    (placed(first, &lengths, &commits), snaps, commits)
}

/// For each boundary, the log's length a kill there leaves: `first`
/// until the first commit, then each commit's length in turn. A kill at
/// a boundary comes after it: at `Committed` the chunk is in.
fn placed(first: i64, lengths: &[i64], commits: &[bool]) -> Vec<i64> {
    let mut next = lengths.iter();
    let mut committed = first;
    commits
        .iter()
        .map(|&c| {
            if c {
                committed = *next.next().expect("a commit length");
            }
            committed
        })
        .collect()
}

/// The reference's chunk placement for the child's run on a copy of the
/// starting store: the same monotonic schedule (100, read as 100 + 1/32
/// on first use, 1/32 a read after) and no pause; the log's length after
/// each commit (read as the head moves, `Store._move_head`).
const PLACEMENT: &str = r#"
import json, sys
import bettermemory.store as module
from bettermemory.store import Store

class Paced:
    def __init__(self):
        self.mono = 100.0
    def monotonic(self):
        self.mono += 1 / 32
        return self.mono
    def sleep(self, seconds):
        pass
    def __getattr__(self, name):
        return getattr(real, name)

real = module.time
module.time = Paced()
s = Store.open(sys.argv[1], keys_dir=sys.argv[2], allow_rekey=True)
lengths = []
moved = s._move_head
def move():
    lengths.append(s.conn.execute("SELECT count(*) FROM log").fetchone()[0])
    moved()
s._move_head = move
s.rename_scope("project:a", "project:b", include_tombstones=True, session="kill")
s.prune_tombstones(__import__("datetime").timedelta(0), session="kill")
s.close()
print(json.dumps(lengths))
"#;

const REFERENCE: &str = r#"
import json, sys
from datetime import datetime, timezone
from bettermemory.models import Memory
from bettermemory.store import Store
out = []
args = sys.argv[1:]
now = datetime(2001, 2, 3, tzinfo=timezone.utc)
for i in range(0, len(args), 2):
    s = Store.open(args[i], keys_dir=args[i + 1], allow_rekey=True)
    try:
        first = s.log_verify()["status"]
        s.put_memory(Memory(id="01K00000000000000000000099", created=now, updated=now,
                            scopes=["global"], confidence="high",
                            source="explicit-statement", body="after the kill"))
        out.append([first, s.log_verify()["status"]])
    finally:
        s.close()
print(json.dumps(out))
"#;

#[test]
fn a_killed_chunked_run_keeps_its_committed_chunks() {
    let dir = scratch("bulk-kills");
    let id = common::stores::store_id();
    let base = base(&dir.0);
    let (expect, snaps, commits) = dry(&base, &dir.0, &id);
    assert!(
        snaps.len() >= 8,
        "chunks committed in the dry run: {}",
        snaps.len()
    );
    let mut jobs: Vec<(usize, &str, &str)> = Vec::new();
    for at in 0..expect.len() {
        jobs.push((at, "wal", "abort"));
        jobs.push((at, "delete", "exit"));
    }
    let outcomes: Vec<(PathBuf, usize, String, i64)> = std::thread::scope(|scope| {
        let workers: Vec<_> = jobs
            .chunks(jobs.len().div_ceil(6))
            .enumerate()
            .map(|(w, chunk)| {
                let (dir, base, snaps, expect, id) = (&dir.0, &base, &snaps, &expect, &id);
                scope.spawn(move || {
                    let mut copies = Vec::new();
                    for (k, &(at, mode, how)) in chunk.iter().enumerate() {
                        let root = dir.join(format!("w{w}k{k}"));
                        fs::create_dir(&root).expect("root");
                        let c = root.join("c");
                        written::keep_copy(base, &c, id);
                        let status = Command::new(std::env::current_exe().expect("exe"))
                            .args([
                                "--exact",
                                "bulk_kill_child",
                                "--ignored",
                                "--test-threads=1",
                            ])
                            .env(ROOT_ENV, &root)
                            .env(AT_ENV, at.to_string())
                            .env(HOW_ENV, how)
                            .env(MODE_ENV, mode)
                            .stdout(std::process::Stdio::null())
                            .stderr(std::process::Stdio::null())
                            .status()
                            .expect("child");
                        assert!(!status.success(), "the child ran past point {at}");
                        let file = c.join("memory.sqlite");
                        {
                            let conn = rusqlite::Connection::open_with_flags(
                                &file,
                                rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
                            )
                            .expect("read-write");
                            conn.busy_timeout(Duration::from_secs(10)).expect("busy");
                            conn.query_row("SELECT count(*) FROM sqlite_master", [], |_| Ok(()))
                                .expect("first read");
                        }
                        let label = format!("{mode} {how} at {at}");
                        let n = rows(&file);
                        assert_eq!(n, expect[at], "{label}: the committed chunks");
                        assert_eq!(
                            snaps.get(&n),
                            Some(&written::tables(&file).0),
                            "{label}: the tables"
                        );
                        written::fts_intact(&file).unwrap_or_else(|e| panic!("{label}: {e}"));
                        if let Some((seq, named)) = written::head_in_log(&c.join("keys"), &file, id)
                        {
                            assert!(named && seq <= n, "{label}: the head is ahead");
                        }
                        let copy = root.join("copy");
                        written::keep_copy(&c, &copy, id);
                        let (status, _) = written::native_verify(&c, id);
                        assert_eq!(status, "ok", "{label}");
                        let mut again = open(&c, "wal");
                        let mut p = steady();
                        again
                            .rename_scope(
                                &mut p,
                                &unicode::cps("project:a"),
                                &unicode::cps("project:c"),
                                true,
                                None,
                            )
                            .unwrap_or_else(|e| panic!("{label}: a further rename: {e}"));
                        again.close().expect("close");
                        assert_eq!(written::native_verify(&c, id).0, "ok", "{label}");
                        copies.push((copy, at, label, n));
                    }
                    copies
                })
            })
            .collect();
        workers
            .into_iter()
            .flat_map(|w| w.join().expect("worker"))
            .collect()
    });
    eprintln!("{} kills over {} boundaries", outcomes.len(), expect.len());
    assert!(outcomes.len() >= 100);
    match reference_python() {
        Err(why) => record(
            "a_killed_chunked_run_keeps_its_committed_chunks",
            "reference Python",
            "i",
            0,
            &["the reference's open, verify and put after each kill".to_owned()],
            &[why],
        ),
        Ok(python) => {
            // The reference's chunks for the same schedule: where it
            // commits is where native's kills leave the log, at a sample of
            // the points (every third, and each commit).
            let c = dir.0.join("placement");
            written::keep_copy(&base, &c, &id);
            let out = run_reference(
                &python,
                &dir.0.join("home"),
                PLACEMENT,
                &[&c.join("memory.sqlite"), &c.join("keys")],
            );
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            let lengths: Vec<i64> = serde_json::from_slice(out.stdout.trim_ascii()).expect("JSON");
            let theirs = placed(expect[0], &lengths, &commits);
            let mut sampled = 0;
            for (_, at, label, n) in &outcomes {
                if at % 3 == 0 || commits[*at] {
                    assert_eq!(*n, theirs[*at], "{label}: the reference's chunks");
                    sampled += 1;
                }
            }
            eprintln!("{sampled} kills against the reference's chunk placement");
            let args: Vec<PathBuf> = outcomes
                .iter()
                .flat_map(|(c, ..)| [c.join("memory.sqlite"), c.join("keys")])
                .collect();
            let refs: Vec<&Path> = args.iter().map(PathBuf::as_path).collect();
            let out = run_reference(&python, &dir.0.join("home"), REFERENCE, &refs);
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            let said: Vec<Value> = serde_json::from_slice(out.stdout.trim_ascii()).expect("JSON");
            assert_eq!(said.len(), outcomes.len());
            assert!(said.iter().all(|s| *s == json!(["ok", "ok"])), "{said:?}");
            record(
                "a_killed_chunked_run_keeps_its_committed_chunks",
                "reference Python",
                "i",
                said.len(),
                &[],
                &[],
            );
        }
    }
}
