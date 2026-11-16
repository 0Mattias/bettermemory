//! `store_provenance.jsonl` has no Windows case: its generator runs the
//! reference's create and read-only open, which write and check the key
//! ring's modes and its binding. `golden_store_provenance.rs`,
//! `store_provenance_cross.rs` and `store_provenance_read_only.rs` are
//! POSIX only; here the gap is recorded, so the parity table names it.
//! The readers themselves run on Windows in
//! `store_provenance_robustness.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_provenance_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_provenance",
        "store_provenance.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture's stores are made and opened by the POSIX reference; Windows has no case"
                .to_owned(),
        ],
    );
}
