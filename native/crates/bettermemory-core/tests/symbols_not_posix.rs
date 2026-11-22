//! `symbols_platform.jsonl` has no Windows case: its trees hold links, a
//! FIFO and a mode-000 file, and the Windows flavour of `realpath`, which
//! `check_symbol_citations` resolves every root and module through, is
//! not ported (it declines). Here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_symbols_fixture_has_no_case_here() {
    common::parity::record(
        "golden_symbols_platform",
        "symbols_platform.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["POSIX trees (links, a FIFO, modes); Windows realpath is not ported".to_owned()],
    );
}
