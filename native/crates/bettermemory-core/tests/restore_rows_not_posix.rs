//! `restore_rows.jsonl` and its Python-gated sibling have no Windows case:
//! their generator takes POSIX `flock`s, makes the keys directory 0500 and
//! opens the created store, whose fixtures are POSIX only.
//! `golden_restore_rows.rs`, `restore_rows_kills.rs` and
//! `restore_rows_robustness.rs` are POSIX only; here the gap is recorded,
//! so the parity table names it instead of showing nothing.

#![cfg(not(unix))]

mod common;

#[test]
fn the_restore_row_fixtures_have_no_case_here() {
    for (test, file) in [
        ("every_step_settles_as_the_reference", "restore_rows.jsonl"),
        (
            "every_nested_step_raises_as_the_reference",
            "restore_rows_python.jsonl",
        ),
    ] {
        common::parity::record(
            test,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["the fixture takes POSIX locks and modes; Windows has no case".to_owned()],
        );
    }
}
