//! The report model of `health.py` (`health_model.py`), unit
//! health_model-1: the shared predicates (`_freshest_touch_ts`,
//! `_has_unresolved_contradiction`, `_is_dead_weight`,
//! `is_hook_telemetry_event`, `applied_tier`, `endorsement_tier`), the
//! constants `_ENDORSEMENT_GRACE_DAYS` and `_HOOKLESS_REASON`, the
//! per-memory, per-marker, per-scope and per-actor report types with their
//! properties and `to_dict`, and `explicit_ratio` and `_iso`, which they
//! call.
//!
//! The types hold Rust values; `to_dict` answers a [`PyValue::Dict`] in the
//! reference's key order, for a caller to spell through `json::dumps`.
//! Counts are `i64` (every caller counts rows); a sum of two of them is
//! taken in `i128`, so no count overflows where a Python `int` would not.
//! An event is a `dict[str, Any]`, read as a [`PyValue`]: a value that is
//! not a dict has no `.get`, and the reference raises AttributeError.

use crate::datetime::{ymd_to_ord, DateTime};
use crate::pyvalue::{PyStr, PyValue};
use crate::records::Category;
use crate::search::num::{int_true_div, py_max, round_digits};
use crate::time_utils::isoformat_utc_optional;
use crate::unicode;

mod debt;
pub use debt::{
    clamp_ratio_threshold, is_weakly_endorsed, unread_dict, unread_fields, weakly_endorsed,
    ActorSlices, ColdEndorsementMemories, CommitDriftDebt, CommitDriftRow, CrossRepoDrift,
    CrossRepoDriftGroup, RecentSilentMiss, SilentMissStats, UnreadFields, VerificationDebt,
    COLD_ENDORSEMENT_CAP, COLD_ENDORSEMENT_MIN_RETRIEVALS, COMMIT_DRIFT_DEBT_CAP,
    CROSS_REPO_MAX_ROOTS, RECENT_SILENT_MISSES_CAP, VERIFICATION_DEBT_CAP,
};

/// `_ENDORSEMENT_GRACE_DAYS`: how old a memory's earliest timestamped
/// retrieval must be before `applied == 0` counts against it.
pub const ENDORSEMENT_GRACE_DAYS: i64 = 2;

/// `_HOOKLESS_REASON`: the one sentence every surface gives when it
/// suppresses a dead-weight verdict.
pub const HOOKLESS_REASON: &str =
    "no Stop-hook settlement telemetry in the event log \u{2014} the only \
settlement left is the in-process auto-commit, which cannot settle \
a retrieval the session never returns to, so zero applied and zero \
explicit-applied counts do not distinguish an unhelpful memory \
from an unwired hook. Wire the Stop hook (the plugin's \
hooks.json carries the shape), then re-check.";

/// Why a question of this module has no answer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HealthModelError {
    /// `.get` of an event that is not a dict.
    AttributeError,
    /// A naive datetime ordered against an aware one.
    TypeError,
    /// `timestamp()` of a naive datetime, which the reference reads as the
    /// machine's local time (`mktime`); that reading is not ported.
    NaiveTimestamp,
}

impl HealthModelError {
    /// The exception's class name, or `NaiveTimestamp` for the decline.
    pub fn name(self) -> &'static str {
        match self {
            HealthModelError::AttributeError => "AttributeError",
            HealthModelError::TypeError => "TypeError",
            HealthModelError::NaiveTimestamp => "NaiveTimestamp",
        }
    }
}

type Answer<T> = Result<T, HealthModelError>;

/// `_iso`: `time_utils.isoformat_utc_optional`.
pub fn iso(dt: Option<&DateTime>) -> Option<String> {
    isoformat_utc_optional(dt)
}

fn iso_value(dt: &DateTime) -> PyValue {
    PyValue::str(&dt.isoformat_utc())
}

/// Python's `n / d` of two integers, `d != 0`: correctly rounded, the sign
/// that of the quotient (`0 / -5` is `-0.0`).
fn true_div(n: i128, d: i128) -> f64 {
    let q = int_true_div(n.abs(), d.abs());
    if (n < 0) != (d < 0) {
        -q
    } else {
        q
    }
}

/// `explicit_ratio(applied, explicit_applied)`: None when `applied <= 0`,
/// else `explicit_applied / applied`.
pub fn explicit_ratio(applied: i64, explicit_applied: i64) -> Option<f64> {
    if applied <= 0 {
        return None;
    }
    Some(true_div(i128::from(explicit_applied), i128::from(applied)))
}

/// `dt.timestamp()` of an aware datetime: the microseconds since the epoch
/// divided by 10**6 with one correct rounding, as CPython's
/// `timedelta.total_seconds` divides. A naive datetime is declined.
pub fn timestamp(dt: &DateTime) -> Answer<f64> {
    let instant = dt.instant_us().ok_or(HealthModelError::NaiveTimestamp)?;
    let epoch = i128::from(ymd_to_ord(1970, 1, 1) - 1) * 86_400_000_000;
    Ok(true_div(instant - epoch, 1_000_000))
}

/// `_freshest_touch_ts`: the latest of the four touches as a timestamp,
/// read in the reference's order (`created`, `updated`, then the two
/// optional ones), each kept only when strictly greater.
pub fn freshest_touch_ts(
    created: &DateTime,
    updated: &DateTime,
    last_verified_at: Option<&DateTime>,
    last_corroborated: Option<&DateTime>,
) -> Answer<f64> {
    let mut ts = timestamp(created)?;
    ts = py_max(ts, timestamp(updated)?);
    if let Some(v) = last_verified_at {
        ts = py_max(ts, timestamp(v)?);
    }
    if let Some(c) = last_corroborated {
        ts = py_max(ts, timestamp(c)?);
    }
    Ok(ts)
}

/// `a > b` of two datetimes: two aware ones as instants, two naive ones by
/// their wall-clock fields, a naive against an aware TypeError.
fn later(a: &DateTime, b: &DateTime) -> Answer<bool> {
    match (a.instant_us(), b.instant_us()) {
        (Some(x), Some(y)) => Ok(x > y),
        (None, None) => Ok(a.wall_micros() > b.wall_micros()),
        _ => Err(HealthModelError::TypeError),
    }
}

/// `_has_unresolved_contradiction`: the contradiction is newer than the
/// later of `updated` and `last_verified_at`.
pub fn has_unresolved_contradiction(
    last_contradicted_at: Option<&DateTime>,
    updated: &DateTime,
    last_verified_at: Option<&DateTime>,
) -> Answer<bool> {
    let Some(contradicted) = last_contradicted_at else {
        return Ok(false);
    };
    let mut resolved = updated;
    if let Some(v) = last_verified_at {
        if later(v, resolved)? {
            resolved = v;
        }
    }
    later(contradicted, resolved)
}

/// The arguments of `_is_dead_weight`, by their keyword names.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct DeadWeight {
    pub category: Option<Category>,
    pub freshest_ts: f64,
    pub retrieval_count: i64,
    pub applied_count: i64,
    pub has_unresolved_contradiction: bool,
    pub earliest_retrieval_ts: Option<f64>,
    pub cutoff_ts: f64,
    pub grace_cutoff_ts: f64,
}

impl DeadWeight {
    /// `_is_dead_weight`: every gate can only exclude. A NaN compares
    /// false, as in Python, so it excludes nothing.
    pub fn is_dead_weight(&self) -> bool {
        if self.category == Some(Category::Ambient)
            || self.freshest_ts >= self.cutoff_ts
            || self.retrieval_count == 0
            || self.applied_count > 0
            || self.has_unresolved_contradiction
        {
            return false;
        }
        !matches!(self.earliest_retrieval_ts, Some(e) if e >= self.grace_cutoff_ts)
    }
}

static NONE: PyValue = PyValue::None;

/// `ev.get(key)` of each key: the last value of a `str` key, None when
/// missing; AttributeError when `ev` is not a dict. One pass from the end
/// reads every key, stopping once all are found (`ev.get` never raises,
/// so reading a key the reference skips changes nothing).
fn fields<'a, const N: usize>(ev: &'a PyValue, keys: [&str; N]) -> Answer<[&'a PyValue; N]> {
    let PyValue::Dict(items) = ev else {
        return Err(HealthModelError::AttributeError);
    };
    let mut found: [Option<&PyValue>; N] = [None; N];
    let mut left = N;
    for (k, v) in items.iter().rev() {
        let PyValue::Str(s) = k else { continue };
        for (slot, key) in found.iter_mut().zip(keys) {
            if slot.is_none() && is_text(s, key) {
                *slot = Some(v);
                left -= 1;
            }
        }
        if left == 0 {
            break;
        }
    }
    Ok(found.map(|v| v.unwrap_or(&NONE)))
}

/// Whether the code points `s` spell `text`; the length is compared first
/// (a `str` literal here is ASCII, one code point to a byte).
pub(crate) fn is_text(s: &[u32], text: &str) -> bool {
    debug_assert!(text.is_ascii());
    s.len() == text.len() && s.iter().zip(text.bytes()).all(|(&c, b)| c == u32::from(b))
}

/// `value == text` for a `str` literal: only a `str` of those code points.
fn is_str(value: &PyValue, text: &str) -> bool {
    matches!(value, PyValue::Str(s) if is_text(s, text))
}

/// `is_hook_telemetry_event`: a `use` with `attribution == "hook"` or
/// `triggered_from == "stop_hook"`, or a `turn_audited` from the stop hook.
pub fn is_hook_telemetry_event(ev: &PyValue) -> Answer<bool> {
    let [kind, attribution, triggered_from] =
        fields(ev, ["kind", "attribution", "triggered_from"])?;
    if is_str(kind, "use") {
        return Ok(is_str(attribution, "hook") || is_str(triggered_from, "stop_hook"));
    }
    if is_str(kind, "turn_audited") {
        return Ok(is_str(triggered_from, "stop_hook"));
    }
    Ok(false)
}

/// The three tiers of `applied_tier`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AppliedTier {
    Auto,
    Hook,
    Model,
}

impl AppliedTier {
    /// The reference's text for the tier.
    pub fn value(self) -> &'static str {
        match self {
            AppliedTier::Auto => "auto",
            AppliedTier::Hook => "hook",
            AppliedTier::Model => "model",
        }
    }
}

/// `applied_tier`: `auto` when `ev.get("auto") is True` (the bool True
/// only), `hook` when `attribution == "hook"`, else `model`.
pub fn applied_tier(ev: &PyValue) -> Answer<AppliedTier> {
    let [auto, attribution] = fields(ev, ["auto", "attribution"])?;
    Ok(tier_of(auto, attribution))
}

fn tier_of(auto: &PyValue, attribution: &PyValue) -> AppliedTier {
    if matches!(auto, PyValue::Bool(true)) {
        AppliedTier::Auto
    } else if is_str(attribution, "hook") {
        AppliedTier::Hook
    } else {
        AppliedTier::Model
    }
}

/// `endorsement_tier`: the tier of a `use` whose outcome is `applied`,
/// else None.
pub fn endorsement_tier(ev: &PyValue) -> Answer<Option<AppliedTier>> {
    let [kind, outcome, auto, attribution] =
        fields(ev, ["kind", "outcome", "auto", "attribution"])?;
    if !is_str(kind, "use") || !is_str(outcome, "applied") {
        return Ok(None);
    }
    Ok(Some(tier_of(auto, attribution)))
}

pub(crate) fn text(s: &[u32]) -> PyValue {
    PyValue::Str(s.to_vec())
}

pub(crate) fn int(n: impl Into<i128>) -> PyValue {
    PyValue::int(n.into())
}

pub(crate) fn opt_float(f: Option<f64>) -> PyValue {
    f.map_or(PyValue::None, PyValue::Float)
}

pub(crate) fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::Str(unicode::cps(k)), v))
            .collect(),
    )
}

/// `TelemetryCoverage`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TelemetryCoverage {
    pub hook_telemetry_events: i64,
    pub covered: bool,
    pub dead_weight_suppressed: bool,
    pub cold_endorsement_suppressed: bool,
    pub reason: Option<PyStr>,
}

impl TelemetryCoverage {
    /// The dataclass's constructor with its defaults (no cold-endorsement
    /// suppression, no reason).
    pub fn new(hook_telemetry_events: i64, covered: bool, dead_weight_suppressed: bool) -> Self {
        TelemetryCoverage {
            hook_telemetry_events,
            covered,
            dead_weight_suppressed,
            cold_endorsement_suppressed: false,
            reason: None,
        }
    }

    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("hook_telemetry_events", int(self.hook_telemetry_events)),
            ("covered", PyValue::Bool(self.covered)),
            (
                "dead_weight_suppressed",
                PyValue::Bool(self.dead_weight_suppressed),
            ),
            (
                "cold_endorsement_suppressed",
                PyValue::Bool(self.cold_endorsement_suppressed),
            ),
            ("reason", self.reason.as_deref().map_or(PyValue::None, text)),
        ])
    }
}

/// `MemoryStats`: the event-driven metrics of one memory.
#[derive(Clone, Debug, PartialEq)]
pub struct MemoryStats {
    pub id: PyStr,
    pub scopes: Vec<PyStr>,
    pub summary: PyStr,
    pub created: DateTime,
    pub updated: DateTime,
    pub retrieval_count: i64,
    pub show_count: i64,
    pub applied_count: i64,
    pub auto_applied_count: i64,
    pub explicit_applied_count: i64,
    pub hook_applied_count: i64,
    pub model_applied_count: i64,
    pub ignored_count: i64,
    pub contradicted_count: i64,
    pub corrected_count: i64,
    pub last_used_at: Option<DateTime>,
    pub last_contradicted_at: Option<DateTime>,
    pub last_verified_at: Option<DateTime>,
    pub last_corroborated: Option<DateTime>,
    pub category: Option<Category>,
    /// Each entry as the caller built it (a dict in the reference); copied
    /// into `to_dict` as given.
    pub resolution_timeline: Vec<PyValue>,
}

impl MemoryStats {
    /// The dataclass's constructor with every defaulted field at its
    /// default (zero counts, no datetimes, no category, no timeline).
    pub fn new(
        id: PyStr,
        scopes: Vec<PyStr>,
        summary: PyStr,
        created: DateTime,
        updated: DateTime,
    ) -> Self {
        MemoryStats {
            id,
            scopes,
            summary,
            created,
            updated,
            retrieval_count: 0,
            show_count: 0,
            applied_count: 0,
            auto_applied_count: 0,
            explicit_applied_count: 0,
            hook_applied_count: 0,
            model_applied_count: 0,
            ignored_count: 0,
            contradicted_count: 0,
            corrected_count: 0,
            last_used_at: None,
            last_contradicted_at: None,
            last_verified_at: None,
            last_corroborated: None,
            category: None,
            resolution_timeline: Vec::new(),
        }
    }

    /// The property `has_unresolved_contradiction`.
    pub fn has_unresolved_contradiction(&self) -> Answer<bool> {
        has_unresolved_contradiction(
            self.last_contradicted_at.as_ref(),
            &self.updated,
            self.last_verified_at.as_ref(),
        )
    }

    /// The property `endorsement_ratio`: `explicit_ratio` of the counts.
    pub fn endorsement_ratio(&self) -> Option<f64> {
        explicit_ratio(self.applied_count, self.explicit_applied_count)
    }

    /// The property `model_endorsement_ratio`: None only when
    /// `applied_count == 0` (a negative count divides).
    pub fn model_endorsement_ratio(&self) -> Option<f64> {
        if self.applied_count == 0 {
            return None;
        }
        Some(true_div(
            i128::from(self.model_applied_count),
            i128::from(self.applied_count),
        ))
    }

    /// `to_dict`, in the reference's key order; TypeError where
    /// `has_unresolved_contradiction` orders a naive against an aware
    /// datetime.
    pub fn to_dict(&self) -> Answer<PyValue> {
        let opt_iso = |dt: &Option<DateTime>| dt.as_ref().map_or(PyValue::None, iso_value);
        Ok(dict(vec![
            ("id", text(&self.id)),
            (
                "scopes",
                PyValue::List(self.scopes.iter().map(|s| text(s)).collect()),
            ),
            ("summary", text(&self.summary)),
            ("created", iso_value(&self.created)),
            ("updated", iso_value(&self.updated)),
            ("retrieval_count", int(self.retrieval_count)),
            ("show_count", int(self.show_count)),
            ("applied_count", int(self.applied_count)),
            ("auto_applied_count", int(self.auto_applied_count)),
            ("explicit_applied_count", int(self.explicit_applied_count)),
            ("hook_applied_count", int(self.hook_applied_count)),
            ("model_applied_count", int(self.model_applied_count)),
            ("endorsement_ratio", opt_float(self.endorsement_ratio())),
            (
                "model_endorsement_ratio",
                opt_float(self.model_endorsement_ratio()),
            ),
            ("ignored_count", int(self.ignored_count)),
            ("contradicted_count", int(self.contradicted_count)),
            ("corrected_count", int(self.corrected_count)),
            ("last_used_at", opt_iso(&self.last_used_at)),
            ("last_verified_at", opt_iso(&self.last_verified_at)),
            (
                "category",
                self.category
                    .map_or(PyValue::None, |c| PyValue::str(c.value())),
            ),
            (
                "has_unresolved_contradiction",
                PyValue::Bool(self.has_unresolved_contradiction()?),
            ),
            (
                "resolution_timeline",
                PyValue::List(self.resolution_timeline.clone()),
            ),
        ]))
    }
}

/// `MarkerStats`: one transient marker's fires and overrides.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MarkerStats {
    pub marker: PyStr,
    pub fire_count: i64,
    pub override_count: i64,
}

impl MarkerStats {
    /// The property `total`: fires plus overrides, as an unbounded `int`.
    pub fn total(&self) -> i128 {
        i128::from(self.fire_count) + i128::from(self.override_count)
    }

    /// The property `override_rate`: overrides over the total, 0.0 when
    /// the total is not positive.
    pub fn override_rate(&self) -> f64 {
        let total = self.total();
        if total > 0 {
            true_div(i128::from(self.override_count), total)
        } else {
            0.0
        }
    }

    /// `to_dict`, the rate as `round(rate, 3)`.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("marker", text(&self.marker)),
            ("fire_count", int(self.fire_count)),
            ("override_count", int(self.override_count)),
            (
                "override_rate",
                PyValue::Float(round_digits(self.override_rate(), 3)),
            ),
        ])
    }
}

/// `ScopeHealth`: one scope's curation counts.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ScopeHealth {
    pub scope: PyStr,
    pub active: i64,
    pub dead: i64,
    pub cold: i64,
    pub contradicted: i64,
    pub applied_total: i64,
}

impl ScopeHealth {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("scope", text(&self.scope)),
            ("active", int(self.active)),
            ("dead", int(self.dead)),
            ("cold", int(self.cold)),
            ("contradicted", int(self.contradicted)),
            ("applied_total", int(self.applied_total)),
        ])
    }
}

/// `ActorSlice`: one client's footprint. `models` and `principals` are
/// dicts in insertion order, their keys distinct as a dict's are.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ActorSlice {
    pub client: Option<PyStr>,
    pub memories: i64,
    pub memories_in_window: i64,
    pub events: i64,
    pub searches: i64,
    pub applies: i64,
    pub models: Vec<(PyStr, i64)>,
    pub principals: Vec<(PyStr, i64)>,
}

fn census(items: &[(PyStr, i64)]) -> PyValue {
    PyValue::Dict(items.iter().map(|(k, v)| (text(k), int(*v))).collect())
}

impl ActorSlice {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("client", self.client.as_deref().map_or(PyValue::None, text)),
            ("memories", int(self.memories)),
            ("memories_in_window", int(self.memories_in_window)),
            ("events", int(self.events)),
            ("searches", int(self.searches)),
            ("applies", int(self.applies)),
            ("models", census(&self.models)),
            ("principals", census(&self.principals)),
        ])
    }
}
