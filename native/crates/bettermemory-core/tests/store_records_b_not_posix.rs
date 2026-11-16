//! `store_records_b.jsonl` has no Windows case: its generator records the
//! state `gen_store_open.state` reads (modes, the binding's resolved POSIX
//! path), as `store_records.jsonl`'s does. `golden_store_records_b.rs` and
//! `store_records_b_kills.rs` are POSIX only; here the gap is recorded, so
//! the parity table names it. `write` and `update` themselves run on
//! Windows in `store_records_b_robustness.rs` and `store_records_b_cross.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_write_and_update_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_records_b",
        "store_records_b.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
