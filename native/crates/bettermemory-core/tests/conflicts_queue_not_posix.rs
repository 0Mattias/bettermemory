//! `conflicts_queue_store.jsonl` has no Windows case: it runs the queue
//! over the store `store_rows_written.jsonl`'s cases open, whose state
//! holds modes and the binding's resolved POSIX path, and its generator is
//! POSIX only. `golden_conflicts_queue_store.rs` is POSIX only; here the
//! gap is recorded, so the parity table names it. The queue itself runs
//! here in `golden_conflicts_queue.rs` (the stub store), and over a real
//! store in `conflicts_queue_robustness.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_queue_store_fixture_has_no_case_here() {
    common::parity::record(
        "golden_conflicts_queue_store",
        "conflicts_queue_store.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture's stores are POSIX only; Windows has no case".to_owned()],
    );
}
