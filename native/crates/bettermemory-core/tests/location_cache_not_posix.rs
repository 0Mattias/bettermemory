//! `location_cache_posix.jsonl` has no Windows case: its generator stamps
//! real trees and reads a mode-000 HEAD, and the Windows flavour of
//! `fsdata` does not port the stat fields a stamp holds nor resolve a
//! relative path (Linear ENG-124), so `signature` declines there where the
//! reference on Windows answers. `golden_location_cache_posix.rs` runs on
//! POSIX only; here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_location_cache_fixture_has_no_case_here() {
    common::parity::record(
        "golden_location_cache_posix",
        "location_cache_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture stamps real trees with modes; the Windows flavour declines".to_owned()],
    );
}
