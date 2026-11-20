//! `location_stamps.jsonl` and `location_stamps_python.jsonl` have no
//! Windows case: their generator names devices by `os.major`, which
//! Windows' `os` lacks. `golden_location_stamps_posix.rs` runs them on
//! POSIX only; here the gap is recorded. `location_stamps_platform.jsonl`,
//! the `win32` section among them, runs everywhere in
//! `golden_location_stamps.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_location_stamps_fixtures_have_no_case_here() {
    for file in ["location_stamps.jsonl", "location_stamps_python.jsonl"] {
        common::parity::record(
            "golden_location_stamps_posix",
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["the generator names devices by os.major; Windows has no case".to_owned()],
        );
    }
}
