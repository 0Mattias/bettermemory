//! `gitconfig_file.jsonl` has no Windows case: its generator makes
//! symbolic links, FIFOs and mode-000 files and opens directories, which
//! Windows answers otherwise. `golden_gitconfig_file.rs` runs it on POSIX
//! only; here the gap is recorded. The Windows flavour's rules are held
//! in `gitconfig_file_robustness.rs` and `fsdata_stdfs.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_gitconfig_file_fixture_has_no_case_here() {
    for test in ["golden_gitconfig_file", "golden_gitconfig_file_stdfs"] {
        common::parity::record(
            test,
            "gitconfig_file.jsonl",
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["the fixture makes links, FIFOs and mode-000 files; Windows has no case".to_owned()],
        );
    }
}
