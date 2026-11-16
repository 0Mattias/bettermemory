//! `store_bulk.jsonl` has no Windows case: its generator records the state
//! `gen_store_open.state` reads (modes, the binding's resolved POSIX path),
//! as `store_tx.jsonl`'s does. `golden_store_bulk.rs` and
//! `store_bulk_kills.rs` are POSIX only; here the gap is recorded, so the
//! parity table names it. Chunked runs themselves run on Windows in
//! `store_bulk_robustness.rs` and `store_bulk_cross.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_chunked_run_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_bulk",
        "store_bulk.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
