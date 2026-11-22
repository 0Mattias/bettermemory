//! `verify_anchoring_attest_posix.jsonl` has no Windows case: its
//! generator makes links in a scratch tree. Here the gap is recorded, with
//! the crate's stated Windows gap (ENG-124): `fsdata::realpath` declines
//! under the Windows flavour, so `resolves_under` declines there
//! (`Raised::NotPorted`) where the reference resolves with `ntpath`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_attestation_check_fixture_has_no_case_here() {
    common::parity::record(
        "golden_verify_anchoring_attest_posix",
        "verify_anchoring_attest_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links; Windows has no case, and realpath is not ported under the Windows flavour".to_owned()],
    );
}
