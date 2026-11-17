//! `store_records_d.jsonl` has no Windows case: its generator records the
//! state `gen_store_open.state` reads (modes, the binding's resolved POSIX
//! path), as `store_records.jsonl`'s does. `golden_store_records_d.rs` and
//! `store_records_d_kills.rs` are POSIX only; here the gap is recorded, so
//! the parity table names it. `put_tombstone`, `restore` and
//! `restore_trimmed` themselves run on Windows in `store_records_d_robustness.rs` and `store_records_d_cross.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_restore_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_records_d",
        "store_records_d.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
