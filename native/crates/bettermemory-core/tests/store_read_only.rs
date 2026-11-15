//! What a read-only open leaves behind, and what it reads while a writer
//! runs. Stores are rebuilt from `store_bases.json` (no reference Python).
//!
//! - The byte-for-byte proof: every file of the store directory and of its
//!   keys directory hashed before and after an open, a read of every
//!   table, a verify and an FTS query, for a rollback-journal store, a
//!   closed WAL store, a WAL store a writer holds open with frames in its
//!   `-wal`, and a `-wal` without its `-shm`.
//! - The recorded exception: a `-wal` and a stale `-shm` left by a writer
//!   that died. SQLite rebuilds the wal-index in the `-shm` as any reader
//!   does (the reference's `mode=ro` read does the same on the same file);
//!   the store file and the `-wal` are unchanged.
//! - The private copy: its names are gone once SQLite has the files open,
//!   its directory goes when the reader drops (on an error and a panic
//!   too), and a directory a dead reader left is swept at the next open.
//! - A writer appending while the native build verifies.

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bettermemory_core::log::{self, head_file_text, Key, LogRow};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::{
    admission, sweep_private_copies, CandidateFilters, StoreError, StoreFile, SCRATCH_PREFIX,
};
use common::stores::{case_store, store_id};
use sha2::{Digest, Sha256};

fn files_of(dir: &Path) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for entry in fs::read_dir(dir).expect("dir") {
        let entry = entry.expect("entry");
        if entry.file_type().expect("type").is_file() {
            let bytes = fs::read(entry.path()).expect("read");
            out.insert(
                entry.file_name().to_string_lossy().into_owned(),
                common::hex(&Sha256::digest(&bytes)),
            );
        }
    }
    out
}

/// Open read-only, read every table, verify the chain, run the FTS query.
fn full_read(dir: &Path) -> String {
    let store = StoreFile::open(&dir.join("memory.sqlite")).expect("open");
    let meta = store.meta().expect("meta");
    for name in store.table_names().expect("tables") {
        store.each_row(&name, &mut |_| {}).expect("rows");
    }
    store.each_memory(&mut |_| {}).expect("memories");
    let get = |k: &str| meta.iter().find(|(key, _)| key == k).expect(k).1.clone();
    let ring = load_key_ring(&dir.join("keys"), &get("store_id")).expect("ring");
    let report = store
        .verify_chain(&ring, &get("key_fingerprint"))
        .expect("verify");
    store
        .query_candidates("docker", &CandidateFilters::default())
        .expect("fts");
    report.status.to_owned()
}

fn assert_unchanged(dir: &Path, what: &str) {
    let before = (files_of(dir), files_of(&dir.join("keys")));
    let status = full_read(dir);
    let after = (files_of(dir), files_of(&dir.join("keys")));
    assert_eq!(before, after, "{what}: files changed by a read-only open");
    eprintln!(
        "{what}: {} files hashed before and after, unchanged (chain {status})",
        before.0.len() + before.1.len()
    );
}

#[test]
fn read_only_rollback_journal_store() {
    let (dir, _) = case_store("rollback_journal");
    assert_unchanged(&dir.0, "rollback journal");
}

#[test]
fn read_only_wal_store_closed() {
    let (dir, _) = case_store("intact");
    assert_unchanged(&dir.0, "WAL, no -wal");
}

fn write_frame(dir: &Path) -> rusqlite::Connection {
    let writer = rusqlite::Connection::open(dir.join("memory.sqlite")).expect("writer");
    writer
        .execute_batch(
            "PRAGMA wal_autocheckpoint = 0; \
             INSERT INTO meta(key, value) VALUES ('n1b_unread_frame', 'x');",
        )
        .expect("write a frame");
    writer
}

fn sees_frame(store: &StoreFile) -> bool {
    store
        .meta()
        .expect("meta")
        .iter()
        .any(|(k, _)| k == "n1b_unread_frame")
}

/// A WAL-mode store with a writer holding it open: frames in its `-wal`
/// not yet checkpointed, `-wal` and `-shm` both there.
#[test]
fn read_only_wal_store_with_live_wal() {
    let (dir, _) = case_store("intact");
    let writer = write_frame(&dir.0);
    assert!(dir.0.join("memory.sqlite-wal").exists() && dir.0.join("memory.sqlite-shm").exists());
    let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
    assert!(!store.reads_a_copy());
    assert!(sees_frame(&store));
    drop(store);
    assert_unchanged(&dir.0, "WAL, live -wal and -shm");
    drop(writer);
}

/// A `-wal` with frames and no `-shm` (copied from under a writer): read
/// from a private copy whose names are removed once open, the frame seen
/// on every later read.
#[test]
fn read_only_wal_store_with_orphan_wal() {
    let (source, _) = case_store("intact");
    let writer = write_frame(&source.0);
    let (dir, _) = case_store("intact");
    fs::copy(source.0.join("memory.sqlite"), dir.0.join("memory.sqlite")).expect("copy");
    fs::copy(
        source.0.join("memory.sqlite-wal"),
        dir.0.join("memory.sqlite-wal"),
    )
    .expect("copy wal");
    drop(writer);
    let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
    assert!(store.reads_a_copy());
    let private = store.private_dir().expect("private").to_path_buf();
    let left: Vec<String> = fs::read_dir(&private)
        .expect("private dir")
        .map(|e| e.expect("entry").file_name().to_string_lossy().into_owned())
        .collect();
    // Windows removes no open file: there the copy keeps its names while
    // the reader lives, and the check is set aside, counted.
    #[cfg(unix)]
    assert!(
        !left
            .iter()
            .any(|n| n == "memory.sqlite" || n == "memory.sqlite-wal"),
        "the copy's names are removed once open: {left:?}"
    );
    #[cfg(not(unix))]
    {
        assert!(left.iter().any(|n| n == "memory.sqlite"), "{left:?}");
        common::parity::record(
            "read_only_wal_store_with_orphan_wal",
            "private copy",
            "os",
            1,
            &["the copy's names are removed once open".to_owned()],
            &["Windows cannot remove an open file".to_owned()],
        );
    }
    // Many reads and transactions after the names are gone.
    for _ in 0..20 {
        assert!(sees_frame(&store));
        store
            .read_transaction(|s| s.each_row("log", &mut |_| {}))
            .expect("txn")
            .expect("rows");
    }
    drop(store);
    assert!(
        !private.exists(),
        "the private directory goes with the reader"
    );
    assert_unchanged(&dir.0, "WAL, -wal without -shm");
}

/// The recorded exception (M1): a writer died with frames in its `-wal`
/// and its `-shm` left as it was. Both are there, so the file is read in
/// place, and SQLite rebuilds the wal-index in the `-shm`; the store file
/// and the `-wal` are unchanged.
#[test]
fn stale_shm_is_rewritten_and_nothing_else() {
    let (source, _) = case_store("intact");
    let writer = write_frame(&source.0);
    let (dir, _) = case_store("intact");
    for suffix in ["", "-wal", "-shm"] {
        fs::copy(
            source.0.join(format!("memory.sqlite{suffix}")),
            dir.0.join(format!("memory.sqlite{suffix}")),
        )
        .expect("copy");
    }
    drop(writer);
    // A stale -shm: its header zeroed, as after a crash mid-write.
    let shm = dir.0.join("memory.sqlite-shm");
    let mut bytes = fs::read(&shm).expect("shm");
    for b in bytes.iter_mut().take(136) {
        *b = 0;
    }
    fs::write(&shm, &bytes).expect("stale shm");
    let before = files_of(&dir.0);
    let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
    assert!(!store.reads_a_copy());
    assert!(sees_frame(&store));
    drop(store);
    let after = files_of(&dir.0);
    let changed: Vec<&String> = before
        .keys()
        .filter(|k| before.get(*k) != after.get(*k))
        .collect();
    eprintln!("stale -shm: changed {changed:?}");
    assert_eq!(
        before.keys().collect::<Vec<_>>(),
        after.keys().collect::<Vec<_>>()
    );
    assert!(
        changed.iter().all(|k| k.as_str() == "memory.sqlite-shm"),
        "{changed:?}"
    );
}

fn my_private_dirs() -> Vec<PathBuf> {
    let prefix = format!("{SCRATCH_PREFIX}{}-", std::process::id());
    fs::read_dir(std::env::temp_dir())
        .expect("temp dir")
        .flatten()
        .filter(|e| e.file_name().to_string_lossy().starts_with(&prefix))
        .map(|e| e.path())
        .collect()
}

/// This process's private directories that no live reader holds (its
/// lock can be taken): what a reader that failed to clean up would leave.
/// Other tests' live readers hold theirs locked.
fn my_unheld_private_dirs() -> Vec<PathBuf> {
    let unheld = |d: &PathBuf| {
        fs::OpenOptions::new()
            .write(true)
            .open(d.join(".lock"))
            .map(|f| f.try_lock().is_ok())
            .unwrap_or(true)
    };
    let first: Vec<PathBuf> = my_private_dirs().into_iter().filter(unheld).collect();
    if first.is_empty() {
        return first;
    }
    // Another test's reader may be mid-drop (its lock file removed, its
    // directory not yet): look again once that has had time to finish.
    std::thread::sleep(Duration::from_millis(200));
    first
        .into_iter()
        .filter(|d| d.exists() && unheld(d))
        .collect()
}

/// Cleanup on the error paths: a refused store, and a panic while a
/// reader is held, leave no private directory.
#[test]
fn private_copies_go_on_error_and_panic() {
    let (dir, _) = case_store("refuse_trigger");
    match StoreFile::open(&dir.0.join("memory.sqlite")) {
        Err(StoreError::Admission(_)) => {}
        other => panic!("expected a refusal, got {other:?}"),
    }
    assert!(
        my_unheld_private_dirs().is_empty(),
        "a refused open left its copy"
    );
    let (wal_store, _) = case_store("intact");
    let path = wal_store.0.join("memory.sqlite");
    let held = std::sync::Mutex::new(None);
    let unwound = std::panic::catch_unwind(|| {
        let store = StoreFile::open(&path).expect("open");
        *held.lock().unwrap() = store.private_dir().map(Path::to_path_buf);
        panic!("a reader that panics");
    });
    assert!(unwound.is_err());
    let private = held.into_inner().unwrap().expect("read from a copy");
    assert!(!private.exists(), "a panicking reader left its copy");
    assert!(my_unheld_private_dirs().is_empty());
}

/// A directory a killed reader left is swept; a live reader's, one with no
/// lock file yet, and the reference's own are not.
#[test]
fn sweep_removes_only_dead_readers_copies() {
    let tmp = std::env::temp_dir();
    let tag = format!("{}-{}", std::process::id(), line!());
    let dead = tmp.join(format!("{SCRATCH_PREFIX}dead-{tag}"));
    let live = tmp.join(format!("{SCRATCH_PREFIX}live-{tag}"));
    let young = tmp.join(format!("{SCRATCH_PREFIX}young-{tag}"));
    let reference = tmp.join(format!("bettermemory-read-{tag}"));
    for d in [&dead, &live, &young, &reference] {
        fs::create_dir_all(d).expect("dir");
        fs::write(d.join("memory.sqlite"), b"a plaintext copy").expect("file");
    }
    fs::write(dead.join(".lock"), b"").expect("lock");
    fs::write(live.join(".lock"), b"").expect("lock");
    // Windows takes a lockable directory for a dead reader's only once its
    // lock file is a minute old: this one is made two minutes old.
    #[cfg(not(unix))]
    fs::OpenOptions::new()
        .write(true)
        .open(dead.join(".lock"))
        .expect("open the dead lock")
        .set_modified(std::time::SystemTime::now() - std::time::Duration::from_secs(120))
        .expect("age the dead lock");
    let held = fs::OpenOptions::new()
        .write(true)
        .open(live.join(".lock"))
        .expect("open lock");
    held.lock().expect("lock");
    let removed = sweep_private_copies();
    assert!(removed >= 1);
    assert!(!dead.exists(), "a dead reader's copy is swept");
    assert!(live.exists(), "a live reader's copy is kept");
    assert!(
        young.exists(),
        "a copy with no lock yet is kept within the hour"
    );
    assert!(
        reference.exists(),
        "the reference's own directories are not touched"
    );
    drop(held);
    for d in [&live, &young, &reference] {
        let _ = fs::remove_dir_all(d);
    }
}

/// A writer appending signed rows and moving the head file while the
/// native build verifies the live store in place. Every report is a
/// whole chain from one snapshot: no MAC, link or seq problem; the head
/// file, read after the rows as the reference reads it, can be behind
/// (`stale`) or ahead (`tail_deleted`) of the snapshot.
#[test]
fn verify_while_a_writer_appends() {
    let (dir, _) = case_store("intact");
    let id = store_id();
    let keys = dir.0.join("keys");
    let key = fs::read(keys.join(format!("{id}.key"))).expect("key");
    let stop = Arc::new(AtomicBool::new(false));
    let writer = {
        let (store, keys, stop, id, key) = (
            dir.0.join("memory.sqlite"),
            keys.clone(),
            stop.clone(),
            id.clone(),
            Key::new(key),
        );
        std::thread::spawn(move || {
            let conn = rusqlite::Connection::open(&store).expect("writer");
            conn.busy_timeout(Duration::from_secs(5)).expect("busy");
            let mut written = 0;
            while !stop.load(Ordering::Relaxed) {
                let last: LogRow = conn
                    .query_row(
                        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log \
                         ORDER BY seq DESC LIMIT 1",
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
                    .expect("last");
                let payload = PyValue::Dict(vec![(PyValue::str("n"), PyValue::int(written))]);
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
                let head = keys.join(format!("{id}.head"));
                let tmp = keys.join(format!("{id}.head.tmp"));
                fs::write(&tmp, head_file_text(row.seq, &row.mac, "t")).expect("head");
                fs::rename(&tmp, &head).expect("rename");
                written += 1;
            }
            written
        })
    };
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    let started = Instant::now();
    let mut reads = 0;
    while reads < 40 {
        let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
        let meta = store.meta().expect("meta");
        let fp = meta
            .iter()
            .find(|(k, _)| k == "key_fingerprint")
            .expect("fp")
            .1
            .clone();
        let ring = load_key_ring(&keys, &id).expect("ring");
        let report = store.verify_chain(&ring, &fp).expect("verify");
        assert!(
            report.problems.is_empty(),
            "chain problems during live writes: {:?}",
            report.problems
        );
        let head = match &report.head[0].1 {
            bettermemory_core::log::ReportValue::Str(s) => {
                bettermemory_core::unicode::to_string(s).unwrap_or_default()
            }
            other => format!("{other:?}"),
        };
        *seen.entry(head).or_default() += 1;
        reads += 1;
    }
    stop.store(true, Ordering::Relaxed);
    let written = writer.join().expect("writer");
    eprintln!(
        "{reads} verifies in {:?} while {written} rows were written; head statuses {seen:?}",
        started.elapsed()
    );
}

/// The deadline: a statement made slow on an admitted connection is
/// interrupted within the bound and refused as timed out.
#[test]
fn the_deadline_interrupts_a_slow_read() {
    let (dir, _) = case_store("intact");
    let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
    let started = Instant::now();
    let err = store.with_admission_deadline("schema", |conn| {
        conn.query_row(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) \
             SELECT count(*) FROM c",
            [],
            |_| Ok(()),
        )
        .map_err(admission::AdmitError::Sqlite)
    });
    let took = started.elapsed();
    match err {
        Err(admission::AdmitError::Refused(r)) => {
            assert!(r.timed_out);
            assert_eq!(
                r.why,
                "reading its schema ran past the 2 s a store file is given before it is admitted"
            );
        }
        other => panic!("not interrupted: {other:?}"),
    }
    assert!(took < Duration::from_secs(4), "{took:?}");
    eprintln!("interrupted after {took:?}");
}

/// A store in WAL mode, closed, with no `-wal`: read through a private
/// copy (`immutable=1`) on every platform. This is the path that failed on
/// Linux in the first CI run (SQLite cannot open `/dev/fd/N` there); the
/// error names the step that failed.
#[test]
fn a_store_without_wal_is_read_through_a_copy() {
    let (dir, _) = case_store("intact");
    let store = StoreFile::open(&dir.0.join("memory.sqlite"))
        .unwrap_or_else(|e| panic!("open a store with no -wal: {e}"));
    assert!(store.reads_a_copy());
    let private = store.private_dir().expect("private").to_path_buf();
    assert!(!store.meta().expect("meta").is_empty());
    store
        .read_transaction(|s| s.each_row("log", &mut |_| {}))
        .expect("txn")
        .expect("rows");
    drop(store);
    assert!(!private.exists(), "the private copy is removed on drop");
}

/// A store copied with no `-wal` (WAL mode, closed): once open, the private
/// directory holds no named copy, only the reader's `.lock`, 0600. On macOS
/// and FreeBSD the copy's name is removed before a byte is written to it;
/// on Linux the named copy is unlinked after SQLite opens it (README, "The
/// store, read only"). POSIX only: Windows keeps the named copy until the
/// reader drops, and has no mode bits (the test below).
#[cfg(unix)]
#[test]
fn a_copy_without_wal_never_has_a_name() {
    use std::os::unix::fs::PermissionsExt;
    let (dir, _) = case_store("intact");
    let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
    assert!(store.reads_a_copy());
    let private = store.private_dir().expect("private").to_path_buf();
    let left: Vec<String> = fs::read_dir(&private)
        .expect("private dir")
        .map(|e| e.expect("entry").file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(left, vec![".lock".to_owned()]);
    let mode = |p: &Path| fs::metadata(p).expect("meta").permissions().mode() & 0o777;
    assert_eq!(mode(&private.join(".lock")), 0o600);
    assert_eq!(mode(&private), 0o700);
    store
        .read_transaction(|s| s.each_row("log", &mut |_| {}))
        .expect("txn")
        .expect("rows");
    drop(store);
    assert!(!private.exists());
}

/// Windows: the copy keeps its name while the reader lives (no open file
/// can be removed) and goes when it drops; mode bits do not exist there,
/// so the 0600/0700 checks are set aside, counted, with that reason.
#[cfg(not(unix))]
#[test]
fn a_copy_without_wal_never_has_a_name() {
    let (dir, _) = case_store("intact");
    let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
    let private = store.private_dir().expect("private").to_path_buf();
    assert!(private.join(".lock").exists());
    drop(store);
    assert!(!private.exists());
    common::parity::record(
        "a_copy_without_wal_never_has_a_name",
        "private copy",
        "os",
        1,
        &["no name before the first write; 0600/0700 modes".to_owned()],
        &["Windows cannot remove an open file and has no mode bits".to_owned()],
    );
}

/// Readers opening copies while sweepers run, in a loop: no sweep removes
/// a live reader's directory (it is locked before it carries the swept
/// name), and every reader reads after the sweeps.
#[test]
fn sweepers_never_remove_a_live_readers_directory() {
    let (dir, _) = case_store("intact");
    let path = dir.0.join("memory.sqlite");
    let stop = Arc::new(AtomicBool::new(false));
    let sweepers: Vec<_> = (0..4)
        .map(|_| {
            let stop = stop.clone();
            std::thread::spawn(move || {
                let mut sweeps = 0u64;
                while !stop.load(Ordering::Relaxed) {
                    sweep_private_copies();
                    sweeps += 1;
                }
                sweeps
            })
        })
        .collect();
    let readers: Vec<_> = (0..4)
        .map(|_| {
            let path = path.clone();
            std::thread::spawn(move || {
                for _ in 0..100 {
                    let store = StoreFile::open(&path).expect("open");
                    let private = store.private_dir().expect("private").to_path_buf();
                    std::thread::yield_now();
                    assert!(
                        private.join(".lock").exists(),
                        "a live reader's lock was swept"
                    );
                    store.meta().expect("meta");
                }
            })
        })
        .collect();
    for r in readers {
        r.join().expect("reader");
    }
    stop.store(true, Ordering::Relaxed);
    let sweeps: u64 = sweepers
        .into_iter()
        .map(|s| s.join().expect("sweeper"))
        .sum();
    eprintln!("400 opens against {sweeps} sweeps, no live directory removed");
    assert!(sweeps > 0);
}
