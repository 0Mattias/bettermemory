//! `store_membership.jsonl` has no Windows case: its generator records the
//! state `gen_store_open.state` reads (modes, the binding's resolved POSIX
//! path), as `store_records_e.jsonl`'s does. `golden_store_membership.rs`,
//! `store_membership_kills.rs` and `store_membership_robustness.rs` are
//! POSIX only; here the gap is recorded, so the parity table names it.

#![cfg(not(unix))]

mod common;

#[test]
fn the_membership_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_membership",
        "store_membership.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
