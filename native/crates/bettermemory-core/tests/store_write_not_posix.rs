//! `store_write.jsonl` has no Windows case: its generator records the
//! state `gen_store_open.state` reads (modes, the binding's resolved
//! POSIX path). `golden_store_write.rs` and `store_write_kills.rs` are
//! POSIX only; here the gap is recorded, so the parity table names it.
//! The writes themselves run on Windows in `store_write_robustness.rs`
//! and `store_write_cross.rs`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_write_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_write",
        "store_write.jsonl",
        "os",
        0,
        &["every sequence of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
