//! A TOML reader held to CPython 3.13's `tomllib.loads`.
//!
//! The parser is `Lib/tomllib/_parser.py` and `_re.py` written out rule
//! for rule: the same statements in the same order, the same position at
//! every refusal, so the same documents are accepted, the same values come
//! out (a table keeps its keys in insertion order, as a `dict` does) and
//! each refusal carries `tomllib`'s message with its `(at line L, column
//! C)` or `(at end of document)` suffix, counted in code points. The input
//! is a Rust `&str`, so it is already Unicode; the product decodes the
//! file's bytes first (`config.read_config_file`).
//!
//! Three outcomes are not `TOMLDecodeError` in the reference and are kept
//! apart here ([`Error`]): an integer of more than 4300 decimal digits is
//! the ValueError `int()` raises (`sys.int_info.default_max_str_digits`);
//! nesting too deep for the reference's recursion is RecursionError. The
//! reference's limit is its stack: from the top of a fresh interpreter it
//! takes 497 nested arrays and 331 nested inline tables (two and three
//! Python frames a level) and fails past them, and less when it is called
//! deeper in a stack. Here the levels cost the same two and three, and a
//! document whose open levels cost more than [`NESTING_BUDGET`] is refused,
//! which reproduces the top-of-stack limits exactly for either kind alone.
//!
//! Nothing recurses past that budget, nothing is quadratic in the input,
//! and an integer is kept as binary limbs, so a hostile document costs time
//! and memory linear in its length.

use std::collections::HashMap;

use crate::datetime::{days_in_month, DateTime};
use crate::float_repr;
use crate::pyvalue::repr_str;
use crate::unicode;

/// The frames a document's open arrays (2 each) and inline tables (3 each)
/// may cost before the reference's recursion limit is reached: 497 arrays
/// cost 994 and 331 tables 993, the deepest of each `tomllib` takes from
/// the top of a fresh interpreter's stack; one more of either costs 996.
///
/// The budget also bounds what the caller's stack does with a returned
/// value: every [`Value`] `loads` returns is at most 497 levels deep, the
/// library's recursive walks over one (`py_repr`, `config::toml_spelling`,
/// `config::report::toml_to_py`, the JSON dump of the reports) stay within
/// that depth, and dropping one does not recurse at all (`impl Drop for
/// Value`). `config_robustness::values_at_the_budget_on_a_small_stack`
/// runs check, show, path and the audit over such values, and drops them,
/// on a 2 MiB thread in an unoptimised build. A value a caller builds
/// deeper by hand is not bounded by this.
pub const NESTING_BUDGET: usize = 995;

/// `sys.int_info.default_max_str_digits`.
pub const INT_MAX_STR_DIGITS: usize = 4300;

/// A parsed TOML value, as `tomllib` returns it.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    String(String),
    Integer(Int),
    Float(f64),
    Boolean(bool),
    /// `datetime.datetime`, naive (a local date-time) or with its offset.
    DateTime(DateTime),
    /// `datetime.date`.
    Date(Date),
    /// `datetime.time` (always naive).
    Time(Time),
    Array(Vec<Value>),
    Table(Table),
}

/// `datetime.date`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Date {
    pub year: i32,
    pub month: u32,
    pub day: u32,
}

/// `datetime.time`, naive.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Time {
    pub hour: u32,
    pub minute: u32,
    pub second: u32,
    pub microsecond: u32,
}

impl Date {
    /// `date.isoformat()`, which is also `str(date)`.
    pub fn isoformat(&self) -> String {
        format!("{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }

    /// `repr(date)`.
    pub fn python_repr(&self) -> String {
        format!("datetime.date({}, {}, {})", self.year, self.month, self.day)
    }
}

impl Time {
    /// `time.isoformat()`, which is also `str(time)`.
    pub fn isoformat(&self) -> String {
        let mut out = format!("{:02}:{:02}:{:02}", self.hour, self.minute, self.second);
        if self.microsecond != 0 {
            out.push_str(&format!(".{:06}", self.microsecond));
        }
        out
    }

    /// `repr(time)`.
    pub fn python_repr(&self) -> String {
        let mut out = format!("datetime.time({}, {}", self.hour, self.minute);
        if self.second != 0 || self.microsecond != 0 {
            out.push_str(&format!(", {}", self.second));
        }
        if self.microsecond != 0 {
            out.push_str(&format!(", {}", self.microsecond));
        }
        out.push(')');
        out
    }
}

/// A Python `int` of any size: a sign and little-endian 32-bit limbs with
/// no high zero limb (zero is no limbs and not negative).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Int {
    negative: bool,
    limbs: Vec<u32>,
}

/// `str(int)` past [`INT_MAX_STR_DIGITS`] digits: the ValueError CPython
/// raises.
pub const INT_STR_LIMIT_MESSAGE: &str = "Exceeds the limit (4300 digits) for integer string \
     conversion; use sys.set_int_max_str_digits() to increase the limit";

impl Int {
    pub fn from_i128(value: i128) -> Int {
        let negative = value < 0;
        let mut magnitude = value.unsigned_abs();
        let mut limbs = Vec::new();
        while magnitude != 0 {
            limbs.push(magnitude as u32);
            magnitude >>= 32;
        }
        Int { negative, limbs }
    }

    pub fn is_negative(&self) -> bool {
        self.negative
    }

    pub fn is_zero(&self) -> bool {
        self.limbs.is_empty()
    }

    /// The value, when it fits an i128.
    pub fn to_i128(&self) -> Option<i128> {
        if self.limbs.len() > 4 {
            return None;
        }
        let mut magnitude: u128 = 0;
        for &limb in self.limbs.iter().rev() {
            magnitude = (magnitude << 32) | u128::from(limb);
        }
        if self.negative {
            if magnitude > i128::MAX as u128 + 1 {
                None
            } else {
                Some((magnitude as i128).wrapping_neg())
            }
        } else {
            i128::try_from(magnitude).ok()
        }
    }

    /// Order against an i128, at any size.
    pub fn cmp_i128(&self, other: i128) -> std::cmp::Ordering {
        match self.to_i128() {
            Some(v) => v.cmp(&other),
            None if self.negative => std::cmp::Ordering::Less,
            None => std::cmp::Ordering::Greater,
        }
    }

    fn mul_add(&mut self, factor: u32, addend: u32) {
        let mut carry = u64::from(addend);
        for limb in self.limbs.iter_mut() {
            let v = u64::from(*limb) * u64::from(factor) + carry;
            *limb = v as u32;
            carry = v >> 32;
        }
        if carry != 0 {
            self.limbs.push(carry as u32);
        }
    }

    /// Digits of a power-of-two base (`bits` per digit), most significant
    /// first: linear in their count.
    fn from_pow2_digits(digits: &[u8], bits: u32) -> Int {
        let mut limbs = Vec::with_capacity(digits.len() * bits as usize / 32 + 1);
        let (mut acc, mut filled) = (0u64, 0u32);
        for &d in digits.iter().rev() {
            acc |= u64::from(d) << filled;
            filled += bits;
            if filled >= 32 {
                limbs.push(acc as u32);
                acc >>= 32;
                filled -= 32;
            }
        }
        if filled > 0 {
            limbs.push(acc as u32);
        }
        while limbs.last() == Some(&0) {
            limbs.pop();
        }
        Int {
            negative: false,
            limbs,
        }
    }

    /// The decimal digits (at most [`INT_MAX_STR_DIGITS`] of them, as the
    /// caller checks) as an Int.
    fn from_decimal_digits(digits: &[u8]) -> Int {
        let mut out = Int {
            negative: false,
            limbs: Vec::new(),
        };
        for chunk in digits.chunks(9) {
            let mut factor = 1u32;
            let mut addend = 0u32;
            for &d in chunk {
                factor *= 10;
                addend = addend * 10 + u32::from(d);
            }
            out.mul_add(factor, addend);
        }
        while out.limbs.last() == Some(&0) {
            out.limbs.pop();
        }
        out
    }

    /// An int from canonical decimal text (an optional `-`, ASCII digits).
    pub fn from_decimal_text(text: &str) -> Int {
        let (negative, digits) = match text.strip_prefix('-') {
            Some(rest) => (true, rest),
            None => (false, text),
        };
        let digits: Vec<u8> = digits.bytes().map(|b| b - b'0').collect();
        let mut out = Int::from_decimal_digits(&digits);
        out.negative = negative && !out.is_zero();
        out
    }

    /// `float(int)`, correctly rounded; None where it overflows.
    pub fn to_f64(&self) -> Option<f64> {
        let text = self.to_decimal().ok()?;
        let value: f64 = text.parse().ok()?;
        value.is_finite().then_some(value)
    }

    /// `hex(int)`: `0x` and lowercase digits, a `-` before a negative.
    pub fn to_hex(&self) -> String {
        let mut out = String::from(if self.negative { "-0x" } else { "0x" });
        match self.limbs.split_last() {
            None => out.push('0'),
            Some((top, rest)) => {
                out.push_str(&format!("{top:x}"));
                for limb in rest.iter().rev() {
                    out.push_str(&format!("{limb:08x}"));
                }
            }
        }
        out
    }

    /// `str(int)`, or the ValueError CPython raises past 4300 digits.
    pub fn to_decimal(&self) -> Result<String, &'static str> {
        // 4300 decimal digits need at most 14284 bits; anything wider has
        // more digits than the limit.
        if self.limbs.len() * 32 > 14_400 {
            return Err(INT_STR_LIMIT_MESSAGE);
        }
        let mut limbs = self.limbs.clone();
        let mut chunks: Vec<u32> = Vec::new();
        while !limbs.is_empty() {
            let mut rem = 0u64;
            for limb in limbs.iter_mut().rev() {
                let cur = (rem << 32) | u64::from(*limb);
                *limb = (cur / 1_000_000_000) as u32;
                rem = cur % 1_000_000_000;
            }
            while limbs.last() == Some(&0) {
                limbs.pop();
            }
            chunks.push(rem as u32);
        }
        let mut out = String::new();
        if self.negative {
            out.push('-');
        }
        match chunks.split_last() {
            None => out.push('0'),
            Some((top, rest)) => {
                out.push_str(&top.to_string());
                for chunk in rest.iter().rev() {
                    out.push_str(&format!("{chunk:09}"));
                }
            }
        }
        let digits = out.len() - usize::from(self.negative);
        if digits > INT_MAX_STR_DIGITS {
            return Err(INT_STR_LIMIT_MESSAGE);
        }
        Ok(out)
    }
}

/// A table: its keys in insertion order, as a Python `dict` keeps them.
#[derive(Clone, Debug, Default)]
pub struct Table {
    entries: Vec<(String, Value)>,
    index: HashMap<String, usize>,
}

impl PartialEq for Table {
    fn eq(&self, other: &Table) -> bool {
        self.entries == other.entries
    }
}

impl Table {
    pub fn new() -> Table {
        Table::default()
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn contains_key(&self, key: &str) -> bool {
        self.index.contains_key(key)
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        self.index.get(key).map(|&i| &self.entries[i].1)
    }

    pub fn get_mut(&mut self, key: &str) -> Option<&mut Value> {
        match self.index.get(key) {
            Some(&i) => Some(&mut self.entries[i].1),
            None => None,
        }
    }

    /// `d[key] = value`: a new key goes last, an existing one keeps its
    /// place.
    pub fn insert(&mut self, key: String, value: Value) {
        match self.index.get(&key) {
            Some(&i) => self.entries[i].1 = value,
            None => {
                self.index.insert(key.clone(), self.entries.len());
                self.entries.push((key, value));
            }
        }
    }

    /// `d.pop(key, None)`.
    pub fn remove(&mut self, key: &str) -> Option<Value> {
        let i = self.index.remove(key)?;
        let (_, value) = self.entries.remove(i);
        for slot in self.index.values_mut() {
            if *slot > i {
                *slot -= 1;
            }
        }
        Some(value)
    }

    pub fn iter(&self) -> impl Iterator<Item = (&String, &Value)> {
        self.entries.iter().map(|(k, v)| (k, v))
    }

    pub fn keys(&self) -> impl Iterator<Item = &String> {
        self.entries.iter().map(|(k, _)| k)
    }
}

impl Drop for Value {
    /// Drops nested containers without native recursion.
    fn drop(&mut self) {
        let mut stack: Vec<Value> = Vec::new();
        let take = |value: &mut Value, stack: &mut Vec<Value>| match value {
            Value::Array(items) => stack.append(items),
            Value::Table(table) => {
                table.index.clear();
                stack.extend(table.entries.drain(..).map(|(_, v)| v));
            }
            _ => {}
        };
        take(self, &mut stack);
        while let Some(mut value) = stack.pop() {
            take(&mut value, &mut stack);
        }
    }
}

/// Why `tomllib.loads` did not return a document.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Error {
    /// `tomllib.TOMLDecodeError`, its whole message (with the location).
    Decode(String),
    /// The ValueError `int()` raises on more than 4300 decimal digits.
    IntDigits(String),
    /// RecursionError: nested past [`NESTING_BUDGET`].
    Recursion,
}

impl Error {
    /// The exception's class name.
    pub fn class(&self) -> &'static str {
        match self {
            Error::Decode(_) => "TOMLDecodeError",
            Error::IntDigits(_) => "ValueError",
            Error::Recursion => "RecursionError",
        }
    }

    /// `str(exc)`.
    pub fn message(&self) -> String {
        match self {
            Error::Decode(m) | Error::IntDigits(m) => m.clone(),
            Error::Recursion => "maximum recursion depth exceeded".to_owned(),
        }
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message())
    }
}

type Key = Vec<String>;

const FROZEN: u8 = 1;
const EXPLICIT_NEST: u8 = 2;

#[derive(Default)]
struct FlagNode {
    flags: u8,
    recursive: u8,
    nested: HashMap<String, FlagNode>,
}

#[derive(Default)]
struct Flags {
    root: HashMap<String, FlagNode>,
    pending: Vec<(Key, u8)>,
}

impl Flags {
    fn add_pending(&mut self, key: Key, flag: u8) {
        self.pending.push((key, flag));
    }

    fn finalize_pending(&mut self) {
        for (key, flag) in std::mem::take(&mut self.pending) {
            self.set(&key, flag, false);
        }
    }

    fn unset_all(&mut self, key: &[String]) {
        let mut cont = &mut self.root;
        let (stem, parent) = key.split_last().expect("a key has a part");
        for k in parent {
            match cont.get_mut(k) {
                Some(node) => cont = &mut node.nested,
                None => return,
            }
        }
        cont.remove(stem);
    }

    fn set(&mut self, key: &[String], flag: u8, recursive: bool) {
        let mut cont = &mut self.root;
        let (stem, parent) = key.split_last().expect("a key has a part");
        for k in parent {
            cont = &mut cont.entry(k.clone()).or_default().nested;
        }
        let node = cont.entry(stem.clone()).or_default();
        if recursive {
            node.recursive |= flag;
        } else {
            node.flags |= flag;
        }
    }

    fn is(&self, key: &[String], flag: u8) -> bool {
        let Some((stem, parent)) = key.split_last() else {
            return false;
        };
        let mut cont = &self.root;
        for k in parent {
            let Some(inner) = cont.get(k) else {
                return false;
            };
            if inner.recursive & flag != 0 {
                return true;
            }
            cont = &inner.nested;
        }
        match cont.get(stem) {
            Some(node) => (node.flags | node.recursive) & flag != 0,
            None => false,
        }
    }
}

/// `NestedDict.get_or_create_nest`. Err is the KeyError it raises.
fn get_or_create_nest<'a>(
    root: &'a mut Table,
    key: &[String],
    access_lists: bool,
) -> Result<&'a mut Table, ()> {
    let mut cont = root;
    for k in key {
        if !cont.contains_key(k) {
            cont.insert(k.clone(), Value::Table(Table::new()));
        }
        let mut value = cont.get_mut(k).expect("just inserted");
        if access_lists {
            if let Value::Array(items) = value {
                // An empty list cannot be reached here: one made by `[[...]]`
                // has an item, and a value's is frozen.
                value = items.last_mut().ok_or(())?;
            }
        }
        match value {
            Value::Table(table) => cont = table,
            _ => return Err(()),
        }
    }
    Ok(cont)
}

fn append_nest_to_list(root: &mut Table, key: &[String]) -> Result<(), ()> {
    let (last, parent) = key.split_last().expect("a key has a part");
    let cont = get_or_create_nest(root, parent, true)?;
    match cont.get_mut(last) {
        Some(Value::Array(items)) => {
            items.push(Value::Table(Table::new()));
            Ok(())
        }
        Some(_) => Err(()),
        None => {
            cont.insert(last.clone(), Value::Array(vec![Value::Table(Table::new())]));
            Ok(())
        }
    }
}

struct Parser<'a> {
    src: &'a [u8],
    /// Open nesting levels' cost (see [`NESTING_BUDGET`]).
    depth: usize,
}

fn is_ws(c: u8) -> bool {
    c == b' ' || c == b'\t'
}

fn is_bare(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'-' || c == b'_'
}

fn is_ascii_ctrl(c: u8) -> bool {
    c < 32 || c == 127
}

/// ILLEGAL_BASIC_STR_CHARS (and the literal and comment ones).
fn illegal_line(c: u8) -> bool {
    is_ascii_ctrl(c) && c != b'\t'
}

/// ILLEGAL_MULTILINE_*_STR_CHARS.
fn illegal_multiline(c: u8) -> bool {
    is_ascii_ctrl(c) && c != b'\t' && c != b'\n'
}

/// `repr` of a key tuple, as the messages print it.
fn key_repr(key: &[String]) -> String {
    let mut out = String::from("(");
    for (i, part) in key.iter().enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        out.push_str(&py_repr_str(part));
    }
    if key.len() == 1 {
        out.push(',');
    }
    out.push(')');
    out
}

/// `repr(s)` of a `str`.
pub fn py_repr_str(s: &str) -> String {
    let cps = repr_str(&unicode::cps(s));
    unicode::to_string(&cps).expect("a repr of text is text")
}

fn char_repr(c: u8) -> String {
    py_repr_str(&char::from(c).to_string())
}

impl<'a> Parser<'a> {
    fn at(&self, pos: usize) -> Option<u8> {
        self.src.get(pos).copied()
    }

    fn starts_with(&self, pos: usize, text: &[u8]) -> bool {
        pos <= self.src.len() && self.src[pos..].starts_with(text)
    }

    /// `suffixed_err`: the message with the location in code points.
    fn err(&self, pos: usize, msg: &str) -> Error {
        let src = self.src;
        let at = if pos >= src.len() {
            "end of document".to_owned()
        } else {
            let before = &src[..pos];
            let line = before.iter().filter(|&&b| b == b'\n').count() + 1;
            let line_start = match before.iter().rposition(|&b| b == b'\n') {
                Some(i) => i + 1,
                None => 0,
            };
            // Code points from the line's start to pos, plus one: UTF-8
            // continuation bytes are not counted.
            let column = src[line_start..pos]
                .iter()
                .filter(|&&b| (b & 0xC0) != 0x80)
                .count()
                + 1;
            format!("line {line}, column {column}")
        };
        Error::Decode(format!("{msg} (at {at})"))
    }

    fn skip_ws(&self, mut pos: usize) -> usize {
        while matches!(self.at(pos), Some(c) if is_ws(c)) {
            pos += 1;
        }
        pos
    }

    fn skip_ws_nl(&self, mut pos: usize) -> usize {
        while matches!(self.at(pos), Some(c) if is_ws(c) || c == b'\n') {
            pos += 1;
        }
        pos
    }

    fn find(&self, pos: usize, expect: &[u8]) -> Option<usize> {
        if pos > self.src.len() {
            return None;
        }
        if expect.len() == 1 {
            return self.src[pos..]
                .iter()
                .position(|&b| b == expect[0])
                .map(|i| pos + i);
        }
        self.src[pos..]
            .windows(expect.len())
            .position(|w| w == expect)
            .map(|i| pos + i)
    }

    fn skip_until(
        &self,
        mut pos: usize,
        expect: &[u8],
        error_on: fn(u8) -> bool,
        error_on_eof: bool,
    ) -> Result<usize, Error> {
        let new_pos = match self.find(pos, expect) {
            Some(p) => p,
            None => {
                let end = self.src.len();
                if error_on_eof {
                    let shown = py_repr_str(std::str::from_utf8(expect).expect("ASCII"));
                    return Err(self.err(end, &format!("Expected {shown}")));
                }
                end
            }
        };
        if self.src[pos.min(new_pos)..new_pos]
            .iter()
            .any(|&c| error_on(c))
        {
            while !error_on(self.src[pos]) {
                pos += 1;
            }
            let shown = char_repr(self.src[pos]);
            return Err(self.err(pos, &format!("Found invalid character {shown}")));
        }
        Ok(new_pos)
    }

    fn skip_comment(&self, pos: usize) -> Result<usize, Error> {
        if self.at(pos) == Some(b'#') {
            return self.skip_until(pos + 1, b"\n", illegal_line, false);
        }
        Ok(pos)
    }

    fn skip_comments_and_array_ws(&self, mut pos: usize) -> Result<usize, Error> {
        loop {
            let before = pos;
            pos = self.skip_ws_nl(pos);
            pos = self.skip_comment(pos)?;
            if pos == before {
                return Ok(pos);
            }
        }
    }

    fn create_dict_rule(
        &self,
        pos: usize,
        data: &mut Table,
        flags: &mut Flags,
    ) -> Result<(usize, Key), Error> {
        let pos = self.skip_ws(pos + 1);
        let (pos, key) = self.parse_key(pos)?;
        if flags.is(&key, EXPLICIT_NEST) || flags.is(&key, FROZEN) {
            return Err(self.err(pos, &format!("Cannot declare {} twice", key_repr(&key))));
        }
        flags.set(&key, EXPLICIT_NEST, false);
        if get_or_create_nest(data, &key, true).is_err() {
            return Err(self.err(pos, "Cannot overwrite a value"));
        }
        if !self.starts_with(pos, b"]") {
            return Err(self.err(pos, "Expected ']' at the end of a table declaration"));
        }
        Ok((pos + 1, key))
    }

    fn create_list_rule(
        &self,
        pos: usize,
        data: &mut Table,
        flags: &mut Flags,
    ) -> Result<(usize, Key), Error> {
        let pos = self.skip_ws(pos + 2);
        let (pos, key) = self.parse_key(pos)?;
        if flags.is(&key, FROZEN) {
            return Err(self.err(
                pos,
                &format!("Cannot mutate immutable namespace {}", key_repr(&key)),
            ));
        }
        flags.unset_all(&key);
        flags.set(&key, EXPLICIT_NEST, false);
        if append_nest_to_list(data, &key).is_err() {
            return Err(self.err(pos, "Cannot overwrite a value"));
        }
        if !self.starts_with(pos, b"]]") {
            return Err(self.err(pos, "Expected ']]' at the end of an array declaration"));
        }
        Ok((pos + 2, key))
    }

    fn key_value_rule(
        &mut self,
        pos: usize,
        data: &mut Table,
        flags: &mut Flags,
        header: &[String],
    ) -> Result<usize, Error> {
        let (pos, key, value) = self.parse_key_value_pair(pos)?;
        let (stem, parent) = key.split_last().expect("a key has a part");
        let mut abs_parent: Key = header.to_vec();
        abs_parent.extend(parent.iter().cloned());
        for i in 1..key.len() {
            let mut cont_key: Key = header.to_vec();
            cont_key.extend(key[..i].iter().cloned());
            if flags.is(&cont_key, EXPLICIT_NEST) {
                return Err(self.err(
                    pos,
                    &format!("Cannot redefine namespace {}", key_repr(&cont_key)),
                ));
            }
            flags.add_pending(cont_key, EXPLICIT_NEST);
        }
        if flags.is(&abs_parent, FROZEN) {
            return Err(self.err(
                pos,
                &format!(
                    "Cannot mutate immutable namespace {}",
                    key_repr(&abs_parent)
                ),
            ));
        }
        let Ok(nest) = get_or_create_nest(data, &abs_parent, true) else {
            return Err(self.err(pos, "Cannot overwrite a value"));
        };
        if nest.contains_key(stem) {
            return Err(self.err(pos, "Cannot overwrite a value"));
        }
        if matches!(value, Value::Array(_) | Value::Table(_)) {
            let mut full: Key = header.to_vec();
            full.extend(key.iter().cloned());
            flags.set(&full, FROZEN, true);
        }
        nest.insert(stem.clone(), value);
        Ok(pos)
    }

    fn parse_key_value_pair(&mut self, pos: usize) -> Result<(usize, Key, Value), Error> {
        let (pos, key) = self.parse_key(pos)?;
        if self.at(pos) != Some(b'=') {
            return Err(self.err(pos, "Expected '=' after a key in a key/value pair"));
        }
        let pos = self.skip_ws(pos + 1);
        let (pos, value) = self.parse_value(pos)?;
        Ok((pos, key, value))
    }

    fn parse_key(&self, pos: usize) -> Result<(usize, Key), Error> {
        let (pos, part) = self.parse_key_part(pos)?;
        let mut key = vec![part];
        let mut pos = self.skip_ws(pos);
        loop {
            if self.at(pos) != Some(b'.') {
                return Ok((pos, key));
            }
            pos = self.skip_ws(pos + 1);
            let (next, part) = self.parse_key_part(pos)?;
            key.push(part);
            pos = self.skip_ws(next);
        }
    }

    fn parse_key_part(&self, pos: usize) -> Result<(usize, String), Error> {
        match self.at(pos) {
            Some(c) if is_bare(c) => {
                let mut end = pos;
                while matches!(self.at(end), Some(c) if is_bare(c)) {
                    end += 1;
                }
                Ok((end, self.text(pos, end)))
            }
            Some(b'\'') => self.parse_literal_str(pos),
            Some(b'"') => self.parse_basic_str(pos + 1, false),
            _ => Err(self.err(pos, "Invalid initial character for a key part")),
        }
    }

    fn text(&self, start: usize, end: usize) -> String {
        // Both ends sit on ASCII characters or the ends of the text, so
        // the slice is whole UTF-8.
        std::str::from_utf8(&self.src[start..end])
            .expect("sliced at character boundaries")
            .to_owned()
    }

    fn enter(&mut self, cost: usize) -> Result<(), Error> {
        self.depth += cost;
        if self.depth > NESTING_BUDGET {
            return Err(Error::Recursion);
        }
        Ok(())
    }

    fn parse_array(&mut self, pos: usize) -> Result<(usize, Value), Error> {
        self.enter(2)?;
        let result = self.parse_array_inner(pos);
        self.depth -= 2;
        result
    }

    fn parse_array_inner(&mut self, pos: usize) -> Result<(usize, Value), Error> {
        let mut array = Vec::new();
        let mut pos = self.skip_comments_and_array_ws(pos + 1)?;
        if self.starts_with(pos, b"]") {
            return Ok((pos + 1, Value::Array(array)));
        }
        loop {
            let (next, value) = self.parse_value(pos)?;
            array.push(value);
            pos = self.skip_comments_and_array_ws(next)?;
            match self.at(pos) {
                Some(b']') => return Ok((pos + 1, Value::Array(array))),
                Some(b',') => {}
                _ => return Err(self.err(pos, "Unclosed array")),
            }
            pos = self.skip_comments_and_array_ws(pos + 1)?;
            if self.starts_with(pos, b"]") {
                return Ok((pos + 1, Value::Array(array)));
            }
        }
    }

    fn parse_inline_table(&mut self, pos: usize) -> Result<(usize, Value), Error> {
        self.enter(3)?;
        let result = self.parse_inline_table_inner(pos);
        self.depth -= 3;
        result
    }

    fn parse_inline_table_inner(&mut self, pos: usize) -> Result<(usize, Value), Error> {
        let mut table = Table::new();
        let mut flags = Flags::default();
        let mut pos = self.skip_ws(pos + 1);
        if self.starts_with(pos, b"}") {
            return Ok((pos + 1, Value::Table(table)));
        }
        loop {
            let (next, key, value) = self.parse_key_value_pair(pos)?;
            pos = next;
            let (stem, parent) = key.split_last().expect("a key has a part");
            if flags.is(&key, FROZEN) {
                return Err(self.err(
                    pos,
                    &format!("Cannot mutate immutable namespace {}", key_repr(&key)),
                ));
            }
            let Ok(nest) = get_or_create_nest(&mut table, parent, false) else {
                return Err(self.err(pos, "Cannot overwrite a value"));
            };
            if nest.contains_key(stem) {
                return Err(self.err(
                    pos,
                    &format!("Duplicate inline table key {}", py_repr_str(stem)),
                ));
            }
            let container = matches!(value, Value::Array(_) | Value::Table(_));
            nest.insert(stem.clone(), value);
            pos = self.skip_ws(pos);
            match self.at(pos) {
                Some(b'}') => return Ok((pos + 1, Value::Table(table))),
                Some(b',') => {}
                _ => return Err(self.err(pos, "Unclosed inline table")),
            }
            if container {
                flags.set(&key, FROZEN, true);
            }
            pos = self.skip_ws(pos + 1);
        }
    }

    fn parse_basic_str_escape(
        &self,
        pos: usize,
        multiline: bool,
        out: &mut String,
    ) -> Result<usize, Error> {
        let id = &self.src[pos..(pos + 2).min(self.src.len())];
        let mut pos = pos + 2;
        if multiline && (id == b"\\ " || id == b"\\\t" || id == b"\\\n") {
            if id != b"\\\n" {
                pos = self.skip_ws(pos);
                match self.at(pos) {
                    None => return Ok(pos),
                    Some(b'\n') => pos += 1,
                    Some(_) => return Err(self.err(pos, "Unescaped '\\' in a string")),
                }
            }
            return Ok(self.skip_ws_nl(pos));
        }
        let replacement = match id {
            b"\\u" => return self.parse_hex_char(pos, 4, out),
            b"\\U" => return self.parse_hex_char(pos, 8, out),
            b"\\b" => '\u{8}',
            b"\\t" => '\t',
            b"\\n" => '\n',
            b"\\f" => '\u{c}',
            b"\\r" => '\r',
            b"\\\"" => '"',
            b"\\\\" => '\\',
            _ => return Err(self.err(pos, "Unescaped '\\' in a string")),
        };
        out.push(replacement);
        Ok(pos)
    }

    fn parse_hex_char(&self, pos: usize, len: usize, out: &mut String) -> Result<usize, Error> {
        // `src[pos:pos+len]` counts code points; a non-ASCII one among
        // them fails the hex test either way, so bytes give the same answer.
        let end = pos + len;
        if end > self.src.len() || !self.src[pos..end].iter().all(u8::is_ascii_hexdigit) {
            return Err(self.err(pos, "Invalid hex value"));
        }
        let text = std::str::from_utf8(&self.src[pos..end]).expect("ASCII");
        let value = u32::from_str_radix(text, 16).expect("hex digits");
        match char::from_u32(value) {
            Some(c) => {
                out.push(c);
                Ok(end)
            }
            None => Err(self.err(end, "Escaped character is not a Unicode scalar value")),
        }
    }

    fn parse_literal_str(&self, pos: usize) -> Result<(usize, String), Error> {
        let start = pos + 1;
        let end = self.skip_until(start, b"'", illegal_line, true)?;
        Ok((end + 1, self.text(start, end)))
    }

    fn parse_multiline_str(&self, pos: usize, literal: bool) -> Result<(usize, Value), Error> {
        let mut pos = pos + 3;
        if self.starts_with(pos, b"\n") {
            pos += 1;
        }
        let (delim, mut result) = if literal {
            let end = self.skip_until(pos, b"'''", illegal_multiline, true)?;
            let text = self.text(pos, end);
            pos = end + 3;
            (b'\'', text)
        } else {
            let (next, text) = self.parse_basic_str(pos, true)?;
            pos = next;
            (b'"', text)
        };
        for _ in 0..2 {
            if !self.starts_with(pos, &[delim]) {
                break;
            }
            pos += 1;
            result.push(char::from(delim));
        }
        Ok((pos, Value::String(result)))
    }

    fn parse_basic_str(&self, pos: usize, multiline: bool) -> Result<(usize, String), Error> {
        let illegal = if multiline {
            illegal_multiline
        } else {
            illegal_line
        };
        let mut result = String::new();
        let mut start = pos;
        let mut pos = pos;
        loop {
            let Some(c) = self.at(pos) else {
                return Err(self.err(pos, "Unterminated string"));
            };
            if c == b'"' {
                if !multiline {
                    result.push_str(&self.text(start, pos));
                    return Ok((pos + 1, result));
                }
                if self.starts_with(pos, b"\"\"\"") {
                    result.push_str(&self.text(start, pos));
                    return Ok((pos + 3, result));
                }
                pos += 1;
                continue;
            }
            if c == b'\\' {
                result.push_str(&self.text(start, pos));
                pos = self.parse_basic_str_escape(pos, multiline, &mut result)?;
                start = pos;
                continue;
            }
            if illegal(c) {
                return Err(self.err(pos, &format!("Illegal character {}", char_repr(c))));
            }
            pos += 1;
        }
    }

    fn parse_value(&mut self, pos: usize) -> Result<(usize, Value), Error> {
        let c = self.at(pos);
        match c {
            Some(b'"') => {
                if self.starts_with(pos, b"\"\"\"") {
                    return self.parse_multiline_str(pos, false);
                }
                let (pos, s) = self.parse_basic_str(pos + 1, false)?;
                return Ok((pos, Value::String(s)));
            }
            Some(b'\'') => {
                if self.starts_with(pos, b"'''") {
                    return self.parse_multiline_str(pos, true);
                }
                let (pos, s) = self.parse_literal_str(pos)?;
                return Ok((pos, Value::String(s)));
            }
            Some(b't') if self.starts_with(pos, b"true") => {
                return Ok((pos + 4, Value::Boolean(true)));
            }
            Some(b'f') if self.starts_with(pos, b"false") => {
                return Ok((pos + 5, Value::Boolean(false)));
            }
            Some(b'[') => return self.parse_array(pos),
            Some(b'{') => return self.parse_inline_table(pos),
            _ => {}
        }
        if let Some(m) = self.match_datetime(pos) {
            return match m.value() {
                Some(value) => Ok((m.end, value)),
                None => Err(self.err(pos, "Invalid date or datetime")),
            };
        }
        if let Some((end, time)) = self.match_time(pos) {
            return Ok((end, Value::Time(time)));
        }
        if let Some(m) = self.match_number(pos) {
            return self.number_value(pos, m).map(|v| (m.end, v));
        }
        let first_three = &self.src[pos.min(self.src.len())..(pos + 3).min(self.src.len())];
        if first_three == b"inf" {
            return Ok((pos + 3, Value::Float(f64::INFINITY)));
        }
        if first_three == b"nan" {
            return Ok((pos + 3, Value::Float(f64::NAN)));
        }
        let first_four = &self.src[pos.min(self.src.len())..(pos + 4).min(self.src.len())];
        match first_four {
            b"-inf" => return Ok((pos + 4, Value::Float(f64::NEG_INFINITY))),
            b"+inf" => return Ok((pos + 4, Value::Float(f64::INFINITY))),
            b"-nan" => return Ok((pos + 4, Value::Float(-f64::NAN))),
            b"+nan" => return Ok((pos + 4, Value::Float(f64::NAN))),
            _ => {}
        }
        Err(self.err(pos, "Invalid value"))
    }

    fn digits(&self, pos: usize, n: usize) -> Option<u32> {
        if pos + n > self.src.len() {
            return None;
        }
        let mut v = 0u32;
        for &c in &self.src[pos..pos + n] {
            if !c.is_ascii_digit() {
                return None;
            }
            v = v * 10 + u32::from(c - b'0');
        }
        Some(v)
    }

    /// `_TIME_RE_STR` at pos: HH:MM:SS with an optional fraction of 1 to 6
    /// digits kept and any more dropped.
    fn match_time(&self, pos: usize) -> Option<(usize, Time)> {
        let hour = self.digits(pos, 2).filter(|&h| h <= 23)?;
        if self.at(pos + 2) != Some(b':') {
            return None;
        }
        let minute = self.digits(pos + 3, 2).filter(|&m| m <= 59)?;
        if self.at(pos + 5) != Some(b':') {
            return None;
        }
        let second = self.digits(pos + 6, 2).filter(|&s| s <= 59)?;
        let mut end = pos + 8;
        let mut microsecond = 0;
        if self.at(end) == Some(b'.') && matches!(self.at(end + 1), Some(c) if c.is_ascii_digit()) {
            let mut i = end + 1;
            let mut kept = String::new();
            while matches!(self.at(i), Some(c) if c.is_ascii_digit()) {
                if kept.len() < 6 {
                    kept.push(char::from(self.src[i]));
                }
                i += 1;
            }
            while kept.len() < 6 {
                kept.push('0');
            }
            microsecond = kept.parse().expect("six digits");
            end = i;
        }
        Some((
            end,
            Time {
                hour,
                minute,
                second,
                microsecond,
            },
        ))
    }

    fn match_datetime(&self, pos: usize) -> Option<DateMatch> {
        let year = self.digits(pos, 4)?;
        if self.at(pos + 4) != Some(b'-') {
            return None;
        }
        let month = self.digits(pos + 5, 2).filter(|m| (1..=12).contains(m))?;
        if self.at(pos + 7) != Some(b'-') {
            return None;
        }
        let day = self.digits(pos + 8, 2).filter(|d| (1..=31).contains(d))?;
        let mut m = DateMatch {
            end: pos + 10,
            year,
            month,
            day,
            time: None,
            offset: None,
        };
        if matches!(self.at(m.end), Some(b'T' | b't' | b' ')) {
            if let Some((end, time)) = self.match_time(m.end + 1) {
                m.time = Some(time);
                m.end = end;
                match self.at(end) {
                    Some(b'Z' | b'z') => {
                        m.offset = Some(0);
                        m.end = end + 1;
                    }
                    Some(sign @ (b'+' | b'-')) => {
                        let hours = self.digits(end + 1, 2).filter(|&h| h <= 23);
                        let colon = self.at(end + 3) == Some(b':');
                        let minutes = self.digits(end + 4, 2).filter(|&mm| mm <= 59);
                        if let (Some(h), true, Some(mm)) = (hours, colon, minutes) {
                            let total = (i64::from(h) * 3600 + i64::from(mm) * 60) * 1_000_000;
                            m.offset = Some(if sign == b'+' { total } else { -total });
                            m.end = end + 6;
                        }
                    }
                    _ => {}
                }
            }
        }
        Some(m)
    }

    fn match_number(&self, pos: usize) -> Option<NumberMatch> {
        // The hex, binary and octal alternative comes first; when it does
        // not match, the decimal one is tried.
        if self.at(pos) == Some(b'0') {
            let (base, valid): (u32, fn(u8) -> bool) = match self.at(pos + 1) {
                Some(b'x') => (16, |c: u8| c.is_ascii_hexdigit()),
                Some(b'b') => (2, |c: u8| c == b'0' || c == b'1'),
                Some(b'o') => (8, |c: u8| (b'0'..=b'7').contains(&c)),
                _ => (0, |_| false),
            };
            if base != 0 && matches!(self.at(pos + 2), Some(c) if valid(c)) {
                let end = self.digit_run(pos + 2, valid);
                return Some(NumberMatch {
                    end,
                    base,
                    float: false,
                });
            }
        }
        let mut i = pos;
        if matches!(self.at(i), Some(b'+' | b'-')) {
            i += 1;
        }
        match self.at(i) {
            Some(b'0') => i += 1,
            Some(b'1'..=b'9') => i = self.digit_run(i, |c| c.is_ascii_digit()),
            _ => return None,
        }
        let int_end = i;
        if self.at(i) == Some(b'.') && matches!(self.at(i + 1), Some(c) if c.is_ascii_digit()) {
            i = self.digit_run(i + 1, |c| c.is_ascii_digit());
        }
        if matches!(self.at(i), Some(b'e' | b'E')) {
            let mut j = i + 1;
            if matches!(self.at(j), Some(b'+' | b'-')) {
                j += 1;
            }
            if matches!(self.at(j), Some(c) if c.is_ascii_digit()) {
                i = self.digit_run(j, |c| c.is_ascii_digit());
            }
        }
        Some(NumberMatch {
            end: i,
            base: 10,
            float: i != int_end,
        })
    }

    /// `d(?:_?d)*` from pos, whose first byte is a digit: the end.
    fn digit_run(&self, pos: usize, valid: fn(u8) -> bool) -> usize {
        let mut i = pos + 1;
        loop {
            match self.at(i) {
                Some(c) if valid(c) => i += 1,
                Some(b'_') if matches!(self.at(i + 1), Some(c) if valid(c)) => i += 2,
                _ => return i,
            }
        }
    }

    fn number_value(&self, pos: usize, m: NumberMatch) -> Result<Value, Error> {
        let text = &self.src[pos..m.end];
        if m.float {
            let cleaned: String = text
                .iter()
                .filter(|&&c| c != b'_')
                .map(|&c| char::from(c))
                .collect();
            let value: f64 = cleaned.parse().expect("a TOML float is a Rust float");
            return Ok(Value::Float(value));
        }
        if m.base != 10 {
            let digits: Vec<u8> = text[2..]
                .iter()
                .filter(|&&c| c != b'_')
                .map(|&c| char::from(c).to_digit(16).expect("checked") as u8)
                .collect();
            let bits = match m.base {
                16 => 4,
                8 => 3,
                _ => 1,
            };
            return Ok(Value::Integer(Int::from_pow2_digits(&digits, bits)));
        }
        let (negative, body) = match text[0] {
            b'-' => (true, &text[1..]),
            b'+' => (false, &text[1..]),
            _ => (false, text),
        };
        let digits: Vec<u8> = body
            .iter()
            .filter(|&&c| c != b'_')
            .map(|&c| c - b'0')
            .collect();
        if digits.len() > INT_MAX_STR_DIGITS {
            return Err(Error::IntDigits(format!(
                "Exceeds the limit ({INT_MAX_STR_DIGITS} digits) for integer string \
                 conversion: value has {} digits; use sys.set_int_max_str_digits() to \
                 increase the limit",
                digits.len()
            )));
        }
        let mut int = Int::from_decimal_digits(&digits);
        int.negative = negative && !int.is_zero();
        Ok(Value::Integer(int))
    }
}

#[derive(Clone, Copy)]
struct NumberMatch {
    end: usize,
    base: u32,
    float: bool,
}

struct DateMatch {
    end: usize,
    year: u32,
    month: u32,
    day: u32,
    time: Option<Time>,
    offset: Option<i64>,
}

impl DateMatch {
    /// `match_to_datetime`; None where `date()` or `datetime()` refuses.
    fn value(&self) -> Option<Value> {
        let year = i64::from(self.year);
        if year < 1 || self.day > days_in_month(year, self.month) {
            return None;
        }
        match self.time {
            None => Some(Value::Date(Date {
                year: self.year as i32,
                month: self.month,
                day: self.day,
            })),
            Some(t) => DateTime::new(
                year,
                self.month,
                self.day,
                t.hour,
                t.minute,
                t.second,
                t.microsecond,
                self.offset,
            )
            .ok()
            .map(Value::DateTime),
        }
    }
}

/// The stack the parser runs on: [`NESTING_BUDGET`]'s levels recurse
/// natively, which an unoptimised build's frames take past the 2 MiB of a
/// test thread (and Windows' 1 MiB main thread).
const PARSE_STACK: usize = 16 << 20;

/// `tomllib.loads(text)`: the document's table, or why not.
///
/// The parse runs on a thread of its own with [`PARSE_STACK`] of stack, so
/// the deepest document the budget admits fits whatever thread calls this;
/// where no thread can be started it runs on the caller's.
pub fn loads(text: &str) -> Result<Table, Error> {
    std::thread::scope(|scope| {
        match std::thread::Builder::new()
            .name("toml".into())
            .stack_size(PARSE_STACK)
            .spawn_scoped(scope, || loads_here(text))
        {
            Ok(handle) => match handle.join() {
                Ok(result) => result,
                Err(panic) => std::panic::resume_unwind(panic),
            },
            Err(_) => loads_here(text),
        }
    })
}

fn loads_here(text: &str) -> Result<Table, Error> {
    let normalized;
    let src: &[u8] = if text.contains("\r\n") {
        normalized = text.replace("\r\n", "\n");
        normalized.as_bytes()
    } else {
        text.as_bytes()
    };
    let mut parser = Parser { src, depth: 0 };
    let mut data = Table::new();
    let mut flags = Flags::default();
    let mut header: Key = Vec::new();
    let mut pos = 0usize;
    loop {
        pos = parser.skip_ws(pos);
        let Some(c) = parser.at(pos) else {
            break;
        };
        if c == b'\n' {
            pos += 1;
            continue;
        }
        if is_bare(c) || c == b'"' || c == b'\'' {
            pos = parser.key_value_rule(pos, &mut data, &mut flags, &header)?;
            pos = parser.skip_ws(pos);
        } else if c == b'[' {
            flags.finalize_pending();
            let (next, key) = if parser.at(pos + 1) == Some(b'[') {
                parser.create_list_rule(pos, &mut data, &mut flags)?
            } else {
                parser.create_dict_rule(pos, &mut data, &mut flags)?
            };
            header = key;
            pos = parser.skip_ws(next);
        } else if c != b'#' {
            return Err(parser.err(pos, "Invalid statement"));
        }
        pos = parser.skip_comment(pos)?;
        match parser.at(pos) {
            None => break,
            Some(b'\n') => pos += 1,
            Some(_) => {
                return Err(parser.err(pos, "Expected newline or end of document after a statement"))
            }
        }
    }
    Ok(data)
}

/// `repr(value)` of a parsed value, as Python spells it.
pub fn py_repr(value: &Value) -> Result<String, &'static str> {
    Ok(match value {
        Value::String(s) => py_repr_str(s),
        Value::Integer(i) => i.to_decimal()?,
        Value::Float(f) => float_repr::repr(*f),
        Value::Boolean(true) => "True".into(),
        Value::Boolean(false) => "False".into(),
        Value::DateTime(dt) => dt.python_repr(),
        Value::Date(d) => d.python_repr(),
        Value::Time(t) => t.python_repr(),
        Value::Array(items) => {
            let parts: Result<Vec<String>, _> = items.iter().map(py_repr).collect();
            format!("[{}]", parts?.join(", "))
        }
        Value::Table(table) => {
            let mut parts = Vec::new();
            for (k, v) in table.iter() {
                parts.push(format!("{}: {}", py_repr_str(k), py_repr(v)?));
            }
            format!("{{{}}}", parts.join(", "))
        }
    })
}

/// `str(value)` of a date or time value (`datetime`'s `str` puts a space
/// between date and time).
pub fn py_str_datetime(value: &Value) -> Option<String> {
    match value {
        Value::DateTime(dt) => Some(dt.python_str()),
        Value::Date(d) => Some(d.isoformat()),
        Value::Time(t) => Some(t.isoformat()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ints_round_trip_through_limbs() {
        for v in [
            0i128,
            1,
            -1,
            4_294_967_296,
            -170_141_183_460_469_231_731_687_303_715_884_105_728,
        ] {
            let int = Int::from_i128(v);
            assert_eq!(int.to_i128(), Some(v));
            assert_eq!(int.to_decimal().unwrap(), v.to_string());
        }
    }

    #[test]
    fn a_hex_literal_reads_exactly() {
        let doc = loads("a = 0xdead_BEEF_0000_0000_0000_0001\n").unwrap();
        let Some(Value::Integer(i)) = doc.get("a") else {
            panic!()
        };
        assert_eq!(i.to_decimal().unwrap(), "68915718005535514953299001345");
    }
}
