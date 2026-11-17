//! `gitfile_trust.jsonl` and `gitfile_trust_python.jsonl` have no Windows
//! case: their trees hold links and modes, and `gitfile_rule::trust`
//! declines the Windows flavour whole. Here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_trust_fixtures_have_no_case_here() {
    for (test, file) in [
        ("golden_gitfile_trust", "gitfile_trust.jsonl"),
        ("golden_gitfile_trust_stdfs", "gitfile_trust.jsonl"),
        ("golden_gitfile_trust_gated", "gitfile_trust_python.jsonl"),
        (
            "golden_gitfile_trust_gated_stdfs",
            "gitfile_trust_python.jsonl",
        ),
    ] {
        common::parity::record(
            test,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["POSIX trees; the unit declines the Windows flavour".to_owned()],
        );
    }
}
