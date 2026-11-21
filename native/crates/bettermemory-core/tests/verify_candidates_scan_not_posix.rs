//! `verify_candidates_scan.jsonl` and `verify_candidates_scan_unicode.jsonl`
//! have no Windows case: the reference expands `~` and spells the
//! comparison form through the host's `Path`, whose Windows flavour parses
//! drives, backslashes and UNC roots, and the port reproduces the POSIX
//! flavour only, and declines here. `golden_verify_candidates_scan.rs` and
//! the kills test run on POSIX only; here the gap is recorded and the
//! decline checked. The robustness test runs everywhere.

#![cfg(not(unix))]

mod common;

use bettermemory_core::verify_candidates::scan::{
    extract_candidates, normalize_for_compare, scan_candidates, CompareError, ScanError, UserHome,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

#[test]
fn the_windows_flavour_declines() {
    let mut none = |_: &[u32]| UserHome::Unknown;
    let home = cps("C:\\Users\\u");
    assert_eq!(
        normalize_for_compare(&cps("/a//b"), Some(&home), &mut none),
        Err(CompareError::NotPorted)
    );
    assert_eq!(
        normalize_for_compare(&[], Some(&home), &mut none),
        Ok(vec![])
    );
    assert_eq!(
        scan_candidates(&cps("see /etc/hosts"), Some(&home), &mut none),
        Err(ScanError::NotPorted)
    );
    assert_eq!(
        extract_candidates(&cps("see /etc/hosts"), Some(&home), &mut none),
        Err(ScanError::NotPorted)
    );
    assert_eq!(extract_candidates(&[], Some(&home), &mut none), Ok(vec![]));
}

#[test]
fn the_scan_fixtures_have_no_case_here() {
    for (test, file) in [
        (
            "verify_candidates_scan_every_case",
            "verify_candidates_scan.jsonl",
        ),
        (
            "verify_candidates_scan_unicode_every_case",
            "verify_candidates_scan_unicode.jsonl",
        ),
    ] {
        common::parity::record(
            test,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["the Windows flavour of Path is not ported; Windows has no case".to_owned()],
        );
    }
}
