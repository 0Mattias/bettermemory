//! The write-payload validators and the declared-claims check of
//! `handlers/_shared.py`: `_validate_scope_count`, `_validate_content_size`,
//! `_validate_content_floor`, `_validate_write_payload` (with
//! `models.validate_scope` and `capture.token_of`), with the module's
//! constants. Nothing here reads the machine.

use std::collections::HashSet;

use crate::pyvalue::{repr_str, PyStr};
use crate::records::{Category, Confidence, Source};
use crate::unicode::{cps, is_space};

/// `_USE_OUTCOMES` (a frozenset in the reference; the order is not read).
pub const USE_OUTCOMES: [&str; 4] = ["applied", "ignored", "contradicted", "corrected"];
/// `_AMBIENT_LONG_BODY_WORDS`.
pub const AMBIENT_LONG_BODY_WORDS: usize = 500;
/// `_NOTE_MAX_LEN`.
pub const NOTE_MAX_LEN: usize = 800;
/// `_WRITE_CATEGORIES`: whether `text` is a category's value.
pub fn is_write_category(text: &[u32]) -> bool {
    Category::MEMBERS.iter().any(|(v, _)| cps(v) == text)
}

/// What a validator raises: the class and the text `str()` gives.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CheckError {
    /// `ValueError`.
    Value(PyStr),
    /// `UnicodeEncodeError` from `content.encode("utf-8")`.
    Encode(PyStr),
}

impl CheckError {
    /// `str()` of the exception.
    pub fn message(&self) -> &[u32] {
        match self {
            CheckError::Value(m) | CheckError::Encode(m) => m,
        }
    }
}

fn value_err(parts: &[&[u32]]) -> CheckError {
    CheckError::Value(parts.concat())
}

fn list_repr<'a>(items: impl Iterator<Item = &'a [u32]>) -> PyStr {
    let mut out = cps("[");
    for (i, item) in items.enumerate() {
        if i > 0 {
            out.extend(cps(", "));
        }
        out.extend(repr_str(item));
    }
    out.push(u32::from(b']'));
    out
}

/// `_validate_scope_count(scopes, max_count)`, given the list's length.
pub fn validate_scope_count(count: usize, max_count: i64) -> Result<(), CheckError> {
    if max_count <= 0 || (count as u64) <= max_count as u64 {
        return Ok(());
    }
    Err(value_err(&[&cps(&format!(
        "too many scopes: {count} entries, more than the limit of {max_count} per write. \
Shorten the scope list."
    ))]))
}

/// `content.encode("utf-8")`: the length, or the `UnicodeEncodeError` the
/// first run of surrogates raises (code-point positions, the run's end
/// inclusive).
pub fn utf8_len(content: &[u32]) -> Result<usize, CheckError> {
    let surrogate = |c: u32| (0xD800..=0xDFFF).contains(&c);
    if let Some(start) = content.iter().position(|&c| surrogate(c)) {
        let run = content[start..]
            .iter()
            .take_while(|&&c| surrogate(c))
            .count();
        let text = if run == 1 {
            format!(
                "'utf-8' codec can't encode character '\\u{:04x}' in position {start}: \
surrogates not allowed",
                content[start]
            )
        } else {
            format!(
                "'utf-8' codec can't encode characters in position {start}-{}: \
surrogates not allowed",
                start + run - 1
            )
        };
        return Err(CheckError::Encode(cps(&text)));
    }
    // No surrogate is left, so every code point is a `char`.
    Ok(content
        .iter()
        .filter_map(|&c| char::from_u32(c))
        .map(char::len_utf8)
        .sum())
}

/// `_validate_content_size(content, max_bytes, field_name=field_name)`.
pub fn validate_content_size(
    content: &[u32],
    max_bytes: i64,
    field_name: &[u32],
) -> Result<(), CheckError> {
    if max_bytes <= 0 {
        return Ok(());
    }
    let size = utf8_len(content)?;
    if (size as u64) <= max_bytes as u64 {
        return Ok(());
    }
    Err(value_err(&[
        field_name,
        &cps(&format!(
            " is too long: {size} bytes of UTF-8, more than the limit of {max_bytes} bytes. \
Shorten the "
        )),
        field_name,
        &cps("."),
    ]))
}

/// `len(content.split())`.
pub fn token_count(content: &[u32]) -> usize {
    content
        .split(|&c| is_space(c))
        .filter(|w| !w.is_empty())
        .count()
}

/// `_validate_content_floor(content, min_tokens)`.
pub fn validate_content_floor(content: &[u32], min_tokens: i64) -> Result<(), CheckError> {
    if min_tokens <= 0 {
        return Ok(());
    }
    let n = token_count(content);
    if (n as u64) >= min_tokens as u64 {
        return Ok(());
    }
    Err(value_err(&[&cps(&format!(
        "content is too short: {n} tokens, fewer than the minimum of {min_tokens}. \
Expand the content."
    ))]))
}

/// `models.validate_scope`: `_SCOPE_RE` (`^[a-z0-9]+(?:[-:][a-z0-9]+)*\Z`)
/// matched by hand over code points (the decision's rule 3: a fixed
/// alternation of ASCII classes with no backtracking that changes the
/// answer). The scope is returned as given.
pub fn validate_scope(scope: &[u32]) -> Result<PyStr, CheckError> {
    let word = |c: u32| (0x61..=0x7A).contains(&c) || (0x30..=0x39).contains(&c);
    let sep = |c: u32| c == u32::from(b'-') || c == u32::from(b':');
    let ok = match (scope.first(), scope.last()) {
        (Some(&a), Some(&z)) => {
            word(a)
                && word(z)
                && scope.iter().all(|&c| word(c) || sep(c))
                && !scope.windows(2).any(|w| sep(w[0]) && sep(w[1]))
        }
        _ => false,
    };
    if ok {
        return Ok(scope.to_vec());
    }
    Err(value_err(&[
        &cps("invalid scope "),
        &repr_str(scope),
        &cps(
            ": must be lowercase alphanumeric, with optional hyphens/colons (e.g. 'projects:foo')",
        ),
    ]))
}

/// The keyword arguments of `_validate_write_payload`.
#[derive(Clone, Debug)]
pub struct WriteArgs<'a> {
    pub content: &'a [u32],
    pub scopes: &'a [PyStr],
    pub confidence: &'a [u32],
    pub source: &'a [u32],
    pub allowed_scopes: &'a [PyStr],
    pub category: &'a [u32],
    pub max_content_bytes: i64,
    pub min_content_tokens: i64,
    pub max_scopes_per_write: i64,
    pub capture: bool,
}

/// The dict `_validate_write_payload` returns.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WritePayload {
    pub content: PyStr,
    pub scopes: Vec<PyStr>,
    pub confidence: Confidence,
    pub source: Source,
    pub category: Category,
}

fn member<T: Copy>(members: &[(&str, T)], text: &[u32]) -> Option<T> {
    members
        .iter()
        .find(|(v, _)| cps(v) == text)
        .map(|&(_, m)| m)
}

fn values<T>(members: &[(&'static str, T)]) -> PyStr {
    let v: Vec<PyStr> = members.iter().map(|(v, _)| cps(v)).collect();
    list_repr(v.iter().map(|s| s.as_slice()))
}

/// `_validate_write_payload`, its checks in the reference's order (empty
/// content, empty scopes, the floor, the size, the scope count, each
/// scope, the allowed list, confidence, source, category).
pub fn validate_write_payload(a: &WriteArgs<'_>) -> Result<WritePayload, CheckError> {
    if a.content.iter().all(|&c| is_space(c)) {
        return Err(value_err(&[&cps("content must be a non-empty string")]));
    }
    if a.scopes.is_empty() {
        return Err(value_err(&[&cps("scopes must contain at least one entry")]));
    }
    validate_content_floor(a.content, a.min_content_tokens)?;
    validate_content_size(a.content, a.max_content_bytes, &cps("content"))?;
    validate_scope_count(a.scopes.len(), a.max_scopes_per_write)?;
    for scope in a.scopes {
        validate_scope(scope)?;
    }
    if !a.allowed_scopes.is_empty() {
        let allowed: HashSet<&[u32]> = a.allowed_scopes.iter().map(|s| s.as_slice()).collect();
        let unknown: Vec<&[u32]> = a
            .scopes
            .iter()
            .filter(|s| !allowed.contains(s.as_slice()))
            .map(|s| s.as_slice())
            .collect();
        if !unknown.is_empty() {
            let mut sorted: Vec<&[u32]> = a.allowed_scopes.iter().map(|s| s.as_slice()).collect();
            sorted.sort();
            return Err(value_err(&[
                &cps("scope(s) not in allowed list: "),
                &list_repr(unknown.into_iter()),
                &cps(". Allowed: "),
                &list_repr(sorted.into_iter()),
            ]));
        }
    }
    let confidence = member(Confidence::MEMBERS, a.confidence).ok_or_else(|| {
        value_err(&[
            &cps("confidence must be one of "),
            &values(Confidence::MEMBERS),
        ])
    })?;
    // `capture.token_of(source) is not None`: the source opens with
    // `SOURCE_PREFIX` ("capture:"); the token itself is not read here.
    let source = if a.source.starts_with(&cps("capture:")) {
        if !a.capture {
            return Err(value_err(&[
                &cps("source "),
                &repr_str(a.source),
                &cps(
                    " names a capture batch, which only memory_write accepts, and only while \
the batch is open",
                ),
            ]));
        }
        Source::Inferred
    } else {
        member(Source::MEMBERS, a.source)
            .ok_or_else(|| value_err(&[&cps("source must be one of "), &values(Source::MEMBERS)]))?
    };
    let category = member(Category::MEMBERS, a.category).ok_or_else(|| {
        // `sorted(_WRITE_CATEGORIES)`.
        let mut v: Vec<PyStr> = Category::MEMBERS.iter().map(|(v, _)| cps(v)).collect();
        v.sort();
        value_err(&[
            &cps("category must be one of "),
            &list_repr(v.iter().map(|s| s.as_slice())),
        ])
    })?;
    Ok(WritePayload {
        content: a.content.to_vec(),
        scopes: a.scopes.to_vec(),
        confidence,
        source,
        category,
    })
}
