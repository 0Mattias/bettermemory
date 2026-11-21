//! `githead_discovery.jsonl` has no case off POSIX: its generator makes
//! symbolic links, FIFOs and modes, and the reference's discovery
//! declines off POSIX (`_check_environment`), as the port's does for the
//! Windows flavour. `golden_githead_discovery.rs` runs it on unix only;
//! here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_githead_discovery_fixture_has_no_case_here() {
    common::parity::record(
        "golden_githead_discovery",
        "githead_discovery.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links, FIFOs and modes; Windows has no case".to_owned()],
    );
}
