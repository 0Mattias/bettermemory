//! `store_schema_posix.jsonl` has no Windows case: its generator makes a
//! store file read-only by its mode and spells a folder's path as the
//! host's `Path` does. `golden_store_schema_posix.rs` runs on POSIX only;
//! here the gap is recorded. `golden_store_schema.rs` runs everywhere.
//!
//! What is not ported for Windows: `refuse_unmigrated_v8` is built for
//! POSIX only (`#[cfg(unix)]`); on Windows the reference's `WindowsPath`
//! takes the parent with its drive and backslashes (`C:\s` for
//! `C:\s\memory.sqlite`) and tests the file through Windows' `stat`.
//! A read-only store there is one whose read-only attribute is set, which
//! SQLite reports as read-only as on POSIX; no case of it is run here.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_store_schema_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_schema_posix",
        "store_schema_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture reads modes and POSIX paths; Windows has no case".to_owned(),
            "refuse_unmigrated_v8 is not built for WindowsPath".to_owned(),
            "a store with the read-only attribute is not run".to_owned(),
        ],
    );
}
