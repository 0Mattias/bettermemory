//! `store_reopen.jsonl` and its Python-gated sibling have no Windows case:
//! their generator reads modes and writes the key ring's binding as a
//! resolved POSIX path. `golden_store_reopen.rs`, `store_reopen_kills.rs`
//! and `store_reopen_robustness.rs` are POSIX only; here the gap is
//! recorded, so the parity table names it instead of showing nothing. On
//! Windows the reopen reads the key's state as the owner's open does: the
//! binding's path through `store/key_state/paths/windows.rs`
//! (`fsdata::realpath::nt_realpath` over the host's calls), not through
//! `fsdata`'s Windows flavour, which declines a realpath (Linear ENG-124).

#![cfg(not(unix))]

mod common;

#[test]
fn the_reopen_fixtures_have_no_case_here() {
    for (test, file) in [
        ("every_reopen_opens_as_the_reference", "store_reopen.jsonl"),
        (
            "every_nested_reopen_raises_as_the_reference",
            "store_reopen_python.jsonl",
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
