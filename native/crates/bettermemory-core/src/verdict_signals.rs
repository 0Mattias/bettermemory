//! `verify.py`'s verdict ladder: the three verdict strings,
//! `_VERDICT_RAISE_STATUSES`, `_COMMIT_DRIFT_ESCALATES`,
//! `_commit_leg_escalates`, `verdict_from_signals` and
//! `compute_staleness_verdict`.

use crate::verify_status::VerificationStatus;

/// `_VERDICT_FRESH`.
pub const VERDICT_FRESH: &str = "fresh";
/// `_VERDICT_RECOMMENDED`.
pub const VERDICT_RECOMMENDED: &str = "spot_check_recommended";
/// `_VERDICT_REQUIRED`.
pub const VERDICT_REQUIRED: &str = "spot_check_required";

/// `_VERDICT_RAISE_STATUSES`: statuses under which a failing check reads
/// required rather than recommended.
pub const VERDICT_RAISE_STATUSES: [&str; 2] = ["never", "stale"];

/// `_COMMIT_DRIFT_ESCALATES`: the commit leg's escalation switch.
pub const COMMIT_DRIFT_ESCALATES: bool = true;

/// `_commit_leg_escalates`: a count above zero escalates; None never does.
pub fn commit_leg_escalates(commit_drift_count: Option<i64>) -> bool {
    if !COMMIT_DRIFT_ESCALATES {
        return false;
    }
    commit_drift_count.is_some_and(|n| n > 0)
}

/// `verdict_from_signals`: the status is the `verification.status` text.
pub fn verdict_from_signals(
    status: &str,
    path_drift_missing: i64,
    commit_drift_count: Option<i64>,
    cited_missing: i64,
    attested_absent_present: i64,
) -> &'static str {
    if status == "remote" {
        return VERDICT_REQUIRED;
    }
    if path_drift_missing > 0
        || attested_absent_present > 0
        || commit_leg_escalates(commit_drift_count)
    {
        if VERDICT_RAISE_STATUSES.contains(&status) {
            return VERDICT_REQUIRED;
        }
        return VERDICT_RECOMMENDED;
    }
    if cited_missing > 0 {
        return VERDICT_RECOMMENDED;
    }
    VERDICT_FRESH
}

/// `compute_staleness_verdict`: [`verdict_from_signals`] over a
/// [`VerificationStatus`].
pub fn compute_staleness_verdict(
    verification: &VerificationStatus,
    path_drift_missing: i64,
    commit_drift_count: Option<i64>,
    cited_missing: i64,
    attested_absent_present: i64,
) -> &'static str {
    verdict_from_signals(
        verification.status.name(),
        path_drift_missing,
        commit_drift_count,
        cited_missing,
        attested_absent_present,
    )
}
