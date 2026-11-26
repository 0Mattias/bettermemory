//! `response_drift.jsonl` has no Windows case: a row's anchors are compared
//! through the host's `Path` (`_normalize_for_compare`), whose Windows
//! flavour (`ntpath`) the port does not reproduce, and declines
//! (`CompareError::NotPorted`, the crate's stated Windows gap, ENG-124).
//! `golden_response_drift.rs` and the kills and robustness cases that look
//! a row with an anchor up run on POSIX only; here the gap is recorded and
//! the decline checked, with what still holds: a row whose anchors are
//! all given answers as on POSIX.

#![cfg(not(unix))]

mod common;
mod response_drift_script;

use bettermemory_core::attestation_paths::Record;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::response_drift::{DriftPass, LookupError, Memo, Row};
use bettermemory_core::verify::anchors::AnchorError;
use bettermemory_core::verify::status::ResolvedCommitDrift;
use bettermemory_core::verify_candidates::scan::CompareError;
use response_drift_script::{cps, shared, Admit, Reads, Resolver, SINCE};
use serde_json::json;

#[test]
fn the_response_drift_fixture_has_no_case_here() {
    common::parity::record(
        "golden_response_drift",
        "response_drift.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the anchors compare through ntpath, which the port declines".to_owned()],
    );
}

#[test]
fn a_lookup_with_an_anchor_declines_under_the_windows_flavour() {
    let cell = shared(&json!({
        "files": "H", "git": "H", "moved": false, "head": "H", "dates": [2, 3],
        "sig": "s1", "attrs": 1, "chains": {}, "walk": 1, "held": true,
        "deny": [], "now": 100.0, "raise": "", "error": "RuntimeError"
    }));
    let mut reads = Reads(cell.clone());
    let mut resolver = Resolver(cell.clone());
    let memo = Memo::new();
    let mut scope = Scope {
        root: Some((cps("outside"), true)),
        ..Scope::default()
    };
    let mut pass = DriftPass::open(
        &mut reads,
        &mut scope,
        cps("/r/s"),
        false,
        Admit(cell.clone()),
        &memo,
    )
    .unwrap();
    let since = DateTime::from_isoformat(&cps(SINCE)).unwrap();
    let anchored = Record {
        verified_paths: vec![cps("src/a.py")],
        ..Record::default()
    };
    let unanchored = Record::default();
    let row = |paths| Row {
        id: "m1",
        origin_repo: true,
        body: &[],
        paths,
        claims: &[],
        verified_head: None,
    };
    let given = [cps("src/a.py")];
    // The anchors are derived before `anchor_paths` replaces them, so a
    // verified path declines even with anchors given.
    for anchor_paths in [None, Some(&given[..])] {
        let got = pass.lookup(
            &mut reads,
            &mut scope,
            &row(&anchored),
            Some(&since),
            true,
            anchor_paths,
            &mut resolver,
        );
        assert_eq!(
            got,
            Err(LookupError::Anchors(AnchorError::Compare(
                CompareError::NotPorted
            )))
        );
    }
    assert_eq!(pass.unstored(), 0, "nothing is kept");
    // No anchor derived and the anchors given: nothing is compared, and the
    // row resolves as on POSIX.
    let got = pass.lookup(
        &mut reads,
        &mut scope,
        &row(&unanchored),
        Some(&since),
        true,
        Some(&given),
        &mut resolver,
    );
    assert_eq!(got, Ok((true, Some(Ok(ResolvedCommitDrift::new(1))))));
}
