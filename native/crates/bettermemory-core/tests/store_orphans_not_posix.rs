//! `store_orphans.jsonl` has no Windows case: a saved orphan's name is
//! plain by the host's path rules, and on Windows a backslash or a drive
//! makes it not plain. `golden_store_orphans.rs` is POSIX only; here the
//! gap is recorded, so the parity table names it instead of showing
//! nothing.

#![cfg(not(unix))]

mod common;

#[test]
fn the_orphans_fixture_has_no_case_here() {
    common::parity::record(
        "every_case_finds_the_orphans_the_reference_finds",
        "store_orphans.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture names files by POSIX path rules; Windows has no case".to_owned()],
    );
}
