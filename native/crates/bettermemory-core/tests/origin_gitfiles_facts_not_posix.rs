//! `origin_gitfiles_facts_posix.jsonl` has no Windows case: its generator
//! joins with the host's `os.path` (`ntpath` on Windows) and makes links
//! and a mode-000 folder. `golden_origin_gitfiles_facts_posix.rs` runs on
//! POSIX only; here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_facts_fixture_has_no_case_here() {
    common::parity::record(
        "golden_origin_gitfiles_facts_posix",
        "origin_gitfiles_facts_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture joins with the host's os.path and makes links; Windows has no case"
                .to_owned(),
        ],
    );
}
