//! `gitfile_identity.jsonl` and `gitfile_identity_python.jsonl` have no
//! Windows case: their trees hold links, hard links and modes, and the
//! helpers need `samestat` and `realpath`, declined on Windows. Here the
//! gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_identity_fixtures_have_no_case_here() {
    for (test, file) in [
        ("golden_gitfile_identity", "gitfile_identity.jsonl"),
        ("golden_gitfile_identity_stdfs", "gitfile_identity.jsonl"),
        (
            "golden_gitfile_identity_gated",
            "gitfile_identity_python.jsonl",
        ),
        (
            "golden_gitfile_identity_gated_stdfs",
            "gitfile_identity_python.jsonl",
        ),
    ] {
        common::parity::record(
            test,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["POSIX trees; samestat and realpath are not ported on Windows".to_owned()],
        );
    }
}
