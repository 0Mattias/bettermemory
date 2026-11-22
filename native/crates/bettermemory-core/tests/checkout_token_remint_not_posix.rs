//! `checkout_token_remint.jsonl` and `checkout_token_remint_posix.jsonl`
//! have no Windows case. The first runs `remint` over a scripted OS whose
//! logged calls spell paths as the host's `Path` does (backslashes on
//! Windows) and whose errno names are the host's; the second writes real
//! trees and reads the C library's error texts. Under the Windows flavour
//! a relative git directory declines (`Ctx::at`, ENG-124), where the
//! reference on Windows answers. `golden_checkout_token_remint.rs`,
//! `golden_checkout_token_remint_posix.rs` and
//! `checkout_token_remint_kills.rs` run on POSIX only; here the gaps are
//! recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_scripted_checkout_token_remint_fixture_has_no_case_here() {
    common::parity::record(
        "golden_checkout_token_remint",
        "checkout_token_remint.jsonl",
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
fn the_posix_checkout_token_remint_fixture_has_no_case_here() {
    common::parity::record(
        "golden_checkout_token_remint_posix",
        "checkout_token_remint_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture writes real trees and C library texts; Windows has no case".to_owned()],
    );
}
