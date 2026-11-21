//! `origin_history_located.jsonl` and `origin_history_located_modes.jsonl`
//! have no case off POSIX: the reference's discovery reads POSIX paths and
//! owners there, and the port's discovery declines under the Windows
//! flavour (`realpath` is not ported there, the crate's stated gap).
//! `golden_origin_history_located.rs` runs them on unix only; here the gap
//! is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_origin_history_located_fixtures_have_no_case_here() {
    for (name, file) in [
        (
            "golden_origin_history_located_plain",
            "origin_history_located.jsonl",
        ),
        (
            "golden_origin_history_located_modes",
            "origin_history_located_modes.jsonl",
        ),
    ] {
        common::parity::record(
            name,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["discovery declines under the Windows flavour; Windows has no case".to_owned()],
        );
    }
}
