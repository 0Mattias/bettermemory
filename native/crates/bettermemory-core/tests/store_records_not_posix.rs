//! `store_records.jsonl` has no Windows case: its generator records the
//! state `gen_store_open.state` reads (modes, the binding's resolved POSIX
//! path), as `store_tx.jsonl`'s does. `golden_store_records.rs` and
//! `store_records_kills.rs` are POSIX only; here the gap is recorded, so
//! the parity table names it. The writers themselves run on Windows in
//! `store_records_robustness.rs` and `store_records_cross.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_record_writer_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_records",
        "store_records.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
