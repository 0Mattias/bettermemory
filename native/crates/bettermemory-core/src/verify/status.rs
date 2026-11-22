//! `verify.py`'s status leaves (lines at v9): `CommitDriftStatus`
//! 245-296, `_drift_recommendation` 299-308, `_claim_drift_recommendation`
//! 311-322, `ResolvedCommitDrift` 436-452, `_index_paths` 579-592,
//! `_unreadable` 674-685, `_gated_claims` 688-702, `commit_drift_status`
//! 1536-1597 and `staleness_verdict_from_report` 1803-1817.

use std::collections::BTreeSet;

use crate::claim_drift::Index;
use crate::claims::{control_character, lone_surrogate, Claim};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::cps;
use crate::verdict_signals::compute_staleness_verdict;
use crate::verify_anchoring::report::PathDriftReport;
use crate::verify_basis::BASIS_AUTHOR_DATE;
use crate::verify_candidates::validate::escapes_root;
use crate::verify_status::VerificationStatus;

/// `CommitDriftStatus`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CommitDriftStatus {
    pub status: String,
    pub commits_since_verify: i64,
    pub recommendation: Option<PyStr>,
    pub claims_checked: i64,
    pub claims_drifted: Vec<PyStr>,
    pub basis: String,
}

impl CommitDriftStatus {
    /// `to_dict()`: status, the count, the recommendation and the basis,
    /// then `claim_drift` where `claims_checked` is not zero.
    pub fn to_dict(&self) -> PyValue {
        let mut out = vec![
            (PyValue::str("status"), PyValue::str(&self.status)),
            (
                PyValue::str("commits_since_verify"),
                PyValue::Int(self.commits_since_verify.to_string()),
            ),
            (
                PyValue::str("recommendation"),
                self.recommendation
                    .clone()
                    .map_or(PyValue::None, PyValue::Str),
            ),
            (PyValue::str("basis"), PyValue::str(&self.basis)),
        ];
        if self.claims_checked != 0 {
            out.push((
                PyValue::str("claim_drift"),
                PyValue::Dict(vec![
                    (
                        PyValue::str("checked"),
                        PyValue::Int(self.claims_checked.to_string()),
                    ),
                    (
                        PyValue::str("drifted"),
                        PyValue::List(
                            self.claims_drifted
                                .iter()
                                .cloned()
                                .map(PyValue::Str)
                                .collect(),
                        ),
                    ),
                ]),
            ));
        }
        PyValue::Dict(out)
    }
}

/// `_drift_recommendation`.
pub fn drift_recommendation(count: i64) -> String {
    let plural = if count == 1 { "" } else { "s" };
    format!(
        "{count} commit{plural} touching this memory's cited or attested \
         paths landed since the last memory_verify \u{2014} calendar verification \
         looks fresh but the claims' ground truth has moved. Spot-check at \
         least one verifiable claim against the current HEAD; call \
         memory_verify(id, note=...) if it still holds, or memory_update \
         first if it has drifted."
    )
}

/// `_claim_drift_recommendation`.
pub fn claim_drift_recommendation(count: i64, drifted: &[PyStr]) -> PyStr {
    let plural = if count == 1 { "" } else { "s" };
    let cplural = if drifted.len() == 1 { "" } else { "s" };
    let verb = if count == 1 {
        "implicates"
    } else {
        "implicate"
    };
    let mut out = cps(&format!(
        "{count} commit{plural} since the last memory_verify {verb} {} declared claim{cplural} (",
        drifted.len()
    ));
    for (i, claim) in drifted.iter().take(3).enumerate() {
        if i > 0 {
            out.extend(cps(", "));
        }
        out.extend_from_slice(claim);
    }
    if drifted.len() > 3 {
        out.push(0x2026);
    }
    out.extend(cps(
        ") \u{2014} the claimed binding itself was touched, not merely its file. \
         Spot-check those claims at HEAD; memory_verify if they hold, \
         memory_update (body and claims together) if not.",
    ));
    out
}

/// `ResolvedCommitDrift`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResolvedCommitDrift {
    pub count: i64,
    pub claims_checked: i64,
    pub claims_drifted: Vec<PyStr>,
    pub basis: String,
}

impl ResolvedCommitDrift {
    /// `ResolvedCommitDrift(count)`, every other field its default.
    pub fn new(count: i64) -> Self {
        ResolvedCommitDrift {
            count,
            claims_checked: 0,
            claims_drifted: Vec::new(),
            basis: BASIS_AUTHOR_DATE.to_string(),
        }
    }
}

/// `_index_paths`.
pub fn index_paths(index: &Index) -> BTreeSet<PyStr> {
    let mut out: BTreeSet<PyStr> = BTreeSet::new();
    out.extend(index.files.iter().cloned());
    out.extend(index.deleted.iter().cloned());
    out.extend(index.changed_text.keys().cloned());
    out.extend(index.changed_fragments.keys().cloned());
    out.extend(index.header_only.keys().cloned());
    out.extend(index.bindings.keys().map(|k| k.0.clone()));
    out
}

/// `_unreadable`.
pub fn unreadable(claims: &[Claim]) -> bool {
    claims
        .iter()
        .any(|c| control_character(&c.rel_path).is_some() || lone_surrogate(&c.rel_path).is_some())
}

/// `_gated_claims`.
pub fn gated_claims(claims: &[Claim]) -> Vec<Claim> {
    claims
        .iter()
        .filter(|c| !escapes_root(&c.rel_path))
        .cloned()
        .collect()
}

/// `commit_drift_status`.
pub fn commit_drift_status(resolved: &ResolvedCommitDrift) -> CommitDriftStatus {
    if resolved.count == 0 {
        return CommitDriftStatus {
            status: "clean".to_string(),
            commits_since_verify: 0,
            recommendation: None,
            claims_checked: resolved.claims_checked,
            claims_drifted: Vec::new(),
            basis: resolved.basis.clone(),
        };
    }
    let recommendation = if resolved.claims_drifted.is_empty() {
        cps(&drift_recommendation(resolved.count))
    } else {
        claim_drift_recommendation(resolved.count, &resolved.claims_drifted)
    };
    CommitDriftStatus {
        status: "drift".to_string(),
        commits_since_verify: resolved.count,
        recommendation: Some(recommendation),
        claims_checked: resolved.claims_checked,
        claims_drifted: resolved.claims_drifted.clone(),
        basis: resolved.basis.clone(),
    }
}

/// `staleness_verdict_from_report`.
pub fn staleness_verdict_from_report(
    verification: &VerificationStatus,
    path_drift: &PathDriftReport,
    commit_drift_count: Option<i64>,
) -> &'static str {
    let n = |v: &Vec<String>| i64::try_from(v.len()).unwrap_or(i64::MAX);
    compute_staleness_verdict(
        verification,
        n(&path_drift.claim_anchored_missing),
        commit_drift_count,
        n(&path_drift.missing),
        n(&path_drift.present_but_attested_absent),
    )
}
