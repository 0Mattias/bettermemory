//! The Windows restore lock is one byte of the record file, at
//! `_WINDOWS_LOCK_OFFSET` (1 MiB), as the reference's `msvcrt.locking`
//! takes it: past any marker, so the marker reads through every handle
//! while a lock is held, and a second handle's take is refused. A lock of
//! the whole file (std's `File::lock`) refused every other handle's
//! `ReadFile` with ERROR_LOCK_VIOLATION, which read as "no record of it
//! reads" (golden_store_create, case restore_marker_beside).

#![cfg(windows)]

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::restore_lock::{
    acquire, admit, read_marker, restore_state, AdmitError, FileKey, Marker, State,
};
use std::path::PathBuf;
use std::time::Duration;

const MARKER: &str = r#"{"format": "bettermemory.restore_lock", "started_at": "2001-02-03T04:05:06Z", "pid": 12, "bundle": "/bundles/b1", "stage": "swap"}"#;

fn nobody(_: FileKey) -> bool {
    false
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("bm-restore-byte-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("scratch directory");
    let store = dir.join("memory.sqlite");
    std::fs::write(dir.join("memory.sqlite.restore-lock"), MARKER).expect("marker");
    store
}

fn has_record(marker: Option<Marker>) {
    let m = marker.expect("a marker");
    assert!(
        m.iter()
            .any(|(k, v)| *k == PyValue::str("stage") && *v == PyValue::str("swap")),
        "{m:?}"
    );
}

#[test]
fn the_marker_reads_while_an_admission_takes_the_lock() {
    let store = scratch("admit");
    match admit(&store, Duration::ZERO, nobody) {
        Err(AdmitError::Locked(refusal)) => {
            let text = refusal.to_string();
            assert!(
                text.contains("started 2001-02-03T04:05:06Z, by process 12"),
                "{text}"
            );
            assert!(text.contains("stopped at swap"), "{text}");
        }
        other => panic!("{other:?}"),
    }
    let _ = std::fs::remove_dir_all(store.parent().unwrap());
}

#[test]
fn the_marker_reads_while_a_restore_holds_and_after_release() {
    let store = scratch("hold");
    let hold = acquire(&store, Duration::ZERO)
        .expect("acquire")
        .expect("taken");
    has_record(read_marker(&store).expect("read"));
    // A second handle's take is refused while the first holds.
    let (state, record) = restore_state(&store, Duration::ZERO, nobody).expect("state");
    assert_eq!(state, State::Running);
    has_record(record);
    hold.release(false);
    has_record(read_marker(&store).expect("read"));
    let (state, record) = restore_state(&store, Duration::ZERO, nobody).expect("state");
    assert_eq!(state, State::Unfinished);
    has_record(record);
    let _ = std::fs::remove_dir_all(store.parent().unwrap());
}
