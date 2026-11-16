//! `store_records_c.jsonl` has no Windows case: its generator records the
//! state `gen_store_open.state` reads (modes, the binding's resolved POSIX
//! path), as `store_records.jsonl`'s does. `golden_store_records_c.rs` and
//! `store_records_c_kills.rs` are POSIX only; here the gap is recorded, so
//! the parity table names it. `mark_verified` and `record_corroboration` themselves run on
//! Windows in `store_records_c_robustness.rs` and `store_records_c_cross.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_verification_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_records_c",
        "store_records_c.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
