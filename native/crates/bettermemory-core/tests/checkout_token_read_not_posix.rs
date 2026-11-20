//! `checkout_token_read_posix.jsonl` has no Windows case: its generator
//! makes links and a FIFO, which Windows answers otherwise.
//! `golden_checkout_token_read_posix.rs` runs it on POSIX only; here the
//! gap is recorded. The scripted reads of `checkout_token_read.jsonl`,
//! the `win32` platform among them, run everywhere.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_checkout_token_read_fixture_has_no_case_here() {
    common::parity::record(
        "golden_checkout_token_read_posix",
        "checkout_token_read_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links and a FIFO; Windows has no case".to_owned()],
    );
}
