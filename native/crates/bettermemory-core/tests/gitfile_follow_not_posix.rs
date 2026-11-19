//! `gitfile_follow.jsonl` and `gitfile_follow_python.jsonl` have no
//! Windows case: their trees hold links and modes, and
//! `gitfile_rule::follow` declines the Windows flavour whole. Here the gap
//! is recorded. (`gitfile_pin_text.jsonl` runs everywhere.)

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_follow_fixtures_have_no_case_here() {
    for (test, file) in [
        ("golden_gitfile_follow", "gitfile_follow.jsonl"),
        ("golden_gitfile_follow_stdfs", "gitfile_follow.jsonl"),
        ("golden_gitfile_follow_gated", "gitfile_follow_python.jsonl"),
        (
            "golden_gitfile_follow_gated_stdfs",
            "gitfile_follow_python.jsonl",
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
