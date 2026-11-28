//! `origin_reach_walk_posix.jsonl` has no Windows case: its generator makes
//! repositories with the host's git and finds them through the owner's
//! check, which declines off POSIX. `golden_origin_reach_walk_posix.rs`
//! runs on POSIX only; here the gap is recorded, with the Windows flavour's
//! stated decline (ENG-124): `walk_files_signature` is not ported for it,
//! so `commits_since_anchor` answers `Error::NotPorted` before the walk
//! runs where the reference keys and walks: before any git runs where the
//! caller passes the root and the head, and after `repo_toplevel_and_head`
//! where it does not (`origin_reach_walk_robustness` pins the decline
//! under that flavour on every platform).

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_walk_fixture_has_no_case_here() {
    common::parity::record(
        "golden_origin_reach_walk_posix",
        "origin_reach_walk_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture makes repositories with the host's git and finds them \
             through the owner's check; the Windows flavour declines \
             walk_files_signature (not ported), so commits_since_anchor \
             declines before the walk runs"
                .to_owned(),
        ],
    );
}

#[test]
fn the_windows_flavour_declines_before_git() {
    common::origin_reach_walk::windows_flavour_declines();
}
