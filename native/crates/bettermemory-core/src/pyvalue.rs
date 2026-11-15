//! The Python values the reference serialises and parses.
//!
//! A string is a sequence of code points ([`PyStr`]), since a Python `str`
//! may hold a lone surrogate. An integer keeps its decimal text, since a
//! Python `int` has no bound.

use std::cmp::Ordering;

use crate::datetime::DateTime;
use crate::float_repr;
use crate::unicode;

/// A Python `str`: code points, surrogates allowed.
pub type PyStr = Vec<u32>;

/// A Python value as the JSON writers and readers of the reference meet it.
#[derive(Clone, Debug, PartialEq)]
pub enum PyValue {
    None,
    Bool(bool),
    /// Canonical decimal text: an optional `-`, no leading zeros.
    Int(String),
    Float(f64),
    Str(PyStr),
    /// A `datetime.datetime`, which `default=str` spells as `str(dt)`.
    DateTime(DateTime),
    /// A `pathlib.PurePosixPath`, by its POSIX text.
    Path(PyStr),
    Tuple(Vec<PyValue>),
    List(Vec<PyValue>),
    /// Insertion order, as a Python `dict` keeps it.
    Dict(Vec<(PyValue, PyValue)>),
}

impl PyValue {
    /// A `str` value from Rust text.
    pub fn str(text: &str) -> Self {
        PyValue::Str(unicode::cps(text))
    }

    /// An `int` value.
    pub fn int(value: i128) -> Self {
        PyValue::Int(value.to_string())
    }

    /// An `int` from decimal text, canonicalised; None when it is not one.
    pub fn int_from_decimal(text: &str) -> Option<Self> {
        let (negative, digits) = match text.strip_prefix('-') {
            Some(rest) => (true, rest),
            None => (false, text),
        };
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        let trimmed = digits.trim_start_matches('0');
        let body = if trimmed.is_empty() { "0" } else { trimmed };
        let sign = if negative && body != "0" { "-" } else { "" };
        Some(PyValue::Int(format!("{sign}{body}")))
    }

    /// The value of a dict entry by `str` key (the last one, as a dict
    /// built from pairs keeps the last).
    pub fn get(&self, key: &str) -> Option<&PyValue> {
        let PyValue::Dict(items) = self else {
            return None;
        };
        let key = unicode::cps(key);
        items.iter().rev().find_map(|(k, v)| match k {
            PyValue::Str(s) if *s == key => Some(v),
            _ => None,
        })
    }
}

/// An exception type the reference raises, by name.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PyError {
    TypeError,
    ValueError,
    OverflowError,
    UnicodeEncodeError,
    RecursionError,
}

impl PyError {
    pub fn name(self) -> &'static str {
        match self {
            PyError::TypeError => "TypeError",
            PyError::ValueError => "ValueError",
            PyError::OverflowError => "OverflowError",
            PyError::UnicodeEncodeError => "UnicodeEncodeError",
            PyError::RecursionError => "RecursionError",
        }
    }
}

enum Number<'a> {
    Int(&'a str),
    Float(f64),
}

fn number(value: &PyValue) -> Option<Number<'_>> {
    match value {
        PyValue::Bool(false) => Some(Number::Int("0")),
        PyValue::Bool(true) => Some(Number::Int("1")),
        PyValue::Int(text) => Some(Number::Int(text)),
        PyValue::Float(f) => Some(Number::Float(*f)),
        _ => None,
    }
}

fn cmp_int_text(a: &str, b: &str) -> Ordering {
    let (an, a) = a.strip_prefix('-').map_or((false, a), |r| (true, r));
    let (bn, b) = b.strip_prefix('-').map_or((false, b), |r| (true, r));
    let magnitude = a.len().cmp(&b.len()).then_with(|| a.cmp(b));
    match (an, bn) {
        (false, false) => magnitude,
        (true, true) => magnitude.reverse(),
        (true, false) => Ordering::Less,
        (false, true) => Ordering::Greater,
    }
}

/// An int against a float, exactly while the int fits an i128 (beyond
/// that through the int's nearest double).
fn cmp_int_float(int: &str, float: f64) -> Option<Ordering> {
    if float.is_nan() {
        return None;
    }
    let Ok(i) = int.parse::<i128>() else {
        return int.parse::<f64>().ok()?.partial_cmp(&float);
    };
    const LIMIT: f64 = 170_141_183_460_469_231_731_687_303_715_884_105_728.0;
    if float >= LIMIT {
        return Some(Ordering::Less);
    }
    if float < -LIMIT {
        return Some(Ordering::Greater);
    }
    let whole = float.trunc();
    let ordering = i.cmp(&(whole as i128));
    if ordering != Ordering::Equal {
        return Some(ordering);
    }
    let fraction = float - whole;
    Some(if fraction > 0.0 {
        Ordering::Less
    } else if fraction < 0.0 {
        Ordering::Greater
    } else {
        Ordering::Equal
    })
}

/// Python's ordering of two dict keys as `sorted` compares them, or None
/// for a pair `<` refuses (TypeError). NaN orders after every number here,
/// where Python's sort leaves its place to the algorithm.
pub fn cmp_keys(a: &PyValue, b: &PyValue) -> Option<Ordering> {
    match (a, b) {
        (PyValue::Str(x), PyValue::Str(y)) => Some(x.cmp(y)),
        _ => {
            let (x, y) = (number(a)?, number(b)?);
            Some(match (x, y) {
                (Number::Int(x), Number::Int(y)) => cmp_int_text(x, y),
                (Number::Float(x), Number::Float(y)) => x.total_cmp(&y),
                (Number::Int(x), Number::Float(y)) => cmp_int_float(x, y).unwrap_or(Ordering::Less),
                (Number::Float(x), Number::Int(y)) => {
                    cmp_int_float(y, x).unwrap_or(Ordering::Less).reverse()
                }
            })
        }
    }
}

/// `str.isprintable` of one code point: not "Other" nor "Separator",
/// except the space.
pub fn is_printable(cp: u32) -> bool {
    if cp == 0x20 {
        return true;
    }
    !matches!(
        unicode::category(cp),
        "Cc" | "Cf" | "Cs" | "Co" | "Cn" | "Zl" | "Zp" | "Zs"
    )
}

fn push_ascii(out: &mut PyStr, text: &str) {
    out.extend(text.bytes().map(u32::from));
}

/// `repr` of a `str`.
pub fn repr_str(text: &[u32]) -> PyStr {
    let quote = if text.contains(&0x27) && !text.contains(&0x22) {
        0x22
    } else {
        0x27
    };
    let mut out = vec![quote];
    for &cp in text {
        match cp {
            _ if cp == quote || cp == 0x5C => {
                out.push(0x5C);
                out.push(cp);
            }
            0x09 => push_ascii(&mut out, "\\t"),
            0x0A => push_ascii(&mut out, "\\n"),
            0x0D => push_ascii(&mut out, "\\r"),
            _ if cp < 0x20 || cp == 0x7F => push_ascii(&mut out, &format!("\\x{cp:02x}")),
            _ if cp < 0x7F || is_printable(cp) => out.push(cp),
            _ if cp < 0x100 => push_ascii(&mut out, &format!("\\x{cp:02x}")),
            _ if cp < 0x10000 => push_ascii(&mut out, &format!("\\u{cp:04x}")),
            _ => push_ascii(&mut out, &format!("\\U{cp:08x}")),
        }
    }
    out.push(quote);
    out
}

/// `repr(value)` for the values JSON parsing yields (and datetimes and
/// paths, as Python spells them), without native recursion. RecursionError
/// past [`crate::json::MAX_DEPTH`] nested containers, as CPython 3.13's
/// `repr` raises on the 9,999th.
pub fn repr(value: &PyValue) -> Result<PyStr, PyError> {
    enum Piece<'a> {
        Value(&'a PyValue, usize),
        Text(&'static str),
    }
    let mut out = Vec::new();
    let mut work = vec![Piece::Value(value, 0)];
    while let Some(piece) = work.pop() {
        let (value, depth) = match piece {
            Piece::Text(text) => {
                push_ascii(&mut out, text);
                continue;
            }
            Piece::Value(value, depth) => (value, depth),
        };
        match value {
            PyValue::Str(s) => out.extend(repr_str(s)),
            PyValue::List(_) | PyValue::Tuple(_) | PyValue::Dict(_)
                if depth + 1 > crate::json::MAX_DEPTH =>
            {
                return Err(PyError::RecursionError);
            }
            PyValue::List(items) | PyValue::Tuple(items) => {
                let list = matches!(value, PyValue::List(_));
                push_ascii(&mut out, if list { "[" } else { "(" });
                work.push(Piece::Text(if list { "]" } else { ")" }));
                if !list && items.len() == 1 {
                    work.push(Piece::Text(","));
                }
                for (i, item) in items.iter().enumerate().rev() {
                    work.push(Piece::Value(item, depth + 1));
                    if i > 0 {
                        work.push(Piece::Text(", "));
                    }
                }
            }
            PyValue::Dict(items) => {
                push_ascii(&mut out, "{");
                work.push(Piece::Text("}"));
                for (i, (k, v)) in items.iter().enumerate().rev() {
                    work.push(Piece::Value(v, depth + 1));
                    work.push(Piece::Text(": "));
                    work.push(Piece::Value(k, depth + 1));
                    if i > 0 {
                        work.push(Piece::Text(", "));
                    }
                }
            }
            PyValue::DateTime(dt) => out.extend(unicode::cps(&dt.python_repr())),
            PyValue::Path(p) => {
                push_ascii(&mut out, "PurePosixPath(");
                out.extend(repr_str(p));
                push_ascii(&mut out, ")");
            }
            PyValue::None | PyValue::Bool(_) | PyValue::Int(_) | PyValue::Float(_) => {
                out.extend(py_str(value)?)
            }
        }
    }
    Ok(out)
}

/// `str(value)`.
pub fn py_str(value: &PyValue) -> Result<PyStr, PyError> {
    Ok(match value {
        PyValue::None => unicode::cps("None"),
        PyValue::Bool(true) => unicode::cps("True"),
        PyValue::Bool(false) => unicode::cps("False"),
        PyValue::Int(text) => unicode::cps(text),
        PyValue::Float(f) => unicode::cps(&float_repr::repr(*f)),
        PyValue::Str(s) | PyValue::Path(s) => s.clone(),
        PyValue::DateTime(dt) => unicode::cps(&dt.python_str()),
        PyValue::Tuple(_) | PyValue::List(_) | PyValue::Dict(_) => return repr(value),
    })
}

impl Drop for PyValue {
    /// Drops nested containers without native recursion, so a value as deep
    /// as the readers allow never exhausts the stack.
    fn drop(&mut self) {
        let mut stack: Vec<PyValue> = Vec::new();
        let take = |value: &mut PyValue, stack: &mut Vec<PyValue>| match value {
            PyValue::List(items) | PyValue::Tuple(items) => stack.append(items),
            PyValue::Dict(items) => {
                for (k, v) in items.drain(..) {
                    stack.push(k);
                    stack.push(v);
                }
            }
            _ => {}
        };
        take(self, &mut stack);
        while let Some(mut value) = stack.pop() {
            take(&mut value, &mut stack);
        }
    }
}

/// The decimal value of a Unicode decimal digit (`unicodedata.decimal`):
/// every run of decimal digits is a whole number of 0-to-9 runs.
fn decimal_value(cp: u32) -> Option<u32> {
    if !unicode::is_decimal(cp) {
        return None;
    }
    let mut start = cp;
    while start > 0 && unicode::is_decimal(start - 1) {
        start -= 1;
    }
    Some((cp - start) % 10)
}

/// `int(text)` for a `str`: surrounding whitespace, a sign, Unicode
/// decimal digits and single underscores between digits; at most 4300
/// digits (`sys.int_info.default_max_str_digits`).
pub fn int_from_str(text: &[u32]) -> Result<PyValue, PyError> {
    let start = text.iter().position(|&c| !unicode::is_space(c));
    let Some(start) = start else {
        return Err(PyError::ValueError);
    };
    let end = text
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .unwrap_or(start)
        + 1;
    let mut body = &text[start..end];
    let mut negative = false;
    if let Some(&sign) = body.first() {
        if sign == u32::from(b'-') || sign == u32::from(b'+') {
            negative = sign == u32::from(b'-');
            body = &body[1..];
        }
    }
    let mut digits = String::new();
    let mut previous_underscore = true;
    for &cp in body {
        if cp == u32::from(b'_') {
            if previous_underscore {
                return Err(PyError::ValueError);
            }
            previous_underscore = true;
            continue;
        }
        let value = decimal_value(cp).ok_or(PyError::ValueError)?;
        digits.push(char::from_digit(value, 10).ok_or(PyError::ValueError)?);
        previous_underscore = false;
    }
    if digits.is_empty() || previous_underscore || digits.len() > 4300 {
        return Err(PyError::ValueError);
    }
    let sign = if negative { "-" } else { "" };
    PyValue::int_from_decimal(&format!("{sign}{digits}")).ok_or(PyError::ValueError)
}

/// `int(value)` for a value JSON parsing yields.
pub fn py_int(value: &PyValue) -> Result<PyValue, PyError> {
    match value {
        PyValue::Bool(b) => Ok(PyValue::int(i128::from(*b))),
        PyValue::Int(_) => Ok(value.clone()),
        PyValue::Float(f) => {
            if f.is_nan() {
                Err(PyError::ValueError)
            } else if f.is_infinite() {
                Err(PyError::OverflowError)
            } else {
                Ok(PyValue::Int(float_to_int_text(*f)))
            }
        }
        PyValue::Str(s) => int_from_str(s),
        _ => Err(PyError::TypeError),
    }
}

/// The exact decimal text of the integral part of a finite double.
fn float_to_int_text(value: f64) -> String {
    let whole = value.trunc();
    if whole.abs() < 1e18 {
        return format!("{}", whole as i64);
    }
    // Exact: a double this large is an integer m * 2**e.
    let bits = whole.abs().to_bits();
    let exponent = ((bits >> 52) & 0x7FF) as i32 - 1075;
    let mantissa = (bits & ((1 << 52) - 1)) | (1 << 52);
    let mut digits = vec![0u32; 1];
    // digits: little-endian base 1e9.
    let push = |value: u64, digits: &mut Vec<u32>| {
        let mut carry = value;
        for d in digits.iter_mut() {
            let v = u64::from(*d) + carry;
            *d = (v % 1_000_000_000) as u32;
            carry = v / 1_000_000_000;
        }
        while carry > 0 {
            digits.push((carry % 1_000_000_000) as u32);
            carry /= 1_000_000_000;
        }
    };
    push(mantissa, &mut digits);
    for _ in 0..exponent.max(0) {
        let mut carry = 0u64;
        for d in digits.iter_mut() {
            let v = u64::from(*d) * 2 + carry;
            *d = (v % 1_000_000_000) as u32;
            carry = v / 1_000_000_000;
        }
        if carry > 0 {
            digits.push(carry as u32);
        }
    }
    let mut text = String::new();
    if value < 0.0 {
        text.push('-');
    }
    let last = digits.len() - 1;
    text.push_str(&digits[last].to_string());
    for d in digits[..last].iter().rev() {
        text.push_str(&format!("{d:09}"));
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ints_from_text() {
        let ok = |s: &str| int_from_str(&unicode::cps(s)).unwrap();
        assert_eq!(ok(" 12 "), PyValue::int(12));
        assert_eq!(ok("-0"), PyValue::int(0));
        assert_eq!(ok("1_000"), PyValue::int(1000));
        assert_eq!(ok("١٢"), PyValue::int(12));
        assert!(int_from_str(&unicode::cps("1__0")).is_err());
        assert!(int_from_str(&unicode::cps("_1")).is_err());
        assert!(int_from_str(&unicode::cps("")).is_err());
    }

    #[test]
    fn floats_to_ints() {
        assert_eq!(float_to_int_text(1e300).len(), 301);
        assert_eq!(float_to_int_text(-2.5), "-2");
        assert_eq!(float_to_int_text(2f64.powi(70)), "1180591620717411303424");
    }

    #[test]
    fn reprs() {
        let r = |s: &str| unicode::to_string(&repr_str(&unicode::cps(s))).unwrap();
        assert_eq!(r("a'b"), "\"a'b\"");
        assert_eq!(r("a\n\u{7f}é\u{200b}"), "'a\\n\\x7fé\\u200b'");
    }
}
