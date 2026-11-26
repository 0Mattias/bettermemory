//! `eval.py`, its first unit: the report `compute_eval` returns,
//! `EvalReport`, with its `to_dict`. The computation (`compute_eval`,
//! `_eval_report`) and the other report classes follow in later units.
//!
//! Counts are `i64`, as `eval_model`'s are. `review` and `surfaces` are
//! the `dict[str, Any]` the reference's `ReviewReport.to_dict()` and
//! `compute_surface_report` make, carried as [`PyValue`]s and passed
//! through `to_dict` as they are. `by_model` keeps the reference's
//! `dict[str, dict[str, int]]` as pairs in insertion order; its keys are
//! distinct, as a `dict`'s are.

use crate::datetime::DateTime;
use crate::eval_model::{
    ColdEndorsementMemoriesRow, RateCI, SilentMissCandidate, DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
};
use crate::health_model::{dict, int, text, HOOKLESS_REASON};
use crate::pyvalue::{PyStr, PyValue};
use crate::time_utils::isoformat_utc_optional;
use crate::unicode;

/// `EvalReport`. The reference gives its first thirteen fields (through
/// `silent_misses`) no default; [`EvalReport::new`] sets them to zero,
/// None and the empty text, and a caller sets each. The rest take the
/// reference's defaults there: `RateCI(0, 0, None, None, None)` for each
/// rate, `DEFAULT_ENDORSEMENT_MIN_RETRIEVALS`, a ratio threshold of 0.0,
/// False, empty lists and dicts, and None.
#[derive(Clone, Debug, PartialEq)]
pub struct EvalReport {
    pub generated_at: DateTime,
    pub window_seconds: Option<i64>,
    pub scope_filter: Option<PyStr>,
    pub threshold_rule: PyStr,
    pub total_events_scanned: i64,
    pub events_in_window: i64,
    pub retrieval_occurrences: i64,
    pub explicit_endorsements_with_excerpt: i64,
    pub applied_total: i64,
    pub applied_explicit: i64,
    pub turns_audited: i64,
    pub turns_no_signal: i64,
    pub silent_misses: i64,
    pub memory_helped_rate: RateCI,
    pub endorsement_rate: RateCI,
    pub silent_miss_rate: RateCI,
    pub cold_endorsement_memories_rows: Vec<ColdEndorsementMemoriesRow>,
    pub cold_endorsement_memories_total: i64,
    pub endorsement_min_retrievals: i64,
    pub cold_endorsement_ratio_threshold: f64,
    pub cold_endorsement_suppressed: bool,
    pub silent_miss_recent: Vec<SilentMissCandidate>,
    pub repeat_audits: i64,
    pub by_model: Vec<(PyStr, Vec<(PyStr, i64)>)>,
    pub window_start: Option<DateTime>,
    pub window_end: Option<DateTime>,
    pub review: Option<PyValue>,
    pub applied_model: i64,
    pub applied_hook: i64,
    pub surfaces: Option<PyValue>,
}

/// `RateCI(0, 0, None, None, None)`: the default of each rate.
pub const NO_RATE: RateCI = RateCI {
    numerator: 0,
    denominator: 0,
    rate: None,
    lower: None,
    upper: None,
    torn_read: false,
};

impl EvalReport {
    /// A report generated at `generated_at`, every other field zero, None,
    /// empty or the reference's default.
    pub fn new(generated_at: DateTime) -> EvalReport {
        EvalReport {
            generated_at,
            window_seconds: None,
            scope_filter: None,
            threshold_rule: Vec::new(),
            total_events_scanned: 0,
            events_in_window: 0,
            retrieval_occurrences: 0,
            explicit_endorsements_with_excerpt: 0,
            applied_total: 0,
            applied_explicit: 0,
            turns_audited: 0,
            turns_no_signal: 0,
            silent_misses: 0,
            memory_helped_rate: NO_RATE,
            endorsement_rate: NO_RATE,
            silent_miss_rate: NO_RATE,
            cold_endorsement_memories_rows: Vec::new(),
            cold_endorsement_memories_total: 0,
            endorsement_min_retrievals: DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
            cold_endorsement_ratio_threshold: 0.0,
            cold_endorsement_suppressed: false,
            silent_miss_recent: Vec::new(),
            repeat_audits: 0,
            by_model: Vec::new(),
            window_start: None,
            window_end: None,
            review: None,
            applied_model: 0,
            applied_hook: 0,
            surfaces: None,
        }
    }

    /// `to_dict`, in the reference's key order; `surfaces` only when it
    /// is not None.
    pub fn to_dict(&self) -> PyValue {
        let iso = |dt: Option<&DateTime>| {
            isoformat_utc_optional(dt).map_or(PyValue::None, |s| PyValue::str(&s))
        };
        let by_model = PyValue::Dict(
            self.by_model
                .iter()
                .map(|(model, counts)| {
                    let counts = counts.iter().map(|(k, n)| (text(k), int(*n))).collect();
                    (text(model), PyValue::Dict(counts))
                })
                .collect(),
        );
        let mut out = dict(vec![
            ("generated_at", PyValue::str(&self.generated_at.isoformat())),
            (
                "window_seconds",
                self.window_seconds.map_or(PyValue::None, int),
            ),
            (
                "window",
                dict(vec![
                    ("since", iso(self.window_start.as_ref())),
                    ("until", iso(self.window_end.as_ref())),
                ]),
            ),
            (
                "scope_filter",
                self.scope_filter.as_deref().map_or(PyValue::None, text),
            ),
            ("threshold_rule", text(&self.threshold_rule)),
            ("total_events_scanned", int(self.total_events_scanned)),
            ("events_in_window", int(self.events_in_window)),
            (
                "counts",
                dict(vec![
                    ("retrieval_occurrences", int(self.retrieval_occurrences)),
                    (
                        "explicit_endorsements_with_excerpt",
                        int(self.explicit_endorsements_with_excerpt),
                    ),
                    ("applied_total", int(self.applied_total)),
                    ("applied_explicit", int(self.applied_explicit)),
                    ("applied_model", int(self.applied_model)),
                    ("applied_hook", int(self.applied_hook)),
                    ("turns_audited", int(self.turns_audited)),
                    ("turns_no_signal", int(self.turns_no_signal)),
                    ("silent_misses", int(self.silent_misses)),
                    ("repeat_audits", int(self.repeat_audits)),
                ]),
            ),
            ("by_model", by_model),
            ("memory_helped_rate", self.memory_helped_rate.to_dict()),
            ("endorsement_rate", self.endorsement_rate.to_dict()),
            ("silent_miss_rate", self.silent_miss_rate.to_dict()),
            (
                "cold_endorsement_memories",
                dict(vec![
                    ("min_retrievals", int(self.endorsement_min_retrievals)),
                    (
                        "ratio_threshold",
                        PyValue::Float(self.cold_endorsement_ratio_threshold),
                    ),
                    ("total", int(self.cold_endorsement_memories_total)),
                    (
                        "rows",
                        PyValue::List(
                            self.cold_endorsement_memories_rows
                                .iter()
                                .map(ColdEndorsementMemoriesRow::to_dict)
                                .collect(),
                        ),
                    ),
                    (
                        "suppressed",
                        PyValue::Bool(self.cold_endorsement_suppressed),
                    ),
                    (
                        "suppressed_reason",
                        if self.cold_endorsement_suppressed {
                            PyValue::str(HOOKLESS_REASON)
                        } else {
                            PyValue::None
                        },
                    ),
                ]),
            ),
            (
                "silent_miss_recent",
                PyValue::List(
                    self.silent_miss_recent
                        .iter()
                        .map(SilentMissCandidate::to_dict)
                        .collect(),
                ),
            ),
            ("review", self.review.clone().unwrap_or(PyValue::None)),
        ]);
        let surfaces = self
            .surfaces
            .as_ref()
            .filter(|s| !matches!(s, PyValue::None));
        if let (Some(surfaces), PyValue::Dict(items)) = (surfaces, &mut out) {
            items.push((PyValue::Str(unicode::cps("surfaces")), surfaces.clone()));
        }
        out
    }
}
