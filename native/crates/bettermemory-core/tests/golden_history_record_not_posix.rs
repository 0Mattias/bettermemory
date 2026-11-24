//! `history_record.jsonl` has no case off POSIX: its stores are made by
//! the reference's `Store.create` and opened by `Store.open`, which write
//! and check the key ring's modes. `golden_history_record.rs` runs on
//! unix only; here the gap is recorded. `history_record_kills.rs` and
//! `history_record_robustness.rs` run everywhere.

#![cfg(not(unix))]

mod common;

#[test]
fn the_history_record_fixture_has_no_case_here() {
    common::parity::record(
        "golden_history_record",
        "history_record.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the stores are made and opened with POSIX key modes; Windows has no case".to_owned()],
    );
}
