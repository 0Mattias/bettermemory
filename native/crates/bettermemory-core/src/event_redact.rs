//! The telemetry recorder's query redaction and named event fields
//! (`events.py`, unit 2): `_SECRET_PATTERNS`, `_strip_known_secrets`,
//! `redact_query`, `NAMED_EVENT_FIELDS`, `named_event_fields`,
//! `_named_inside`, `_named_entries` and `_redact_event_fields`.
//!
//! The second pass of the strip reads `credentials.secret_spans`, which
//! is not built: it is the `spans` argument, called on the text the five
//! patterns left and answering `(start, end, kind)` in code points, as
//! the reference's detectors do (disjoint, sorted by start).

use std::sync::OnceLock;

use sha2::{Digest, Sha256};

use crate::control_tokens::named_tokens;
use crate::events::{QUERY_HASH_PREFIX, QUERY_PREVIEW_CHARS, REDACTED_TEXT_FIELDS};
use crate::pyre::regex::Regex;
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::cps;

/// `credentials.secret_spans`, passed in.
pub type Spans<'a> = &'a mut dyn FnMut(&[u32]) -> Vec<(usize, usize, PyStr)>;

/// `_SECRET_PATTERNS`: each pattern's text (no flags) and its marker, in
/// the order they run.
pub const SECRET_PATTERNS: [(&str, &str); 5] = [
    (r"\bsk-ant-[A-Za-z0-9_-]{20,}\b", "[REDACTED:anthropic-key]"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "[REDACTED:openai-key]"),
    (r"\bghp_[A-Za-z0-9]{30,}\b", "[REDACTED:github-token]"),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "[REDACTED:github-pat]"),
    (r"\bAKIA[A-Z0-9]{16}\b", "[REDACTED:aws-access-key]"),
];

/// `NAMED_EVENT_FIELDS`: a field named whole (None) or by the keys inside
/// it, sorted by name.
pub const NAMED_EVENT_FIELDS: &[(&str, Option<&[&str]>)] = &[
    ("claim_excerpts", None),
    ("client_filter", None),
    ("model_filter", None),
    ("note", None),
    ("probe_query", None),
    ("query", None),
    ("reason", None),
    ("review_answers", Some(&["said"])),
    ("said", None),
    ("top_hits", Some(&["snippet"])),
];

fn patterns() -> &'static [(Regex, PyStr); 5] {
    static BUILT: OnceLock<[(Regex, PyStr); 5]> = OnceLock::new();
    BUILT.get_or_init(|| {
        SECRET_PATTERNS.map(|(p, marker)| {
            let re = Regex::new_str(p, 0).expect("the engine builds _SECRET_PATTERNS");
            (re, cps(marker))
        })
    })
}

/// `text[from:to]` with Python's clamping of slice bounds.
fn slice(text: &[u32], from: usize, to: usize) -> &[u32] {
    let to = to.min(text.len());
    text.get(from.min(to)..to).unwrap_or_default()
}

/// `_strip_known_secrets(text)`: the five patterns' `sub` in order, then
/// each span `spans` sees in what is left as `[REDACTED:<kind>]`, the
/// pieces joined once.
pub fn strip_known_secrets(text: &[u32], spans: Spans) -> PyStr {
    let mut text = text.to_vec();
    for (re, marker) in patterns() {
        text = re
            .sub(marker, &text, 0)
            .expect("a bounded pattern with a literal marker");
    }
    let found = spans(&text);
    if found.is_empty() {
        return text;
    }
    let mut out = Vec::with_capacity(text.len());
    let mut cursor = 0;
    for (start, end, kind) in found {
        out.extend_from_slice(slice(&text, cursor, start));
        out.extend(cps("[REDACTED:"));
        out.extend_from_slice(&kind);
        out.push(u32::from(b']'));
        cursor = end;
    }
    out.extend_from_slice(slice(&text, cursor, text.len()));
    out
}

/// `redact_query(text)`: `{"hash", "preview", "len"}`. The hash is the
/// first sixteen hex digits of SHA-256 over the stripped text's UTF-8,
/// each lone surrogate encoded as `?`; the preview its first 32 code
/// points; `len` the original's length in code points.
pub fn redact_query(text: &[u32], spans: Spans) -> PyValue {
    let stripped = strip_known_secrets(text, spans);
    let utf8: String = stripped
        .iter()
        .map(|&c| char::from_u32(c).unwrap_or('?'))
        .collect();
    let digest: String = Sha256::digest(utf8.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let preview = stripped.iter().take(QUERY_PREVIEW_CHARS).copied().collect();
    PyValue::Dict(vec![
        (
            PyValue::str("hash"),
            PyValue::str(&digest[..QUERY_HASH_PREFIX]),
        ),
        (PyValue::str("preview"), PyValue::Str(preview)),
        (PyValue::str("len"), PyValue::int(text.len() as i128)),
    ])
}

fn is(value: &PyValue, word: &str) -> bool {
    matches!(value, PyValue::Str(s) if *s == cps(word))
}

fn among(value: &PyValue, words: &[&str]) -> bool {
    words.iter().any(|w| is(value, w))
}

/// `_named_entries`: a dict with its entries under `keys` named, anything
/// else as it is.
pub fn named_entries(value: &PyValue, keys: &[&str]) -> PyValue {
    let PyValue::Dict(items) = value else {
        return value.clone();
    };
    PyValue::Dict(
        items
            .iter()
            .map(|(k, v)| {
                let v = if among(k, keys) {
                    named_tokens(v)
                } else {
                    v.clone()
                };
                (k.clone(), v)
            })
            .collect(),
    )
}

/// `_named_inside`: `_named_entries` of a dict, or of each item of a list.
pub fn named_inside(value: &PyValue, keys: &[&str]) -> PyValue {
    match value {
        PyValue::List(items) => {
            PyValue::List(items.iter().map(|i| named_entries(i, keys)).collect())
        }
        other => named_entries(other, keys),
    }
}

/// `named_event_fields(fields)`: each field of `NAMED_EVENT_FIELDS` the
/// dict holds, named in place; every other field as it is, in order. A
/// field the dict lacks is not added (`named_tokens(None)` is None).
pub fn named_event_fields(fields: &[(PyValue, PyValue)]) -> Vec<(PyValue, PyValue)> {
    let mut out = fields.to_vec();
    for (name, inside) in NAMED_EVENT_FIELDS {
        // `dict.get`: a key equal to the name; a dict holds it once.
        let Some(slot) = out.iter_mut().find(|(k, _)| is(k, name)) else {
            continue;
        };
        slot.1 = match inside {
            None => named_tokens(&slot.1),
            Some(keys) => named_inside(&slot.1, keys),
        };
    }
    out
}

/// `_redact_event_fields(fields)`: `query` and `probe_query` that are a
/// `str` through `redact_query`, then `named_event_fields`.
pub fn redact_event_fields(fields: &[(PyValue, PyValue)], spans: Spans) -> Vec<(PyValue, PyValue)> {
    let mut out = fields.to_vec();
    for key in REDACTED_TEXT_FIELDS {
        if let Some((_, value)) = out.iter_mut().find(|(k, _)| is(k, key)) {
            if let PyValue::Str(text) = value {
                *value = redact_query(text, &mut *spans);
            }
        }
    }
    named_event_fields(&out)
}
