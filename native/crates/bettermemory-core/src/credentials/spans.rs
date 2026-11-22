//! Units 3 and 4 of `credentials.py`: the generic `keyword = value` rule
//! and the connection-URI password (`_GENERIC_KEYWORD_RE`,
//! `_URI_USERINFO_RE`, 238-265), each built by the engine from the
//! reference's own pattern text and flags, and the spans, redactions,
//! snippets and markers built on every detector (475-632).
//!
//! Texts are code points and every offset is a code point index, as a
//! `str` match's `start()` and `end()` are.

use std::sync::LazyLock;

use super::gate::looks_like_secret;
use super::prefixed::{is_masked_token, is_spent_run, prefixed_spans, DETECTORS};
use crate::pyre::regex::{Regex, RegexError};
use crate::pyre::tree::IGNORECASE;
use crate::pyvalue::PyStr;
use crate::unicode;

/// `_GENERIC_KEYWORD_RE.pattern`, compiled with `re.IGNORECASE`.
pub const GENERIC_KEYWORD_PATTERN: &str = concat!(
    r"(?:",
    r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,30}[_-]){0,5}",
    r"(?:password|passwd|secret(?:[_-]?(?:access[_-]?)?key)?|api[ _-]?key|",
    r"access[ _-]?token|auth[ _-]?token|client[ _-]?secret|private[ _-]?key|",
    r#"bearer(?:[_-]token)?)\b[\"'*`]*"#,
    r"\s*(?:=>|[:=]=?|\b(?:is|was)(?:\s+(?:set|changed|updated|reset|rotated))?",
    r"(?:\s+(?:to|now))?\b)\s*",
    r"|\bauthorization\s*:\s*bearer\s+",
    r")",
    r#"[\"']?(?P<value>[^\s\"']{8,})[\"']?"#,
);

/// `_URI_USERINFO_RE.pattern`, compiled with `re.IGNORECASE`.
pub const URI_USERINFO_PATTERN: &str =
    r#"\b[a-z][a-z0-9+.\-]{1,30}://[^\s:/@\"']{1,64}:(?P<value>[^\s/@\"']{8,})@"#;

/// The pattern `_safe_snippet` hands to `re.sub`.
pub const SNIPPET_SPACE_PATTERN: &str = r"\s+";

/// `_CONTEXT_CHARS`.
pub const CONTEXT_CHARS: usize = 24;

pub const GENERIC_KIND: &str = "generic-secret-assignment";
pub const URI_KIND: &str = "connection-uri-password";
const PEM_KIND: &str = "private-key-pem";

static GENERIC_KEYWORD_RE: LazyLock<Result<Regex, RegexError>> =
    LazyLock::new(|| Regex::new_str(GENERIC_KEYWORD_PATTERN, IGNORECASE));
static URI_USERINFO_RE: LazyLock<Result<Regex, RegexError>> =
    LazyLock::new(|| Regex::new_str(URI_USERINFO_PATTERN, IGNORECASE));
static SNIPPET_SPACE_RE: LazyLock<Result<Regex, RegexError>> =
    LazyLock::new(|| Regex::new_str(SNIPPET_SPACE_PATTERN, 0));

fn built(re: &'static LazyLock<Result<Regex, RegexError>>) -> &'static Regex {
    re.as_ref()
        .expect("the engine builds the reference's pattern")
}

/// The `value` group's span of every match of `re.finditer(text)`, in
/// order, before the value gate.
fn value_spans(
    re: &'static LazyLock<Result<Regex, RegexError>>,
    text: &[u32],
) -> Vec<(usize, usize)> {
    let re = built(re);
    let value = re
        .names()
        .iter()
        .find(|(_, name)| name.as_slice() == unicode::cps("value").as_slice())
        .map(|(g, _)| *g)
        .expect("the pattern names `value`");
    re.finditer(text, 0, isize::MAX)
        .expect("a str pattern over a str, with no budget")
        .iter()
        .filter_map(|m| m.group(value))
        .map(|(a, b)| (a as usize, b as usize))
        .collect()
}

/// `_GENERIC_KEYWORD_RE.finditer(text)`: each match's `value` span.
pub fn generic_values(text: &[u32]) -> Vec<(usize, usize)> {
    value_spans(&GENERIC_KEYWORD_RE, text)
}

/// `_URI_USERINFO_RE.finditer(text)`: each match's `value` span.
pub fn uri_values(text: &[u32]) -> Vec<(usize, usize)> {
    value_spans(&URI_USERINFO_RE, text)
}

/// The spans of one rule whose value passes `_looks_like_secret`.
fn gated(text: &[u32], spans: Vec<(usize, usize)>) -> impl Iterator<Item = (usize, usize)> + '_ {
    spans
        .into_iter()
        .filter(move |&(a, b)| looks_like_secret(&text[a..b]))
}

/// `_merged_secret_spans` and `secret_spans`: the prefixed spans, then
/// the generic and URI spans whose value passes the gate, sorted stably
/// by start and unioned, each union keeping its first kind.
pub fn secret_spans(text: &[u32]) -> Vec<(usize, usize, &'static str)> {
    let mut spans = prefixed_spans(text);
    spans.extend(gated(text, generic_values(text)).map(|(a, b)| (a, b, GENERIC_KIND)));
    spans.extend(gated(text, uri_values(text)).map(|(a, b)| (a, b, URI_KIND)));
    spans.sort_by_key(|s| s.0);
    let mut merged: Vec<(usize, usize, &'static str)> = Vec::new();
    for (start, end, kind) in spans {
        match merged.last_mut() {
            Some(last) if start < last.1 => last.1 = last.1.max(end),
            _ => merged.push((start, end, kind)),
        }
    }
    merged
}

/// [`secret_spans`] for a caller holding a `&str`.
pub fn secret_spans_str(text: &str) -> Vec<(usize, usize, &'static str)> {
    secret_spans(&unicode::cps(text))
}

fn marker(kind: &str) -> PyStr {
    unicode::cps(&format!("[redacted:{kind}]"))
}

/// `_redact_all`: every span replaced by its marker, right to left.
pub fn redact_all(text: &[u32]) -> PyStr {
    let mut out = text.to_vec();
    for (start, end, kind) in secret_spans(text).into_iter().rev() {
        out.splice(start..end, marker(kind));
    }
    out
}

/// `text[a:b]` with Python's clamping. Indices are `i128`, which holds
/// every `usize` and every sum of two, so nothing wraps.
fn slice(text: &[u32], a: i128, b: i128) -> &[u32] {
    let len = text.len() as i128;
    let (a, b) = (a.clamp(0, len) as usize, b.clamp(0, len) as usize);
    if a < b {
        &text[a..b]
    } else {
        &[]
    }
}

/// `_safe_snippet(text, start, end, kind)`.
pub fn safe_snippet(text: &[u32], start: usize, end: usize, kind: &str) -> PyStr {
    snippet_over(text, &secret_spans(text), start, end, kind)
}

/// `_safe_snippet` with the full text's merged spans already read. The
/// window arithmetic runs in `i128`, as Python's ints do not overflow.
fn snippet_over(
    text: &[u32],
    spans: &[(usize, usize, &'static str)],
    start: usize,
    end: usize,
    kind: &str,
) -> PyStr {
    let len = text.len() as i128;
    let s = (start as i128 - CONTEXT_CHARS as i128).max(0);
    let trailing = if kind == PEM_KIND {
        0
    } else {
        CONTEXT_CHARS as i128
    };
    let e = (end as i128 + trailing).min(len);
    let mut window = slice(text, s, e).to_vec();
    for &(sp_start, sp_end, sp_kind) in spans.iter().rev() {
        let (sp_start, sp_end) = (sp_start as i128, sp_end as i128);
        if sp_end <= s || sp_start >= e {
            continue;
        }
        let w_start = (sp_start - s).max(0);
        let w_end = (sp_end - s).min(window.len() as i128);
        let mut next = slice(&window, 0, w_start).to_vec();
        next.extend(marker(sp_kind));
        next.extend_from_slice(slice(&window, w_end, i128::MAX));
        window = next;
    }
    let lf: Vec<u32> = window
        .iter()
        .map(|&c| {
            if c == u32::from('\n') {
                u32::from(' ')
            } else {
                c
            }
        })
        .collect();
    let spaced = built(&SNIPPET_SPACE_RE)
        .sub(&[u32::from(' ')], &lf, 0)
        .expect("a literal replacement with no budget");
    let mut out = if s > 0 {
        unicode::cps("...")
    } else {
        Vec::new()
    };
    out.extend_from_slice(unicode::py_strip(&spaced));
    if e < len {
        out.extend(unicode::cps("..."));
    }
    out
}

/// `CredentialMatch`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CredentialMatch {
    pub kind: &'static str,
    pub snippet: PyStr,
}

/// `find_credential_markers(content)`: per kind, its first hit with its
/// snippet: the first kept match of each prefixed detector (a kind
/// already seen adds nothing), then the first generic and the first URI
/// value that passes the gate.
pub fn find_credential_markers(content: &[u32]) -> Vec<CredentialMatch> {
    let spans = secret_spans(content);
    let mut hits: Vec<CredentialMatch> = Vec::new();
    let mut add = |kind: &'static str, start: usize, end: usize| {
        if hits.iter().all(|h| h.kind != kind) {
            let snippet = snippet_over(content, &spans, start, end, kind);
            hits.push(CredentialMatch { kind, snippet });
        }
    };
    for d in DETECTORS {
        let first = d
            .find_iter(content)
            .into_iter()
            .find(|m| !is_spent_run(m) && !is_masked_token(content, m));
        if let Some(m) = first {
            add(d.kind(), m.start, m.end);
        }
    }
    if let Some((a, b)) = gated(content, generic_values(content)).next() {
        add(GENERIC_KIND, a, b);
    }
    if let Some((a, b)) = gated(content, uri_values(content)).next() {
        add(URI_KIND, a, b);
    }
    hits
}

/// [`find_credential_markers`] for a caller holding a `&str`.
pub fn find_credential_markers_str(content: &str) -> Vec<CredentialMatch> {
    find_credential_markers(&unicode::cps(content))
}
