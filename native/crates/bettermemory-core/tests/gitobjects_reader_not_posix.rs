//! `gitobjects_reader_posix.jsonl` has no Windows case: its generator
//! makes FIFOs and modes and walks an alternates file whose directory
//! Windows' `ntpath.realpath` spells by asking the system.
//! `golden_gitobjects_reader_posix.rs` runs on POSIX only; here the gap is
//! recorded. `golden_gitobjects_reader.rs` runs everywhere.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_gitobjects_reader_fixture_has_no_case_here() {
    common::parity::record(
        "golden_gitobjects_reader_posix",
        "gitobjects_reader_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes FIFOs, modes and an alternates walk; Windows has no case".to_owned()],
    );
}
