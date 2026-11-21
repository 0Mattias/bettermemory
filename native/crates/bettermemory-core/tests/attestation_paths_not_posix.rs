//! `attestation_paths_posix.jsonl` has no Windows case: its generator
//! makes links and a mode-000 folder, which Windows answers otherwise.
//! `golden_attestation_paths_posix.rs` runs on POSIX only; here the gap is
//! recorded, with the crate's stated Windows gap: on a Windows host
//! `ntpath.normcase` asks `LCMapStringEx`, which is not ported, so
//! `Host::nt_normcase` declines by default and `target_path` with a drive
//! root declines through it.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_attestation_paths_fixture_has_no_case_here() {
    common::parity::record(
        "golden_attestation_paths_posix",
        "attestation_paths_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links and a mode-000 folder; Windows has no case".to_owned()],
    );
}
