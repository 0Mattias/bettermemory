//! A row writer killed anywhere is whole or absent: the store opens and
//! both builds verify its chain ok, the head never names a row the log
//! lacks, and a further write succeeds and verifies.
//!
//! The starting store: created natively (`store_create.json`'s values),
//! opened, one record written, closed. The test binary runs itself as the
//! writing child (`rows_kill_child`, ignored when run alone), which opens
//! it in WAL or (switched on its connection) rollback-journal mode and
//! makes four calls, one transaction each: `write_episode` with a home
//! (two rows), `put_standing` (one), `put_episode` with a home (two) and
//! `put_quarantine` (one). The writers take no hook, so the child's kill
//! points are its provider's reads, counted in order within each call: the
//! mint, the wall clock (an episode's `created`, a home's `decided`, a
//! standing row's `answered`), the log clock before each row is appended,
//! and the log clock the head's move reads after the commit (so a kill
//! there falls between commit and head move). It ends at each by `abort`
//! and by `exit`; on top, children slowed a millisecond per read are
//! killed from outside at pseudo-random delays, which reach the points
//! between reads (a row applied, the commit itself). After each kill,
//! read first through a read-write connection (a hot journal rolls back
//! there): the tables the writers touch equal those a dry run left at that
//! log length (so only whole calls), the FTS5 index passes its
//! `integrity-check`, the head names a row the log holds and is not ahead
//! of it, the native check verifies ok, and a further native write
//! succeeds and verifies ok. Copies are then opened and verified by the
//! reference in one child, when `BM_REFERENCE_PYTHON` imports
//! `bettermemory` (else a PARITY set-aside).

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
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Episode;
use bettermemory_core::store::create::{self, Provider};
use bettermemory_core::store::home::{Answer, Home};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::tables::{EpisodeArgs, Quarantine};
use common::created::{self, reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::rows::Counting;
use common::stores::scratch;
use common::written::{self, ulid};

const ROOT_ENV: &str = "BM_TEST_ROWS_ROOT";
const AT_ENV: &str = "BM_TEST_ROWS_KILL_AT";
const HOW_ENV: &str = "BM_TEST_ROWS_KILL_HOW";
const MODE_ENV: &str = "BM_TEST_ROWS_MODE";

const MODES: [&str; 2] = ["wal", "delete"];
const CALLS: usize = 4;
const STAMP: &str = "2001-02-03T06:00:00.000001Z";

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

/// A provider that passes each read to `point` first.
struct Points<'a> {
    inner: Counting,
    point: &'a mut dyn FnMut(),
}

impl Provider for Points<'_> {
    fn store_id(&mut self) -> io::Result<String> {
        self.inner.store_id()
    }
    fn key(&mut self) -> io::Result<Key> {
        self.inner.key()
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        self.inner.membership_store_id()
    }
    fn now(&mut self) -> io::Result<DateTime> {
        (self.point)();
        self.inner.now()
    }
    fn wall_now(&mut self) -> io::Result<DateTime> {
        (self.point)();
        self.inner.wall_now()
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn ulid(&mut self) -> io::Result<String> {
        (self.point)();
        self.inner.ulid()
    }
}

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn home(p: &str) -> Home {
    Home {
        project: s(p),
        checkout: PyValue::None,
        rule: s("repository"),
    }
}

/// The child's calls, in order.
fn call(store: &mut WritableStore, k: usize, p: &mut dyn Provider) {
    let session = Some("kill");
    match k {
        0 => {
            let args = EpisodeArgs {
                session_id: s("s-kill"),
                body: s("a homed entry about sqlite"),
                scopes: PyValue::None,
                takeaway: PyValue::None,
                swarm_id: PyValue::None,
                origin: PyValue::None,
            };
            store
                .write_episode(&args, None, Some(&home("p-one")), p)
                .expect("write_episode");
        }
        1 => {
            let answer = Answer {
                probability: PyValue::Float(0.5),
                source: s("user"),
                answered_by: s("kill"),
            };
            store
                .put_standing(&ulid(1, "01J"), &answer, session, p)
                .expect("put_standing");
        }
        2 => {
            let e = Episode::from_payload(&written::payload(
                r#"{"id": "01E00000000000000000000001", "session_id": "s-kill",
                    "created": "2002-03-04T05:06:07Z", "body": "put whole\n"}"#,
            ))
            .expect("episode");
            store
                .put_episode(&e, session, Some(&home("p-two")), p)
                .expect("put_episode");
        }
        _ => {
            let q = Quarantine {
                name: s("q"),
                reason: s("unreadable"),
                at: s("t"),
                detail: PyValue::None,
                remote: PyValue::None,
                size: PyValue::int(3),
                sha256: PyValue::None,
            };
            store
                .put_quarantine(&q, session, p)
                .expect("put_quarantine");
        }
    }
}

/// The writing child: `$ROOT/c` opened in `$MODE`, its calls made, ended
/// at `$AT` (`call,n`: the n-th provider read of a call) by `$HOW` (abort,
/// exit), or slowed (`slow`) for a kill from outside. Writes each point
/// passed to `$ROOT/progress`.
#[test]
#[ignore = "run by the kill test as its child"]
fn rows_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let at = std::env::var(AT_ENV).ok();
    let how = std::env::var(HOW_ENV).unwrap_or_default();
    let mode = std::env::var(MODE_ENV).unwrap_or_default();
    let progress = root.join("progress");
    let mut store = open(&root.join("c"), &mode);
    for k in 0..CALLS {
        let mut n = 0;
        let mut point = || {
            let here = format!("{k},{n}");
            n += 1;
            let _ = fs::write(&progress, &here);
            if how == "slow" {
                std::thread::sleep(Duration::from_millis(1));
            }
            if at.as_deref() == Some(here.as_str()) {
                if how == "abort" {
                    std::process::abort();
                }
                std::process::exit(9);
            }
        };
        let mut p = Points {
            inner: Counting::at(STAMP, 10 * k as u64),
            point: &mut point,
        };
        call(&mut store, k, &mut p);
    }
    store.close().expect("closed");
}

/// The starting store, made once and copied for each kill.
fn base(dir: &Path) -> PathBuf {
    let c = dir.join("base");
    let doc = created::fixture();
    create::create(&c, &c.join("keys"), &mut created::fixed(&doc)).expect("create");
    let mut store = open(&c, "wal");
    let payload = written::memory_payload(&ulid(1, "01J"), "a record", &["global"], &[]);
    store
        .mutate(
            "memory_put",
            &payload,
            Some("base"),
            &mut Counting::at(STAMP, 0),
        )
        .expect("base write");
    store.close().expect("close");
    c
}

/// The tables the writers touch as one text, and the log's length.
fn tables(file: &Path) -> (String, i64) {
    let (t, n) = written::tables(file);
    let conn =
        rusqlite::Connection::open_with_flags(file, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .expect("read");
    let mut parts = vec![t];
    for sql in [
        "SELECT record_id || kind || project || rule FROM homes ORDER BY record_id",
        "SELECT record_id || probability || body_sha256 FROM standing ORDER BY record_id",
        "SELECT id || body || log_mac FROM episodes ORDER BY id",
        "SELECT name || reason FROM quarantine ORDER BY name",
    ] {
        let rows: Vec<String> = conn
            .prepare(sql)
            .expect("select")
            .query_map([], |r| r.get(0))
            .expect("query")
            .collect::<rusqlite::Result<_>>()
            .expect("rows");
        parts.push(format!("[{}]", rows.join(", ")));
    }
    (parts.join(" "), n)
}

/// The tables after each call of a dry run, by log length, and how many
/// provider reads each call makes.
fn dry_run(base: &Path, dir: &Path, id: &str) -> (BTreeMap<i64, String>, Vec<usize>) {
    let c = dir.join("dry");
    written::keep_copy(base, &c, id);
    let file = c.join("memory.sqlite");
    let mut out = BTreeMap::new();
    let mut counts = Vec::new();
    let mut store = open(&c, "wal");
    let (t, n) = tables(&file);
    out.insert(n, t);
    for k in 0..CALLS {
        let mut count = 0;
        let mut point = || count += 1;
        let mut p = Points {
            inner: Counting::at(STAMP, 10 * k as u64),
            point: &mut point,
        };
        call(&mut store, k, &mut p);
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
        "rows_kill_child",
        "--ignored",
        "--nocapture",
        "--test-threads=1",
    ])
    .env(ROOT_ENV, root)
    .env(HOW_ENV, how)
    .env(MODE_ENV, MODES[mode])
    .stdout(std::process::Stdio::null())
    .stderr(std::process::Stdio::null());
    if let Some((k, n)) = at {
        cmd.env(AT_ENV, format!("{k},{n}"));
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
    let (status, _) = written::native_verify(&c, id);
    assert_eq!(status, "ok", "{label}");
    // A further write succeeds and verifies.
    let mut again = open(&c, "wal");
    let q = Quarantine {
        name: s("after"),
        reason: s("again"),
        at: s("t"),
        detail: PyValue::None,
        remote: PyValue::None,
        size: PyValue::None,
        sha256: PyValue::None,
    };
    again
        .put_quarantine(&q, Some("after"), &mut Counting::at(STAMP, 900))
        .unwrap_or_else(|e| panic!("a further write after {label}: {e}"));
    again.close().expect("close");
    let (status, _) = written::native_verify(&c, id);
    assert_eq!(status, "ok", "after a further write: {label}");
    Outcome {
        at,
        mode,
        reached,
        rows,
        copy,
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
        out.append([s.log_verify()["status"], s.conn.execute("SELECT max(seq) FROM log").fetchone()[0]])
    finally:
        s.close()
print(json.dumps(out))
"#;

#[test]
fn a_killed_row_writer_is_whole_or_absent() {
    let name = "a_killed_row_writer_is_whole_or_absent";
    let dir = scratch("rows-kills");
    let id = common::stores::store_id();
    let base = base(&dir.0);
    let (snaps, counts) = dry_run(&base, &dir.0, &id);
    assert_eq!(snaps.len(), CALLS + 1, "each call adds rows");
    let lengths: Vec<i64> = snaps.keys().copied().collect();
    assert_eq!(
        lengths.windows(2).map(|w| w[1] - w[0]).collect::<Vec<_>>(),
        [2, 1, 2, 1]
    );
    let mut jobs: Vec<Job> = Vec::new();
    for mode in 0..MODES.len() {
        for (k, &count) in counts.iter().enumerate() {
            for n in 0..count {
                for how in ["abort", "exit"] {
                    jobs.push((mode, Some((k, n)), how, None));
                }
            }
        }
    }
    let mut seed: u64 = 0x2545_f491_4f6c_dd1d;
    for k in 0..60 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        jobs.push((k % MODES.len(), None, "slow", Some(10_000 + seed % 60_000)));
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
        if let Some((k, n)) = o.at {
            assert_eq!(
                o.reached.as_deref(),
                Some(format!("{k},{n}").as_str()),
                "the child stopped elsewhere"
            );
        }
        let at =
            o.at.map_or("killed".to_owned(), |(k, n)| format!("call {k} read {n}"));
        *table
            .entry(format!("{} {at}: log {}", MODES[o.mode], o.rows))
            .or_default() += 1;
    }
    for (label, count) in &table {
        eprintln!("{label} x{count}");
    }
    eprintln!(
        "{} kills; provider reads per call {counts:?}",
        outcomes.len()
    );
    match reference_python() {
        Err(why) => {
            eprintln!("NOT RUN the reference's half: {why}");
            record(
                name,
                "reference Python",
                "i",
                0,
                &["the reference's open and verify of each killed store".to_owned()],
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
            let said: Vec<(String, i64)> =
                serde_json::from_slice(out.stdout.trim_ascii()).expect("JSON");
            assert_eq!(said.len(), outcomes.len());
            for (o, (status, seq)) in outcomes.iter().zip(&said) {
                assert_eq!(status, "ok");
                assert_eq!(*seq, o.rows);
            }
            eprintln!("the reference opened and verified {} stores ok", said.len());
            record(name, "reference Python", "i", said.len(), &[], &[]);
        }
    }
}
