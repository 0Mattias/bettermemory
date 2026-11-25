//! `store_open_upkeep.jsonl` and its Python-gated sibling have no Windows
//! case: their generator reads modes, writes the key ring's binding as a
//! resolved POSIX path, takes POSIX `flock`s and makes the keys directory
//! 0500. `golden_store_open_upkeep.rs`, `store_open_upkeep_kills.rs` and
//! `store_open_upkeep_robustness.rs` are POSIX only; here the gap is
//! recorded, so the parity table names it instead of showing nothing.

#![cfg(not(unix))]

mod common;

#[test]
fn the_upkeep_fixtures_have_no_case_here() {
    for (test, file) in [
        (
            "every_case_upkeeps_as_the_reference",
            "store_open_upkeep.jsonl",
        ),
        (
            "every_nested_case_raises_as_the_reference",
            "store_open_upkeep_python.jsonl",
        ),
    ] {
        common::parity::record(
            test,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
        );
    }
}
