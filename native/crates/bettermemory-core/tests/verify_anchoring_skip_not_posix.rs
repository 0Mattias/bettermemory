//! `verify_anchoring_skip.jsonl` has no Windows case: its generator
//! resolves working trees through links on disk and spells paths by
//! `posixpath`. The port's Windows flavour declines where it would
//! resolve a path or stamp a file (`realpath` and the stamp's inode are
//! not ported for it, ENG-124). `golden_verify_anchoring_skip.rs` runs on
//! POSIX only; here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_verify_anchoring_skip_fixture_has_no_case_here() {
    common::parity::record(
        "golden_verify_anchoring_skip",
        "verify_anchoring_skip.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture makes links and spells POSIX paths; Windows has no case; \
           the Windows flavour declines (ENG-124)"
                .to_owned(),
        ],
    );
}
