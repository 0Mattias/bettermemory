//! `githead_signature.jsonl` has no case off POSIX: its generator makes
//! symbolic links and modes and reads owners. `golden_githead_signature.rs`
//! runs it on unix only; here the gap is recorded. Under the Windows
//! flavour `fsdata` does not port `realpath` (ENG-124), so `entry_above`
//! declines (None) and `signature` answers the undetermined marker where
//! the reference on Windows resolves the path: with `owner` its
//! `_check_environment` declines there too, without it `_located` reads.

#![cfg(not(unix))]

mod common;

#[test]
fn the_githead_signature_fixture_has_no_case_here() {
    common::parity::record(
        "golden_githead_signature",
        "githead_signature.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the fixture makes links and modes and reads owners; Windows has no case; \
           the Windows flavour's realpath is not ported (ENG-124)"
                .to_owned(),
        ],
    );
}
