//! Exact native strings at UTF-8 boundaries (`native_text.py` 15-40):
//! `has_surrogates`, `utf8_text`, `encoded_json`, `origin_projection`.
//! `mutation_payload` is left for the unit that ports the log's writes.

use crate::json::{self, DumpOptions};
use crate::pyvalue::{PyError, PyStr, PyValue};

fn is_surrogate(c: u32) -> bool {
    (0xD800..=0xDFFF).contains(&c)
}

/// `has_surrogates`: a `str` with a code point in U+D800..U+DFFF, or a
/// dict, list or tuple holding one (a dict's keys too). Anything else,
/// a path among them (`isinstance(value, str)` is false), answers false.
/// Nested values are walked on an explicit work list, so every depth
/// answers; the reference recurses and raises RecursionError near the
/// interpreter's recursion limit (see the README).
pub fn has_surrogates(value: &PyValue) -> bool {
    let mut work = vec![value];
    while let Some(v) = work.pop() {
        match v {
            PyValue::Str(s) => {
                if s.iter().any(|&c| is_surrogate(c)) {
                    return true;
                }
            }
            PyValue::Dict(items) => {
                for (k, v) in items.iter().rev() {
                    work.push(v);
                    work.push(k);
                }
            }
            PyValue::List(items) | PyValue::Tuple(items) => work.extend(items.iter().rev()),
            _ => {}
        }
    }
    false
}

/// `utf8_text`: `text.encode("utf-8", "backslashreplace").decode()`. A
/// surrogate, the only code point UTF-8 cannot encode, becomes `\udXXX`
/// in lower-case hex; every other code point is unchanged.
pub fn utf8_text(text: &[u32]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(text.len());
    for &c in text {
        match char::from_u32(c) {
            Some(ch) => out.push(ch),
            None => {
                // Written in place: a surrogate is four hex digits.
                out.push_str("\\u");
                for shift in [12, 8, 4, 0] {
                    out.push(char::from(HEX[((c >> shift) & 0xF) as usize]));
                }
            }
        }
    }
    out
}

/// `encoded_json`: `json.dumps(value, sort_keys=True, ensure_ascii=True,
/// separators=(",", ":"))`, whose keys sort by code point.
pub fn encoded_json(value: &PyValue) -> Result<PyStr, PyError> {
    json::dumps(value, DumpOptions::SORTED_ASCII_COMPACT)
}

/// An origin's entries, in insertion order.
pub type Entries = Vec<(PyValue, PyValue)>;

/// `origin_projection` of an origin's entries in insertion order: the
/// whole origin and `None` when it holds no surrogate; otherwise the
/// entries whose value holds none (a surrogate key with a clean value
/// stays) and the `encoded_json` of the whole origin.
pub fn origin_projection(
    origin: &[(PyValue, PyValue)],
) -> Result<(Entries, Option<PyStr>), PyError> {
    let whole = PyValue::Dict(origin.to_vec());
    if !has_surrogates(&whole) {
        return Ok((origin.to_vec(), None));
    }
    let legacy = origin
        .iter()
        .filter(|(_, v)| !has_surrogates(v))
        .cloned()
        .collect();
    Ok((legacy, Some(encoded_json(&whole)?)))
}
