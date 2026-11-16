//! `gitfile_leaves_posix.jsonl` has no Windows case: its generator makes
//! symbolic links and mode-000 files, which Windows answers otherwise.
//! `golden_gitfile_leaves_posix.rs` runs it on POSIX only; here the gap
//! is recorded. The Windows flavour's parse is held to `ntpath` in
//! `golden_gitfile_leaves.rs` (every platform) and its reads in
//! `gitfile_leaves_robustness.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_gitfile_leaves_fixture_has_no_case_here() {
    for test in [
        "golden_gitfile_leaves_posix",
        "golden_gitfile_leaves_posix_stdfs",
    ] {
        common::parity::record(
            test,
            "gitfile_leaves_posix.jsonl",
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["the fixture makes links and mode-000 files; Windows has no case".to_owned()],
        );
    }
}
