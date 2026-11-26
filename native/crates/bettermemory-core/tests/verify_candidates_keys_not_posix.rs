//! `verify_candidates_keys.jsonl`, `verify_candidates_keys_unicode.jsonl`
//! and `verify_candidates_keys_python.jsonl` have no Windows case: the
//! reference resolves through `ntpath` there (`os.name == "nt"`:
//! `realpath` by one handle, `normcase` folding case, `os.altsep` folded),
//! which this unit does not port, and every function of
//! `verify_candidates::keys` that reads a path declines (where
//! `is_multi_segment_routelike`'s text alone does not answer). Here the
//! gap is recorded and the decline checked.

#![cfg(not(unix))]

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs};
use bettermemory_core::verify_candidates::keys::{
    claim_key, home_ignores_case, is_multi_segment_routelike, is_under_home, path_exists,
    ClaimKeys, Raised, RECURSION_LIMIT,
};
use bettermemory_core::verify_candidates::scan::UserHome;

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

#[test]
fn the_windows_flavour_declines() {
    let fs = MemFs::new(Flavour::Windows);
    let mut none = |_: &[u32]| UserHome::Unknown;
    let home = cps("C:\\Users\\u");
    let p = cps("C:\\x\\y");
    assert_eq!(
        claim_key(&fs, &p, Some(&home), &mut none),
        Err(Raised::NotPorted)
    );
    assert_eq!(claim_key(&fs, &[], Some(&home), &mut none), Ok(vec![]));
    assert_eq!(
        ClaimKeys::new(RECURSION_LIMIT).key(&fs, &p, Some(&home), &mut none),
        Err(Raised::NotPorted)
    );
    assert_eq!(
        is_under_home(&fs, &p, Some(&home), &mut none),
        Err(Raised::NotPorted)
    );
    assert_eq!(
        path_exists(&fs, &p, Some(&home), &mut none),
        Err(Raised::NotPorted)
    );
    assert_eq!(home_ignores_case(&fs, &home), Err(Raised::NotPorted));
    assert_eq!(home_ignores_case(&fs, &[]), Ok(false));
    // The text alone answers before the home is read, as the reference
    // answers on Windows; past that, the decline.
    assert_eq!(
        is_multi_segment_routelike(&fs, &cps("C:\\x\\y"), Some(&home), &mut none),
        Ok(false)
    );
    assert_eq!(
        is_multi_segment_routelike(&fs, &cps("/api/v1/x"), Some(&home), &mut none),
        Err(Raised::NotPorted)
    );
}

#[test]
fn the_keys_fixtures_have_no_case_here() {
    for (test, file) in [
        (
            "verify_candidates_keys_every_case",
            "verify_candidates_keys.jsonl",
        ),
        (
            "verify_candidates_keys_unicode_every_case",
            "verify_candidates_keys_unicode.jsonl",
        ),
        (
            "verify_candidates_keys_python_every_case",
            "verify_candidates_keys_python.jsonl",
        ),
    ] {
        common::parity::record(
            test,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["ntpath's realpath and normcase are not ported; Windows has no case".to_owned()],
        );
    }
}
