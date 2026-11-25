//! `store_open_restore.jsonl` and its Python-gated sibling have no Windows
//! case: their generator reads modes, writes the key ring's binding as a
//! resolved POSIX path, takes POSIX `flock`s and makes the keys directory
//! 0500. `golden_store_open_restore.rs` and `store_open_restore_kills.rs`
//! are POSIX only; here the gap is recorded, so the parity table names it
//! instead of showing nothing.

#![cfg(not(unix))]

mod common;

#[test]
fn the_restore_fixtures_have_no_case_here() {
    for (test, file) in [
        (
            "every_case_settles_as_the_reference",
            "store_open_restore.jsonl",
        ),
        (
            "every_python_case_settles_as_the_reference",
            "store_open_restore_python.jsonl",
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
