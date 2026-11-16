//! Python's `json.dumps` and `json.loads`, as the reference calls them.
//!
//! [`dumps`] takes the options the reference's call sites pass (sorted
//! keys, ASCII escaping, separators, indent, `default=str`) and writes the
//! text CPython's encoder writes, byte for byte. [`loads`] reads what
//! CPython's decoder reads, including `NaN`/`Infinity` and lone surrogate
//! escapes. [`canonical_payload`] is the log's payload text.

use std::collections::HashMap;

use crate::float_repr;
use crate::pyvalue::{
    cmp_keys, lt_keys, py_str, python_small_sort, PyError, PyStr, PyValue, SMALL_SORT,
};
use crate::unicode;

/// The keyword arguments a `json.dumps` call site passes.
#[derive(Clone, Copy, Debug)]
pub struct DumpOptions {
    pub sort_keys: bool,
    pub ensure_ascii: bool,
    pub item_separator: &'static str,
    pub key_separator: &'static str,
    pub indent: Option<usize>,
    /// `default=str`: a value JSON has no spelling for is written as its
    /// `str()` (a datetime, a path); without it such a value is TypeError.
    pub default_str: bool,
}

impl DumpOptions {
    /// `json.dumps(value)`'s defaults.
    pub const DEFAULT: DumpOptions = DumpOptions {
        sort_keys: false,
        ensure_ascii: true,
        item_separator: ", ",
        key_separator: ": ",
        indent: None,
        default_str: false,
    };

    /// The log payload's options: sorted keys, compact, non-ASCII kept,
    /// `default=str` (`log.canonical_payload`, before its
    /// backslash-replace of surrogates).
    pub const LOG_PAYLOAD: DumpOptions = DumpOptions {
        sort_keys: true,
        ensure_ascii: false,
        item_separator: ",",
        key_separator: ":",
        indent: None,
        default_str: true,
    };

    /// Sorted, ASCII, compact: `native_text.encoded_json`, and with a line
    /// feed after it the anchor and audit-export lines.
    pub const SORTED_ASCII_COMPACT: DumpOptions = DumpOptions {
        sort_keys: true,
        ensure_ascii: true,
        item_separator: ",",
        key_separator: ":",
        indent: None,
        default_str: false,
    };

    /// Sorted, non-ASCII kept, default separators: record JSON columns
    /// (`store_rows._json_text`) and history values.
    pub const SORTED_UNICODE_SPACED: DumpOptions = DumpOptions {
        sort_keys: true,
        ensure_ascii: false,
        item_separator: ", ",
        key_separator: ": ",
        indent: None,
        default_str: false,
    };

    /// Sorted, ASCII, default separators: the store digest's fingerprint
    /// text, `schema_steps`, the restore-lock marker, the membership digest.
    pub const SORTED_ASCII_SPACED: DumpOptions = DumpOptions {
        sort_keys: true,
        ensure_ascii: true,
        item_separator: ", ",
        key_separator: ": ",
        indent: None,
        default_str: false,
    };

    /// Sorted, non-ASCII kept, compact: `review.canonical_json` (then
    /// UTF-8) and `search_lexicon._spelling_tables`.
    pub const SORTED_UNICODE_COMPACT: DumpOptions = DumpOptions {
        sort_keys: true,
        ensure_ascii: false,
        item_separator: ",",
        key_separator: ":",
        indent: None,
        default_str: false,
    };

    /// `indent=2, sort_keys=True` (then a line feed): backup manifests,
    /// mirror payloads, machine bodies, key-lifecycle and restore documents.
    pub const DOCUMENT_INDENT2: DumpOptions = DumpOptions {
        sort_keys: true,
        ensure_ascii: true,
        item_separator: ",",
        key_separator: ": ",
        indent: Some(2),
        default_str: false,
    };

    /// Insertion order, ASCII, compact (then a line feed): rollback
    /// report lines.
    pub const UNSORTED_ASCII_COMPACT: DumpOptions = DumpOptions {
        sort_keys: false,
        ensure_ascii: true,
        item_separator: ",",
        key_separator: ":",
        indent: None,
        default_str: false,
    };
}

fn push(out: &mut PyStr, text: &str) {
    out.extend(text.bytes().map(u32::from));
}

fn push_hex4(out: &mut PyStr, unit: u32) {
    push(out, &format!("\\u{unit:04x}"));
}

fn encode_str(out: &mut PyStr, text: &[u32], ensure_ascii: bool) {
    out.push(u32::from(b'"'));
    for &cp in text {
        match cp {
            0x22 => push(out, "\\\""),
            0x5C => push(out, "\\\\"),
            0x0A => push(out, "\\n"),
            0x0D => push(out, "\\r"),
            0x09 => push(out, "\\t"),
            0x08 => push(out, "\\b"),
            0x0C => push(out, "\\f"),
            _ if cp < 0x20 => push_hex4(out, cp),
            _ if !ensure_ascii || cp < 0x7F => out.push(cp),
            _ if cp >= 0x10000 => {
                let v = cp - 0x10000;
                push_hex4(out, 0xD800 | (v >> 10));
                push_hex4(out, 0xDC00 | (v & 0x3FF));
            }
            _ => push_hex4(out, cp),
        }
    }
    out.push(u32::from(b'"'));
}

fn float_text(value: f64) -> String {
    if value.is_nan() {
        "NaN".to_owned()
    } else if value.is_infinite() {
        if value > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
    } else {
        float_repr::repr(value)
    }
}

/// A dict key as the encoder spells it: str, float, bool, None or int.
fn key_text(key: &PyValue) -> Result<PyStr, PyError> {
    Ok(match key {
        PyValue::Str(s) => s.clone(),
        PyValue::Float(f) => unicode::cps(&float_text(*f)),
        PyValue::Bool(true) => unicode::cps("true"),
        PyValue::Bool(false) => unicode::cps("false"),
        PyValue::None => unicode::cps("null"),
        PyValue::Int(text) => unicode::cps(text),
        _ => return Err(PyError::TypeError),
    })
}

/// The deepest nesting of containers the reference reads or writes: CPython
/// 3.13's C JSON decoder and encoder and `repr` raise RecursionError on the
/// 9,999th nested container (measured through `json.loads`,
/// `log.canonical_payload`, `KeyRing.read_head`, `KeyRing.read_origin` and
/// `log._rekey_fingerprints`, and `json.dumps` with and without an
/// indent; see the README). The native reader and
/// writer are iterative, so the limit is a choice, made equal to the
/// reference's: anything deeper is RecursionError here too.
pub const MAX_DEPTH: usize = 9998;

enum Frame<'a> {
    List {
        items: &'a [PyValue],
        next: usize,
    },
    Dict {
        items: Vec<&'a (PyValue, PyValue)>,
        next: usize,
    },
}

struct Encoder {
    options: DumpOptions,
    out: PyStr,
}

impl Encoder {
    fn newline(&mut self, level: usize) {
        if let Some(indent) = self.options.indent {
            self.out.push(u32::from(b'\n'));
            self.out.extend(std::iter::repeat_n(
                u32::from(b' '),
                indent.saturating_mul(level),
            ));
        }
    }

    fn max_depth(&self) -> usize {
        // With or without an indent: CPython 3.13's C encoder writes both.
        MAX_DEPTH
    }

    /// Writes a scalar, or an empty container, or opens a container and
    /// returns its frame.
    fn open<'a>(&mut self, value: &'a PyValue, depth: usize) -> Result<Option<Frame<'a>>, PyError> {
        match value {
            PyValue::None => push(&mut self.out, "null"),
            PyValue::Bool(true) => push(&mut self.out, "true"),
            PyValue::Bool(false) => push(&mut self.out, "false"),
            PyValue::Int(text) => push(&mut self.out, text),
            PyValue::Float(f) => push(&mut self.out, &float_text(*f)),
            PyValue::Str(s) => encode_str(&mut self.out, s, self.options.ensure_ascii),
            PyValue::DateTime(_) | PyValue::Path(_) => {
                if !self.options.default_str {
                    return Err(PyError::TypeError);
                }
                let text = py_str(value)?;
                encode_str(&mut self.out, &text, self.options.ensure_ascii);
            }
            PyValue::List(items) | PyValue::Tuple(items) => {
                if depth + 1 > self.max_depth() {
                    return Err(PyError::RecursionError);
                }
                if items.is_empty() {
                    push(&mut self.out, "[]");
                } else {
                    push(&mut self.out, "[");
                    self.newline(depth + 1);
                    return Ok(Some(Frame::List { items, next: 0 }));
                }
            }
            PyValue::Dict(items) => {
                if depth + 1 > self.max_depth() {
                    return Err(PyError::RecursionError);
                }
                if items.is_empty() {
                    push(&mut self.out, "{}");
                } else {
                    let mut order: Vec<&(PyValue, PyValue)> = items.iter().collect();
                    if self.options.sort_keys && order.len() > 1 {
                        // `sorted(dct.items())` raises TypeError for any two
                        // keys `<` refuses; a sort of two or more keys of
                        // classes that do not compare always meets such a
                        // pair.
                        let first = &order[0].0;
                        if order.iter().any(|(k, _)| cmp_keys(first, k).is_none()) {
                            return Err(PyError::TypeError);
                        }
                        // Every pair now compares. Few keys sort as
                        // CPython 3.13's binary insertion leaves them, NaN
                        // included; many by a total order that agrees with
                        // `<` on every pair without a NaN.
                        if order.len() < SMALL_SORT {
                            python_small_sort(&mut order, |a, b| {
                                lt_keys(&a.0, &b.0).unwrap_or(false)
                            });
                        } else {
                            order.sort_by(|a, b| {
                                cmp_keys(&a.0, &b.0).unwrap_or(std::cmp::Ordering::Equal)
                            });
                        }
                    }
                    push(&mut self.out, "{");
                    self.newline(depth + 1);
                    return Ok(Some(Frame::Dict {
                        items: order,
                        next: 0,
                    }));
                }
            }
        }
        Ok(None)
    }

    /// The whole value, without native recursion.
    fn encode(&mut self, value: &PyValue) -> Result<(), PyError> {
        let mut stack: Vec<Frame> = Vec::new();
        if let Some(frame) = self.open(value, 0)? {
            stack.push(frame);
        }
        loop {
            let depth = stack.len();
            let Some(frame) = stack.last_mut() else {
                break;
            };
            let item = match frame {
                Frame::List { items, next } => {
                    let index = *next;
                    *next += 1;
                    items.get(index).map(|item| (index, None, item))
                }
                Frame::Dict { items, next } => {
                    let index = *next;
                    *next += 1;
                    items.get(index).map(|(k, v)| (index, Some(k), v))
                }
            };
            let Some((index, key, item)) = item else {
                let close = match stack.pop() {
                    Some(Frame::List { .. }) => "]",
                    _ => "}",
                };
                self.newline(depth - 1);
                push(&mut self.out, close);
                continue;
            };
            if index > 0 {
                push(&mut self.out, self.options.item_separator);
                self.newline(depth);
            }
            if let Some(key) = key {
                let key = key_text(key)?;
                encode_str(&mut self.out, &key, self.options.ensure_ascii);
                push(&mut self.out, self.options.key_separator);
            }
            if let Some(frame) = self.open(item, depth)? {
                stack.push(frame);
            }
        }
        Ok(())
    }
}

/// `json.dumps(value, **options)`: the returned `str` (which may hold a
/// lone surrogate when `ensure_ascii` is off), or the exception type.
pub fn dumps(value: &PyValue, options: DumpOptions) -> Result<PyStr, PyError> {
    let mut encoder = Encoder {
        options,
        out: Vec::new(),
    };
    encoder.encode(value)?;
    Ok(encoder.out)
}

/// `text.encode("utf-8", "backslashreplace").decode("utf-8")`: each lone
/// surrogate as `\udXXX`, everything else unchanged
/// (`native_text.utf8_text`).
pub fn utf8_text(text: &[u32]) -> String {
    let mut out = String::with_capacity(text.len());
    for &cp in text {
        match char::from_u32(cp) {
            Some(c) => out.push(c),
            None => out.push_str(&format!("\\u{cp:04x}")),
        }
    }
    out
}

/// `log.canonical_payload`: the one text a log payload has and its MAC
/// covers.
pub fn canonical_payload(payload: &PyValue) -> Result<String, PyError> {
    Ok(utf8_text(&dumps(payload, DumpOptions::LOG_PAYLOAD)?))
}

/// `review._thawed` recurses in Python through directly nested dicts, so
/// `review.canonical_json` stops at `sys.getrecursionlimit()` (1000) less
/// the frames its caller holds, a limit with no fixed depth. Native takes
/// a chain of fewer than 1000 dicts, which the reference takes from a
/// shallow enough caller, and refuses 1000, which it refuses from any.
pub const REVIEW_THAW_LIMIT: usize = 1000;

/// The dict chain below `value`, counted iteratively and capped at
/// `limit` (only whether it reaches the limit matters).
fn dict_chain_bounded(value: &PyValue, limit: usize) -> usize {
    let mut stack: Vec<(&PyValue, usize)> = vec![(value, 1)];
    let mut best = 0;
    while let Some((v, d)) = stack.pop() {
        if let PyValue::Dict(items) = v {
            best = best.max(d);
            if best >= limit {
                return best;
            }
            stack.extend(
                items
                    .iter()
                    .filter(|(_, x)| matches!(x, PyValue::Dict(_)))
                    .map(|(_, x)| (x, d + 1)),
            );
        }
    }
    best
}

/// `review.canonical_json`: sorted keys, compact, non-ASCII kept, as UTF-8
/// bytes; RecursionError for a chain of [`REVIEW_THAW_LIMIT`] dicts
/// (`_thawed`), UnicodeEncodeError for a lone surrogate.
pub fn review_canonical_json(value: &PyValue) -> Result<Vec<u8>, PyError> {
    if dict_chain_bounded(value, REVIEW_THAW_LIMIT) >= REVIEW_THAW_LIMIT {
        return Err(PyError::RecursionError);
    }
    let text = dumps(value, DumpOptions::SORTED_UNICODE_COMPACT)?;
    unicode::to_string(&text)
        .map(String::into_bytes)
        .ok_or(PyError::UnicodeEncodeError)
}

enum Open {
    List(Vec<PyValue>),
    Dict {
        items: Vec<(PyValue, PyValue)>,
        index: HashMap<PyStr, usize>,
        key: PyStr,
    },
}

struct Decoder<'a> {
    text: &'a [u32],
    pos: usize,
    /// Set when an integer past 4300 digits stopped the parse: Python
    /// raises a plain ValueError there, not a `JSONDecodeError`.
    digits_limit: bool,
}

const WS: [u32; 4] = [0x20, 0x09, 0x0A, 0x0D];

impl Decoder<'_> {
    fn peek(&self) -> Option<u32> {
        self.text.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while self.peek().is_some_and(|c| WS.contains(&c)) {
            self.pos += 1;
        }
    }

    fn starts_with(&self, word: &str) -> bool {
        word.bytes()
            .enumerate()
            .all(|(k, b)| self.text.get(self.pos + k) == Some(&u32::from(b)))
    }

    fn scalar(&mut self) -> Result<PyValue, PyError> {
        if self.peek() == Some(0x22) {
            self.pos += 1;
            return Ok(PyValue::Str(self.string()?));
        }
        for (word, value) in [
            ("null", PyValue::None),
            ("true", PyValue::Bool(true)),
            ("false", PyValue::Bool(false)),
            ("NaN", PyValue::Float(f64::NAN)),
            ("Infinity", PyValue::Float(f64::INFINITY)),
            ("-Infinity", PyValue::Float(f64::NEG_INFINITY)),
        ] {
            if self.starts_with(word) {
                self.pos += word.len();
                return Ok(value);
            }
        }
        self.number()
    }

    /// An object's key and its colon, the decoder at the key's quote.
    fn key(&mut self) -> Result<PyStr, PyError> {
        if self.peek() != Some(0x22) {
            return Err(PyError::ValueError);
        }
        self.pos += 1;
        let key = self.string()?;
        self.skip_ws();
        if self.peek() != Some(0x3A) {
            return Err(PyError::ValueError);
        }
        self.pos += 1;
        self.skip_ws();
        Ok(key)
    }

    /// The value at the decoder, without native recursion: containers are
    /// kept on an explicit stack, at most [`MAX_DEPTH`] deep.
    fn value(&mut self) -> Result<PyValue, PyError> {
        let mut stack: Vec<Open> = Vec::new();
        loop {
            // Read the start of a value: a scalar, an empty container, or
            // the opening of one (then read its first element).
            let mut value = match self.peek() {
                Some(0x5B) | Some(0x7B) => {
                    if stack.len() + 1 > MAX_DEPTH {
                        return Err(PyError::RecursionError);
                    }
                    let list = self.peek() == Some(0x5B);
                    self.pos += 1;
                    self.skip_ws();
                    if list {
                        if self.peek() == Some(0x5D) {
                            self.pos += 1;
                            PyValue::List(Vec::new())
                        } else {
                            stack.push(Open::List(Vec::new()));
                            continue;
                        }
                    } else if self.peek() == Some(0x7D) {
                        self.pos += 1;
                        PyValue::Dict(Vec::new())
                    } else {
                        let key = self.key()?;
                        stack.push(Open::Dict {
                            items: Vec::new(),
                            index: HashMap::new(),
                            key,
                        });
                        continue;
                    }
                }
                Some(_) => self.scalar()?,
                None => return Err(PyError::ValueError),
            };
            // Hand the finished value to the container it belongs to, closing
            // every container it completes.
            loop {
                match stack.last_mut() {
                    None => return Ok(value),
                    Some(Open::List(items)) => {
                        items.push(value);
                        self.skip_ws();
                        match self.peek() {
                            Some(0x2C) => {
                                self.pos += 1;
                                self.skip_ws();
                                break;
                            }
                            Some(0x5D) => {
                                self.pos += 1;
                                let Some(Open::List(items)) = stack.pop() else {
                                    return Err(PyError::ValueError);
                                };
                                value = PyValue::List(items);
                            }
                            _ => return Err(PyError::ValueError),
                        }
                    }
                    Some(Open::Dict { items, index, key }) => {
                        // A repeated key keeps its first place and its last
                        // value.
                        let key = std::mem::take(key);
                        match index.get(&key) {
                            Some(&at) => items[at].1 = value,
                            None => {
                                index.insert(key.clone(), items.len());
                                items.push((PyValue::Str(key), value));
                            }
                        }
                        self.skip_ws();
                        match self.peek() {
                            Some(0x2C) => {
                                self.pos += 1;
                                self.skip_ws();
                                let next = self.key()?;
                                if let Some(Open::Dict { key, .. }) = stack.last_mut() {
                                    *key = next;
                                }
                                break;
                            }
                            Some(0x7D) => {
                                self.pos += 1;
                                let Some(Open::Dict { items, .. }) = stack.pop() else {
                                    return Err(PyError::ValueError);
                                };
                                value = PyValue::Dict(items);
                            }
                            _ => return Err(PyError::ValueError),
                        }
                    }
                }
            }
        }
    }

    fn hex4(&mut self) -> Result<u32, PyError> {
        let digits = self
            .text
            .get(self.pos..self.pos + 4)
            .ok_or(PyError::ValueError)?;
        let mut value = 0;
        for &d in digits {
            let digit = char::from_u32(d)
                .and_then(|c| c.to_digit(16))
                .ok_or(PyError::ValueError)?;
            value = value * 16 + digit;
        }
        self.pos += 4;
        Ok(value)
    }

    fn string(&mut self) -> Result<PyStr, PyError> {
        let mut out = Vec::new();
        loop {
            let c = self.peek().ok_or(PyError::ValueError)?;
            self.pos += 1;
            match c {
                0x22 => return Ok(out),
                0x5C => {
                    let e = self.peek().ok_or(PyError::ValueError)?;
                    self.pos += 1;
                    let simple = match e {
                        0x22 => Some(0x22),
                        0x5C => Some(0x5C),
                        0x2F => Some(0x2F),
                        0x62 => Some(0x08),
                        0x66 => Some(0x0C),
                        0x6E => Some(0x0A),
                        0x72 => Some(0x0D),
                        0x74 => Some(0x09),
                        _ => None,
                    };
                    if let Some(s) = simple {
                        out.push(s);
                        continue;
                    }
                    if e != 0x75 {
                        return Err(PyError::ValueError);
                    }
                    let unit = self.hex4()?;
                    if (0xD800..0xDC00).contains(&unit) && self.starts_with("\\u") {
                        let save = self.pos;
                        self.pos += 2;
                        let low = self.hex4()?;
                        if (0xDC00..0xE000).contains(&low) {
                            out.push(0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00));
                            continue;
                        }
                        self.pos = save;
                    }
                    out.push(unit);
                }
                _ if c < 0x20 => return Err(PyError::ValueError),
                _ => out.push(c),
            }
        }
    }

    fn number(&mut self) -> Result<PyValue, PyError> {
        let start = self.pos;
        let digit = |c: Option<u32>| c.is_some_and(|c| (0x30..=0x39).contains(&c));
        if self.peek() == Some(0x2D) {
            self.pos += 1;
        }
        match self.peek() {
            Some(0x30) => self.pos += 1,
            Some(c) if (0x31..=0x39).contains(&c) => {
                while digit(self.peek()) {
                    self.pos += 1;
                }
            }
            _ => return Err(PyError::ValueError),
        }
        let mut is_float = false;
        if self.peek() == Some(0x2E) && digit(self.text.get(self.pos + 1).copied()) {
            is_float = true;
            self.pos += 1;
            while digit(self.peek()) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(0x65 | 0x45)) {
            let save = self.pos;
            self.pos += 1;
            if matches!(self.peek(), Some(0x2B | 0x2D)) {
                self.pos += 1;
            }
            if digit(self.peek()) {
                is_float = true;
                while digit(self.peek()) {
                    self.pos += 1;
                }
            } else {
                self.pos = save;
            }
        }
        let text: String = self.text[start..self.pos]
            .iter()
            .filter_map(|&c| char::from_u32(c))
            .collect();
        if is_float {
            return text
                .parse::<f64>()
                .map(PyValue::Float)
                .map_err(|_| PyError::ValueError);
        }
        let digits = text.trim_start_matches('-').len();
        if digits > 4300 {
            self.digits_limit = true;
            return Err(PyError::ValueError);
        }
        PyValue::int_from_decimal(&text).ok_or(PyError::ValueError)
    }
}

/// `json.loads(text)` for a `str`: the value, or the exception type
/// (ValueError for every malformed text, as `JSONDecodeError` is one).
pub fn loads(text: &[u32]) -> Result<PyValue, PyError> {
    loads_detailed(text).map_err(LoadError::py_error)
}

/// Why `json.loads` refused a text, by the exception class it raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LoadError {
    /// `json.JSONDecodeError`.
    Decode,
    /// A plain ValueError: an integer longer than 4300 digits.
    IntDigits,
    /// RecursionError.
    Recursion,
}

impl LoadError {
    /// The class name the reference raises.
    pub fn name(self) -> &'static str {
        match self {
            LoadError::Decode => "JSONDecodeError",
            LoadError::IntDigits => "ValueError",
            LoadError::Recursion => "RecursionError",
        }
    }

    fn py_error(self) -> PyError {
        match self {
            LoadError::Recursion => PyError::RecursionError,
            LoadError::Decode | LoadError::IntDigits => PyError::ValueError,
        }
    }
}

/// [`loads`], naming which exception the reference raises.
pub fn loads_detailed(text: &[u32]) -> Result<PyValue, LoadError> {
    if text.first() == Some(&0xFEFF) {
        return Err(LoadError::Decode);
    }
    let mut decoder = Decoder {
        text,
        pos: 0,
        digits_limit: false,
    };
    let classify = |e: PyError, digits_limit: bool| match e {
        PyError::RecursionError => LoadError::Recursion,
        _ if digits_limit => LoadError::IntDigits,
        _ => LoadError::Decode,
    };
    decoder.skip_ws();
    let value = decoder
        .value()
        .map_err(|e| classify(e, decoder.digits_limit))?;
    decoder.skip_ws();
    if decoder.pos != text.len() {
        return Err(LoadError::Decode);
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn load(text: &str) -> Result<PyValue, PyError> {
        loads(&unicode::cps(text))
    }

    #[test]
    fn loads_python_extensions() {
        assert!(matches!(load("NaN"), Ok(PyValue::Float(f)) if f.is_nan()));
        assert_eq!(load(r#""\ud800""#), Ok(PyValue::Str(vec![0xD800])));
        assert_eq!(load(r#""😀""#), Ok(PyValue::Str(vec![0x1F600])));
        assert_eq!(
            load("[1, 2.5e1]"),
            Ok(PyValue::List(vec![PyValue::int(1), PyValue::Float(25.0)]))
        );
        assert_eq!(load("1."), Err(PyError::ValueError));
        assert_eq!(load("01"), Err(PyError::ValueError));
        assert_eq!(load(" {} "), Ok(PyValue::Dict(vec![])));
        assert_eq!(
            load(r#"{"a":1,"b":2,"a":3}"#).unwrap().get("a"),
            Some(&PyValue::int(3))
        );
    }
}
