//! `store_docfreq_store.jsonl` has no Windows case: its stores are made
//! and opened as `store_tx.jsonl`'s are, whose create and open write and
//! check the key ring's modes and binding. `golden_store_docfreq_store.rs`
//! and `store_docfreq_store_kills.rs` are POSIX only; here the gap is
//! recorded, so the parity table names it.
//! `store_docfreq_store_robustness.rs` runs everywhere.

#![cfg(not(unix))]

mod common;

#[test]
fn the_store_docfreq_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_docfreq_store",
        "store_docfreq_store.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture's stores are created and opened with POSIX modes; Windows has no case"
                .to_owned(),
        ],
    );
}
