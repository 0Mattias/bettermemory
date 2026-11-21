//! `settings_create_posix.jsonl` has no case off POSIX: its generator reads
//! mode bits and fails the write over a file parent and a mode-0600
//! parent, which Windows answers otherwise, and the file is not generated
//! there. `golden_settings_create_posix.rs` reads it on unix only; here the
//! gap is recorded, so the parity table names it.

#![cfg(not(unix))]

mod common;

#[test]
fn the_settings_create_posix_fixture_has_no_case_here() {
    common::parity::record(
        "golden_settings_create_posix",
        "settings_create_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads POSIX modes; Windows has no case".to_owned()],
    );
}
