//! `store_restore_lock_hold.json` and `store_restore_lock_hold_python.json`
//! have no Windows case: its generator holds
//! a POSIX directory `flock` as a restore does, and on Windows the lock is
//! a record file locked by byte range (ENG-321). Here the gap is recorded,
//! so the parity table names it instead of showing nothing. Under
//! `fsdata`'s Windows flavour nothing in the module resolves a path
//! (ENG-124). The per-process registry of held locks is keyed there by
//! the volume serial number and file index, as `os.fstat` reads them.

#![cfg(not(unix))]

mod common;

#[test]
fn the_restore_hold_rows_have_no_case_here() {
    common::parity::record(
        "golden_restore_lock_hold",
        "store_restore_lock_hold.json",
        "os",
        0,
        &["every hold and probe row of the fixture".to_owned()],
        &["the rows drive a POSIX directory flock; Windows has no case".to_owned()],
    );
}

#[test]
fn the_deep_marker_hold_row_has_no_case_here() {
    common::parity::record(
        "golden_restore_lock_hold",
        "store_restore_lock_hold_python.json",
        "os",
        0,
        &["the hold row of the marker nested 70,000 lists deep".to_owned()],
        &["the row drives a POSIX directory flock; Windows has no case".to_owned()],
    );
}
