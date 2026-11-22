//! `store_upgrade_posix.jsonl` has no case off POSIX: its generator holds
//! SQLite's file locks from a second connection, seen on POSIX only.
//! `golden_store_upgrade_posix.rs` runs there; here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_store_upgrade_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_upgrade_posix",
        "store_upgrade_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture holds file locks from a second connection; seen on POSIX only".to_owned()],
    );
}
