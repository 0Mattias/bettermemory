//! `location_view_posix.jsonl` has no Windows case: its generator builds
//! repositories with links and a mode-000 folder, and keys them by
//! `st_dev` and `st_ino`. Under the Windows flavour `view_state` declines
//! where `_common_of` must resolve a path (`fsdata` ports no `realpath`
//! there, Linear ENG-124). `golden_location_view_posix.rs` runs on POSIX
//! only; here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_location_view_fixture_has_no_case_here() {
    common::parity::record(
        "golden_location_view_posix",
        "location_view_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture builds links and a mode-000 folder; Windows has no case".to_owned()],
    );
}
