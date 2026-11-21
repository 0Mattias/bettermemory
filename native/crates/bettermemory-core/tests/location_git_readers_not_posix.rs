//! `location_git_readers_posix.jsonl` has no case off POSIX: its generator
//! makes links and mode-000 entries, which Windows answers otherwise.
//! `golden_location_git_readers_posix.rs` runs on POSIX only; here the gap
//! is recorded. The history fixture runs everywhere.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_location_git_readers_fixture_has_no_case_here() {
    common::parity::record(
        "golden_location_git_readers_posix",
        "location_git_readers_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links and mode-000 entries; Windows has no case".to_owned()],
    );
}
