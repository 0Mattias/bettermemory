//! `verify_anchoring_detect.jsonl` has no Windows case: the reference's
//! candidate scan spells paths through the host's `Path` and its
//! discovery resolves with `os.path.realpath`. Here the gap is recorded,
//! with the crate's stated Windows gap (ENG-124): the candidate scan
//! declines off Unix (`ScanError::NotPorted`), and `holds_commit`
//! declines under the Windows flavour where `fsdata::realpath` is not
//! ported, where the reference reads the store with `ntpath`.

#![cfg(not(unix))]

mod common;

#[test]
fn the_drift_reader_fixture_has_no_case_here() {
    common::parity::record(
        "golden_verify_anchoring_detect",
        "verify_anchoring_detect.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the candidate scan and realpath are not ported under the Windows flavour".to_owned()],
    );
}
