//! The reader's side of the restore lock against `store_restore_lock.json`
//! (`restore_lock.reading`): a read during a restore (a helper holding the
//! store's directory exclusively) is refused with the reference's text and
//! makes nothing; a marker with no lock held refuses no read; a read holds
//! a restore off until the store drops. Gated on the platform: the lock
//! is a directory `flock` on POSIX and a record file and a handle on
//! Windows, and the committed files were made on POSIX. The fixture has
//! no Windows case (its generator drives a directory `flock`), so this
//! file is POSIX only; `restore_lock_not_posix.rs` records that on Windows.

#![cfg(unix)]

mod common;

use std::fs::{self, File};
use std::path::Path;
use std::time::Duration;

use bettermemory_core::store::restore_lock::{lock_path, RESTORE_RUNNING_STATUS};
use bettermemory_core::store::{StoreError, StoreFile};
use common::parity::Ledger;
use common::stores::case_store;
use serde_json::Value;

const FILE: &str = "store_restore_lock.json";

fn names(dir: &Path) -> Vec<String> {
    let mut out: Vec<String> = fs::read_dir(dir)
        .expect("dir")
        .map(|e| e.expect("entry").file_name().to_string_lossy().into_owned())
        .collect();
    out.sort();
    out
}

#[test]
fn reads_meet_the_restore_lock_as_the_reference() {
    let text = fs::read_to_string(common::golden_path("store_restore_lock.json")).expect("file");
    let doc: Value = serde_json::from_str(&text).expect("JSON");
    assert_eq!(doc["lock_suffix"], ".restore-lock");
    let cases = doc["cases"].as_array().expect("cases");
    let mut ledger = Ledger::new("reads_meet_the_restore_lock_as_the_reference", FILE);
    for case in cases {
        let name = case["name"].as_str().expect("name");
        ledger.case(name, || {
            let (dir, _) = case_store("intact");
            let store = dir.0.join("memory.sqlite");
            if case["marker"].as_bool().expect("marker") {
                fs::write(
                    lock_path(&store),
                    "{\"format\": \"bettermemory.restore_lock\"}\n",
                )
                .expect("marker");
            }
            let helper = File::open(&dir.0).expect("helper");
            if case["held"].as_bool().expect("held") {
                helper.try_lock().expect("helper takes the lock");
            }
            let before = names(&dir.0);
            let opened = StoreFile::open_waiting(&store, Duration::from_millis(200));
            if case["refused"].as_bool().expect("refused") {
                let err = opened.expect_err(name);
                assert!(matches!(err, StoreError::RestoreRunning), "{name}: {err}");
                assert_eq!(
                    format!("<store> {err}"),
                    case["message"].as_str().expect("message"),
                    "{name}"
                );
                assert_eq!(case["error"], "RestoreRunningError");
                assert_eq!(
                    i64::from(RESTORE_RUNNING_STATUS),
                    case["status"].as_i64().unwrap()
                );
            } else {
                let opened = opened.unwrap_or_else(|e| panic!("{name}: {e}"));
                assert_eq!(case["holds"], true, "{name}");
                let kept_off = matches!(helper.try_lock(), Err(fs::TryLockError::WouldBlock));
                assert_eq!(
                    kept_off,
                    case["restore_kept_off"].as_bool().unwrap(),
                    "{name}"
                );
                drop(opened);
                helper.try_lock().expect("the lock is let go at close");
                helper.unlock().expect("unlock");
            }
            assert_eq!(names(&dir.0), before, "{name}: a read made nothing");
            let marker_there = lock_path(&store).exists();
            let expected_names: Vec<&str> = case["names_after"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap())
                .collect();
            assert_eq!(
                marker_there,
                expected_names.contains(&"memory.sqlite.restore-lock"),
                "{name}"
            );
        });
    }
    assert_eq!(cases.len(), 4);
    ledger.finish(cases.len());
}

/// A read refused during a restore creates nothing in the store's
/// directory or its keys directory, and leaves no private copy.
#[test]
fn a_read_during_a_restore_refuses_and_creates_nothing() {
    let (dir, _) = case_store("intact");
    let store = dir.0.join("memory.sqlite");
    let helper = File::open(&dir.0).expect("helper");
    helper.try_lock().expect("lock");
    let before = (names(&dir.0), names(&dir.0.join("keys")));
    let started = std::time::Instant::now();
    let err = StoreFile::open(&store).expect_err("refused");
    let waited = started.elapsed();
    assert!(matches!(err, StoreError::RestoreRunning));
    assert!(waited >= Duration::from_millis(1900), "waited {waited:?}");
    assert!(waited < Duration::from_secs(4), "waited {waited:?}");
    assert_eq!((names(&dir.0), names(&dir.0.join("keys"))), before);
    helper.unlock().expect("unlock");
    // Once the restore lets go, the read goes through.
    drop(StoreFile::open(&store).expect("open after the restore"));
}

fn golden() -> Value {
    let text = fs::read_to_string(common::golden_path("store_restore_lock.json")).expect("file");
    serde_json::from_str(&text).expect("JSON")
}

/// The wait and the exit status are the reference's.
#[test]
fn the_wait_and_status_are_the_references() {
    use bettermemory_core::store::restore_lock::OPEN_WAIT;
    let doc = golden();
    let mut ledger = Ledger::new("the_wait_and_status_are_the_references", FILE);
    ledger.case("wait and status", || {
        assert_eq!(
            OPEN_WAIT.as_secs_f64(),
            doc["open_wait_seconds"].as_f64().unwrap()
        );
        assert_eq!(doc["poll_seconds"].as_f64().unwrap(), 0.05);
        assert_eq!(
            i64::from(RESTORE_RUNNING_STATUS),
            doc["restore_running_status"].as_i64().unwrap()
        );
    });
    ledger.finish(1);
}

/// The record mode (a directory `flock` cannot lock): no record file, one
/// held, one free.
#[test]
fn record_mode_as_the_reference() {
    use bettermemory_core::store::restore_lock::{record_mode_reading, Refused};
    let doc = golden();
    let cases = doc["record_mode"].as_array().expect("cases");
    let mut ledger = Ledger::new("record_mode_as_the_reference", FILE);
    for case in cases {
        let name = case["name"].as_str().unwrap();
        ledger.case(name, || {
            let (dir, _) = case_store("intact");
            let store = dir.0.join("memory.sqlite");
            let mut helper = None;
            if case["record"].as_bool().unwrap() {
                fs::write(lock_path(&store), "{}\n").expect("record");
                let f = File::open(lock_path(&store)).expect("record");
                if case["held"].as_bool().unwrap() {
                    f.try_lock().expect("helper");
                }
                helper = Some(f);
            }
            let before = names(&dir.0);
            let got = record_mode_reading(&store, Duration::from_millis(200));
            match got {
                Err(Refused::Running) => {
                    assert_eq!(case["refused"], true, "{name}");
                    assert_eq!(case["error"], "RestoreRunningError", "{name}");
                }
                Ok(hold) => {
                    assert_eq!(case["refused"], false, "{name}");
                    assert_eq!(hold.is_some(), case["holds"].as_bool().unwrap(), "{name}");
                }
            }
            assert_eq!(names(&dir.0), before, "{name}");
            drop(helper);
        });
    }
    assert_eq!(cases.len(), 3);
    ledger.finish(cases.len());
}

/// `path` relative to the current directory, by `..` steps.
fn relative_to_cwd(path: &Path) -> std::path::PathBuf {
    let cwd = fs::canonicalize(std::env::current_dir().expect("cwd")).expect("cwd");
    let common: Vec<_> = cwd
        .components()
        .zip(path.components())
        .take_while(|(a, b)| a == b)
        .collect();
    let mut out = std::path::PathBuf::new();
    for _ in common.len()..cwd.components().count() {
        out.push("..");
    }
    for c in path.components().skip(common.len()) {
        out.push(c);
    }
    out
}

/// The lock is the parent of the path as given: a relative path, one
/// through `..` and a linked directory reach the real directory; a link
/// to the store file in another directory does not.
#[test]
fn paths_as_given_as_the_reference() {
    let doc = golden();
    let cases = doc["paths"].as_array().expect("cases");
    let mut ledger = Ledger::new("paths_as_given_as_the_reference", FILE);
    for case in cases {
        let name = case["name"].as_str().unwrap();
        ledger.case(name, || {
            let (dir, _) = case_store("intact");
            let root = dir.0.join("case");
            let real = root.join("real");
            fs::create_dir_all(&real).expect("real");
            for entry in ["memory.sqlite", "keys"] {
                fs::rename(dir.0.join(entry), real.join(entry)).expect("move");
            }
            fs::create_dir(root.join("other")).expect("other");
            fs::create_dir(root.join("linkdir")).expect("linkdir");
            #[cfg(unix)]
            {
                std::os::unix::fs::symlink(
                    real.join("memory.sqlite"),
                    root.join("linkdir/memory.sqlite"),
                )
                .expect("link");
                std::os::unix::fs::symlink(&real, root.join("ldir")).expect("link");
            }
            let helper = File::open(&real).expect("helper");
            helper.try_lock().expect("helper takes the lock");
            let given = relative_to_cwd(&root).join(case["path"].as_str().unwrap());
            assert!(given.is_relative(), "{name}");
            let got = StoreFile::open_waiting(&given, Duration::from_millis(200));
            if case["refused"].as_bool().unwrap() {
                assert!(
                    matches!(got, Err(StoreError::RestoreRunning)),
                    "{name}: {got:?}"
                );
            } else {
                let store = got.unwrap_or_else(|e| panic!("{name}: {e}"));
                assert_eq!(case["holds"], true, "{name}");
                drop(store);
            }
        });
    }
    assert_eq!(cases.len(), 4);
    ledger.finish(cases.len());
}

/// A restore meeting a read that holds the lock: its exclusive take fails
/// while the native read is open (the reference's `acquire` returns None
/// and `_take_restore_lock` refuses with the recorded detail), and nothing
/// is made.
#[test]
fn a_restore_meets_a_native_read() {
    let doc = golden();
    let case = &doc["restore_meets_reader"];
    let mut ledger = Ledger::new("a_restore_meets_a_native_read", FILE);
    ledger.case("restore_meets_reader", || {
        assert_eq!(case["reader_holds"], true);
        assert_eq!(case["restore_took_lock"], false);
        assert!(case["detail"].as_str().unwrap().starts_with(
            "another restore of <store> is running, or a process is opening or reading"
        ));
        let (dir, _) = case_store("intact");
        let store = dir.0.join("memory.sqlite");
        let reader = StoreFile::open(&store).expect("open");
        let before = names(&dir.0);
        let restore = File::open(&dir.0).expect("restore");
        assert!(matches!(
            restore.try_lock(),
            Err(fs::TryLockError::WouldBlock)
        ));
        assert_eq!(names(&dir.0), before);
        drop(reader);
        restore.try_lock().expect("taken once the read closes");
    });
    ledger.finish(1);
}
