//! `_response.py`'s search and list serializers: `ResponseBuilder` with
//! `hit_to_dict`, `summary_to_dict` and `tombstone_summary_to_dict`, and
//! the constant `SIMILAR_HITS_SHOWN`.
//!
//! Each method answers the reference's dict as a [`PyValue::Dict`] with
//! the keys in the reference's insertion order. The clock reaches the two
//! methods that read it as the argument `now`. A naive `now` against an
//! aware stamp is the reference's `TypeError` from
//! `compute_verification_status`, answered here as [`NaiveAwareMix`].

use crate::datetime::DateTime;
use crate::pyvalue::{PyStr, PyValue};
use crate::records::summaries::{MemoryHit, MemorySummary, TombstonedSummary};
use crate::records::Category;
use crate::verdict_signals::compute_staleness_verdict;
use crate::verify_status::{compute_verification_status, NaiveAwareMix};

/// `SIMILAR_HITS_SHOWN`: how many similar memories a write response lists
/// per list.
pub const SIMILAR_HITS_SHOWN: usize = 5;

/// `ResponseBuilder`: the per-request serializer, holding the staleness
/// threshold it was built with.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ResponseBuilder {
    pub stale_after_days: i64,
}

fn key(text: &str) -> PyValue {
    PyValue::str(text)
}

fn text(value: &PyStr) -> PyValue {
    PyValue::Str(value.clone())
}

fn texts(values: &[PyStr]) -> PyValue {
    PyValue::List(values.iter().map(text).collect())
}

fn opt_text(value: &Option<PyStr>) -> PyValue {
    value.as_ref().map_or(PyValue::None, text)
}

fn category(value: Option<Category>) -> PyValue {
    value.map_or(PyValue::None, |c| PyValue::str(c.value()))
}

/// `isoformat` (`time_utils.isoformat_utc`).
fn iso(value: &DateTime) -> PyValue {
    PyValue::str(&value.isoformat_utc())
}

/// `isoformat_optional`.
fn iso_opt(value: &Option<DateTime>) -> PyValue {
    value.as_ref().map_or(PyValue::None, iso)
}

fn count(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

impl ResponseBuilder {
    /// `ResponseBuilder(stale_after_days=...)`.
    pub fn new(stale_after_days: i64) -> ResponseBuilder {
        ResponseBuilder { stale_after_days }
    }

    /// `hit_to_dict(hit, now=now)`: the fifteen fixed keys, then the
    /// `path_drift` block when the missing, verified, expected-absent or
    /// present-but-attested-absent bucket is not empty, its three fixed
    /// keys followed by each additive key whose bucket is not empty.
    pub fn hit_to_dict(&self, hit: &MemoryHit, now: DateTime) -> Result<PyValue, NaiveAwareMix> {
        let verification =
            compute_verification_status(hit.last_verified_at, now, self.stale_after_days)?;
        let verdict = compute_staleness_verdict(
            &verification,
            count(hit.path_drift_claim_anchored_missing_paths.len()),
            None,
            count(hit.path_drift_missing_paths.len()),
            count(hit.path_drift_present_but_attested_absent.len()),
        );
        let mut out = vec![
            (key("id"), text(&hit.id)),
            (key("scopes"), texts(&hit.scopes)),
            (key("confidence"), PyValue::str(hit.confidence.value())),
            (key("category"), category(hit.category)),
            (key("snippet"), text(&hit.snippet)),
            (key("score"), PyValue::Float(hit.score)),
            (key("relevance"), text(&hit.relevance)),
            (key("match_terms"), texts(&hit.match_terms)),
            (key("created"), iso(&hit.created)),
            (key("updated"), iso(&hit.updated)),
            (key("last_verified_at"), iso_opt(&hit.last_verified_at)),
            (key("verification"), verification.to_dict()),
            (
                key("path_drift_checked"),
                PyValue::int(i128::from(hit.path_drift_checked)),
            ),
            (
                key("path_drift_missing"),
                PyValue::int(i128::from(hit.path_drift_missing)),
            ),
            (key("staleness_verdict"), PyValue::str(verdict)),
        ];
        if !hit.path_drift_missing_paths.is_empty()
            || !hit.path_drift_verified_paths.is_empty()
            || !hit.path_drift_expected_absent_paths.is_empty()
            || !hit.path_drift_present_but_attested_absent.is_empty()
        {
            let mut drift = vec![
                (key("checked"), texts(&hit.path_drift_checked_paths)),
                (key("missing"), texts(&hit.path_drift_missing_paths)),
                (key("verified"), texts(&hit.path_drift_verified_paths)),
            ];
            let additive: [(&str, &Vec<PyStr>); 4] = [
                ("expected_absent", &hit.path_drift_expected_absent_paths),
                (
                    "present_but_attested_absent",
                    &hit.path_drift_present_but_attested_absent,
                ),
                ("dropped_as_route", &hit.path_drift_dropped_as_route_paths),
                (
                    "claim_anchored_missing",
                    &hit.path_drift_claim_anchored_missing_paths,
                ),
            ];
            for (name, bucket) in additive {
                if !bucket.is_empty() {
                    drift.push((key(name), texts(bucket)));
                }
            }
            out.push((key("path_drift"), PyValue::Dict(drift)));
        }
        Ok(PyValue::Dict(out))
    }

    /// `summary_to_dict(summary, now=now)`: nine keys, then `actor` with
    /// the declared `client` and `model` (in that order) when either is
    /// not None.
    pub fn summary_to_dict(
        &self,
        summary: &MemorySummary,
        now: DateTime,
    ) -> Result<PyValue, NaiveAwareMix> {
        let verification =
            compute_verification_status(summary.last_verified_at, now, self.stale_after_days)?;
        let mut row = vec![
            (key("id"), text(&summary.id)),
            (key("scopes"), texts(&summary.scopes)),
            (key("confidence"), PyValue::str(summary.confidence.value())),
            (key("category"), category(summary.category)),
            (key("summary"), text(&summary.summary)),
            (key("created"), iso(&summary.created)),
            (key("updated"), iso(&summary.updated)),
            (key("last_verified_at"), iso_opt(&summary.last_verified_at)),
            (key("verification"), verification.to_dict()),
        ];
        if let Some(actor) = &summary.actor {
            let declared: Vec<(PyValue, PyValue)> =
                [("client", &actor.client), ("model", &actor.model)]
                    .into_iter()
                    .filter_map(|(name, value)| value.as_ref().map(|v| (key(name), text(v))))
                    .collect();
            if !declared.is_empty() {
                row.push((key("actor"), PyValue::Dict(declared)));
            }
        }
        Ok(PyValue::Dict(row))
    }

    /// `tombstone_summary_to_dict(summary)`: the summary's eight stored
    /// keys and the three removal keys; no verification block and no clock.
    pub fn tombstone_summary_to_dict(&self, summary: &TombstonedSummary) -> PyValue {
        PyValue::Dict(vec![
            (key("id"), text(&summary.id)),
            (key("scopes"), texts(&summary.scopes)),
            (key("confidence"), PyValue::str(summary.confidence.value())),
            (key("category"), category(summary.category)),
            (key("summary"), text(&summary.summary)),
            (key("created"), iso(&summary.created)),
            (key("updated"), iso(&summary.updated)),
            (key("last_verified_at"), iso_opt(&summary.last_verified_at)),
            (key("removed"), iso(&summary.removed)),
            (key("removed_reason"), text(&summary.removed_reason)),
            (key("removed_session"), opt_text(&summary.removed_session)),
        ])
    }
}
