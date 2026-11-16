//! `store_rows_written.jsonl` has no Windows case: its generator records
//! the state `gen_store_open.state` reads (modes, the binding's resolved
//! POSIX path), as `store_tx.jsonl`'s does. `golden_store_rows_written.rs`
//! and `store_rows_written_kills.rs` are POSIX only; here the gap is
//! recorded, so the parity table names it. The writers themselves run on
//! Windows in `store_rows_written_robustness.rs` and
//! `store_rows_written_cross.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_row_writers_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_rows_written",
        "store_rows_written.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
