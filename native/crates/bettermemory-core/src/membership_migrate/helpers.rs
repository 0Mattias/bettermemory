//! The migration's pure text helpers (`membership_migrate.py` 464-517).

use std::collections::BTreeSet;
use std::sync::OnceLock;

use crate::capture::paths::norm;
use crate::claims::strip;
use crate::json;
use crate::pyre::regex::Regex;
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::unicode;

const SLASH: u32 = b'/' as u32;
const BACKSLASH: u32 = b'\\' as u32;

/// Whether the host's `os.path` is `ntpath` (Windows) rather than
/// `posixpath`.
pub const HOST_NT: bool = cfg!(windows);

/// `_basename(path)` as the host's `os.path` answers it.
pub fn basename(path: &[u32]) -> PyStr {
    basename_on(path, HOST_NT)
}

/// `_basename(path)` with `os.path` being `ntpath` (`nt`) or `posixpath`:
/// the last segment of the trimmed path with backslashes read as
/// slashes, or `path` itself when that is empty.
pub fn basename_on(path: &[u32], nt: bool) -> PyStr {
    let p: PyStr = norm(path)
        .into_iter()
        .map(|c| if c == BACKSLASH { SLASH } else { c })
        .collect();
    let tail = if nt { nt_tail(&p) } else { &p[..] };
    let start = tail.iter().rposition(|&c| c == SLASH).map_or(0, |i| i + 1);
    let base = &tail[start..];
    if base.is_empty() {
        path.to_vec()
    } else {
        base.to_vec()
    }
}

/// What follows the drive and root of `ntpath.splitroot(p)`, for a `p`
/// whose backslashes `basename_on` has already turned into slashes.
fn nt_tail(p: &[u32]) -> &[u32] {
    let sep = |c: Option<&u32>| c == Some(&SLASH);
    let find_sep = |from: usize| {
        p.iter()
            .enumerate()
            .skip(from)
            .find(|(_, &c)| c == SLASH)
            .map(|(i, _)| i)
    };
    if sep(p.first()) {
        if !sep(p.get(1)) {
            return &p[1..];
        }
        let start = if is_unc_prefix(p) { 8 } else { 2 };
        let Some(index) = find_sep(start) else {
            return &[];
        };
        let Some(index2) = find_sep(index + 1) else {
            return &[];
        };
        return &p[index2 + 1..];
    }
    if p.get(1) == Some(&u32::from(b':')) {
        return if sep(p.get(2)) { &p[3..] } else { &p[2..] };
    }
    p
}

/// `normp[:8].upper() == "\\\\?\\UNC\\"`, read on slashes.
fn is_unc_prefix(p: &[u32]) -> bool {
    if p.len() < 8 {
        return false;
    }
    let s = |c: u32| c == SLASH;
    let up = |c: u32, want: u8| c == u32::from(want) || c == u32::from(want.to_ascii_lowercase());
    s(p[0])
        && s(p[1])
        && p[2] == u32::from(b'?')
        && s(p[3])
        && up(p[4], b'U')
        && up(p[5], b'N')
        && up(p[6], b'C')
        && s(p[7])
}

/// Python truth of a value.
fn truthy(v: &PyValue) -> bool {
    match v {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(t) => t != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) => !s.is_empty(),
        PyValue::DateTime(_) | PyValue::Path(_) => true,
        PyValue::Tuple(x) | PyValue::List(x) => !x.is_empty(),
        PyValue::Dict(x) => !x.is_empty(),
    }
}

/// `_origin(raw)`: the JSON object `raw` holds, or None for a false
/// value, a value `json.loads` refuses with TypeError or ValueError, or
/// one that is not an object. RecursionError passes through. (A `bytes`
/// value, which `json.loads` would read, has no `PyValue`.)
pub fn origin(raw: &PyValue) -> Result<Option<Vec<(PyValue, PyValue)>>, PyError> {
    if !truthy(raw) {
        return Ok(None);
    }
    let PyValue::Str(s) = raw else {
        return Ok(None);
    };
    match json::loads(s) {
        Ok(mut v) => Ok(match &mut v {
            PyValue::Dict(d) => Some(std::mem::take(d)),
            _ => None,
        }),
        Err(PyError::RecursionError) => Err(PyError::RecursionError),
        Err(_) => Ok(None),
    }
}

/// `_text(origin, key)`: the value under `key` when it is a text with
/// something besides whitespace, unstripped.
pub fn text(origin: Option<&[(PyValue, PyValue)]>, key: &[u32]) -> Option<PyStr> {
    let d = origin?;
    let value = d
        .iter()
        .rev()
        .find(|(k, _)| matches!(k, PyValue::Str(s) if s == key))
        .map(|(_, v)| v)?;
    match value {
        PyValue::Str(s) if !strip(s).is_empty() => Some(s.clone()),
        _ => None,
    }
}

/// `_id_order(id)`: `(len(id), id)`, so P9 sorts before P10.
pub fn id_order(membership_id: &[u32]) -> (usize, PyStr) {
    (membership_id.len(), membership_id.to_vec())
}

/// `_repository_name(key)`: the last path segment of a remote key, a
/// `local:` prefix dropped and one `.git` suffix too, or `key` itself
/// when that is empty.
pub fn repository_name(key: &[u32]) -> PyStr {
    let prefix = unicode::cps("local:");
    let path = key.strip_prefix(&prefix[..]).unwrap_or(key);
    let path: PyStr = path
        .iter()
        .map(|&c| if c == BACKSLASH { SLASH } else { c })
        .collect();
    let end = path.iter().rposition(|&c| c != SLASH).map_or(0, |i| i + 1);
    let trimmed = &path[..end];
    let start = trimmed
        .iter()
        .rposition(|&c| c == SLASH)
        .map_or(0, |i| i + 1);
    let mut last = &trimmed[start..];
    let suffix = unicode::cps(".git");
    if let Some(s) = last.strip_suffix(&suffix[..]) {
        last = s;
    }
    if last.is_empty() {
        key.to_vec()
    } else {
        last.to_vec()
    }
}

/// `_SHA`'s pattern text, as the reference compiles it (no flags).
pub const SHA_PATTERN: &str = "[0-9a-f]{4,64}";

/// `_SHA.fullmatch(t)`, on the engine (pyre decision, section 5 rule 1).
fn is_sha(t: &[u32]) -> bool {
    static SHA: OnceLock<Regex> = OnceLock::new();
    let re = SHA.get_or_init(|| Regex::new_str(SHA_PATTERN, 0).expect("the engine builds _SHA"));
    // No budget is given, so the engine's one failure is memory, which
    // the plain call ends as an allocation failure ends.
    matches!(re.fullmatch(t, 0, isize::MAX), Ok(Some(_)))
}

/// `_shas(values)`: each text among `values`, stripped and lowercased,
/// that is a full or abbreviated commit id.
pub fn shas(values: &[PyValue]) -> BTreeSet<PyStr> {
    values
        .iter()
        .filter_map(|v| match v {
            PyValue::Str(s) => {
                let t = unicode::lower(strip(s));
                is_sha(&t).then_some(t)
            }
            _ => None,
        })
        .collect()
}

/// `_json_list(raw)`: a list as it is, the list a non-empty text holds,
/// or an empty list. RecursionError passes through.
pub fn json_list(raw: &PyValue) -> Result<Vec<PyValue>, PyError> {
    match raw {
        PyValue::List(items) => Ok(items.clone()),
        PyValue::Str(s) if !s.is_empty() => match json::loads(s) {
            Ok(mut v) => Ok(match &mut v {
                PyValue::List(items) => std::mem::take(items),
                _ => Vec::new(),
            }),
            Err(PyError::RecursionError) => Err(PyError::RecursionError),
            Err(_) => Ok(Vec::new()),
        },
        _ => Ok(Vec::new()),
    }
}
