//! `scope_match_posix.jsonl`, `scope_match_posix_python.jsonl`,
//! `scope_match_unicode.jsonl` and `scope_match_volume.jsonl` have no
//! Windows case: the reference folds
//! `os.altsep` (`/`) into `os.sep` (`\`) there and reads the home through
//! `WindowsPath`, which this unit does not port, so every function of
//! `scope_match` that reads a separator or the home declines.
//! `golden_scope_match_posix.rs`, `scope_match_kills.rs` and
//! `scope_match_robustness.rs` run on POSIX only; here the gap is recorded
//! and the decline checked. `golden_scope_match.rs` (the leaves that read
//! neither) runs everywhere.

#![cfg(not(unix))]

mod common;

use bettermemory_core::fsdata::StdFs;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::scope_match::{
    boundary_after, declared_root_covers, declared_root_spans, drop_ancestor_roots,
    find_root_occurrence, folded_home, home_alias, roots_of, trim_evidence, volume_probe,
    ScopeError, EVIDENCE_PADDING,
};

fn cps(t: &str) -> PyStr {
    t.chars().map(u32::from).collect()
}

#[test]
fn the_windows_flavour_declines() {
    let home = cps("C:\\Users\\u");
    let home = Some(home.as_slice());
    let fs = StdFs;
    let mut probe = volume_probe(&fs);
    let scope = cps("projects:a");
    let root = cps("C:\\w\\a");
    assert_eq!(folded_home(home), Err(ScopeError::NotPorted));
    assert_eq!(
        roots_of(
            &[(scope.clone(), vec![(root.clone(), 1)])],
            home,
            &mut probe
        ),
        Err(ScopeError::NotPorted)
    );
    assert_eq!(
        drop_ancestor_roots(vec![(scope.clone(), root.clone())]),
        Err(ScopeError::NotPorted)
    );
    assert_eq!(
        home_alias(&root, home, &mut probe),
        Err(ScopeError::NotPorted)
    );
    assert_eq!(
        declared_root_spans(
            &root,
            std::slice::from_ref(&scope),
            &[(scope.clone(), root.clone())],
            home,
            &mut probe
        ),
        Err(ScopeError::NotPorted)
    );
    assert_eq!(
        declared_root_covers(&root, 0, &root, &[], false, home, &mut probe),
        Err(ScopeError::NotPorted)
    );
    // The home probe itself declines on a non-empty path.
    assert_eq!(probe(&root), Err(ScopeError::NotPorted));
    // The leaves that read neither answer as on every platform.
    assert!(boundary_after(&cps("C:\\w\\a x"), 6));
    assert_eq!(
        find_root_occurrence(&cps("x C:\\w\\a"), &root),
        Some((2, 8))
    );
    assert_eq!(
        trim_evidence(&cps("a\nb"), 0, 3, EVIDENCE_PADDING),
        cps("a b")
    );
}

#[test]
fn the_scope_match_fixtures_have_no_case_here() {
    for (test, file) in [
        ("scope_match_posix_every_case", "scope_match_posix.jsonl"),
        (
            "scope_match_posix_python_every_case",
            "scope_match_posix_python.jsonl",
        ),
        (
            "scope_match_unicode_every_case",
            "scope_match_unicode.jsonl",
        ),
        ("scope_match_volume_every_case", "scope_match_volume.jsonl"),
    ] {
        common::parity::record(
            test,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &[
                "os.sep, os.altsep and WindowsPath's home are not ported; Windows has no case"
                    .to_owned(),
            ],
        );
    }
}
