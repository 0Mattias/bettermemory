//! The Python values the reference serialises and parses.
//!
//! A string is a sequence of code points ([`PyStr`]), since a Python `str`
//! may hold a lone surrogate. An integer keeps its decimal text, since a
//! Python `int` has no bound.

pub mod key_objects;

pub(crate) mod integer;
pub(crate) mod list_sort;

use std::cmp::Ordering;

use crate::datetime::DateTime;
use crate::float_repr;
use crate::unicode;

/// A Python `str`: code points, surrogates allowed.
pub type PyStr = Vec<u32>;

/// A Python value as the JSON writers and readers of the reference meet it.
///
/// `Clone`, `PartialEq`, `Debug` and `Drop` are written out without native
/// recursion (an explicit work list each), so a value of any depth a
/// caller builds is copied, compared, formatted and dropped without
/// exhausting the stack. They give exactly what the derived traits gave.
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

/// The type's name as CPython's error messages spell it (`tp_name`:
/// `'datetime.datetime' object has no attribute 'strip'`, where
/// `type(x).__name__` is `datetime`), checked against the reference for
/// every variant.
pub(crate) fn type_name(v: &PyValue) -> &'static str {
    match v {
        PyValue::None => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::Str(_) => "str",
        PyValue::DateTime(_) => "datetime.datetime",
        PyValue::Path(_) => "PurePosixPath",
        PyValue::Tuple(_) => "tuple",
        PyValue::List(_) => "list",
        PyValue::Dict(_) => "dict",
    }
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
    UnicodeDecodeError,
    RecursionError,
}

impl PyError {
    pub fn name(self) -> &'static str {
        match self {
            PyError::TypeError => "TypeError",
            PyError::ValueError => "ValueError",
            PyError::OverflowError => "OverflowError",
            PyError::UnicodeEncodeError => "UnicodeEncodeError",
            PyError::UnicodeDecodeError => "UnicodeDecodeError",
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

pub(crate) fn cmp_int_text(a: &str, b: &str) -> Ordering {
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

/// The exact decimal text of an integer-valued finite double.
fn integer_decimal(w: f64) -> String {
    if w.abs() < 9_223_372_036_854_775_808.0 {
        return (w as i64).to_string();
    }
    // |w| >= 2^63: w = mantissa * 2^exponent with exponent >= 11.
    let bits = w.to_bits();
    let exponent = ((bits >> 52) & 0x7ff) as i32 - 1075;
    let mantissa = (bits & ((1u64 << 52) - 1)) | (1u64 << 52);
    // Little-endian base-10^9 limbs, doubled `exponent` times.
    let mut limbs: Vec<u64> = vec![mantissa % 1_000_000_000, mantissa / 1_000_000_000];
    for _ in 0..exponent.max(0) {
        let mut carry = 0;
        for limb in &mut limbs {
            let v = *limb * 2 + carry;
            *limb = v % 1_000_000_000;
            carry = v / 1_000_000_000;
        }
        if carry > 0 {
            limbs.push(carry);
        }
    }
    while limbs.len() > 1 && limbs.last() == Some(&0) {
        limbs.pop();
    }
    let mut out = String::new();
    if w < 0.0 {
        out.push('-');
    }
    let mut iter = limbs.iter().rev();
    if let Some(top) = iter.next() {
        out.push_str(&top.to_string());
    }
    for limb in iter {
        out.push_str(&format!("{limb:09}"));
    }
    out
}

/// An int (canonical decimal text) against a float, exactly, as Python
/// compares them; None against NaN.
fn cmp_int_float(int: &str, float: f64) -> Option<Ordering> {
    if float.is_nan() {
        return None;
    }
    if float.is_infinite() {
        return Some(if float > 0.0 {
            Ordering::Less
        } else {
            Ordering::Greater
        });
    }
    let whole = float.trunc();
    let ordering = cmp_int_text(int, &integer_decimal(whole));
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

/// A total order on two dict keys that `<` compares, or None for a pair
/// `<` refuses (TypeError). It agrees with `<` on every pair without a
/// NaN or signed zero. JSON key sorting uses [`lt_keys`] so NaNs
/// keep the ordering CPython's comparison sequence gives them.
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

/// `a < b` of two dict keys, as Python answers it: None when `<` refuses
/// (TypeError); false both ways for a NaN.
pub fn lt_keys(a: &PyValue, b: &PyValue) -> Option<bool> {
    match (a, b) {
        (PyValue::Str(x), PyValue::Str(y)) => Some(x < y),
        _ => {
            let (x, y) = (number(a)?, number(b)?);
            Some(match (x, y) {
                (Number::Int(x), Number::Int(y)) => cmp_int_text(x, y) == Ordering::Less,
                (Number::Float(x), Number::Float(y)) => x < y,
                (Number::Int(x), Number::Float(y)) => cmp_int_float(x, y) == Some(Ordering::Less),
                (Number::Float(x), Number::Int(y)) => {
                    cmp_int_float(y, x) == Some(Ordering::Greater)
                }
            })
        }
    }
}

/// CPython's minimum run length: below it `list.sort` is one binary
/// insertion sort ([`python_small_sort`]).
pub const SMALL_SORT: usize = 64;

/// CPython 3.13's `list.sort` of fewer than [`SMALL_SORT`] items under
/// `lt`: `count_run` (an ascending run, or a descending one whose groups of
/// items equal under `lt` keep their order), then binary insertion of the
/// rest. With a consistent `lt` this is any stable sort; with one that is
/// not (a NaN key, false both ways against everything) it leaves each
/// item where CPython 3.13 does (3.11 and 3.12 read runs otherwise).
pub fn python_small_sort<T>(v: &mut [T], mut lt: impl FnMut(&T, &T) -> bool) {
    let n = v.len();
    if n < 2 {
        return;
    }
    let mut m = 1;
    while m < n && !lt(&v[m], &v[m - 1]) {
        m += 1;
    }
    if m < n {
        let mut descending = true;
        if m > 1 {
            if lt(&v[0], &v[m - 1]) {
                descending = false;
            } else {
                v[..m].reverse();
            }
        }
        if descending {
            let mut neq = 0;
            m += 1;
            while m < n {
                if lt(&v[m], &v[m - 1]) {
                    if neq > 0 {
                        v[m - neq - 1..m].reverse();
                        neq = 0;
                    }
                } else if lt(&v[m - 1], &v[m]) {
                    break;
                } else {
                    neq += 1;
                }
                m += 1;
            }
            if neq > 0 {
                v[m - neq - 1..m].reverse();
            }
            v[..m].reverse();
            while m < n && !lt(&v[m], &v[m - 1]) {
                m += 1;
            }
        }
    }
    for start in m..n {
        let (mut l, mut r) = (0, start);
        while l < r {
            let mid = l + (r - l) / 2;
            if lt(&v[start], &v[mid]) {
                r = mid;
            } else {
                l = mid + 1;
            }
        }
        v[l..=start].rotate_right(1);
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
/// past [`crate::json::MAX_DEPTH`] nested objects, as CPython 3.13's `repr`
/// raises on the 9,999th: every object `PyObject_Repr` spells counts, a
/// scalar at the bottom too, so 9,997 lists around an int are the most it
/// writes (9,998 around an empty list).
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
        // `PyObject_Repr` counts a level for every object it spells, a
        // scalar or an empty container at the bottom included.
        if depth + 1 > crate::json::MAX_DEPTH {
            return Err(PyError::RecursionError);
        }
        match value {
            PyValue::Str(s) => out.extend(repr_str(s)),
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

/// A container being rebuilt by [`PyValue::clone`]: its kind, the children
/// still to copy (a dict's keys and values in turn), the copies so far.
struct CloneFrame<'a> {
    kind: u8,
    rest: std::slice::Iter<'a, PyValue>,
    pairs: std::slice::Iter<'a, (PyValue, PyValue)>,
    pending: Option<&'a PyValue>,
    out: Vec<PyValue>,
}

impl<'a> CloneFrame<'a> {
    const TUPLE: u8 = 0;
    const LIST: u8 = 1;
    const DICT: u8 = 2;

    /// A scalar's copy, or the frame of a container.
    fn open(value: &'a PyValue) -> Result<PyValue, CloneFrame<'a>> {
        let frame = |kind, rest: &'a [PyValue], pairs: &'a [(PyValue, PyValue)], n| CloneFrame {
            kind,
            rest: rest.iter(),
            pairs: pairs.iter(),
            pending: None,
            out: Vec::with_capacity(n),
        };
        Ok(match value {
            PyValue::None => PyValue::None,
            PyValue::Bool(b) => PyValue::Bool(*b),
            PyValue::Int(t) => PyValue::Int(t.clone()),
            PyValue::Float(f) => PyValue::Float(*f),
            PyValue::Str(s) => PyValue::Str(s.clone()),
            PyValue::DateTime(d) => PyValue::DateTime(*d),
            PyValue::Path(s) => PyValue::Path(s.clone()),
            PyValue::Tuple(items) => return Err(frame(Self::TUPLE, items, &[], items.len())),
            PyValue::List(items) => return Err(frame(Self::LIST, items, &[], items.len())),
            PyValue::Dict(items) => return Err(frame(Self::DICT, &[], items, 2 * items.len())),
        })
    }

    fn next(&mut self) -> Option<&'a PyValue> {
        if self.kind != Self::DICT {
            return self.rest.next();
        }
        if let Some(v) = self.pending.take() {
            return Some(v);
        }
        let (k, v) = self.pairs.next()?;
        self.pending = Some(v);
        Some(k)
    }

    fn finish(mut self) -> PyValue {
        let out = std::mem::take(&mut self.out);
        match self.kind {
            Self::TUPLE => PyValue::Tuple(out),
            Self::LIST => PyValue::List(out),
            _ => {
                let mut pairs = Vec::with_capacity(out.len() / 2);
                let mut it = out.into_iter();
                while let (Some(k), Some(v)) = (it.next(), it.next()) {
                    pairs.push((k, v));
                }
                PyValue::Dict(pairs)
            }
        }
    }
}

impl Clone for PyValue {
    /// A deep copy, made without native recursion.
    fn clone(&self) -> Self {
        let mut stack = match CloneFrame::open(self) {
            Ok(scalar) => return scalar,
            Err(frame) => vec![frame],
        };
        loop {
            let top = stack.last_mut().expect("a frame");
            match top.next() {
                Some(child) => match CloneFrame::open(child) {
                    Ok(scalar) => top.out.push(scalar),
                    Err(frame) => stack.push(frame),
                },
                None => {
                    let done = stack.pop().expect("a frame").finish();
                    match stack.last_mut() {
                        Some(parent) => parent.out.push(done),
                        None => return done,
                    }
                }
            }
        }
    }
}

impl PartialEq for PyValue {
    /// The derived equality (variant by variant, `f64`'s `==` for a float,
    /// so a NaN equals nothing), compared without native recursion.
    fn eq(&self, other: &Self) -> bool {
        let mut work: Vec<(&PyValue, &PyValue)> = vec![(self, other)];
        while let Some((a, b)) = work.pop() {
            let same = match (a, b) {
                (PyValue::None, PyValue::None) => true,
                (PyValue::Bool(x), PyValue::Bool(y)) => x == y,
                (PyValue::Int(x), PyValue::Int(y)) => x == y,
                (PyValue::Float(x), PyValue::Float(y)) => x == y,
                (PyValue::Str(x), PyValue::Str(y)) | (PyValue::Path(x), PyValue::Path(y)) => x == y,
                (PyValue::DateTime(x), PyValue::DateTime(y)) => x == y,
                (PyValue::Tuple(x), PyValue::Tuple(y)) | (PyValue::List(x), PyValue::List(y)) => {
                    if x.len() != y.len() {
                        return false;
                    }
                    work.extend(x.iter().zip(y.iter()).rev());
                    true
                }
                (PyValue::Dict(x), PyValue::Dict(y)) => {
                    if x.len() != y.len() {
                        return false;
                    }
                    for ((ka, va), (kb, vb)) in x.iter().zip(y.iter()).rev() {
                        work.push((va, vb));
                        work.push((ka, kb));
                    }
                    true
                }
                _ => false,
            };
            if !same {
                return false;
            }
        }
        true
    }
}

impl std::fmt::Debug for PyValue {
    /// The derived `{:?}` text (`List([Int("1"), Str([97])])`), written
    /// without native recursion. `{:#?}` gives the same one-line text.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        enum Piece<'a> {
            Value(&'a PyValue),
            Text(&'static str),
        }
        let mut work = vec![Piece::Value(self)];
        while let Some(piece) = work.pop() {
            let value = match piece {
                Piece::Text(t) => {
                    f.write_str(t)?;
                    continue;
                }
                Piece::Value(v) => v,
            };
            match value {
                PyValue::None => f.write_str("None")?,
                PyValue::Bool(b) => write!(f, "Bool({b:?})")?,
                PyValue::Int(t) => write!(f, "Int({t:?})")?,
                PyValue::Float(x) => write!(f, "Float({x:?})")?,
                PyValue::Str(s) => write!(f, "Str({s:?})")?,
                PyValue::DateTime(d) => write!(f, "DateTime({d:?})")?,
                PyValue::Path(s) => write!(f, "Path({s:?})")?,
                PyValue::Tuple(items) | PyValue::List(items) => {
                    let open = if matches!(value, PyValue::Tuple(_)) {
                        "Tuple(["
                    } else {
                        "List(["
                    };
                    f.write_str(open)?;
                    work.push(Piece::Text("])"));
                    for (i, item) in items.iter().enumerate().rev() {
                        work.push(Piece::Value(item));
                        if i > 0 {
                            work.push(Piece::Text(", "));
                        }
                    }
                }
                PyValue::Dict(items) => {
                    f.write_str("Dict([")?;
                    work.push(Piece::Text("])"));
                    for (i, (k, v)) in items.iter().enumerate().rev() {
                        work.push(Piece::Text(")"));
                        work.push(Piece::Value(v));
                        work.push(Piece::Text(", "));
                        work.push(Piece::Value(k));
                        work.push(Piece::Text("("));
                        if i > 0 {
                            work.push(Piece::Text(", "));
                        }
                    }
                }
            }
        }
        Ok(())
    }
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

/// The whitespace `int()` strips: ASCII's six and every non-ASCII
/// `str.isspace` character (CPython maps the latter to a space, then
/// strips with `Py_ISSPACE`), so not U+001C..U+001F, which `isspace`
/// counts.
fn int_space(c: u32) -> bool {
    matches!(c, 0x09..=0x0D | 0x20) || (c >= 0x80 && unicode::is_space(c))
}

/// `int(text)` for a `str`: surrounding whitespace, a sign, Unicode
/// decimal digits and single underscores between digits; at most 4300
/// digits (`sys.int_info.default_max_str_digits`).
pub fn int_from_str(text: &[u32]) -> Result<PyValue, PyError> {
    let start = text.iter().position(|&c| !int_space(c));
    let Some(start) = start else {
        return Err(PyError::ValueError);
    };
    let end = text.iter().rposition(|&c| !int_space(c)).unwrap_or(start) + 1;
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

    /// The shape `PyValue` had with derived traits: their output is the
    /// standard the written-out traits are held to.
    #[derive(Clone, Debug, PartialEq)]
    enum Mirror {
        None,
        Bool(bool),
        Int(String),
        Float(f64),
        Str(PyStr),
        DateTime(DateTime),
        Path(PyStr),
        Tuple(Vec<Mirror>),
        List(Vec<Mirror>),
        Dict(Vec<(Mirror, Mirror)>),
    }

    fn mirror(v: &PyValue) -> Mirror {
        match v {
            PyValue::None => Mirror::None,
            PyValue::Bool(b) => Mirror::Bool(*b),
            PyValue::Int(t) => Mirror::Int(t.clone()),
            PyValue::Float(f) => Mirror::Float(*f),
            PyValue::Str(s) => Mirror::Str(s.clone()),
            PyValue::DateTime(d) => Mirror::DateTime(*d),
            PyValue::Path(s) => Mirror::Path(s.clone()),
            PyValue::Tuple(i) => Mirror::Tuple(i.iter().map(mirror).collect()),
            PyValue::List(i) => Mirror::List(i.iter().map(mirror).collect()),
            PyValue::Dict(i) => {
                Mirror::Dict(i.iter().map(|(k, v)| (mirror(k), mirror(v))).collect())
            }
        }
    }

    fn samples() -> Vec<PyValue> {
        let dt = DateTime::from_unix_micros_utc(1_234_567).expect("dt");
        let leaves = vec![
            PyValue::None,
            PyValue::Bool(true),
            PyValue::int(-12),
            PyValue::Float(f64::NAN),
            PyValue::Float(-0.0),
            PyValue::Float(1.5),
            PyValue::Str(vec![0xD800, 0x22, 0x41]),
            PyValue::DateTime(dt),
            PyValue::Path(unicode::cps("a/\"b")),
        ];
        let mut out = leaves.clone();
        out.push(PyValue::Tuple(leaves.clone()));
        out.push(PyValue::List(vec![]));
        out.push(PyValue::Dict(vec![]));
        out.push(PyValue::List(vec![
            PyValue::Tuple(vec![PyValue::List(leaves.clone())]),
            PyValue::Dict(vec![
                (PyValue::str("k"), PyValue::List(leaves.clone())),
                (
                    PyValue::Tuple(vec![PyValue::int(1)]),
                    PyValue::Dict(vec![(PyValue::None, PyValue::None)]),
                ),
            ]),
        ]));
        out
    }

    #[test]
    fn the_written_out_traits_are_the_derived_ones() {
        let all = samples();
        for a in &all {
            let c = a.clone();
            // Debug text, not `==`: a NaN equals nothing, a copy included.
            assert_eq!(format!("{:?}", mirror(&c)), format!("{:?}", mirror(a)));
            assert_eq!(format!("{a:?}"), format!("{:?}", mirror(a)));
            assert_eq!(format!("{a:#?}"), format!("{a:?}"));
            for b in &all {
                assert_eq!(a == b, mirror(a) == mirror(b), "{a:?} == {b:?}");
            }
        }
        // A changed leaf deep inside, and a different length, differ.
        let a = PyValue::List(vec![PyValue::Dict(vec![(
            PyValue::str("k"),
            PyValue::int(1),
        )])]);
        let b = PyValue::List(vec![PyValue::Dict(vec![(
            PyValue::str("k"),
            PyValue::int(2),
        )])]);
        assert_ne!(a, b);
        assert_ne!(PyValue::List(vec![]), PyValue::List(vec![PyValue::None]));
        assert_ne!(PyValue::List(vec![]), PyValue::Tuple(vec![]));
    }

    #[test]
    fn a_million_deep_value_is_cloned_compared_formatted_and_dropped() {
        std::thread::Builder::new()
            .stack_size(512 * 1024)
            .spawn(|| {
                let deep = |leaf: PyValue| {
                    let mut v = leaf;
                    for i in 0..1_000_000 {
                        v = if i % 3 == 0 {
                            PyValue::Dict(vec![(PyValue::str("k"), v)])
                        } else if i % 3 == 1 {
                            PyValue::Tuple(vec![v])
                        } else {
                            PyValue::List(vec![PyValue::None, v])
                        };
                    }
                    v
                };
                let a = deep(PyValue::int(1));
                let b = a.clone();
                assert!(a == b);
                let c = deep(PyValue::int(2));
                assert!(a != c);
                let text = format!("{a:?}");
                assert!(text.starts_with("Dict([(Str([107]), List([None, Tuple([Dict(["));
                assert!(text.contains("Int(\"1\")"));
                drop((a, b, c, text));
            })
            .expect("thread")
            .join()
            .expect("no stack overflow");
    }

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
        assert!(int_from_str(&unicode::cps("\u{1c}8")).is_err());
        assert_eq!(ok("\u{85}8\u{2028}"), PyValue::int(8));
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
