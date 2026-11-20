//! `checkout_token_read_posix.jsonl` and `checkout_token_read.jsonl` have
//! no Windows case. The first's generator makes links and a FIFO, which
//! Windows answers otherwise. The second runs the reads over a scripted
//! OS, but its logged open spells the path as the host's `Path` does
//! (backslashes on Windows) and an errno's name is the host's
//! (`WSAELOOP` for ELOOP), so its bytes follow the OS family.
//! `golden_checkout_token_read_posix.rs`, `golden_checkout_token_read.rs`
//! and `checkout_token_read_kills.rs` run on POSIX only; here the gaps are
//! recorded. `checkout_token_read_robustness.rs` runs everywhere.

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

#[test]
fn the_scripted_checkout_token_read_fixture_has_no_case_here() {
    common::parity::record(
        "golden_checkout_token_read",
        "checkout_token_read.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the scripted calls spell paths and errno names as the host does; \
           Windows has no case"
                .to_owned(),
        ],
    );
}
