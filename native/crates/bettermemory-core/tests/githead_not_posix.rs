//! `githead_posix.jsonl` has no Windows case: its generator makes
//! symbolic links and FIFOs, and reads a file where a directory of the
//! path should be, which Windows answers otherwise. `golden_githead.rs`
//! runs it on POSIX only; here the gap is recorded. The Windows flavour's
//! answers to links and ENOTDIR are held to their rule in
//! `githead_robustness.rs` on every platform.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_githead_fixture_has_no_case_here() {
    common::parity::record(
        "golden_githead_posix",
        "githead_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links and FIFOs; Windows has no case".to_owned()],
    );
}
