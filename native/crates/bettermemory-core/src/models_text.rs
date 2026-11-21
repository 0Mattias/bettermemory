//! The text helpers of `models.py` (lines 884-1127): a memory's slug and
//! file name, its one-line summary, the truncation heuristic and the
//! search snippets.
//!
//! Text is a `str` as code points, indexed by code point as Python
//! indexes it. `str.strip`, `str.lower`, `str.isalnum` and `re`'s `\s` and
//! `\d` are the crate's `unicode` tables (Unicode 15.1.0). The three
//! patterns of these lines (`_SLUG_RE`, `_LEADING_DATE_RE`,
//! `_SENTENCE_END_RE`) are matched by hand. Integer parameters are Python
//! ints the callers pass; the port takes them as `i64` and computes in
//! `i128`, so no value of an `i64` overflows, and slices follow Python's
//! rules for a negative bound.

use crate::datetime::{year_field, DateTime, STRFTIME_PADS_YEAR};
use crate::pyvalue::PyStr;
use crate::unicode;

/// `_ABBREVIATIONS`.
pub const ABBREVIATIONS: [&str; 24] = [
    "e.g", "i.e", "etc", "vs", "cf", "viz", "mr", "mrs", "ms", "dr", "prof", "jr", "sr", "st",
    "u.s", "u.k", "a.k.a", "a.m", "p.m", "fig", "no", "vol", "ca", "approx",
];

/// `_BODY_TERMINALS`.
pub const BODY_TERMINALS: &str = ".!?:;)\"'`\u{bb}]}>*_|";

/// `make_slug`'s and `first_summary_line`'s and `snippet_for`'s defaults.
pub const SLUG_MAX_WORDS: i64 = 6;
pub const SLUG_MAX_CHARS: i64 = 60;
pub const SUMMARY_MAX_CHARS: i64 = 80;
pub const SNIPPET_MAX_CHARS: i64 = 200;

/// Where `text[:stop]` ends, for a Python int `stop`.
fn stop_at(len: usize, stop: i128) -> usize {
    let len_i = len as i128;
    if stop < 0 {
        (len_i + stop).max(0) as usize
    } else {
        stop.min(len_i) as usize
    }
}

/// `str.strip()` (no argument): `str.isspace` at both ends.
pub fn strip(text: &[u32]) -> &[u32] {
    let start = text
        .iter()
        .position(|&c| !unicode::is_space(c))
        .unwrap_or(text.len());
    let end = text
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .map_or(start, |i| i + 1);
    &text[start..end]
}

/// `str.splitlines`' line boundaries.
fn is_line_break(c: u32) -> bool {
    matches!(
        c,
        0x0A | 0x0B | 0x0C | 0x0D | 0x1C | 0x1D | 0x1E | 0x85 | 0x2028 | 0x2029
    )
}

/// `\d{n}` at `at`: the end, or `None`.
fn digits(text: &[u32], at: usize, n: usize) -> Option<usize> {
    let end = at.checked_add(n)?;
    (end <= text.len() && text[at..end].iter().all(|&c| unicode::is_re_digit(c))).then_some(end)
}

fn is(text: &[u32], at: usize, set: &[u8]) -> bool {
    text.get(at)
        .is_some_and(|&c| set.iter().any(|&s| u32::from(s) == c))
}

/// `_LEADING_DATE_RE.match(text)`'s end, or `None`. Every part after the
/// date is optional and the trailing class is a star, so the greedy
/// choice at each step is the match: nothing after it can fail.
fn leading_date(text: &[u32]) -> Option<usize> {
    let mut i = digits(text, 0, 4)?;
    if !is(text, i, b"-") {
        return None;
    }
    i = digits(text, i + 1, 2)?;
    if !is(text, i, b"-") {
        return None;
    }
    i = digits(text, i + 1, 2)?;
    // (?:[ tT]\d{2}:\d{2}(?::\d{2})?(?:[zZ]|[+\-]\d{2}:?\d{2})?)?
    if is(text, i, b" tT") {
        let time = digits(text, i + 1, 2)
            .filter(|&j| is(text, j, b":"))
            .and_then(|j| digits(text, j + 1, 2));
        if let Some(mut j) = time {
            if is(text, j, b":") {
                if let Some(k) = digits(text, j + 1, 2) {
                    j = k;
                }
            }
            if is(text, j, b"zZ") {
                j += 1;
            } else if is(text, j, b"+-") {
                let hours = digits(text, j + 1, 2);
                let zone = hours.and_then(|h| {
                    is(text, h, b":")
                        .then(|| digits(text, h + 1, 2))
                        .flatten()
                        .or_else(|| digits(text, h, 2))
                });
                if let Some(k) = zone {
                    j = k;
                }
            }
            i = j;
        }
    }
    // [\s\-_:.,;|/]*
    while let Some(&c) = text.get(i) {
        if unicode::is_space(c) || is(text, i, b"-_:.,;|/") {
            i += 1;
        } else {
            break;
        }
    }
    Some(i)
}

/// `make_slug(content, max_words, max_chars)`.
pub fn make_slug(content: &[u32], max_words: i64, max_chars: i64) -> PyStr {
    let lowered = unicode::lower(strip(content));
    let line_end = lowered
        .iter()
        .position(|&c| is_line_break(c))
        .unwrap_or(lowered.len());
    let line = &lowered[..line_end];
    let line = &line[leading_date(line).unwrap_or(0)..];
    let mut words: Vec<&[u32]> = line
        .split(|&c| !(c < 0x80 && (c as u8).is_ascii_lowercase() || (0x30..=0x39).contains(&c)))
        .filter(|w| !w.is_empty())
        .collect();
    let fallback = unicode::cps("memory");
    if words.is_empty() {
        words.push(&fallback);
    }
    let kept = &words[..stop_at(words.len(), i128::from(max_words))];
    let joined: PyStr = kept.join(&0x2D);
    let cut = &joined[..stop_at(joined.len(), i128::from(max_chars))];
    let start = cut.iter().position(|&c| c != 0x2D).unwrap_or(cut.len());
    let end = cut
        .iter()
        .rposition(|&c| c != 0x2D)
        .map_or(start, |i| i + 1);
    let slug = &cut[start..end];
    if slug.is_empty() {
        fallback
    } else {
        slug.to_vec()
    }
}

/// `build_filename(created, slug)`: `<YYYY-MM-DD>-<slug>.md`, the year
/// spelt as this target's C library writes `strftime`'s `%Y`
/// ([`STRFTIME_PADS_YEAR`]): a year before 1000 padded to four digits,
/// except on Linux with glibc, where it is unpadded.
pub fn build_filename(created: &DateTime, slug: &[u32]) -> PyStr {
    let mut out = unicode::cps(&format!(
        "{}-{:02}-{:02}-",
        year_field(created.year, STRFTIME_PADS_YEAR),
        created.month,
        created.day
    ));
    out.extend_from_slice(slug);
    out.extend(unicode::cps(".md"));
    out
}

/// `_word_ending_at(text, end)`: the alnum/dot run ending before `end`.
/// `None` where the reference raises `IndexError` (`end` past the text's
/// length); a negative `end` answers empty.
pub fn word_ending_at(text: &[u32], end: i64) -> Option<PyStr> {
    if end <= 0 {
        return Some(Vec::new());
    }
    let end = usize::try_from(end).ok()?;
    if end > text.len() {
        return None;
    }
    let mut i = end;
    while i > 0 && (unicode::is_alnum(text[i - 1]) || text[i - 1] == 0x2E) {
        i -= 1;
    }
    Some(text[i..end].to_vec())
}

/// `looks_truncated(body)`.
pub fn looks_truncated(body: &[u32]) -> bool {
    match strip(body).last() {
        None => false,
        Some(&c) => !BODY_TERMINALS.chars().any(|t| t as u32 == c),
    }
}

/// `first_summary_line(body, max_chars)`.
pub fn first_summary_line(body: &[u32], max_chars: i64) -> PyStr {
    let text: PyStr = strip(body)
        .iter()
        .map(|&c| if c == 0x0A { 0x20 } else { c })
        .collect();
    let max = i128::from(max_chars);
    for (start, &c) in text.iter().enumerate() {
        if !matches!(c, 0x2E | 0x21 | 0x3F) {
            continue;
        }
        // (?=\s|$): no newline is left, so `$` is the end of the text.
        if text.get(start + 1).is_some_and(|&n| !unicode::is_space(n)) {
            continue;
        }
        if c == 0x2E {
            let word = unicode::lower(&word_ending_at(&text, start as i64).unwrap_or_default());
            let is_abbreviation = ABBREVIATIONS.iter().any(|a| {
                a.len() == word.len() && a.bytes().zip(&word).all(|(b, &c)| u32::from(b) == c)
            });
            if is_abbreviation {
                continue;
            }
        }
        let sentence = strip(&text[..start]);
        if !sentence.is_empty() && sentence.len() as i128 <= max {
            return sentence.to_vec();
        }
        break;
    }
    if text.len() as i128 <= max {
        return text;
    }
    truncate_at_word(&text, max_chars)
}

/// `snippet_for(body, max_chars)`.
pub fn snippet_for(body: &[u32], max_chars: i64) -> PyStr {
    let text = strip(body);
    if text.len() as i128 <= i128::from(max_chars) {
        return text.to_vec();
    }
    truncate_at_word(text, max_chars)
}

/// `snippet_window(body, start, max_chars)`.
pub fn snippet_window(body: &[u32], start: i64, max_chars: i64) -> PyStr {
    let text = strip(body);
    if start <= 0 {
        return snippet_for(text, max_chars);
    }
    let from = usize::try_from(start).map_or(text.len(), |s| s.min(text.len()));
    let tail = &text[from..];
    let budget = i128::from(max_chars) - 3;
    let mut out = unicode::cps("...");
    if tail.len() as i128 <= budget {
        out.extend_from_slice(tail);
    } else {
        out.extend(truncate_at_word_wide(tail, budget));
    }
    out
}

/// `_truncate_at_word(text, max_chars)`.
pub fn truncate_at_word(text: &[u32], max_chars: i64) -> PyStr {
    truncate_at_word_wide(text, i128::from(max_chars))
}

fn truncate_at_word_wide(text: &[u32], max_chars: i128) -> PyStr {
    let mut truncated = &text[..stop_at(text.len(), max_chars)];
    // max(rfind(" "), rfind("\n"), rfind("\t")): -1 when there is none.
    let space_idx = truncated
        .iter()
        .rposition(|&c| c == 0x20 || c == 0x0A || c == 0x09)
        .map_or(-1, |i| i as i128);
    if space_idx >= max_chars - 40 {
        // `truncated[:-1]` when there is no whitespace: a negative stop.
        truncated = &truncated[..stop_at(truncated.len(), space_idx)];
    }
    let keep = truncated
        .iter()
        .rposition(|&c| !b" ,;:.-".iter().any(|&s| u32::from(s) == c))
        .map_or(0, |i| i + 1);
    let mut out = truncated[..keep].to_vec();
    out.extend_from_slice(&[0x2E, 0x2E, 0x2E]);
    out
}
