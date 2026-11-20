//! `gitfile_discovery.jsonl` and `gitfile_discovery_python.jsonl` have no
//! Windows case: their trees hold links, FIFOs and modes, and
//! `gitfile_rule::discovery` declines the Windows flavour whole. Here the
//! gap is recorded. (`gitfile_discovery_kills.rs` and
//! `gitfile_discovery_robustness.rs` run everywhere.)

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_discovery_fixtures_have_no_case_here() {
    for (test, file) in [
        ("golden_gitfile_discovery", "gitfile_discovery.jsonl"),
        ("golden_gitfile_discovery_stdfs", "gitfile_discovery.jsonl"),
        (
            "golden_gitfile_discovery_gated",
            "gitfile_discovery_python.jsonl",
        ),
        (
            "golden_gitfile_discovery_gated_stdfs",
            "gitfile_discovery_python.jsonl",
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
