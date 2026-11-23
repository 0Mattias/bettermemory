//! `verify_count.jsonl` has no Windows case: the anchors are deduplicated
//! on `_normalize_for_compare`, which spells a path through the host's
//! `Path`, and the scan's Windows flavour is not ported, so a non-empty
//! anchor declines. `golden_verify_count.rs`, `verify_count_kills.rs` and
//! `verify_count_robustness.rs` run on POSIX only; here the gap is
//! recorded and the decline checked.

#![cfg(not(unix))]

mod common;

use bettermemory_core::attestation_paths::{Kind, Paths};
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::anchors::{commit_drift_anchor_paths, AnchorError};
use bettermemory_core::verify_candidates::scan::{CompareError, UserHome};

#[test]
fn the_verify_count_fixture_has_no_case_here() {
    common::parity::record(
        "golden_verify_count",
        "verify_count.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the anchors' compare form is the host's Path; Windows declines".to_owned()],
    );
}

#[test]
fn an_anchor_declines_and_an_empty_record_answers() {
    let mut none = |_: &[u32]| UserHome::Unknown;
    let empty = Paths {
        kind: Kind::Reference,
        items: Vec::new(),
    };
    assert_eq!(
        commit_drift_anchor_paths(&[], &empty, None, &mut none),
        Ok(Vec::new())
    );
    let one = Paths {
        kind: Kind::Literal,
        items: vec![cps("src/a.py")],
    };
    assert_eq!(
        commit_drift_anchor_paths(&[], &one, None, &mut none),
        Err(AnchorError::Compare(CompareError::NotPorted))
    );
}
