//! `checkout_token_write.jsonl` and `checkout_token_write_posix.jsonl`
//! have no Windows case. The first runs `write` over a scripted OS whose
//! logged calls spell paths as the host's `Path` does (backslashes on
//! Windows) and whose errno names are the host's; the second writes real
//! trees with modes and reads the C library's error texts.
//! `golden_checkout_token_write.rs`, `golden_checkout_token_write_posix.rs`
//! and `checkout_token_write_kills.rs` run on POSIX only; here the gaps
//! are recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_scripted_checkout_token_write_fixture_has_no_case_here() {
    common::parity::record(
        "golden_checkout_token_write",
        "checkout_token_write.jsonl",
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

#[test]
fn the_posix_checkout_token_write_fixture_has_no_case_here() {
    common::parity::record(
        "golden_checkout_token_write_posix",
        "checkout_token_write_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture writes trees with modes and C library texts; Windows has no case"
                .to_owned(),
        ],
    );
}
