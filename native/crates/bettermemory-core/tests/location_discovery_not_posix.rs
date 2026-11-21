//! `location_discovery.jsonl` has no case off POSIX: its generator makes
//! links and spells paths with `/`, and fsdata's Windows flavour does not
//! port `realpath`, so the port declines there where the reference on
//! Windows answers (the crate's stated Windows gap).
//! `golden_location_discovery.rs` runs on POSIX only; here the gap is
//! recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_location_discovery_fixture_has_no_case_here() {
    common::parity::record(
        "golden_location_discovery",
        "location_discovery.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links and spells paths with /; the Windows flavour declines (realpath not ported)".to_owned()],
    );
}
