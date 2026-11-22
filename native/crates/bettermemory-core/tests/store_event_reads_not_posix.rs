//! `store_event_reads_naive.jsonl` has no Windows case: its generator
//! sets `TZ` through `time.tzset`, which Windows lacks. The naive path is
//! checked here by `store_event_reads_kills.rs`, which runs everywhere.

#![cfg(not(unix))]

mod common;

#[test]
fn the_naive_event_reads_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_event_reads_naive",
        "store_event_reads_naive.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the generator sets TZ with time.tzset; Windows has no case".to_owned()],
    );
}
