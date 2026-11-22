//! `_response.py`'s write and review shapes: `ResponseBuilder.committed`,
//! `review`, `roster`, `similar_to_dict`, `add_similar`,
//! `transient_to_dict`, `credential_to_dict`, `origin_to_dict` and
//! `actor_to_dict`.
//!
//! None of these methods reads the builder's `stale_after_days`, so they
//! are free functions here; `ResponseBuilder` (`response_rows`) may wrap
//! them as methods. Each answers the reference's dict as a
//! [`PyValue::Dict`] with the keys in the reference's insertion order.

use crate::datetime::DateTime;
use crate::durability::TransientMatch;
use crate::identity::SOURCE_PROCESS_CWD;
use crate::native_text::has_surrogates;
use crate::pyvalue::{PyStr, PyValue};
use crate::records::{Actor, Memory, Origin};
use crate::response_rows::SIMILAR_HITS_SHOWN;
use crate::search_dedup::SimilarHit;

fn key(text: &str) -> PyValue {
    PyValue::str(text)
}

fn text(value: &PyStr) -> PyValue {
    PyValue::Str(value.clone())
}

fn texts(values: &[PyStr]) -> PyValue {
    PyValue::List(values.iter().map(text).collect())
}

fn iso(value: &DateTime) -> PyValue {
    PyValue::str(&value.isoformat_utc())
}

fn int(n: usize) -> PyValue {
    PyValue::Int(n.to_string())
}

/// The optional arguments of `committed`, each omitted from the answer
/// when empty (`hint` when None or empty, as the reference's truth test
/// reads it).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct CommittedExtras {
    pub related: Option<Vec<SimilarHit>>,
    pub removed_related: Option<Vec<SimilarHit>>,
    pub warnings: Option<Vec<PyStr>>,
    pub supersedes: Option<Vec<PyValue>>,
    pub conflicts_filed: Option<Vec<PyValue>>,
    pub hint: Option<PyStr>,
    pub update_suggested: Option<Vec<PyStr>>,
    pub extends: Option<Vec<PyValue>>,
}

fn filled<T>(v: &Option<Vec<T>>) -> Option<&[T]> {
    v.as_deref().filter(|v| !v.is_empty())
}

/// `committed(memory, ...)`: the ten fixed keys, `actor` when the record
/// carries a non-empty one, then each optional list that is not empty in
/// the reference's order, `hint` last.
pub fn committed(memory: &Memory, extras: &CommittedExtras) -> PyValue {
    let f = &memory.fields;
    let mut out = vec![
        (key("status"), key("committed")),
        (key("id"), text(&f.id)),
        (key("scopes"), texts(&f.scopes)),
        (key("confidence"), key(f.confidence.value())),
        (key("source"), key(f.source.value())),
        (
            key("category"),
            f.category.map_or(PyValue::None, |c| key(c.value())),
        ),
        (key("created"), iso(&f.created)),
        (key("updated"), iso(&f.updated)),
        (
            key("last_verified_at"),
            f.last_verified_at.as_ref().map_or(PyValue::None, iso),
        ),
    ];
    if let Some(actor) = actor_to_dict(f.actor.as_ref()) {
        out.push((key("actor"), actor));
    }
    if let Some(hits) = filled(&extras.related) {
        add_similar(&mut out, "related", hits);
    }
    if let Some(hits) = filled(&extras.removed_related) {
        add_similar(&mut out, "removed_related", hits);
    }
    if let Some(v) = filled(&extras.warnings) {
        out.push((key("warnings"), texts(v)));
    }
    if let Some(v) = filled(&extras.supersedes) {
        out.push((key("supersedes"), PyValue::List(v.to_vec())));
    }
    if let Some(v) = filled(&extras.conflicts_filed) {
        out.push((key("conflicts_filed"), PyValue::List(v.to_vec())));
    }
    if let Some(v) = filled(&extras.update_suggested) {
        out.push((key("update_suggested"), texts(v)));
    }
    if let Some(v) = filled(&extras.extends) {
        out.push((key("extends"), PyValue::List(v.to_vec())));
    }
    if let Some(h) = extras.hint.as_ref().filter(|h| !h.is_empty()) {
        out.push((key("hint"), text(h)));
    }
    PyValue::Dict(out)
}

/// `review(review_id=..., related=..., questions=..., hint=...)`.
pub fn review(
    review_id: &PyStr,
    related: &[PyValue],
    questions: &PyValue,
    hint: &PyStr,
) -> PyValue {
    PyValue::Dict(vec![
        (key("status"), key("review")),
        (key("written"), PyValue::Bool(false)),
        (
            key("review"),
            PyValue::Dict(vec![
                (key("review_id"), text(review_id)),
                (key("related"), PyValue::List(related.to_vec())),
                (key("questions"), questions.clone()),
            ]),
        ),
        (key("hint"), text(hint)),
    ])
}

/// `roster(review_id=..., roster=..., question=..., hint=...)`.
pub fn roster(review_id: &PyStr, roster: &[PyStr], question: &PyStr, hint: &PyStr) -> PyValue {
    PyValue::Dict(vec![
        (key("status"), key("review")),
        (key("written"), PyValue::Bool(false)),
        (
            key("review"),
            PyValue::Dict(vec![
                (key("review_id"), text(review_id)),
                (key("roster"), texts(roster)),
                (key("question"), text(question)),
            ]),
        ),
        (key("hint"), text(hint)),
    ])
}

/// `similar_to_dict(hit)`: eight keys, then `removed_at` and
/// `removed_reason` each when set.
pub fn similar_to_dict(hit: &SimilarHit) -> PyValue {
    let mut out = vec![
        (key("id"), text(&hit.id)),
        (key("scopes"), texts(&hit.scopes)),
        (key("confidence"), key(hit.confidence.value())),
        (key("snippet"), text(&hit.snippet)),
        (key("similarity"), PyValue::Float(hit.similarity)),
        (key("relevance"), key(hit.relevance)),
        (key("created"), iso(&hit.created)),
        (key("updated"), iso(&hit.updated)),
    ];
    if let Some(at) = &hit.removed_at {
        out.push((key("removed_at"), iso(at)));
    }
    if let Some(reason) = &hit.removed_reason {
        out.push((key("removed_reason"), text(reason)));
    }
    PyValue::Dict(out)
}

/// `add_similar(out, key, hits)`: sets `out[key]` to the first
/// `SIMILAR_HITS_SHOWN` hits and `out[key + "_more"]` to the count left
/// out when some were. A key already present is replaced in place, as a
/// dict assignment does.
pub fn add_similar(out: &mut Vec<(PyValue, PyValue)>, name: &str, hits: &[SimilarHit]) {
    let shown = PyValue::List(
        hits.iter()
            .take(SIMILAR_HITS_SHOWN)
            .map(similar_to_dict)
            .collect(),
    );
    set(out, key(name), shown);
    if hits.len() > SIMILAR_HITS_SHOWN {
        set(
            out,
            key(&format!("{name}_more")),
            int(hits.len() - SIMILAR_HITS_SHOWN),
        );
    }
}

fn set(out: &mut Vec<(PyValue, PyValue)>, k: PyValue, v: PyValue) {
    match out.iter_mut().find(|(have, _)| *have == k) {
        Some(slot) => slot.1 = v,
        None => out.push((k, v)),
    }
}

/// `transient_to_dict(hit)`.
pub fn transient_to_dict(hit: &TransientMatch) -> PyValue {
    PyValue::Dict(vec![
        (key("marker"), text(&hit.marker)),
        (key("snippet"), text(&hit.snippet)),
    ])
}

/// `credential_to_dict(hit)`, over the match's `kind` and `snippet`.
pub fn credential_to_dict(kind: &PyStr, snippet: &PyStr) -> PyValue {
    PyValue::Dict(vec![
        (key("kind"), text(kind)),
        (key("snippet"), text(snippet)),
    ])
}

/// How `os.fsencode` encodes a surrogate on the host: UTF-8 with
/// `surrogateescape` on POSIX, with `surrogatepass` on Windows.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FsEncoding {
    SurrogateEscape,
    SurrogatePass,
}

/// The host's [`FsEncoding`].
#[cfg(windows)]
pub const HOST_FS_ENCODING: FsEncoding = FsEncoding::SurrogatePass;
/// The host's [`FsEncoding`].
#[cfg(not(windows))]
pub const HOST_FS_ENCODING: FsEncoding = FsEncoding::SurrogateEscape;

/// `os.fsencode(text)` under `encoding`: None where it raises
/// `UnicodeEncodeError` (a surrogate outside U+DC80-U+DCFF under
/// `surrogateescape`).
pub fn fsencode_as(text: &[u32], encoding: FsEncoding) -> Option<Vec<u8>> {
    match encoding {
        FsEncoding::SurrogateEscape => crate::gitfile_rule::follow::fsencode(text),
        FsEncoding::SurrogatePass => {
            let mut out = Vec::with_capacity(text.len());
            for &cp in text {
                if (0xD800..=0xDFFF).contains(&cp) {
                    out.push(0xE0 | (cp >> 12) as u8);
                    out.push(0x80 | ((cp >> 6) & 0x3F) as u8);
                    out.push(0x80 | (cp & 0x3F) as u8);
                } else {
                    let c = char::from_u32(cp)?;
                    let mut buf = [0; 4];
                    out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
                }
            }
            Some(out)
        }
    }
}

/// Standard base64 with padding (`base64.b64encode`).
fn b64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |acc, (i, &b)| acc | (u32::from(b) << (16 - 8 * i)));
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(char::from(ALPHABET[((n >> (18 - 6 * i)) & 0x3F) as usize]));
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// `UnicodeEncodeError` from `os.fsencode` in `origin_to_dict`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct UnicodeEncodeError;

/// `origin_to_dict(origin)` with the host's `os.fsencode`.
pub fn origin_to_dict(origin: Option<&Origin>) -> Result<Option<PyValue>, UnicodeEncodeError> {
    origin_to_dict_as(origin, HOST_FS_ENCODING)
}

/// `origin_to_dict(origin)` under `encoding`: the set fields, `source`
/// defaulted to `process-cwd`, and each of `cwd`, `worktree_root`, `repo`
/// and `branch` that holds a surrogate set to None with its bytes in a
/// `<field>_bytes_base64` key appended. None for no origin or one with no
/// field set.
pub fn origin_to_dict_as(
    origin: Option<&Origin>,
    encoding: FsEncoding,
) -> Result<Option<PyValue>, UnicodeEncodeError> {
    let Some(origin) = origin else {
        return Ok(None);
    };
    let dumped = origin.dump(true);
    let mut payload = match &dumped {
        PyValue::Dict(items) => items.clone(),
        _ => unreachable!("Origin::dump answers a dict"),
    };
    if payload.is_empty() {
        return Ok(None);
    }
    if !payload.iter().any(|(k, _)| *k == key("source")) {
        payload.push((key("source"), key(SOURCE_PROCESS_CWD)));
    }
    for field in ["cwd", "worktree_root", "repo", "branch"] {
        let Some(at) = payload.iter().position(|(k, _)| *k == key(field)) else {
            continue;
        };
        let value = match &payload[at].1 {
            PyValue::Str(v) => v.clone(),
            _ => continue,
        };
        if has_surrogates(&PyValue::Str(value.clone())) {
            let bytes = fsencode_as(&value, encoding).ok_or(UnicodeEncodeError)?;
            payload[at].1 = PyValue::None;
            payload.push((
                key(&format!("{field}_bytes_base64")),
                PyValue::str(&b64(&bytes)),
            ));
        }
    }
    Ok(Some(PyValue::Dict(payload)))
}

/// `actor_to_dict(actor)`: the full `model_dump`, nulls included, or None
/// for no actor or an empty one (`Actor.is_empty`, which ignores
/// `sources`).
pub fn actor_to_dict(actor: Option<&Actor>) -> Option<PyValue> {
    let actor = actor?;
    let empty = actor.client.is_none()
        && actor.client_version.is_none()
        && actor.model.is_none()
        && actor.principal.is_none()
        && actor.session.is_none();
    (!empty).then(|| actor.dump())
}
