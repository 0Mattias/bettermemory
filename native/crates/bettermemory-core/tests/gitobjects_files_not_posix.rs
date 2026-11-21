//! `gitobjects_files_posix.jsonl` has no Windows case: its generator makes
//! links, FIFOs and modes, and its directories are spelled by
//! `posixpath.realpath`, where Windows' `ntpath.realpath` asks the system.
//! `golden_gitobjects_files_posix.rs` runs on POSIX only; here the gap is
//! recorded. `golden_gitobjects_files.rs` runs everywhere.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_gitobjects_files_fixture_has_no_case_here() {
    common::parity::record(
        "golden_gitobjects_files_posix",
        "gitobjects_files_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links, FIFOs and modes; Windows has no case".to_owned()],
    );
}
