//! `store_tx.jsonl` has no Windows case: its generator records the state
//! `gen_store_open.state` reads (modes, the binding's resolved POSIX
//! path), as `store_write.jsonl`'s does. `golden_store_tx.rs` and
//! `store_tx_kills.rs` are POSIX only; here the gap is recorded, so the
//! parity table names it. Transactions themselves run on Windows in
//! `store_tx_robustness.rs` and `store_tx_cross.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_transaction_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_tx",
        "store_tx.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
