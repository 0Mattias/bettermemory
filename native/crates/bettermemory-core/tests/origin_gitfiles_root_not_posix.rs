//! `origin_gitfiles_root_posix.jsonl` has no Windows case: its generator
//! makes links, a FIFO and a mode-000 file and spells a root as the host's
//! `Path` does. `golden_origin_gitfiles_root_posix.rs` runs on POSIX only;
//! here the gap is recorded, with the Windows flavour's stated decline:
//! `refused_worktree_root` answers `NotPorted` there, since `realpath` is
//! not ported for it.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_root_fixture_has_no_case_here() {
    common::parity::record(
        "golden_origin_gitfiles_root_posix",
        "origin_gitfiles_root_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture makes links and a FIFO and spells roots by the host's Path; \
             the Windows flavour declines refused_worktree_root (realpath not ported)"
                .to_owned(),
        ],
    );
}
