//! The value gate (`credentials.py` 269-336 and 362-472): `_is_placeholder`
//! and `_looks_like_secret` with `_NUMBER_SUFFIX_RE`, `_DATETIME_SHAPE_RE`,
//! the two placeholder tables and the six patterns the two functions spell
//! inline. The gate decides whether the value the generic `keyword = value`
//! rule or the connection-URI rule captured is a secret.
//!
//! Texts are code points and offsets are code point indices, as a `str`'s
//! are. Every pattern is matched by hand; the README's "Unit 2" says, per
//! pattern, why the matcher answers as `re` does. Past ASCII the gate reads
//! the interpreter's Unicode 15.1.0 data: `str.strip` and `str.isspace`
//! ([`unicode::is_space`]), `str.lower` ([`unicode::lower`]), `re`'s `\d`
//! and `\w` ([`unicode::is_re_digit`], [`unicode::is_word`]),
//! `str.isalpha`, `str.isdigit` and `str.isupper` of one character
//! ([`is_upper`]).
//!
//! A code unit past U+10FFFF, which no `str` holds, is read as a character
//! of no class: not space, digit, letter, word or upper case. Nothing panics
//! on it.

use crate::unicode;

/// `_PLACEHOLDER_VALUES`, in the reference's source order.
pub const PLACEHOLDER_VALUES: [&str; 14] = [
    "none",
    "null",
    "true",
    "false",
    "changeme",
    "redacted",
    "placeholder",
    "example",
    "your_api_key",
    "your-api-key",
    "your_token_here",
    "xxxxxxxxxxxx",
    "************",
    "############",
];

/// `_PLACEHOLDER_WORDS`, in the reference's source order.
pub const PLACEHOLDER_WORDS: [&str; 22] = [
    "api",
    "auth",
    "change",
    "changeme",
    "demo",
    "dev",
    "dummy",
    "example",
    "fake",
    "here",
    "key",
    "local",
    "me",
    "passwd",
    "password",
    "placeholder",
    "replace",
    "sample",
    "secret",
    "test",
    "token",
    "your",
];

/// `_NUMBER_SUFFIX_RE.pattern`.
pub const NUMBER_SUFFIX_PATTERN: &str = r"[-_]?(?<!\d)\d+$";

/// `_DATETIME_SHAPE_RE.pattern`.
pub const DATETIME_SHAPE_PATTERN: &str = concat!(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?",
    r"(?:[Zz]|[+-]\d{2}:?\d{2})?",
    r"|\d{8}[Tt]\d{4,6}(?:[Zz]|[+-]\d{2}:?\d{2})?",
);

/// The patterns `_is_placeholder` and `_looks_like_secret` hand to `re`
/// inline, in source order: the token split, the path start, the relative
/// path, the dotted reference, the call expression, the constant name.
pub const INLINE_PATTERNS: [&str; 6] = [
    r"[-_]+",
    r"(?:\.{0,2}/|~/|[A-Za-z]:/)",
    r"[a-z0-9_.\-]+(?:/[a-z0-9_.\-]+)+",
    r"[A-Za-z_][\w.]*",
    r"[A-Za-z_][\w.]*\(.*\)",
    r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+",
];

/// `_looks_like_secret`'s length floor and ceiling, in code points.
pub const MIN_VALUE_LEN: usize = 12;
pub const MAX_VALUE_LEN: usize = 1024;

/// `len(set(v)) < 8`: a value with fewer distinct characters is a mask.
pub const MIN_VALUE_DIVERSITY: usize = 8;

const fn cp(b: u8) -> u32 {
    b as u32
}

const NEWLINE: u32 = cp(b'\n');
const HYPHEN: u32 = cp(b'-');
const UNDERSCORE: u32 = cp(b'_');
const DOT: u32 = cp(b'.');
const SLASH: u32 = cp(b'/');
const BACKSLASH: u32 = cp(b'\\');
const COLON: u32 = cp(b':');

fn one_of(c: u32, set: &str) -> bool {
    set.bytes().any(|b| cp(b) == c)
}

fn is_separator(c: u32) -> bool {
    c == HYPHEN || c == UNDERSCORE
}

fn ascii_digit(c: u32) -> bool {
    c < 0x80 && (c as u8).is_ascii_digit()
}

fn ascii_alpha(c: u32) -> bool {
    c < 0x80 && (c as u8).is_ascii_alphabetic()
}

fn ascii_upper(c: u32) -> bool {
    c < 0x80 && (c as u8).is_ascii_uppercase()
}

/// `text in table`, for a table of ASCII strings.
fn in_table<'a>(table: &[&str], text: impl Iterator<Item = &'a u32> + Clone) -> bool {
    table
        .iter()
        .any(|entry| entry.bytes().map(cp).eq(text.clone().copied()))
}

/// `Other_Uppercase` of Unicode 15.1.0: the code points `str.isupper`
/// reads as uppercase whose category is not `Lu`.
const OTHER_UPPERCASE: [(u32, u32); 5] = [
    (0x2160, 0x216F),
    (0x24B6, 0x24CF),
    (0x1F130, 0x1F149),
    (0x1F150, 0x1F169),
    (0x1F170, 0x1F189),
];

/// `str.isupper` of a one-character string: the `Uppercase` property
/// (`Lu` and `Other_Uppercase`). A titlecase letter is not uppercase.
pub fn is_upper(c: u32) -> bool {
    if c < 0x80 {
        return (c as u8).is_ascii_uppercase();
    }
    unicode::category(c) == "Lu"
        || OTHER_UPPERCASE
            .iter()
            .any(|&(lo, hi)| (lo..=hi).contains(&c))
}

/// `_NUMBER_SUFFIX_RE.search(text)`: the span of the one match the
/// pattern can have, which `sub("", text)` removes. `$` holds at the end
/// and before a final line feed; the `\d` run that ends there (Unicode
/// decimal digits) is taken whole, since the lookbehind lets a match start
/// only where a run starts, with the one `-` or `_` before it.
pub fn number_suffix(text: &[u32]) -> Option<(usize, usize)> {
    let end = match text.split_last() {
        Some((&NEWLINE, head)) => head.len(),
        _ => text.len(),
    };
    let run = text[..end]
        .iter()
        .rev()
        .take_while(|&&c| unicode::is_re_digit(c))
        .count();
    if run == 0 {
        return None;
    }
    let mut start = end - run;
    if start > 0 && is_separator(text[start - 1]) {
        start -= 1;
    }
    Some((start, end))
}

/// `_is_placeholder`.
pub fn is_placeholder(v: &[u32]) -> bool {
    let lowered = unicode::lower(v);
    if in_table(&PLACEHOLDER_VALUES, lowered.iter()) {
        return true;
    }
    // `_NUMBER_SUFFIX_RE.sub("", lowered)`; with no match it is `lowered`,
    // which the lookup above already refused.
    if let Some((start, end)) = number_suffix(&lowered) {
        let rest = lowered[..start].iter().chain(&lowered[end..]);
        if in_table(&PLACEHOLDER_VALUES, rest) {
            return true;
        }
    }
    // `re.split(r"[-_]+", lowered)` splits at each run of separators; a
    // split at each separator adds only empty tokens, which `all` passes.
    lowered.split(|&c| is_separator(c)).all(|token| {
        let digits = token.iter().rev().take_while(|&&c| ascii_digit(c)).count();
        let token = &token[..token.len() - digits];
        token.is_empty() || in_table(&PLACEHOLDER_WORDS, token.iter())
    })
}

/// A cursor over the text for `_DATETIME_SHAPE_RE`.
struct Shape<'a> {
    text: &'a [u32],
    at: usize,
}

impl Shape<'_> {
    /// `\d{n}`.
    fn digits(&mut self, n: usize) -> bool {
        let ok = self
            .text
            .get(self.at..self.at + n)
            .is_some_and(|run| run.iter().all(|&c| unicode::is_re_digit(c)));
        if ok {
            self.at += n;
        }
        ok
    }

    /// One character of `set`.
    fn one(&mut self, set: &str) -> bool {
        let ok = self.text.get(self.at).is_some_and(|&c| one_of(c, set));
        if ok {
            self.at += 1;
        }
        ok
    }

    /// `(?:[Zz]|[+-]\d{2}:?\d{2})?` and then the end of the text.
    fn zone_then_end(mut self) -> bool {
        if self.one("Zz") {
            return self.at == self.text.len();
        }
        if self.one("+-") {
            if !self.digits(2) {
                return false;
            }
            self.one(":");
            return self.digits(2) && self.at == self.text.len();
        }
        self.at == self.text.len()
    }
}

/// `_DATETIME_SHAPE_RE.fullmatch(text) is not None`. `\d` is a Unicode
/// decimal digit. Each optional part opens with a character nothing after
/// it can start with (`:` the seconds, `.` the fraction, `Z`, `z`, `+`
/// and `-` the zone), so a part that is present must be taken and one
/// reading decides.
pub fn datetime_shape(text: &[u32]) -> bool {
    let mut s = Shape { text, at: 0 };
    if s.digits(4) {
        if s.one("-") {
            // The extended form.
            let fixed = s.digits(2)
                && s.one("-")
                && s.digits(2)
                && s.one("Tt")
                && s.digits(2)
                && s.one(":")
                && s.digits(2);
            if !fixed {
                return false;
            }
            if s.one(":") {
                if !s.digits(2) {
                    return false;
                }
                if s.one(".") {
                    if !s.digits(1) {
                        return false;
                    }
                    while s.digits(1) {}
                }
            }
            return s.zone_then_end();
        }
        // The basic form: `\d{8}[Tt]\d{4,6}`, the repetition greedy with
        // a digit after it never a way to end.
        if !(s.digits(4) && s.one("Tt") && s.digits(4)) {
            return false;
        }
        let _ = s.digits(1) && s.digits(1);
        return s.zone_then_end();
    }
    false
}

fn strip(mut v: &[u32], left: bool, drop: impl Fn(u32) -> bool) -> &[u32] {
    while let Some((&last, head)) = v.split_last() {
        if !drop(last) {
            break;
        }
        v = head;
    }
    if left {
        while let Some((&first, tail)) = v.split_first() {
            if !drop(first) {
                break;
            }
            v = tail;
        }
    }
    v
}

fn contains(v: &[u32], needle: &str) -> bool {
    let needle: Vec<u32> = needle.bytes().map(cp).collect();
    v.windows(needle.len()).any(|w| w == needle)
}

fn starts_with(v: &[u32], prefix: &str) -> bool {
    v.len() >= prefix.len() && prefix.bytes().map(cp).eq(v[..prefix.len()].iter().copied())
}

/// `[a-z0-9_.\-]`.
fn lower_path(c: u32) -> bool {
    c < 0x80 && {
        let b = c as u8;
        b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'.' || b == b'-'
    }
}

/// Every piece of `v` between two `sep` is non-empty and all of `class`.
fn segments(v: &[u32], sep: u32, class: fn(u32) -> bool) -> bool {
    v.split(|&c| c == sep)
        .all(|seg| !seg.is_empty() && seg.iter().all(|&c| class(c)))
}

/// Fewer than `MIN_VALUE_DIVERSITY` distinct code points.
fn low_diversity(v: &[u32]) -> bool {
    let mut seen = [0u32; MIN_VALUE_DIVERSITY];
    let mut count = 0;
    for &c in v {
        if !seen[..count].contains(&c) {
            seen[count] = c;
            count += 1;
            if count == MIN_VALUE_DIVERSITY {
                return false;
            }
        }
    }
    true
}

/// `_looks_like_secret`.
pub fn looks_like_secret(value: &[u32]) -> bool {
    let v = strip(value, true, unicode::is_space);
    let v = strip(v, true, |c| one_of(c, "\"'"));
    let mut v = strip(v, false, |c| one_of(c, ".,;:!?"));
    // One trailing closer, once, when nothing in the value opens it.
    if let Some((&last, head)) = v.split_last() {
        let lone = match last {
            c if c == cp(b'}') => !v.contains(&cp(b'{')),
            c if c == cp(b'>') => !v.contains(&cp(b'<')),
            c if c == cp(b'%') => !head.contains(&cp(b'%')),
            _ => false,
        };
        if lone {
            v = head;
        }
    }
    if v.len() < MIN_VALUE_LEN || v.len() > MAX_VALUE_LEN {
        return false;
    }
    let (first, last) = (v[0], v[v.len() - 1]);
    if one_of(first, "$<{%") || one_of(last, ">}%") {
        return false;
    }
    if is_placeholder(v) {
        return false;
    }
    if v.contains(&BACKSLASH) {
        return false;
    }
    if v.contains(&SLASH) {
        // `re.match(r"(?:\.{0,2}/|~/|[A-Za-z]:/)", v)`.
        let path_start = ["/", "./", "../", "~/"].iter().any(|p| starts_with(v, p))
            || (ascii_alpha(first) && v[1] == COLON && v[2] == SLASH);
        if path_start || contains(v, "://") {
            return false;
        }
        // `[a-z0-9_.\-]+(?:/[a-z0-9_.\-]+)+`: `/` is outside the class,
        // so the pieces between slashes are the only reading.
        if segments(v, SLASH, lower_path) {
            return false;
        }
    }
    if ascii_alpha(first) || first == UNDERSCORE {
        // `[\w.]*` ends where its run ends: nothing after it in either
        // pattern can start inside the run.
        let run = 1 + v[1..]
            .iter()
            .take_while(|&&c| unicode::is_word(c) || c == DOT)
            .count();
        if run == v.len() {
            // `[A-Za-z_][\w.]*`, asked only of a value holding a dot.
            if v.contains(&DOT) {
                return false;
            }
        } else if v[run] == cp(b'(')
            && last == cp(b')')
            && run + 1 < v.len()
            && !v[run + 1..v.len() - 1].contains(&NEWLINE)
        {
            // `[A-Za-z_][\w.]*\(.*\)`: `.` is anything but a line feed.
            return false;
        }
    }
    // `[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+`.
    if ascii_upper(first)
        && v.contains(&UNDERSCORE)
        && segments(v, UNDERSCORE, |c| ascii_upper(c) || ascii_digit(c))
    {
        return false;
    }
    if datetime_shape(v) || low_diversity(v) {
        return false;
    }
    if (v.contains(&HYPHEN) || v.contains(&UNDERSCORE)) && !v.iter().any(|&c| is_upper(c)) {
        return false;
    }
    v.iter().any(|&c| unicode::is_alpha(c)) && v.iter().any(|&c| unicode::is_digit(c))
}

/// A convenience for callers holding a `&str`.
pub fn looks_like_secret_str(value: &str) -> bool {
    looks_like_secret(&unicode::cps(value))
}
