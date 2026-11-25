//! The names `\N{...}` reads in a str literal, as CPython 3.13.13's
//! `unicode_escape` decoder resolves them: `_PyUnicode_Name_CAPI`'s
//! `getcode(name, len, &code, 0)` (`Modules/unicodedata.c`), over
//! Unicode 15.1.0.
//!
//! What `getcode` accepts was read from the interpreter (the module's C
//! source is not on the build machine) and holds for every name it
//! knows: a name is compared with ASCII letters upper-cased, against
//! exactly one spelling per code point. The spellings are every name
//! `unicodedata.name` gives, every alias (`NameAliases.txt`, which
//! `unicodedata` keeps at U+F0000 on), and the ideographs whose name is a
//! prefix and the code point in hex: CJK unified (`unicodedata.name`
//! names them), CJK compatibility, Khitan, Nushu and the Tangut
//! ideographs, which `getcode` reads although `unicodedata.name` names
//! none of them. The hex is the canonical `%04X` (`4e00` matches as
//! `4E00`; `04E00` matches nothing). A Hangul syllable is `HANGUL
//! SYLLABLE ` and its three short names, each column read by
//! `find_syllable`'s longest match. Named sequences are never read (the
//! decoder passes `with_named_seq` 0).
//!
//! The tables in `name_tables.rs` are written by
//! `native/tools/gen_unicode_tables.py` (`generate_names`), which reads
//! the aliases and the rule ranges through `getcode` and `getname`
//! themselves.

use std::sync::OnceLock;

use super::name_tables::{JAMO_L, JAMO_T, JAMO_V, NAMES, NAME_COUNT, NAME_WORDS, RULE_RANGES};

const HANGUL_PREFIX: &[u8] = b"HANGUL SYLLABLE ";
const HANGUL_FIRST: u32 = 0xAC00;
/// The digits of `NAMES` that end a number, and those that lead one.
const CODE_FINAL: u32 = 60;
const CODE_LEADING: u32 = 31;

/// The value of one character of `NAMES`: its place in printable ASCII
/// without `"`, `.` and the backslash.
fn digit(b: u8) -> u32 {
    let skipped = [b'"', b'.', b'\\'].iter().filter(|&&s| b > s).count() as u32;
    u32::from(b) - 0x21 - skipped
}

/// Every name and alias the table holds, sorted by name.
fn table() -> &'static [(Box<str>, u32)] {
    static TABLE: OnceLock<Vec<(Box<str>, u32)>> = OnceLock::new();
    TABLE.get_or_init(|| {
        let words: Vec<&str> = NAME_WORDS.split(',').collect();
        let mut out = Vec::with_capacity(NAME_COUNT);
        let (mut cp, mut lead) = (0u32, 0u32);
        let mut numbers: Vec<u32> = Vec::new();
        for b in NAMES.bytes() {
            if b == b'.' {
                cp += numbers[0];
                let name: Vec<&str> = numbers[1..].iter().map(|&w| words[w as usize]).collect();
                out.push((name.join(" ").into_boxed_str(), cp));
                numbers.clear();
                continue;
            }
            let d = digit(b);
            if d >= CODE_FINAL {
                lead = lead * CODE_LEADING + d - CODE_FINAL;
            } else {
                numbers.push(lead * CODE_FINAL + d);
                lead = 0;
            }
        }
        out.sort_unstable_by(|a, b| a.0.cmp(&b.0));
        out
    })
}

/// The names and aliases held as data (not made by a rule), sorted by
/// name, with their code points.
pub fn stored() -> impl Iterator<Item = (&'static str, u32)> {
    table().iter().map(|(name, cp)| (&**name, *cp))
}

/// `getcode(name, len, &code, 0)`: the code point `\N{name}` stands for,
/// or None where the decoder raises "unknown Unicode character name".
pub fn lookup(name: &[u32]) -> Option<u32> {
    // The parser hands the decoder a code point past ASCII as
    // `\Uxxxxxxxx`, and no name holds a backslash.
    if name.iter().any(|&c| c >= 0x80) {
        return None;
    }
    let upper: Vec<u8> = name
        .iter()
        .map(|&c| (c as u8).to_ascii_uppercase())
        .collect();
    if let Some(rest) = upper.strip_prefix(HANGUL_PREFIX) {
        return hangul(rest);
    }
    for &(prefix, _, _) in RULE_RANGES {
        if let Some(rest) = upper.strip_prefix(prefix.as_bytes()) {
            let cp = canonical_hex(rest)?;
            return RULE_RANGES
                .iter()
                .any(|&(p, first, last)| p == prefix && (first..=last).contains(&cp))
                .then_some(cp);
        }
    }
    let t = table();
    let at = t.binary_search_by(|(n, _)| n.as_bytes().cmp(&upper)).ok()?;
    Some(t[at].1)
}

/// The value of upper-case hex written as `%04X` writes it, else None.
fn canonical_hex(text: &[u8]) -> Option<u32> {
    if !(4..=6).contains(&text.len()) {
        return None;
    }
    let mut v = 0u32;
    for &b in text {
        v = v * 16 + char::from(b).to_digit(16)?;
    }
    (format!("{v:04X}").as_bytes() == text).then_some(v)
}

/// `find_syllable`: the longest short name of `column` that `text`
/// starts with (the first of equal length), by index and length.
fn longest(text: &[u8], column: &[&str]) -> Option<(usize, usize)> {
    let mut best: Option<(usize, usize)> = None;
    for (i, short) in column.iter().enumerate() {
        if text.starts_with(short.as_bytes()) && best.is_none_or(|(_, len)| short.len() > len) {
            best = Some((i, short.len()));
        }
    }
    best
}

/// A Hangul syllable from its short names, which must use the whole text.
fn hangul(text: &[u8]) -> Option<u32> {
    let (l, a) = longest(text, JAMO_L)?;
    let (v, b) = longest(&text[a..], JAMO_V)?;
    let (t, c) = longest(&text[a + b..], JAMO_T)?;
    let index = (l * JAMO_V.len() + v) * JAMO_T.len() + t;
    (a + b + c == text.len()).then_some(HANGUL_FIRST + index as u32)
}
