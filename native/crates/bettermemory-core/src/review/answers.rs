//! The answers of `review.py` (unit review-2): `ReviewAnswers` with
//! `to_event` and `is_zero_attestation`, the readers `parse_answers` and
//! `parse_update_answers` with `_score` and `_chain_keys`, their teaching
//! errors, and the two checks `check_answered_ids` and `check_named`.
//!
//! `raw` is the `review` argument as the JSON reader gives it. Nothing
//! here reads the store, the clock or the environment.

use std::collections::{HashMap, HashSet};

use super::{LABELS, LABEL_CHOICES, LABEL_UNRELATED};
use crate::pyvalue::{self, cmp_keys, PyError, PyStr, PyValue};
use crate::records::is_valid_ulid;
use crate::unicode;

/// `ANSWER_FORMAT`.
pub fn answer_format() -> String {
    format!(
        r#"review={{"review_id": <id>, "relation": {{<related id>: {LABEL_CHOICES}}}, "unsafe": <0 to 1>, "secret": <0 to 1>, "standing": <0 to 1>}}"#
    )
}

/// `NAMED_FORMAT`: the roster's answer, as its hint and errors write it.
pub const NAMED_FORMAT: &str = r#""named": [<roster id>, ...] ([] when the write touches none)"#;

/// `UPDATE_ANSWER_FORMAT`: the answers of an update's standing roster.
pub fn update_answer_format() -> String {
    format!(
        r#"review={{"review_id": <id>, "named": [<roster id>, ...], "relation": {{<named id>: {LABEL_CHOICES}}}}}"#
    )
}

const ANSWER_KEYS: [&str; 7] = [
    "review_id",
    "relation",
    "unsafe",
    "secret",
    "standing",
    "said",
    "named",
];
const UPDATE_ANSWER_KEYS: [&str; 3] = ["review_id", "relation", "named"];

/// What a reader or a check raises: `ReviewAnswerError` with its text, or
/// another exception of the reference (`float` of an int past the float
/// range is OverflowError; `repr` and `sorted` may raise).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AnswerFault {
    Teach(PyStr),
    Py(PyError),
}

impl From<PyError> for AnswerFault {
    fn from(e: PyError) -> Self {
        AnswerFault::Py(e)
    }
}

type R<T> = Result<T, AnswerFault>;

/// The message of a teaching error, built from text and code points.
struct Msg(PyStr);

impl Msg {
    fn new() -> Self {
        Msg(Vec::new())
    }
    fn s(mut self, text: &str) -> Self {
        self.0.extend(unicode::cps(text));
        self
    }
    fn p(mut self, text: &[u32]) -> Self {
        self.0.extend_from_slice(text);
        self
    }
    fn err<T>(self) -> R<T> {
        Err(AnswerFault::Teach(self.0))
    }
}

/// `ReviewAnswers`: one call's answers. `relation` keeps insertion order.
#[derive(Clone, Debug, PartialEq)]
pub struct ReviewAnswers {
    pub relation: Vec<(PyStr, PyStr)>,
    pub r#unsafe: f64,
    pub secret: f64,
    pub review_id: Option<PyStr>,
    pub standing: Option<f64>,
    pub said: Option<PyStr>,
    pub named: Option<Vec<PyStr>>,
}

impl ReviewAnswers {
    /// `to_event()`: relation, unsafe, secret, then standing, said and
    /// named where given, in the reference's key order.
    pub fn to_event(&self) -> PyValue {
        let relation = self
            .relation
            .iter()
            .map(|(k, v)| (PyValue::Str(k.clone()), PyValue::Str(v.clone())))
            .collect();
        let mut out = vec![
            (PyValue::str("relation"), PyValue::Dict(relation)),
            (PyValue::str("unsafe"), PyValue::Float(self.r#unsafe)),
            (PyValue::str("secret"), PyValue::Float(self.secret)),
        ];
        if let Some(s) = self.standing {
            out.push((PyValue::str("standing"), PyValue::Float(s)));
        }
        if let Some(s) = &self.said {
            out.push((PyValue::str("said"), PyValue::Str(s.clone())));
        }
        if let Some(n) = &self.named {
            let items = n.iter().cloned().map(PyValue::Str).collect();
            out.push((PyValue::str("named"), PyValue::List(items)));
        }
        PyValue::Dict(out)
    }

    /// `is_zero_attestation()`: both scores 0 and every label unrelated.
    pub fn is_zero_attestation(&self) -> bool {
        let unrelated = unicode::cps(LABEL_UNRELATED);
        self.r#unsafe == 0.0
            && self.secret == 0.0
            && self.relation.iter().all(|(_, l)| *l == unrelated)
    }
}

/// `type(value).__name__`.
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

/// `float(int)`: correctly rounded (Rust's parse rounds half to even, as
/// CPython does), OverflowError past the float range.
fn int_to_float(text: &str) -> Result<f64, PyError> {
    match text.parse::<f64>() {
        Ok(x) if x.is_finite() => Ok(x),
        _ => Err(PyError::OverflowError),
    }
}

/// `_score(raw, key)`.
fn score(raw: &PyValue, key: &str) -> R<f64> {
    let fmt = answer_format();
    let Some(value) = raw.get(key) else {
        return Msg::new()
            .s(&format!(
                "review.{key} is missing: answer it with a number from 0 to 1. Format: {fmt}"
            ))
            .err();
    };
    let number = match value {
        PyValue::Int(t) => int_to_float(t)?,
        PyValue::Float(f) => *f,
        other => {
            let name = type_name(other);
            return Msg::new()
                .s(&format!(
                    "review.{key} must be a number from 0 to 1, got {name}. Format: {fmt}"
                ))
                .err();
        }
    };
    if !number.is_finite() || !(0.0..=1.0).contains(&number) {
        return Msg::new()
            .s(&format!("review.{key} must be from 0 to 1, got "))
            .p(&pyvalue::repr(value)?)
            .s(&format!(". Format: {fmt}"))
            .err();
    }
    Ok(number)
}

type Chain = (Option<PyStr>, Vec<(PyStr, PyStr)>, Option<Vec<PyStr>>);

/// `_chain_keys(raw, fmt)`: the review_id, the relation and `named`.
fn chain_keys(raw: &PyValue, fmt: &str) -> R<Chain> {
    let review_id = match raw.get("review_id") {
        None | Some(PyValue::None) => None,
        Some(PyValue::Str(s)) if is_valid_ulid(s) => Some(s.clone()),
        Some(other) => {
            return Msg::new()
                .s("review.review_id must be the review_id a review returned, got ")
                .p(&pyvalue::repr(other)?)
                .s(&format!(". Omit it if you have none. Format: {fmt}"))
                .err();
        }
    };
    let items: &[(PyValue, PyValue)] = match raw.get("relation") {
        None | Some(PyValue::None) => &[],
        Some(PyValue::Dict(items)) => items,
        Some(_) => {
            return Msg::new()
                .s(&format!(
                    "review.relation must be an object of id to label. Format: {fmt}"
                ))
                .err();
        }
    };
    let mut relation: Vec<(PyStr, PyStr)> = Vec::new();
    let mut at: HashMap<&PyStr, usize> = HashMap::new();
    for (k, v) in items {
        let (PyValue::Str(id), PyValue::Str(label)) = (k, v) else {
            return Msg::new()
                .s(&format!(
                    "review.relation maps ids to labels. Format: {fmt}"
                ))
                .err();
        };
        if !LABELS.iter().any(|l| unicode::cps(l) == *label) {
            return Msg::new()
                .s("review.relation[")
                .p(&pyvalue::repr_str(id))
                .s("] is ")
                .p(&pyvalue::repr_str(label))
                .s(&format!(
                    ", which is not a label; use one of {}. Format: {fmt}",
                    LABELS.join(", ")
                ))
                .err();
        }
        match at.get(id) {
            Some(&n) => relation[n].1 = label.clone(),
            None => {
                at.insert(id, relation.len());
                relation.push((id.clone(), label.clone()));
            }
        }
    }
    let named = match raw.get("named") {
        None | Some(PyValue::None) => None,
        Some(PyValue::List(xs) | PyValue::Tuple(xs))
            if xs.iter().all(|x| matches!(x, PyValue::Str(_))) =>
        {
            let mut seen = HashSet::new();
            let mut out = Vec::new();
            for x in xs {
                if let PyValue::Str(s) = x {
                    if seen.insert(s.clone()) {
                        out.push(s.clone());
                    }
                }
            }
            Some(out)
        }
        Some(_) => {
            return Msg::new()
                .s(&format!(
                    "review.named is a list of roster ids. Format: {NAMED_FORMAT}"
                ))
                .err();
        }
    };
    Ok((review_id, relation, named))
}

/// `sorted(set(raw) - keys)`, as a list value.
fn unknown_keys(items: &[(PyValue, PyValue)], keys: &[&str]) -> R<Vec<PyValue>> {
    let keys: Vec<PyStr> = keys.iter().map(|k| unicode::cps(k)).collect();
    let mut out: Vec<PyValue> = Vec::new();
    for (k, _) in items {
        if !matches!(k, PyValue::Str(s) if keys.contains(s)) {
            out.push(k.clone());
        }
    }
    let mut refused = false;
    out.sort_by(|a, b| {
        cmp_keys(a, b).unwrap_or_else(|| {
            refused = true;
            std::cmp::Ordering::Equal
        })
    });
    if refused {
        return Err(AnswerFault::Py(PyError::TypeError));
    }
    out.dedup();
    Ok(out)
}

/// The shared opening of both readers: None for None and `{}`.
fn opening<'a>(raw: Option<&'a PyValue>, fmt: &str) -> R<Option<&'a [(PyValue, PyValue)]>> {
    match raw {
        None | Some(PyValue::None) => Ok(None),
        Some(PyValue::Dict(items)) if items.is_empty() => Ok(None),
        Some(PyValue::Dict(items)) => Ok(Some(items)),
        Some(_) => Msg::new()
            .s(&format!("review must be an object. Format: {fmt}"))
            .err(),
    }
}

/// `parse_answers(raw)`.
pub fn parse_answers(raw: Option<&PyValue>) -> R<Option<ReviewAnswers>> {
    let fmt = answer_format();
    let Some(items) = opening(raw, &fmt)? else {
        return Ok(None);
    };
    let raw = raw.expect("a dict");
    let unknown = unknown_keys(items, &ANSWER_KEYS)?;
    if !unknown.is_empty() {
        return Msg::new()
            .s("review carries unknown keys ")
            .p(&pyvalue::repr(&PyValue::List(unknown))?)
            .s(&format!(". Format: {fmt}"))
            .err();
    }
    let (review_id, relation, named) = chain_keys(raw, &fmt)?;
    let said = match raw.get("said") {
        None | Some(PyValue::None) => None,
        Some(PyValue::Str(s)) if s.iter().any(|&c| !unicode::is_space(c)) => Some(s),
        Some(_) => {
            return Msg::new()
                .s("review.said quotes the user's words as they typed them, as a string; omit it \
                    when the user said nothing about this. Format: \"said\": \"<the user's words>\"")
                .err();
        }
    };
    let r#unsafe = score(raw, "unsafe")?;
    let secret = score(raw, "secret")?;
    let standing = score(raw, "standing")?;
    let said = said.map(|s| {
        let words: Vec<&[u32]> = s
            .split(|&c| unicode::is_space(c))
            .filter(|w| !w.is_empty())
            .collect();
        words.join(&0x20)
    });
    Ok(Some(ReviewAnswers {
        relation,
        r#unsafe,
        secret,
        review_id,
        standing: Some(standing),
        said,
        named,
    }))
}

/// `parse_update_answers(raw)`.
pub fn parse_update_answers(raw: Option<&PyValue>) -> R<Option<ReviewAnswers>> {
    let fmt = update_answer_format();
    let Some(items) = opening(raw, &fmt)? else {
        return Ok(None);
    };
    let raw = raw.expect("a dict");
    let unknown = unknown_keys(items, &UPDATE_ANSWER_KEYS)?;
    if !unknown.is_empty() {
        return Msg::new()
            .s("review carries keys an update does not read: ")
            .p(&pyvalue::repr(&PyValue::List(unknown))?)
            .s(&format!(
                ". The standing answer and the user's words are the call's own standing and said. \
                 Format: {fmt}"
            ))
            .err();
    }
    let (review_id, relation, named) = chain_keys(raw, &fmt)?;
    Ok(Some(ReviewAnswers {
        relation,
        r#unsafe: 0.0,
        secret: 0.0,
        review_id,
        standing: None,
        said: None,
        named,
    }))
}

fn sorted_list(mut ids: Vec<PyStr>) -> R<PyStr> {
    ids.sort();
    Ok(pyvalue::repr(&PyValue::List(
        ids.into_iter().map(PyValue::Str).collect(),
    ))?)
}

/// `check_answered_ids(answers, allowed)`.
pub fn check_answered_ids(answers: &ReviewAnswers, allowed: &[PyStr]) -> R<()> {
    let allowed: HashSet<&PyStr> = allowed.iter().collect();
    let foreign: Vec<PyStr> = answers
        .relation
        .iter()
        .filter(|(i, _)| !allowed.contains(i))
        .map(|(i, _)| i.clone())
        .collect();
    if foreign.is_empty() {
        return Ok(());
    }
    Msg::new()
        .s("review.relation names ids no review of this write showed: ")
        .p(&sorted_list(foreign)?)
        .s(&format!(
            ". Answer only the related ids the review listed. Format: {}",
            answer_format()
        ))
        .err()
}

/// `check_named(answers, roster, shown=, required=)`: the named ids.
pub fn check_named(
    answers: &ReviewAnswers,
    roster: &HashSet<PyStr>,
    shown: &HashSet<PyStr>,
    required: bool,
) -> R<Vec<PyStr>> {
    let named = match &answers.named {
        Some(n) => n.clone(),
        None if required => {
            return Msg::new()
                .s(&format!(
                    "review.named is missing: this write was shown the standing roster. Keep every \
                     answer and add the roster ids the write restates, amends, extends or competes \
                     with. Format: {NAMED_FORMAT}"
                ))
                .err();
        }
        None => Vec::new(),
    };
    let foreign: Vec<PyStr> = named
        .iter()
        .filter(|i| !roster.contains(*i))
        .cloned()
        .collect();
    let named_set: HashSet<&PyStr> = named.iter().collect();
    if !foreign.is_empty() {
        return Msg::new()
            .s("review.named names ids the roster did not list: ")
            .p(&sorted_list(foreign)?)
            .s(&format!(
                ". Name only ids from the roster. Format: {NAMED_FORMAT}"
            ))
            .err();
    }
    let unnamed: Vec<PyStr> = answers
        .relation
        .iter()
        .map(|(i, _)| i)
        .filter(|i| roster.contains(*i) && !named_set.contains(i) && !shown.contains(*i))
        .cloned()
        .collect();
    if !unnamed.is_empty() {
        return Msg::new()
            .s("review.relation answers roster ids not named: ")
            .p(&sorted_list(unnamed)?)
            .s(&format!(
                ". Add them to review.named; each named record is shown in full before its answer \
                 counts. Format: {NAMED_FORMAT}"
            ))
            .err();
    }
    Ok(named)
}
