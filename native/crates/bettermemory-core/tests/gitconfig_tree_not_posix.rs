//! `gitconfig_tree.jsonl` has no Windows case: its generator builds real
//! trees with links, FIFOs and modes and reads them with the host's
//! `os.path` and `pwd`. Here the gap is recorded; the Windows path rules
//! are held by `gitconfig_tree_rules.jsonl` on every platform.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_gitconfig_tree_fixture_has_no_case_here() {
    for test in ["golden_gitconfig_tree", "golden_gitconfig_tree_stdfs"] {
        common::parity::record(
            test,
            "gitconfig_tree.jsonl",
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["the fixture builds POSIX trees; Windows has no case".to_owned()],
        );
    }
}
