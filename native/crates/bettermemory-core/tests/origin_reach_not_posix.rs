//! `origin_reach_posix.jsonl` has no Windows case: its generator makes
//! repositories with the host's git and finds them through the owner's
//! check, which declines off POSIX. `golden_origin_reach_posix.rs` runs on
//! POSIX only; here the gap is recorded, with the Windows flavour's stated
//! decline (ENG-124): `realpath` is not ported for it, so `repo_toplevel`
//! and `repo_toplevel_and_head` answer `Error::NotPorted` where git names
//! a root the reference resolves, and `RepositoryReads` declines
//! `spelled_as_git` and the shallow cuts.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_reach_fixture_has_no_case_here() {
    common::parity::record(
        "golden_origin_reach_posix",
        "origin_reach_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture makes repositories with the host's git and finds them \
             through the owner's check; the Windows flavour declines Path.resolve \
             (realpath not ported) and the shallow cuts"
                .to_owned(),
        ],
    );
}
