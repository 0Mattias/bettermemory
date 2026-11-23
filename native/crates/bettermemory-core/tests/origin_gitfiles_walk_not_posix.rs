//! `origin_gitfiles_walk_posix.jsonl` has no Windows case: its generator
//! discovers repositories through the owner's check, which the reference
//! runs on POSIX only. `golden_origin_gitfiles_walk_posix.rs` runs on
//! POSIX only; here the gap is recorded, with the Windows flavour's stated
//! declines: `walk_files_signature` (and so `files_held` over a walk key)
//! answers `NotPorted` under the Windows flavour where the environment
//! permits the walk, and discovery and
//! `locate` find no repository there (realpath is not ported for it,
//! ENG-124), so `capture_files` and `located_head` answer none where the
//! reference on Windows may answer.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_walk_fixture_has_no_case_here() {
    common::parity::record(
        "golden_origin_gitfiles_walk_posix",
        "origin_gitfiles_walk_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture checks owners; the Windows flavour declines discovery \
             (realpath not ported) and walk_files_signature"
                .to_owned(),
        ],
    );
}
