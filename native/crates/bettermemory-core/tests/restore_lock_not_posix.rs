//! `store_restore_lock.json` has no Windows case: its generator holds a
//! POSIX directory `flock` as a restore does, and on Windows the lock is a
//! record file and a handle on the store (ENG-321). `golden_restore_lock.rs`
//! is POSIX only; here the gap is recorded, so the parity table names it
//! instead of showing nothing. The Windows lock itself is taken by every
//! test that opens a store.

#![cfg(not(unix))]

mod common;

#[test]
fn the_restore_lock_fixture_has_no_case_here() {
    common::parity::record(
        "golden_restore_lock",
        "store_restore_lock.json",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture drives a POSIX directory flock; Windows has no case".to_owned()],
    );
}
