//! `store_record_event_posix.jsonl` has no Windows case: its cases open the
//! store `store_create.json` pins, whose key ring is bound by its resolved
//! POSIX path under `flock`, as `store_tx.jsonl`'s are.
//! `golden_store_record_event.rs` is POSIX only; here the gap is recorded,
//! so the parity table names it. The pure leaves (`busy`, `caught`,
//! `time_left`, `event_payload`, `named_payload`) are checked everywhere
//! in `store_record_event_kills.rs` and `store_record_event_robustness.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_event_write_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_record_event",
        "store_record_event_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture opens stores bound by POSIX paths; Windows has no case".to_owned()],
    );
}
