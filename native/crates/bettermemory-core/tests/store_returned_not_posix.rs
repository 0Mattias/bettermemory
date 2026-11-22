//! `store_returned_posix.jsonl` has no Windows case: its generator records
//! the state `gen_store_open.state` reads (modes, the binding's resolved
//! POSIX path), as `store_records_e.jsonl`'s does.
//! `golden_store_returned_posix.rs` is POSIX only; here the gap is
//! recorded, so the parity table names it. `store_returned.jsonl`'s
//! cases and the kills and robustness tests run here as well. The
//! backfill's relink loads the key ring and reads provenance, which the
//! crate's Windows flavour answers as on POSIX; a path it must resolve
//! declines there (the crate's stated Windows gap).

#![cfg(not(unix))]

mod common;

#[test]
fn the_backfill_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_returned_posix",
        "store_returned_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture reads modes and POSIX binding paths; Windows has no case".to_owned()],
    );
}
