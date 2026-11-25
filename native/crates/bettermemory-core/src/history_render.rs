//! `history.py`'s fifth unit: a record's history as `memory history`
//! prints it. `render_text` with its helpers `_actor_text`, `_value_text`
//! and `_VERIFIED_TEXT`, and `render_json`, pure functions over a
//! [`RecordHistory`].
//!
//! Every text is code points ([`PyStr`]), as the reference's `str` is.
//! `render_text` passes each line through `printable` on its own, the
//! header too, after the lines are built and before they are joined: a
//! line feed, an escape sequence or a bidirectional control inside a
//! record id, a provenance label, a row's kind or a detail value is
//! written as its escape and cannot start a line of its own, while the
//! line feeds of a body diff, split before the escaping, each start one.
//! Every category `printable` escapes includes the lone surrogates, so
//! the text holds none. `render_json` writes ASCII (`ensure_ascii`) and
//! is not escaped further.
//!
//! The values a history holds come from `json.loads` of log payloads:
//! `None`, `bool`, `int`, `float`, `str`, `list` and `dict` with `str`
//! keys. Any of them is written. What else a caller could build is
//! answered as the reference answers it, typed: a value `json.dumps`
//! cannot spell (a datetime or a path, a dict whose keys do not sort or
//! are not of a JSON key type, an `int` past 4,300 digits) is its
//! TypeError or ValueError, and an actor that is not a mapping is the
//! AttributeError of `actor.get`. Nesting past [`json::MAX_DEPTH`] is
//! RecursionError, the port's budget (the reference's bound follows the
//! release and the caller's stack: CPython 3.13's C encoder raises on the
//! 9,999th container, 3.11 and 3.12 encode an indented document in Python
//! and stop near a thousand). The budget is one for every value. For a
//! value no store produces whose bottom raises (a datetime or a path, a
//! dict whose keys do not sort or a tuple key, an `int` past 4,300
//! digits) the reference's bound sits one to five levels lower, since the
//! raise spends stack of its own: within those levels the port answers
//! the bottom's TypeError or ValueError, or a detail name's text, where
//! CPython 3.13.13 answers RecursionError.
//!
//! A row's actor of `Some(PyValue::None)`, which no reference row holds
//! (its actor is a mapping or None, and `record_history` keeps only
//! mappings), is read as no actor, as [`HistoryRow::to_dict`] writes it
//! (`null`): both renderers then answer as the reference does for a row
//! whose actor is None.

use crate::history_record::RecordHistory;
use crate::history_rows::{HistoryRow, ACTOR_FROM_RECORD};
use crate::json::{self, DumpOptions};
use crate::printable::printable_into;
use crate::pyvalue::{py_str, PyError, PyStr, PyValue};
use crate::toml::INT_MAX_STR_DIGITS;
use crate::unicode::cps;

/// `_value_text`'s longest text, in code points; a longer one is cut.
pub const VALUE_TEXT_MAX: usize = 80;
/// What a cut text keeps of its start, before `...`.
pub const VALUE_TEXT_KEPT: usize = 77;

/// `json.dumps(..., indent=2)`: insertion order, ASCII, `,` and `: `.
pub const JSON_OPTIONS: DumpOptions = DumpOptions {
    sort_keys: false,
    ..DumpOptions::DOCUMENT_INDENT2
};

/// What `render_text` raises, typed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RenderError {
    /// What `json.dumps` or `str()` of a detail raised.
    Raised(PyError),
    /// AttributeError: `actor.get` of an actor that is not a mapping.
    ActorNotMapping,
}

impl RenderError {
    /// The exception's class name.
    pub fn name(self) -> &'static str {
        match self {
            RenderError::Raised(e) => e.name(),
            RenderError::ActorNotMapping => "AttributeError",
        }
    }
}

impl From<PyError> for RenderError {
    fn from(e: PyError) -> Self {
        RenderError::Raised(e)
    }
}

/// `_VERIFIED_TEXT[verified]`.
pub fn verified_text(verified: Option<bool>) -> &'static str {
    match verified {
        Some(true) => "mac ok",
        Some(false) => "MAC FAILS",
        None => "mac unverifiable",
    }
}

/// The names `_actor_text` reads, in the order it writes them.
const ACTOR_NAMES: [&str; 3] = ["client", "model", "principal"];

/// `_actor_text(actor)`: `name=value` for each of client, model and
/// principal whose value is a non-empty `str`, joined by a space, or
/// `undeclared`. A name given twice is its last, as `json.loads` keeps
/// it. The pairs are read once, from the last, until all three names
/// are met, not once per name.
pub fn actor_text(actor: &PyValue) -> Result<PyStr, RenderError> {
    let PyValue::Dict(items) = actor else {
        return Err(RenderError::ActorNotMapping);
    };
    let names = ACTOR_NAMES.map(cps);
    let mut found: [Option<&PyValue>; 3] = [None; 3];
    let mut left = found.len();
    for (key, value) in items.iter().rev() {
        let PyValue::Str(key) = key else {
            continue;
        };
        let Some(at) = names.iter().position(|name| name == key) else {
            continue;
        };
        if found[at].is_none() {
            found[at] = Some(value);
            left -= 1;
            if left == 0 {
                break;
            }
        }
    }
    let mut out = PyStr::new();
    for (name, value) in ACTOR_NAMES.into_iter().zip(found) {
        let Some(PyValue::Str(value)) = value else {
            continue;
        };
        if value.is_empty() {
            continue;
        }
        if !out.is_empty() {
            out.push(u32::from(b' '));
        }
        out.extend(cps(name));
        out.push(u32::from(b'='));
        out.extend_from_slice(value);
    }
    if out.is_empty() {
        return Ok(cps("undeclared"));
    }
    Ok(out)
}

/// `_value_text(value)`: a `str` as itself, anything else as
/// `json.dumps(value, ensure_ascii=False, sort_keys=True)`; then a text
/// of more than 80 code points is its first 77 and `...`. A `str` is
/// cut from its own code points, as the reference slices it: no more
/// than 80 of them are copied.
pub fn value_text(value: &PyValue) -> Result<PyStr, PyError> {
    Ok(match value {
        PyValue::Str(s) => cut(s),
        _ => cut(&json::dumps(value, DumpOptions::SORTED_UNICODE_SPACED)?),
    })
}

/// `text if len(text) <= 80 else text[:77] + "..."`.
fn cut(text: &[u32]) -> PyStr {
    if text.len() <= VALUE_TEXT_MAX {
        return text.to_vec();
    }
    let mut out = Vec::with_capacity(VALUE_TEXT_KEPT + 3);
    out.extend_from_slice(&text[..VALUE_TEXT_KEPT]);
    out.extend(cps("..."));
    out
}

/// A detail name as an f-string writes it: a `str` as itself, anything
/// else as `str()` of it, which raises ValueError for an `int` past
/// 4,300 digits and RecursionError past the port's budget, the first met
/// in the order `repr` writes.
fn name_text(name: &PyValue) -> Result<PyStr, PyError> {
    if let PyValue::Str(s) = name {
        return Ok(s.clone());
    }
    let mut work = vec![(name, 1usize)];
    while let Some((value, depth)) = work.pop() {
        if depth > json::MAX_DEPTH {
            return Err(PyError::RecursionError);
        }
        match value {
            PyValue::Int(digits) if digits.trim_start_matches('-').len() > INT_MAX_STR_DIGITS => {
                return Err(PyError::ValueError);
            }
            PyValue::List(items) | PyValue::Tuple(items) => {
                work.extend(items.iter().rev().map(|item| (item, depth + 1)));
            }
            PyValue::Dict(items) => {
                for (key, item) in items.iter().rev() {
                    work.push((item, depth + 1));
                    work.push((key, depth + 1));
                }
            }
            _ => {}
        }
    }
    py_str(name)
}

fn push(line: &mut PyStr, text: &str) {
    line.extend(text.chars().map(u32::from));
}

/// One row's line: seq, ts, kind, action and the MAC's verdict, then the
/// actor, the changed fields and each detail item, in that order.
fn row_line(row: &HistoryRow) -> Result<PyStr, RenderError> {
    let mut line = PyStr::new();
    push(
        &mut line,
        &format!(
            "  seq {} {} {} {} [{}]",
            row.seq,
            row.ts,
            row.kind,
            row.action,
            verified_text(row.verified)
        ),
    );
    // `Some(PyValue::None)` is no actor, as `to_dict` writes it.
    if let Some(actor) = row.actor.as_ref().filter(|a| !matches!(a, PyValue::None)) {
        let label = if row.actor_source == Some(ACTOR_FROM_RECORD) {
            " author "
        } else {
            " actor "
        };
        push(&mut line, label);
        line.extend(actor_text(actor)?);
    }
    if !row.changed.is_empty() {
        push(&mut line, " changed ");
        for (i, name) in row.changed.iter().enumerate() {
            if i > 0 {
                line.push(u32::from(b','));
            }
            line.extend_from_slice(name);
        }
    }
    for (name, value) in &row.detail {
        line.push(u32::from(b' '));
        line.extend(name_text(name)?);
        line.push(u32::from(b'='));
        line.extend(value_text(value)?);
    }
    Ok(line)
}

/// `render_text(history, diff=diff)`: the header, the provenance and key
/// lines, one line per row with its body diff under it when `diff` is
/// set and the diff is not empty, and the findings last; each line
/// through `printable`, each followed by a line feed.
pub fn render_text(history: &RecordHistory, diff: bool) -> Result<PyStr, RenderError> {
    let mut out = PyStr::new();
    let mut emit = |line: &[u32]| {
        printable_into(line, &mut out);
        out.push(u32::from(b'\n'));
    };
    let kind = history
        .record_kind
        .filter(|k| !k.is_empty())
        .unwrap_or("record");
    let count = history.rows.len();
    let mut header = cps("bettermemory memory history ");
    header.extend_from_slice(&history.record_id);
    push(
        &mut header,
        &format!(
            ": {kind}, {}, {count} row{}",
            history.state,
            if count == 1 { "" } else { "s" }
        ),
    );
    emit(&header);
    if let Some(provenance) = &history.provenance {
        // Only an active record's label rests on the key.
        let keyed = history.state == "active" && !history.key_present;
        let checked = if keyed { " (unverified: no key)" } else { "" };
        emit(&cps(&format!("  provenance {provenance}{checked}")));
    }
    if !history.key_present {
        emit(&cps("  key not held here: row MACs cannot be checked"));
    }
    for row in &history.rows {
        emit(&row_line(row)?);
        match &row.diff {
            Some(text) if diff && !text.is_empty() => {
                for piece in text.split(|&c| c == u32::from(b'\n')) {
                    let mut line = cps("      ");
                    line.extend_from_slice(piece);
                    emit(&line);
                }
            }
            _ => {}
        }
    }
    let findings = history.findings();
    if !findings.is_empty() {
        emit(&cps("  findings"));
        for finding in &findings {
            emit(&cps(&format!("    {finding}")));
        }
    }
    Ok(out)
}

/// `render_json(history, diff=diff)`: `json.dumps(history.to_dict(diff),
/// indent=2)` and a line feed. Keys in insertion order, ASCII escapes.
pub fn render_json(history: &RecordHistory, diff: bool) -> Result<PyStr, PyError> {
    let mut out = json::dumps(&history.to_dict(diff), JSON_OPTIONS)?;
    out.push(u32::from(b'\n'));
    Ok(out)
}
