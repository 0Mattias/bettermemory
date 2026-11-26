//! The write review's counters (`review_counters.py`), unit
//! review_counters-1a: the module's tables, its field readers (`_str`,
//! `_ids`, `_session_of`, `_bucket_value`, `_score`), the three row
//! readers (`IssuedRow`, `DecidedRow`, `GradeRow`, each with
//! `from_event`) and `is_review_row`.
//!
//! An event is a `dict[str, Any]` read from the log, here a
//! [`PyValue`]. A value that is not a dict has no `.get`, and the
//! reference raises AttributeError, answered as
//! [`CountersError::AttributeError`]. `.get(key)` is [`PyValue::get`]:
//! the last value of a `str` key, as a dict built from those pairs keeps
//! it. A datetime is a [`DateTime`] with its offset, as `parse_event_ts`
//! answers it. An `int` is kept as `PyValue::Int`'s decimal text, since
//! the reference keeps any `int`, past `i64` too.

use std::collections::HashMap;

use crate::datetime::{parse_event_ts, DateTime};
use crate::pyvalue::{type_name, PyStr, PyValue};
use crate::review::{
    ACTION_COMMIT, ACTION_RESTATES, ACTION_SECRET, ACTION_STANDING, ACTION_UNSAFE, DETECTOR_REVIEW,
    DETECTOR_REVIEW_DISAGREES, LABEL_UNRELATED, REVIEW_ISSUED, UNDECLARED,
};
use crate::unicode::cps;

mod chains;
pub use chains::*;

/// `ANSWER_WINDOW` (30 minutes), in microseconds.
pub const ANSWER_WINDOW_US: i64 = 30 * 60 * 1_000_000;
/// `REVIEW_GRADE`: the event kind `bettermemory review grade` writes.
pub const REVIEW_GRADE: &str = "review_grade";
/// `REFUSAL_ACTIONS`, in the reference's order.
pub const REFUSAL_ACTIONS: [&str; 3] = [ACTION_SECRET, ACTION_UNSAFE, ACTION_RESTATES];
/// `DETECTORS`, in the reference's order.
pub const DETECTORS: [&str; 2] = [DETECTOR_REVIEW, DETECTOR_REVIEW_DISAGREES];
/// `_STATUS_ACTION`: the action of a write row that predates
/// `review_action`, by status, in the reference's order.
pub const STATUS_ACTION: [(&str, &str); 5] = [
    ("committed", ACTION_COMMIT),
    ("credential_warning", ACTION_SECRET),
    ("instruction_warning", ACTION_UNSAFE),
    ("duplicate", ACTION_RESTATES),
    (ACTION_STANDING, ACTION_STANDING),
];

/// The exception a reader of this module raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CountersError {
    /// AttributeError: `.get` of an event of this type, not a dict.
    AttributeError(&'static str),
    /// OverflowError: `float(int)` of a score past the float range. Its
    /// text follows the release; the class is the answer.
    OverflowError,
}

impl CountersError {
    /// The exception's class name.
    pub fn name(self) -> &'static str {
        match self {
            CountersError::AttributeError(_) => "AttributeError",
            CountersError::OverflowError => "OverflowError",
        }
    }
}

type Answer<T> = Result<T, CountersError>;

/// The event itself, refused with AttributeError when it is not a dict.
fn dict_of(event: &PyValue) -> Answer<&PyValue> {
    match event {
        PyValue::Dict(_) => Ok(event),
        other => Err(CountersError::AttributeError(type_name(other))),
    }
}

/// `_str`: a non-empty `str`, else None.
pub fn str_of(value: Option<&PyValue>) -> Option<PyStr> {
    match value {
        Some(PyValue::Str(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    }
}

/// `_ids`: a `list`'s non-empty `str` items in order; a tuple, or
/// anything else, gives none.
pub fn ids(value: Option<&PyValue>) -> Vec<PyStr> {
    match value {
        Some(PyValue::List(items)) => items
            .iter()
            .filter_map(|v| match v {
                PyValue::Str(s) if !s.is_empty() => Some(s.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// The event's `actor` when it is a dict (a `Mapping`).
fn actor_of(event: &PyValue) -> Option<&PyValue> {
    match event.get("actor") {
        Some(actor @ PyValue::Dict(_)) => Some(actor),
        _ => None,
    }
}

/// `_session_of`: the actor block's session, then the row's own.
pub fn session_of(event: &PyValue) -> Answer<Option<PyStr>> {
    let event = dict_of(event)?;
    if let Some(session) = actor_of(event).and_then(|a| str_of(a.get("session"))) {
        return Ok(Some(session));
    }
    Ok(str_of(event.get("session")))
}

/// `_bucket_value`: the row's own field, then its actor block's, then
/// `UNDECLARED`.
pub fn bucket_value(event: &PyValue, key: &str) -> Answer<PyStr> {
    let event = dict_of(event)?;
    let value = str_of(event.get(key)).or_else(|| actor_of(event).and_then(|a| str_of(a.get(key))));
    Ok(value.unwrap_or_else(|| cps(UNDECLARED)))
}

/// `float(int)` of decimal text: correctly rounded (Rust's parse rounds
/// half to even, as CPython does), OverflowError from 2**1024 - 2**970 on.
/// A zero is +0.0: an `int` has no negative zero, and a nonzero `int`
/// never rounds to 0.
fn int_to_float(text: &str) -> Answer<f64> {
    match text.parse::<f64>() {
        Ok(x) if x.is_finite() => Ok(if x == 0.0 { 0.0 } else { x }),
        _ => Err(CountersError::OverflowError),
    }
}

/// `_score`: a bool is None; an `int` or a `float` is `float(value)`.
pub fn score(value: Option<&PyValue>) -> Answer<Option<f64>> {
    match value {
        Some(PyValue::Int(text)) => int_to_float(text).map(Some),
        Some(PyValue::Float(f)) => Ok(Some(*f)),
        _ => Ok(None),
    }
}

/// `ts` read by `parse_event_ts`: None for a value that is not a `str`.
fn ts_of(value: Option<&PyValue>) -> Option<DateTime> {
    match value {
        Some(PyValue::Str(s)) => parse_event_ts(s),
        _ => None,
    }
}

/// `value is True`.
fn is_true(value: Option<&PyValue>) -> bool {
    matches!(value, Some(PyValue::Bool(true)))
}

/// One `review_issued` row.
#[derive(Clone, Debug, PartialEq)]
pub struct IssuedRow {
    pub review_id: PyStr,
    pub parent_review_id: Option<PyStr>,
    pub ts: DateTime,
    pub session: Option<PyStr>,
    pub client: PyStr,
    pub model: PyStr,
    pub related: Vec<PyStr>,
    pub pre_answered: bool,
    /// An `int` that is not a bool, as its decimal text.
    pub response_chars: Option<String>,
    pub roster: Vec<PyStr>,
}

impl IssuedRow {
    /// `chain_key`: the parent's review_id, else the row's own.
    pub fn chain_key(&self) -> &PyStr {
        self.parent_review_id.as_ref().unwrap_or(&self.review_id)
    }

    /// `from_event`: None for no review_id or time, or a row that carries
    /// a `door` (any value but None, `false` and `0` included).
    pub fn from_event(event: &PyValue) -> Answer<Option<IssuedRow>> {
        let event = dict_of(event)?;
        let review_id = str_of(event.get("review_id"));
        let ts = ts_of(event.get("ts"));
        let door = !matches!(event.get("door"), None | Some(PyValue::None));
        let (Some(review_id), Some(ts), false) = (review_id, ts, door) else {
            return Ok(None);
        };
        Ok(Some(IssuedRow {
            review_id,
            parent_review_id: str_of(event.get("parent_review_id")),
            ts,
            session: session_of(event)?,
            client: bucket_value(event, "client")?,
            model: bucket_value(event, "model")?,
            related: ids(event.get("related")),
            pre_answered: is_true(event.get("pre_answered")),
            response_chars: match event.get("response_chars") {
                Some(PyValue::Int(text)) => Some(text.clone()),
                _ => None,
            },
            roster: ids(event.get("roster")),
        }))
    }
}

/// One `write` row a reviewed call recorded.
#[derive(Clone, Debug, PartialEq)]
pub struct DecidedRow {
    pub review_id: PyStr,
    pub ts: DateTime,
    pub session: Option<PyStr>,
    pub client: PyStr,
    pub status: PyStr,
    pub action: PyStr,
    /// The `str` to `str` pairs of the answers' relation, in its order.
    pub relation: Vec<(PyStr, PyStr)>,
    pub r#unsafe: Option<f64>,
    pub secret: Option<f64>,
    pub memory_id: Option<PyStr>,
    pub links: Vec<PyStr>,
    pub update_suggested: Vec<PyStr>,
    pub filings: Vec<(PyStr, PyStr)>,
    pub credential_kinds: Vec<PyStr>,
    pub matches: Vec<PyStr>,
}

/// A dict's `items()`: a key held twice keeps its first place and its
/// last value, as a dict built from those pairs does. Only `str` keys are
/// kept: no other key equals a `str`, and the caller drops the rest.
fn str_items(pairs: &[(PyValue, PyValue)]) -> Vec<(&PyStr, &PyValue)> {
    let mut out: Vec<(&PyStr, &PyValue)> = Vec::new();
    let mut at: HashMap<&PyStr, usize> = HashMap::new();
    for (k, v) in pairs {
        let PyValue::Str(k) = k else { continue };
        match at.get(k) {
            Some(&i) => out[i].1 = v,
            None => {
                at.insert(k, out.len());
                out.push((k, v));
            }
        }
    }
    out
}

impl DecidedRow {
    /// `committed`: the action is `ACTION_COMMIT`.
    pub fn committed(&self) -> bool {
        self.action == cps(ACTION_COMMIT)
    }

    /// `zero_attestation`: both scores equal 0 (a float compare: -0.0
    /// does, NaN and None do not) and every relation label unrelated.
    pub fn zero_attestation(&self) -> bool {
        let unrelated = cps(LABEL_UNRELATED);
        self.r#unsafe == Some(0.0)
            && self.secret == Some(0.0)
            && self.relation.iter().all(|(_, label)| *label == unrelated)
    }

    /// `from_event`: None for no review_id, time or status;
    /// OverflowError for a score `int` past the float range.
    pub fn from_event(event: &PyValue) -> Answer<Option<DecidedRow>> {
        let event = dict_of(event)?;
        let review_id = str_of(event.get("review_id"));
        let ts = ts_of(event.get("ts"));
        let status = str_of(event.get("status"));
        let (Some(review_id), Some(ts), Some(status)) = (review_id, ts, status) else {
            return Ok(None);
        };
        let action = str_of(event.get("review_action")).unwrap_or_else(|| {
            STATUS_ACTION
                .iter()
                .find(|(s, _)| cps(s) == status)
                .map_or_else(|| status.clone(), |(_, a)| cps(a))
        });
        let mut relation = Vec::new();
        let (mut r#unsafe, mut secret) = (None, None);
        if let Some(answers @ PyValue::Dict(_)) = event.get("review_answers") {
            if let Some(PyValue::Dict(pairs)) = answers.get("relation") {
                relation = str_items(pairs)
                    .into_iter()
                    .filter_map(|(k, v)| match v {
                        PyValue::Str(v) => Some((k.clone(), v.clone())),
                        _ => None,
                    })
                    .collect();
            }
            r#unsafe = score(answers.get("unsafe"))?;
            secret = score(answers.get("secret"))?;
        }
        let mut filings = Vec::new();
        if let Some(PyValue::List(rows)) = event.get("review_filings") {
            for row in rows {
                if !matches!(row, PyValue::Dict(_)) {
                    continue;
                }
                if let (Some(target), Some(detector)) =
                    (str_of(row.get("id")), str_of(row.get("detector")))
                {
                    filings.push((target, detector));
                }
            }
        }
        Ok(Some(DecidedRow {
            review_id,
            ts,
            session: session_of(event)?,
            client: bucket_value(event, "client")?,
            status,
            action,
            relation,
            r#unsafe,
            secret,
            memory_id: str_of(event.get("id")),
            links: ids(event.get("supersedes_reviewed")),
            update_suggested: ids(event.get("update_suggested")),
            filings,
            credential_kinds: ids(event.get("credential_kinds")),
            matches: ids(event.get("matches")),
        }))
    }
}

/// One `review_grade` row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GradeRow {
    pub review_id: PyStr,
    pub ts: DateTime,
    pub sample_ts: Option<DateTime>,
    pub right_answers: bool,
    pub right_action: bool,
    pub pre_answered: bool,
}

impl GradeRow {
    /// `from_event`: None for no review_id or time, or a verdict that is
    /// not a bool (an `int` 1 is not).
    pub fn from_event(event: &PyValue) -> Answer<Option<GradeRow>> {
        let event = dict_of(event)?;
        let review_id = str_of(event.get("review_id"));
        let ts = ts_of(event.get("ts"));
        let (
            Some(review_id),
            Some(ts),
            Some(PyValue::Bool(right_answers)),
            Some(PyValue::Bool(right_action)),
        ) = (
            review_id,
            ts,
            event.get("right_answers"),
            event.get("right_action"),
        )
        else {
            return Ok(None);
        };
        Ok(Some(GradeRow {
            review_id,
            ts,
            sample_ts: ts_of(event.get("sample_ts")),
            right_answers: *right_answers,
            right_action: *right_action,
            pre_answered: is_true(event.get("pre_answered")),
        }))
    }
}

/// `is_review_row`: a `review_issued` or `review_grade` row, or a `write`
/// row whose review_id is a `str` (the empty one too). The kind is
/// compared by `==` (a tuple's `in`), so a kind of any type, an
/// unhashable one included, answers False rather than raising.
pub fn is_review_row(event: &PyValue) -> Answer<bool> {
    let event = dict_of(event)?;
    let kind = match event.get("kind") {
        Some(PyValue::Str(kind)) => kind,
        _ => return Ok(false),
    };
    if *kind == cps(REVIEW_ISSUED) || *kind == cps(REVIEW_GRADE) {
        return Ok(true);
    }
    Ok(*kind == cps("write") && matches!(event.get("review_id"), Some(PyValue::Str(_))))
}
