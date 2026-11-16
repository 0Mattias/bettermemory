//! Two writers on one store at once, in two processes: a native writer
//! and the reference's (`put_memory`, when `BM_REFERENCE_PYTHON` imports
//! `bettermemory`; else a PARITY set-aside), and two native writers. In
//! WAL mode, and in rollback-journal mode: each writer, native or the
//! reference's, switches its own connection to it after its open (each
//! open asks for WAL, and an open while the other writer is idle gets it:
//! `PRAGMA journal_mode = WAL` needs only that no connection holds a lock,
//! so the store would otherwise be WAL again). Each writer's mode and the
//! file's at the end are printed. Both finish; every row is there once;
//! seqs are gapless; both
//! builds verify the chain ok; the head names the last row.
//!
//! The head: each process moves it after its own commit under
//! `<id>.head.lock` (`flock`, the same lock both builds take), forward
//! only, so the one that commits last but moves first is not rewound.

#![cfg(unix)]

mod common;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use bettermemory_core::store::create;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use common::created::{fixed, fixture, reference_python};
use common::opened;
use common::parity::record;
use common::stores::scratch;
use common::written::{self, instant, ulid};

const ROOT_ENV: &str = "BM_TEST_TWO_ROOT";
const TAG_ENV: &str = "BM_TEST_TWO_TAG";
const MODE_ENV: &str = "BM_TEST_TWO_MODE";
const N: u64 = 200;

fn open(c: &Path) -> WritableStore {
    open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("opened")
}

fn journal(store: &WritableStore) -> String {
    store
        .connection()
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .expect("mode")
}

/// A native writer: opens `$ROOT/c`, switches to `$MODE` when asked
/// (`delete`), says it is ready (`ready-$TAG`), waits for `go`, writes
/// N records numbered by `$TAG`, and prints its journal mode.
#[test]
#[ignore = "run by the two-writer test as its child"]
fn two_writer_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let tag: u64 = std::env::var(TAG_ENV).expect("tag").parse().expect("tag");
    let mode = std::env::var(MODE_ENV).unwrap_or_default();
    let c = root.join("c");
    let mut store = open(&c);
    if mode == "delete" {
        store
            .connection()
            .query_row("PRAGMA journal_mode = DELETE", [], |_| Ok(()))
            .expect("journal");
    }
    fs::write(root.join(format!("ready-{tag}")), journal(&store)).expect("ready");
    while !root.join("go").exists() {
        std::thread::sleep(Duration::from_millis(2));
    }
    let mut clock = written::clock(&[instant("2001-02-03T09:00:00.000001Z")]);
    for k in 0..N {
        let p = written::memory_payload(
            &ulid(tag * 10_000 + k, "01J"),
            &format!("writer {tag} record {k}"),
            &["global"],
            &[],
        );
        store
            .mutate("memory_put", &p, Some("two"), &mut clock)
            .expect("write");
    }
    fs::write(root.join(format!("done-{tag}")), journal(&store)).expect("done");
    store.close().expect("close");
}

const REFERENCE_WRITER: &str = r#"
import sys, time
from datetime import datetime, timezone
from pathlib import Path
from bettermemory.models import Confidence, Memory, Source
from bettermemory.store import Store
root, tag, n, mode = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
c = root / "c"
store = Store.open(c / "memory.sqlite", keys_dir=c / "keys", allow_rekey=True)
if mode == "delete":
    store.conn.execute("PRAGMA journal_mode = DELETE")
mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
(root / f"ready-{tag}").write_text(mode)
while not (root / "go").exists():
    time.sleep(0.002)
now = datetime.now(timezone.utc)
for k in range(n):
    store.put_memory(Memory(id=f"01K{tag * 10000 + k:023d}", created=now, updated=now,
        scopes=["global"], confidence=Confidence.HIGH, source=Source.EXPLICIT,
        body=f"reference writer {tag} record {k}"), session="two")
(root / f"done-{tag}").write_text(store.conn.execute("PRAGMA journal_mode").fetchone()[0])
store.close()
"#;

const REFERENCE_CHECK: &str = r#"
import json, sys
from bettermemory.store import Store
s = Store.open(sys.argv[1], keys_dir=sys.argv[2], allow_rekey=True)
try:
    print(json.dumps([s.log_verify()["status"], sum(1 for _ in s.iter_memories())]))
finally:
    s.close()
"#;

fn native_child(root: &Path, tag: u64, mode: &str) -> Child {
    Command::new(std::env::current_exe().expect("exe"))
        .args([
            "--exact",
            "two_writer_child",
            "--ignored",
            "--test-threads=1",
        ])
        .env(ROOT_ENV, root)
        .env(TAG_ENV, tag.to_string())
        .env(MODE_ENV, mode)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("child")
}

fn reference_child(python: &Path, root: &Path, tag: u64, mode: &str) -> Child {
    let home = root.join("home");
    fs::create_dir_all(&home).expect("home");
    let mut cmd = Command::new(python);
    cmd.env_clear();
    for keep in ["PATH", "TMPDIR"] {
        if let Some(v) = std::env::var_os(keep) {
            cmd.env(keep, v);
        }
    }
    for (name, rel) in [
        ("HOME", ""),
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
        ("BETTERMEMORY_DIR", "mem"),
        ("BETTERMEMORY_KEYS_DIR", "keys-default"),
        ("BETTERMEMORY_STATE_DIR", "state"),
    ] {
        cmd.env(name, home.join(rel));
    }
    cmd.env("PYTHONDONTWRITEBYTECODE", "1")
        .arg("-c")
        .arg(REFERENCE_WRITER)
        .arg(root)
        .arg(tag.to_string())
        .arg(N.to_string())
        .arg(mode)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("reference child")
}

fn wait_for(path: &Path, children: &mut [Child]) {
    let start = Instant::now();
    while !path.exists() {
        for child in children.iter_mut() {
            if let Ok(Some(status)) = child.try_wait() {
                assert!(status.success(), "a writer ended early: {status}");
            }
        }
        assert!(start.elapsed() < Duration::from_secs(120), "no {path:?}");
        std::thread::sleep(Duration::from_millis(5));
    }
}

/// One pair: (`second` is a native or reference writer).
fn pair(root: &Path, mode: &str, python: Option<&Path>) -> String {
    let c = root.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let id = common::stores::store_id();
    // The second opener's open asks for WAL and gets it (the first is
    // idle); a switch away from WAL needs no other connection in WAL, so
    // each switches right after its own open: the reference first, when
    // there is one, then native.
    let mut first = match python {
        Some(p) => reference_child(p, root, 2, mode),
        None => native_child(root, 2, mode),
    };
    wait_for(&root.join("ready-2"), std::slice::from_mut(&mut first));
    let second = native_child(root, 1, mode);
    let mut children = vec![first, second];
    wait_for(&root.join("ready-1"), &mut children);
    fs::write(root.join("go"), "").expect("go");
    for child in children {
        let out = child.wait_with_output().expect("wait");
        assert!(
            out.status.success(),
            "a writer failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let file = c.join("memory.sqlite");
    // Read before any check opens the store (an open asks for WAL).
    let mode_of = |t: u64| fs::read_to_string(root.join(format!("done-{t}"))).unwrap_or_default();
    let file_mode: String =
        rusqlite::Connection::open_with_flags(&file, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .and_then(|c| c.query_row("PRAGMA journal_mode", [], |r| r.get(0)))
            .expect("the file's mode");
    let ready_of = |t: u64| fs::read_to_string(root.join(format!("ready-{t}"))).unwrap_or_default();
    assert_eq!(
        file_mode,
        if mode == "delete" { "delete" } else { "wal" },
        "ready {} {}, done {} {}",
        ready_of(1),
        ready_of(2),
        mode_of(1),
        mode_of(2)
    );
    let conn = rusqlite::Connection::open(&file).expect("open");
    let (rows, max, distinct): (i64, i64, i64) = conn
        .query_row(
            "SELECT count(*), max(seq), (SELECT count(DISTINCT payload) FROM log \
             WHERE kind = 'memory_put') FROM log",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .expect("count");
    drop(conn);
    // The create's row, and N puts from each writer (the reference's
    // writer may add rows of its own; the native one adds none).
    assert_eq!(rows, max, "seqs have a gap");
    assert_eq!(distinct, 2 * N as i64, "a put lost or doubled");
    let (status, n) = written::native_verify(&c, &id);
    assert_eq!(status, "ok");
    assert_eq!(n, 2 * N as usize);
    let (seq, named) = written::head_in_log(&c.join("keys"), &file, &id).expect("a head");
    assert!(
        named && seq == max,
        "the head ends at {seq}, the log at {max}"
    );
    if let Some(python) = python {
        let out = common::created::run_reference(
            python,
            &root.join("home"),
            REFERENCE_CHECK,
            &[&file, &c.join("keys")],
        );
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let said: (String, usize) = serde_json::from_slice(out.stdout.trim_ascii()).expect("JSON");
        assert_eq!(said, ("ok".to_owned(), 2 * N as usize));
    }
    format!(
        "{rows} rows; the writers' journal modes {} and {}; the file's {file_mode}",
        mode_of(1),
        mode_of(2)
    )
}

#[test]
fn two_writers_both_finish_and_verify() {
    let dir = scratch("two-writers");
    let python = reference_python();
    let mut k = 0;
    for mode in ["wal", "delete"] {
        let root = dir.0.join(format!("n{k}"));
        k += 1;
        fs::create_dir(&root).expect("root");
        eprintln!("native and native, {mode}: {}", pair(&root, mode, None));
        if let Ok(p) = &python {
            let root = dir.0.join(format!("n{k}"));
            k += 1;
            fs::create_dir(&root).expect("root");
            eprintln!(
                "native and reference, {mode}: {}",
                pair(&root, mode, Some(p))
            );
        }
    }
    match python {
        Ok(_) => record(
            "two_writers_both_finish_and_verify",
            "reference Python",
            "i",
            2,
            &[],
            &[],
        ),
        Err(why) => {
            eprintln!("NOT RUN the reference writer: {why}");
            record(
                "two_writers_both_finish_and_verify",
                "reference Python",
                "i",
                0,
                &["a native and a reference writer".to_owned()],
                &[why],
            )
        }
    }
}
