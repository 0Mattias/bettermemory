//! `log_replaced_store.jsonl` has no Windows case: it is made on the store
//! `store_create.json` pins and takes `store_tx.jsonl`'s gates, POSIX only
//! among them. `golden_log_replaced_store.rs` is POSIX only; here the gap
//! is recorded, so the parity table names it.

#![cfg(not(unix))]

mod common;

#[test]
fn the_append_fixture_has_no_case_here() {
    common::parity::record(
        "golden_log_replaced_store",
        "log_replaced_store.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture is made on the created store, POSIX only as store_tx.jsonl".to_owned()],
    );
}
