//! `health_summary.py` 22-85: the two answers `memory_admin(action=
//! "health")` gives from one report, over the report as an ordered
//! `PyValue::Dict`.

use crate::json::{self, DumpOptions};
use crate::pyvalue::{self, PyError, PyStr, PyValue};
use crate::unicode;

/// `SUMMARY_IDS`: the ids (or scopes) each attention row names.
pub const SUMMARY_IDS: usize = 3;

/// `HEADLINE_KEYS`, in order.
pub const HEADLINE_KEYS: [&str; 5] = [
    "generated_at",
    "window_days",
    "total_active_memories",
    "total_events",
    "distinct_sessions",
];

/// `json.dumps(value, separators=(",", ":"), default=str)`.
const SECTION_DUMP: DumpOptions = DumpOptions {
    sort_keys: false,
    ensure_ascii: true,
    item_separator: ",",
    key_separator: ":",
    indent: None,
    default_str: true,
};

/// An exception the reference raises: its class, and `str(exc)` where
/// the port spells it (KeyError and ValueError).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HealthError {
    pub name: &'static str,
    pub text: Option<PyStr>,
}

impl HealthError {
    fn bare(name: &'static str) -> Self {
        HealthError { name, text: None }
    }
}

impl From<PyError> for HealthError {
    fn from(e: PyError) -> Self {
        HealthError::bare(e.name())
    }
}

/// `report.get`: a report that is not a dict has no `get`.
fn items(report: &PyValue) -> Result<&[(PyValue, PyValue)], HealthError> {
    match report {
        PyValue::Dict(items) => Ok(items),
        _ => Err(HealthError::bare("AttributeError")),
    }
}

/// `for name in report`: a dict's keys, a list's or tuple's items, a
/// string's characters; anything else is TypeError.
fn iterated(report: &PyValue) -> Result<Vec<PyValue>, HealthError> {
    match report {
        PyValue::Dict(_) | PyValue::List(_) | PyValue::Tuple(_) | PyValue::Str(_) => listed(report),
        _ => Err(HealthError::bare("TypeError")),
    }
}

/// `report[name]` for a name the report iterated: a dict answers the
/// value of the key (the last of a repeated key); a list, tuple or string
/// takes an int or bool index (IndexError out of range) and is TypeError
/// for any other.
fn indexed(report: &PyValue, name: &PyValue) -> Result<PyValue, HealthError> {
    let seq: Vec<PyValue> = match report {
        PyValue::Dict(items) => {
            return items
                .iter()
                .rev()
                .find_map(|(k, v)| (k == name).then(|| v.clone()))
                .ok_or_else(|| HealthError::bare("KeyError"));
        }
        PyValue::List(v) | PyValue::Tuple(v) => v.clone(),
        PyValue::Str(_) => listed(report)?,
        _ => return Err(HealthError::bare("TypeError")),
    };
    let index: i128 = match name {
        PyValue::Bool(b) => i128::from(*b),
        PyValue::Int(text) => text.parse().map_err(|_| HealthError::bare("IndexError"))?,
        _ => return Err(HealthError::bare("TypeError")),
    };
    let len = seq.len() as i128;
    let at = if index < 0 { index + len } else { index };
    if at < 0 || at >= len {
        return Err(HealthError::bare("IndexError"));
    }
    Ok(seq[at as usize].clone())
}

/// `key in report`: a dict's keys, a list's or tuple's items, a substring
/// of a string.
fn contains(report: &PyValue, key: &[u32]) -> Result<bool, HealthError> {
    match report {
        PyValue::Dict(items) => Ok(items
            .iter()
            .any(|(k, _)| matches!(k, PyValue::Str(s) if s == key))),
        PyValue::List(v) | PyValue::Tuple(v) => {
            Ok(v.iter().any(|e| matches!(e, PyValue::Str(s) if s == key)))
        }
        PyValue::Str(s) => Ok(key.is_empty() || s.windows(key.len()).any(|w| w == key)),
        _ => Err(HealthError::bare("TypeError")),
    }
}

fn is_headline(key: &PyValue) -> bool {
    matches!(key, PyValue::Str(s) if HEADLINE_KEYS.iter().any(|h| unicode::cps(h) == *s))
}

fn lookup<'a>(items: &'a [(PyValue, PyValue)], key: &str) -> Option<&'a PyValue> {
    let key = unicode::cps(key);
    items.iter().rev().find_map(|(k, v)| match k {
        PyValue::Str(s) if *s == key => Some(v),
        _ => None,
    })
}

/// `bool(value)`.
fn truthy(value: &PyValue) -> bool {
    match value {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(text) => text != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) => !s.is_empty(),
        PyValue::Tuple(v) | PyValue::List(v) => !v.is_empty(),
        PyValue::Dict(v) => !v.is_empty(),
        PyValue::DateTime(_) | PyValue::Path(_) => true,
    }
}

/// `list(value)`.
fn listed(value: &PyValue) -> Result<Vec<PyValue>, HealthError> {
    match value {
        PyValue::Tuple(v) | PyValue::List(v) => Ok(v.clone()),
        PyValue::Dict(v) => Ok(v.iter().map(|(k, _)| k.clone()).collect()),
        PyValue::Str(s) => Ok(s.iter().map(|&c| PyValue::Str(vec![c])).collect()),
        _ => Err(HealthError::bare("TypeError")),
    }
}

/// `_empty`: None or an empty list or dict; a zero or a False is a value.
fn empty(value: &PyValue) -> bool {
    match value {
        PyValue::None => true,
        PyValue::List(v) => v.is_empty(),
        PyValue::Dict(v) => v.is_empty(),
        _ => false,
    }
}

fn key_error(key: &str) -> HealthError {
    HealthError {
        name: "KeyError",
        text: Some(pyvalue::repr_str(&unicode::cps(key))),
    }
}

/// `", ".join(texts)`: a part that is not a `str` is TypeError.
fn joined(parts: &[PyValue]) -> Result<PyStr, HealthError> {
    let mut out = Vec::new();
    for (i, part) in parts.iter().enumerate() {
        let PyValue::Str(s) = part else {
            return Err(HealthError::bare("TypeError"));
        };
        if i > 0 {
            out.extend(unicode::cps(", "));
        }
        out.extend(s);
    }
    Ok(out)
}

/// `str.split(", ")`.
fn split_comma_space(text: &[u32]) -> Vec<PyStr> {
    let mut parts = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i + 1 < text.len() {
        if text[i] == 0x2C && text[i + 1] == 0x20 {
            parts.push(text[start..i].to_vec());
            i += 2;
            start = i;
        } else {
            i += 1;
        }
    }
    parts.push(text[start..].to_vec());
    parts
}

/// `_attention`: `<kind> <count>: <action> [<ids or scopes>]`.
fn attention(recommendation: &PyValue) -> Result<PyStr, HealthError> {
    let PyValue::Dict(rec) = recommendation else {
        return Err(HealthError::bare("AttributeError"));
    };
    let mut named = match lookup(rec, "memory_ids") {
        Some(ids) if truthy(ids) => listed(ids)?,
        _ => Vec::new(),
    };
    named.truncate(SUMMARY_IDS);
    if let Some(scope) = lookup(rec, "scope").filter(|s| truthy(s)) {
        let text = pyvalue::py_str(scope)?;
        named = split_comma_space(&text)
            .into_iter()
            .take(SUMMARY_IDS)
            .map(PyValue::Str)
            .collect();
    }
    let mut line = Vec::new();
    for (key, after) in [("kind", " "), ("count", ": "), ("action", "")] {
        let value = lookup(rec, key).ok_or_else(|| key_error(key))?;
        line.extend(pyvalue::py_str(value)?);
        line.extend(unicode::cps(after));
    }
    if named.is_empty() {
        return Ok(line);
    }
    line.extend(unicode::cps(" ["));
    line.extend(joined(&named)?);
    line.push(0x5D);
    Ok(line)
}

/// `section_names`: every key of the report that is not a headline key.
pub fn section_names(report: &PyValue) -> Result<Vec<PyValue>, HealthError> {
    Ok(iterated(report)?
        .into_iter()
        .filter(|k| !is_headline(k))
        .collect())
}

/// `health_summary`: the headline keys present, `attention`, then
/// `section_chars` (each non-empty section's compact JSON length).
pub fn health_summary(report: &PyValue) -> Result<PyValue, HealthError> {
    let mut sections = Vec::new();
    for name in section_names(report)? {
        let value = indexed(report, &name)?;
        if !empty(&value) {
            let text = json::dumps(&value, SECTION_DUMP)?;
            sections.push((name.clone(), PyValue::int(text.len() as i128)));
        }
    }
    let mut out = Vec::new();
    for key in HEADLINE_KEYS {
        let key_cps = unicode::cps(key);
        if contains(report, &key_cps)? {
            out.push((PyValue::str(key), indexed(report, &PyValue::Str(key_cps))?));
        }
    }
    let items = items(report)?;
    let mut lines = Vec::new();
    if let Some(recs) = lookup(items, "recommendations").filter(|r| truthy(r)) {
        let recs = match recs {
            PyValue::List(v) | PyValue::Tuple(v) => v.clone(),
            PyValue::Str(_) | PyValue::Dict(_) => listed(recs)?,
            _ => return Err(HealthError::bare("TypeError")),
        };
        for rec in &recs {
            lines.push(PyValue::Str(attention(rec)?));
        }
    }
    out.push((PyValue::str("attention"), PyValue::List(lines)));
    out.push((PyValue::str("section_chars"), PyValue::Dict(sections)));
    Ok(PyValue::Dict(out))
}

/// `str.strip()`: Python's whitespace (`str.isspace`) off both ends.
fn strip(text: &[u32]) -> &[u32] {
    let start = text.iter().position(|&c| !unicode::is_space(c));
    let Some(start) = start else {
        return &[];
    };
    let end = text
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .unwrap_or(start);
    &text[start..=end]
}

/// `health_section`: the sections `name` lists (separated by commas),
/// each under its own key; ValueError naming the sections for a name the
/// report has not, or for no name at all.
pub fn health_section(report: &PyValue, name: &[u32]) -> Result<PyValue, HealthError> {
    let names = section_names(report)?;
    let asked: Vec<&[u32]> = name
        .split(|&c| c == 0x2C)
        .map(strip)
        .filter(|p| !p.is_empty())
        .collect();
    let known = |part: &[u32]| {
        names
            .iter()
            .any(|n| matches!(n, PyValue::Str(s) if s == part))
    };
    let unknown = asked.iter().find(|p| !known(p));
    if unknown.is_some() || asked.is_empty() {
        let mut text = unicode::cps("health has no section ");
        text.extend(pyvalue::repr_str(unknown.copied().unwrap_or(name)));
        text.extend(unicode::cps("; the sections are "));
        text.extend(joined(&names)?);
        return Err(HealthError {
            name: "ValueError",
            text: Some(text),
        });
    }
    let mut out: Vec<(PyValue, PyValue)> = Vec::new();
    for part in asked {
        if out
            .iter()
            .any(|(k, _)| matches!(k, PyValue::Str(s) if s == part))
        {
            continue;
        }
        let value = indexed(report, &PyValue::Str(part.to_vec()))?;
        out.push((PyValue::Str(part.to_vec()), value));
    }
    Ok(PyValue::Dict(out))
}
