//! The store-free half of `standing.py` (unit standing-1): its constants,
//! the standing test, the probability check, the answer a put writes
//! beside its record, and the lines and questions the surfaces carry.
//!
//! `asking` takes the review's switch as an argument (`review.current_tau`
//! is the review's), and `answered_by` takes the request's actor
//! (`identity::Published::current_actor`). What reads the store (the body
//! sha memo, `answers`, `backlog`, `guarded_ids`, `makes_standing`,
//! `held_ids`, `standing_ids`, `refusal_rows`, `held_by_removal`) is
//! standing-2's.

use crate::float_repr;
use crate::identity::Actor;
use crate::models_text::{snippet_window, strip};
use crate::pyvalue::{self, PyError, PyStr, PyValue};
use crate::store::home::Answer;
use crate::unicode;

/// `review.RELATED_CAP` (review.py:91). Kept here until the review unit
/// is built, which moves it and re-exports it.
pub const RELATED_CAP: usize = 11;
/// `review.RELATED_TEXT_CHARS` (review.py:96), likewise.
pub const RELATED_TEXT_CHARS: usize = 600;
/// `review.STANDING_STATEMENT` (review.py:208-211), likewise.
pub const STANDING_STATEMENT: &str =
    "The statement directs future work in its scope until it is replaced: \
a ruling, rule or standing preference the user gave.";

/// `TAU_STANDING`: the standing threshold, inclusive; None turns it off.
pub const TAU_STANDING: Option<f64> = Some(0.3);
pub const SOURCE_REVIEW: &str = "review";
pub const SOURCE_UPDATE: &str = "update";
pub const SOURCE_ASKED: &str = "asked";
/// `SOURCES`, in the reference's order.
pub const SOURCES: [&str; 3] = [SOURCE_REVIEW, SOURCE_UPDATE, SOURCE_ASKED];
/// `BATCH`: the records one answer_standing response shows.
pub const BATCH: usize = RELATED_CAP;
/// `_CALL`.
pub const CALL: &str = "memory_admin(action=\"answer_standing\")";
/// `_ANSWER_FORMAT`.
pub const ANSWER_FORMAT: &str = "standing={<id>: <0 to 1>, ...}";

/// The exception the reference raises: its class and message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StandingError {
    pub class: PyError,
    pub text: String,
}

fn refused(class: PyError, text: String) -> StandingError {
    StandingError { class, text }
}

fn text_of(cps: &[u32]) -> String {
    cps.iter()
        .map(|&c| char::from_u32(c).unwrap_or('\u{FFFD}'))
        .collect()
}

/// `asking()`, given whether `review.current_tau()` is set.
pub fn asking(tau_set: bool) -> bool {
    tau_set
}

/// `is_standing(category, probability)`: `category` compared to
/// `"ambient"` as text.
pub fn is_standing(category: Option<&[u32]>, probability: Option<f64>) -> bool {
    if category == Some(&unicode::cps("ambient")[..]) {
        return true;
    }
    match (TAU_STANDING, probability) {
        (Some(tau), Some(p)) => p >= tau,
        _ => false,
    }
}

/// `type(value).__name__` of the values `PyValue` holds.
fn type_name(value: &PyValue) -> &'static str {
    match value {
        PyValue::None => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::Str(_) => "str",
        PyValue::DateTime(_) => "datetime",
        PyValue::Path(_) => "PurePosixPath",
        PyValue::Tuple(_) => "tuple",
        PyValue::List(_) => "list",
        PyValue::Dict(_) => "dict",
    }
}

/// `check_probability(value, name)`.
pub fn check_probability(value: &PyValue, name: &str) -> Result<f64, StandingError> {
    let number = match value {
        // `float(int)` rounds half to even: the text's nearest double,
        // which is infinite exactly where CPython overflows.
        PyValue::Int(text) => {
            let f = text.parse::<f64>().unwrap_or(f64::INFINITY);
            if f.is_infinite() {
                return Err(refused(
                    PyError::OverflowError,
                    "int too large to convert to float".to_owned(),
                ));
            }
            f
        }
        PyValue::Float(f) => *f,
        other => {
            return Err(refused(
                PyError::ValueError,
                format!(
                    "{name} must be a number from 0 to 1, got {}",
                    type_name(other)
                ),
            ))
        }
    };
    if !(0.0..=1.0).contains(&number) {
        let shown = match value {
            PyValue::Float(f) => float_repr::repr(*f),
            PyValue::Int(text) => text.clone(),
            _ => unreachable!("checked above"),
        };
        return Err(refused(
            PyError::ValueError,
            format!("{name} must be from 0 to 1, got {shown}"),
        ));
    }
    Ok(number)
}

/// `answered_by()` for the request's actor: `{client or 'none'}/{model or
/// 'none'}`.
pub fn answered_by(actor: Option<&Actor>) -> PyStr {
    let none = unicode::cps("none");
    let part = |v: Option<&PyStr>| match v {
        Some(t) if !t.is_empty() => t.clone(),
        _ => none.clone(),
    };
    let mut out = part(actor.and_then(|a| a.client.as_ref()));
    out.push(u32::from('/'));
    out.extend(part(actor.and_then(|a| a.model.as_ref())));
    out
}

/// `answer_of(probability, source)`: the source checked first, then the
/// probability, then who answered.
pub fn answer_of(
    probability: &PyValue,
    source: &[u32],
    actor: Option<&Actor>,
) -> Result<Answer, StandingError> {
    if !SOURCES.iter().any(|s| unicode::cps(s) == source) {
        return Err(refused(
            PyError::ValueError,
            format!(
                "unknown standing source {}",
                text_of(&pyvalue::repr_str(source))
            ),
        ));
    }
    let p = check_probability(probability, "standing")?;
    Ok(Answer {
        probability: PyValue::Float(p),
        source: PyValue::Str(source.to_vec()),
        answered_by: PyValue::Str(answered_by(actor)),
    })
}

/// `record_text(body)`.
pub fn record_text(body: &[u32]) -> PyStr {
    let text = strip(body);
    if text.len() <= RELATED_TEXT_CHARS {
        return text.to_vec();
    }
    snippet_window(text, 0, RELATED_TEXT_CHARS as i64 - 3)
}

/// `backlog_line(count)`.
pub fn backlog_line(count: i128) -> String {
    if count <= 0 {
        return String::new();
    }
    let noun = if count == 1 {
        "memory has"
    } else {
        "memories have"
    };
    format!(
        "bettermemory: {count} {noun} no standing answer. Answer them \
yourself: {CALL} shows {BATCH} at a time."
    )
}

/// `held_lines(ids)`.
pub fn held_lines(ids: &[PyStr]) -> Vec<PyStr> {
    if ids.is_empty() {
        return Vec::new();
    }
    let mut out = vec![unicode::cps(&format!(
        "Held out of force until an agent reads it against the standing roster ({}):",
        ids.len()
    ))];
    for id in ids {
        let mut line = unicode::cps("  memory_update(id=\"");
        line.extend_from_slice(id);
        line.extend(unicode::cps("\", standing=<0 to 1>)"));
        out.push(line);
    }
    out
}

/// `held_line(ids)`.
pub fn held_line(ids: &[PyStr]) -> PyStr {
    if ids.is_empty() {
        return Vec::new();
    }
    let shown = &ids[..ids.len().min(BATCH)];
    let more = ids.len() - shown.len();
    let noun = if ids.len() == 1 {
        "memory is"
    } else {
        "memories are"
    };
    let mut out = unicode::cps(&format!(
        "bettermemory: {} standing {noun} held out of force until read: \
memory_update(id, standing=<0 to 1>) on ",
        ids.len()
    ));
    for (i, id) in shown.iter().enumerate() {
        if i > 0 {
            out.extend(unicode::cps(", "));
        }
        out.extend_from_slice(id);
    }
    if more > 0 {
        out.extend(unicode::cps(&format!(" ({more} more)")));
    }
    out.push(u32::from('.'));
    out
}

/// `question(record_id)`: the dict, in the reference's key order.
pub fn question(record_id: &[u32]) -> PyValue {
    let mut hint = unicode::cps(
        "This memory has no standing answer. Give the probability that \
the statement holds of it: memory_update(id=\"",
    );
    hint.extend_from_slice(record_id);
    hint.extend(unicode::cps("\", standing=<0 to 1>)."));
    PyValue::Dict(vec![
        (PyValue::str("type"), PyValue::str("probability")),
        (
            PyValue::str("instructions"),
            PyValue::str(STANDING_STATEMENT),
        ),
        (PyValue::str("hint"), PyValue::Str(hint)),
    ])
}

/// `batch_hint(remaining)`.
pub fn batch_hint(remaining: i128) -> String {
    if remaining <= 0 {
        return "No memory here waits for a standing answer.".to_owned();
    }
    format!(
        "Give each record the probability that the statement holds of its \
text, in one call: {}, {ANSWER_FORMAT}). The next records come back with the reply.",
        &CALL[..CALL.len() - 1]
    )
}
