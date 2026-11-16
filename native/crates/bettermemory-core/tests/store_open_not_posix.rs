//! `store_open.jsonl` has no Windows case: its generator reads modes,
//! writes the key ring's binding as a resolved POSIX path and takes POSIX
//! `flock`s. `golden_store_open.rs`, `store_open_kills.rs` and the
//! unix-only parts of `store_open_robustness.rs` are POSIX only; here the
//! gap is recorded, so the parity table names it instead of showing
//! nothing. The writing open itself runs on Windows in
//! `store_open_robustness.rs` and `store_open_cross.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_writing_open_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_open",
        "store_open.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
