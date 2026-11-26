//! `durability.py`, unit 1: the marker's canonical form, the quoted spans
//! a marker is exempt inside, and the snippet and sentence helpers the
//! detectors share (`canonical_marker`, `quoted_spans`, `in_quoted_span`,
//! `_snippet_around`, `_at_sentence_start`, `_is_titlecase_name`,
//! `sentence_around`, reference lines 270-571).
//!
//! A text is its code points (a Python `str` may hold a lone surrogate);
//! every position is a code point index, as the reference's are. Each of
//! the reference's regular expressions is matched by hand here; the
//! README section "Transient markers (`durability.py`)" says why each
//! reading equals `re`'s.

use crate::pyvalue::PyStr;
use crate::unicode;
use crate::verify_candidates::validate::is_lower;

mod capture;
mod markers;

pub use capture::*;
pub use markers::*;

/// `SHA_MARKER`: the one name every commit-hash marker folds onto.
pub const SHA_MARKER: &str = "sha:<commit>";

/// `_CONTEXT_CHARS`: the padding `_snippet_around` keeps on each side.
pub const CONTEXT_CHARS: usize = 20;

/// `TransientMatch`: one marker hit, its canonical name and its snippet.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TransientMatch {
    pub marker: PyStr,
    pub snippet: PyStr,
}

const NL: u32 = 0x0A;
const QUOTE: u32 = 0x22;
const OPEN_CURLY: u32 = 0x201C;
const CLOSE_CURLY: u32 = 0x201D;

/// `canonical_marker`: `_LEGACY_SHA_MARKER_RE` (`^sha:[0-9a-f]{7}$`) by
/// `re.match`, whose `$` also matches before one newline that ends the
/// text, so `"sha:abcdef0\n"` folds too.
pub fn canonical_marker(marker: &[u32]) -> PyStr {
    if legacy_sha(marker) {
        unicode::cps(SHA_MARKER)
    } else {
        marker.to_vec()
    }
}

fn legacy_sha(m: &[u32]) -> bool {
    let body = match m {
        [rest @ .., NL] if rest.len() == 11 => rest,
        _ if m.len() == 11 => m,
        _ => return false,
    };
    let head: Vec<u32> = "sha:".chars().map(u32::from).collect();
    body[..4] == head[..]
        && body[4..]
            .iter()
            .all(|&c| matches!(c, 0x30..=0x39 | 0x61..=0x66))
}

/// `quoted_spans`: `_QUOTED_SPAN_RE.finditer` (`"[^"\n]*"` or
/// `“[^“”\n]*”`), half-open spans in text order.
///
/// Every match is at least two code points, so `finditer` never takes an
/// empty match; a failed attempt moves the search on by one.
pub fn quoted_spans(text: &[u32]) -> Vec<(usize, usize)> {
    let mut spans = Vec::new();
    let mut i = 0;
    while i < text.len() {
        let close = match text[i] {
            QUOTE => scan(text, i + 1, |c| c == QUOTE, |c| c == NL),
            OPEN_CURLY => scan(
                text,
                i + 1,
                |c| c == CLOSE_CURLY,
                |c| c == NL || c == OPEN_CURLY,
            ),
            _ => None,
        };
        match close {
            Some(j) => {
                spans.push((i, j + 1));
                i = j + 1;
            }
            None => i += 1,
        }
    }
    spans
}

/// The index of the first `close` at or after `from`, unless a `stop`
/// (a code point the body's class excludes) comes first.
fn scan(
    text: &[u32],
    from: usize,
    close: impl Fn(u32) -> bool,
    stop: impl Fn(u32) -> bool,
) -> Option<usize> {
    for (k, &c) in text.iter().enumerate().skip(from) {
        if close(c) {
            return Some(k);
        }
        if stop(c) {
            return None;
        }
    }
    None
}

/// `in_quoted_span`: `[start, end)` wholly inside one of `spans`.
pub fn in_quoted_span(spans: &[(usize, usize)], start: usize, end: usize) -> bool {
    spans.iter().any(|&(a, b)| a <= start && end <= b)
}

/// `_snippet_around`: up to [`CONTEXT_CHARS`] on each side, newlines made
/// spaces, `str.strip`ped, runs of `\s` (`str.isspace`, which `re`'s `\s`
/// reads on a `str` pattern) collapsed to one space, and `...` on a side
/// that was cut.
pub fn snippet_around(text: &[u32], start: usize, end: usize) -> PyStr {
    let s = start.saturating_sub(CONTEXT_CHARS);
    let e = text.len().min(end.saturating_add(CONTEXT_CHARS));
    let chunk: &[u32] = if s < e { &text[s..e] } else { &[] };
    // Each code point is classed once: `(code point, isspace)`.
    let chunk: Vec<(u32, bool)> = chunk
        .iter()
        .map(|&c| {
            let c = if c == NL { 0x20 } else { c };
            (c, unicode::is_space(c))
        })
        .collect();
    let lo = chunk.iter().position(|&(_, sp)| !sp);
    let mut out = Vec::new();
    if s > 0 {
        out.extend_from_slice(&[0x2E; 3]);
    }
    if let Some(lo) = lo {
        let hi = chunk.iter().rposition(|&(_, sp)| !sp).unwrap_or(lo);
        let mut in_space = false;
        for &(c, sp) in &chunk[lo..=hi] {
            if sp {
                if !in_space {
                    out.push(0x20);
                }
                in_space = true;
            } else {
                out.push(c);
                in_space = false;
            }
        }
    }
    if e < text.len() {
        out.extend_from_slice(&[0x2E; 3]);
    }
    out
}

/// `_SENTENCE_BOUNDARY_CHARS`.
const SENTENCE_BOUNDARY: [u32; 13] = [
    0x2E, 0x21, 0x3F, 0x3A, 0x3B, NL, 0x2D, 0x2A, 0x2022, QUOTE, 0x27, OPEN_CURLY, 0x28,
];
const OPEN_BRACKET: u32 = 0x5B;

/// `_at_sentence_start`: skip back over spaces and tabs; the start of the
/// text, a boundary character, or a `Pd` or `So` code point before the
/// word opens a sentence. `None` where the reference raises `IndexError`
/// (`pos` more than one past the end).
pub fn at_sentence_start(text: &[u32], pos: usize) -> Option<bool> {
    let mut i = pos;
    loop {
        if i == 0 {
            return Some(true);
        }
        let c = *text.get(i - 1)?;
        if c == 0x20 || c == 0x09 {
            i -= 1;
            continue;
        }
        if c == OPEN_BRACKET || SENTENCE_BOUNDARY.contains(&c) {
            return Some(true);
        }
        return Some(matches!(unicode::category(c), "Pd" | "So"));
    }
}

/// `str.isupper` of one code point: the `Uppercase` property (`Lu` and
/// `Other_Uppercase`).
fn is_upper(cp: u32) -> bool {
    const OTHER_UPPERCASE: &[(u32, u32)] = &[
        (0x2160, 0x216F),
        (0x24B6, 0x24CF),
        (0x1F130, 0x1F149),
        (0x1F150, 0x1F169),
        (0x1F170, 0x1F189),
    ];
    if cp < 0x80 {
        return (cp as u8).is_ascii_uppercase();
    }
    unicode::category(cp) == "Lu" || OTHER_UPPERCASE.iter().any(|&(a, b)| (a..=b).contains(&cp))
}

/// `str.istitle`: CPython's loop over upper or title (`Lt`), lower, and
/// uncased code points.
pub fn istitle(s: &[u32]) -> bool {
    let (mut cased, mut previous) = (false, false);
    for &c in s {
        if is_upper(c) || (c >= 0x80 && unicode::category(c) == "Lt") {
            if previous {
                return false;
            }
            previous = true;
            cased = true;
        } else if is_lower(c) {
            if !previous {
                return false;
            }
            previous = true;
            cased = true;
        } else {
            previous = false;
        }
    }
    cased
}

/// `str.split()` with no argument: runs of non-`isspace` code points.
fn split_words(s: &[u32]) -> Vec<&[u32]> {
    s.split(|&c| unicode::is_space(c))
        .filter(|w| !w.is_empty())
        .collect()
}

/// `_NEXT_WORD_RE.match(text, end)` (`\s+([\w'’-]+)`): the word after
/// the whitespace at `end`. No code point is both `\s` and in the word
/// class, so the greedy runs never backtrack.
fn next_word(text: &[u32], end: usize) -> Option<&[u32]> {
    let rest = text.get(end..)?;
    let spaces = rest.iter().take_while(|&&c| unicode::is_space(c)).count();
    if spaces == 0 {
        return None;
    }
    let rest = &rest[spaces..];
    let n = rest
        .iter()
        .take_while(|&&c| unicode::is_word(c) || c == 0x27 || c == 0x2019 || c == 0x2D)
        .count();
    (n > 0).then(|| &rest[..n])
}

/// `_is_titlecase_name` for the match `text[start..end]`. `None` where the
/// reference raises `IndexError` (a match with no word in it, or a start
/// past the end).
pub fn is_titlecase_name(text: &[u32], start: usize, end: usize) -> Option<bool> {
    let group: &[u32] = if start < end && start < text.len() {
        &text[start..end.min(text.len())]
    } else {
        &[]
    };
    let words = split_words(group);
    let first = words.first()?;
    if words.len() > 1 {
        return Some(words[1..].iter().any(|w| istitle(w)));
    }
    if !istitle(first) {
        return Some(false);
    }
    if !at_sentence_start(text, start)? {
        return Some(true);
    }
    Some(match next_word(text, end) {
        None => false,
        Some(nxt) => nxt != [0x49] && istitle(nxt),
    })
}

/// One `_SENTENCE_END_RE` (`[!?\n]|(?<!\d)\.|\.(?!\d)`) test at `i`, with
/// the text ending at `limit` for the lookahead (`endpos`) and the whole
/// text before `i` visible to the lookbehind.
fn sentence_end_at(text: &[u32], i: usize, limit: usize) -> bool {
    match text[i] {
        0x21 | 0x3F | NL => true,
        0x2E => {
            let before = i > 0 && unicode::is_re_digit(text[i - 1]);
            let after = i + 1 < limit && unicode::is_re_digit(text[i + 1]);
            !(before && after)
        }
        _ => false,
    }
}

/// `sentence_around`: from just past the last terminator before `pos` to
/// the next terminator at or after it. The first scan is
/// `finditer(text, 0, pos)`, so a `.` just before `pos` cannot see the
/// digit after it and always ends a sentence there (`"3.13"` at 2 is
/// `"13"`); the reference does this and so does the port.
pub fn sentence_around(text: &[u32], pos: usize) -> PyStr {
    let limit = pos.min(text.len());
    let start = (0..limit)
        .rev()
        .find(|&i| sentence_end_at(text, i, limit))
        .map_or(0, |i| i + 1);
    let end = (limit..text.len())
        .find(|&i| sentence_end_at(text, i, text.len()))
        .unwrap_or(text.len());
    text[start..end].to_vec()
}
