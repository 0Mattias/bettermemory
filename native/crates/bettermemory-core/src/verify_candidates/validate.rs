//! Unit 1 of `verify_candidates.py`: one candidate spelling read and
//! validated (`_normalize_candidate` and every helper it calls), the path
//! gate's two spelling checks (`_refused_spelling`, `_escapes_root`),
//! `_fold_altsep` and the scan bound (`_bounded_scan_text`). Nothing here
//! reads the machine: no home, no disk, no clock.
//!
//! The one regular expression on this path, `_LINE_SUFFIX_RE`, is matched
//! by hand (`line_suffix`); `_DRIVE_PREFIX_RE` is two comparisons.

use std::fmt;

use super::{
    MAX_BODY_SCAN_BYTES, MAX_PATH_LENGTH, PLACEHOLDER_PATHS, PLACEHOLDER_PREFIXES, TRAILING_PUNCT,
    WELLKNOWN_ROUTE_SEGMENTS,
};
use crate::unicode;

const SLASH: u32 = '/' as u32;
const BACKSLASH: u32 = '\\' as u32;
const COLON: u32 = ':' as u32;
const SPACE: u32 = ' ' as u32;
const TAB: u32 = '\t' as u32;
const NEWLINE: u32 = '\n' as u32;

/// The `IndexError` `_command_shaped` raises when a spaced candidate holds
/// nothing but whitespace (`parts[0]` of an empty split).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct IndexError;

impl fmt::Display for IndexError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("list index out of range")
    }
}

impl std::error::Error for IndexError {}

fn cps(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

fn eq(a: &[u32], b: &str) -> bool {
    a.len() == b.chars().count() && a.iter().zip(b.chars()).all(|(&x, y)| x == u32::from(y))
}

fn starts(s: &[u32], prefix: &str) -> bool {
    let p = cps(prefix);
    s.starts_with(&p)
}

fn find_seq(s: &[u32], pat: &[u32], from: usize) -> Option<usize> {
    if pat.is_empty() {
        return Some(from.min(s.len()));
    }
    if s.len() < pat.len() {
        return None;
    }
    // Knuth-Morris-Pratt: linear in `s` and `pat`, so a long needle costs
    // no more than CPython's fastsearch does.
    let mut fail = vec![0usize; pat.len()];
    let mut k = 0;
    for i in 1..pat.len() {
        while k > 0 && pat[i] != pat[k] {
            k = fail[k - 1];
        }
        if pat[i] == pat[k] {
            k += 1;
        }
        fail[i] = k;
    }
    let mut k = 0;
    for (i, &c) in s.iter().enumerate().skip(from) {
        while k > 0 && c != pat[k] {
            k = fail[k - 1];
        }
        if c == pat[k] {
            k += 1;
            if k == pat.len() {
                return Some(i + 1 - k);
            }
        }
    }
    None
}

fn contains_seq(s: &[u32], pat: &str) -> bool {
    find_seq(s, &cps(pat), 0).is_some()
}

fn index_of(s: &[u32], c: char) -> Option<usize> {
    s.iter().position(|&x| x == u32::from(c))
}

fn count(s: &[u32], c: u32) -> usize {
    s.iter().filter(|&&x| x == c).count()
}

/// `str.replace(old, new)` with a non-empty `old`: leftmost, not
/// overlapping.
fn replace(s: &[u32], old: &[u32], new: &[u32]) -> Vec<u32> {
    let mut out = Vec::with_capacity(s.len());
    let mut at = 0;
    while let Some(i) = find_seq(s, old, at) {
        out.extend_from_slice(&s[at..i]);
        out.extend_from_slice(new);
        at = i + old.len();
    }
    out.extend_from_slice(&s[at..]);
    out
}

/// `str.strip()`: the whitespace `str.isspace` reads, from both ends.
fn strip(s: &[u32]) -> &[u32] {
    let start = s.iter().position(|&c| !unicode::is_space(c));
    match start {
        None => &[],
        Some(i) => {
            let j = s.iter().rposition(|&c| !unicode::is_space(c)).unwrap_or(i);
            &s[i..=j]
        }
    }
}

/// `str.split()`: runs of non-whitespace, by `str.isspace`.
fn split_ws(s: &[u32]) -> Vec<&[u32]> {
    s.split(|&c| unicode::is_space(c))
        .filter(|p| !p.is_empty())
        .collect()
}

fn is_sep(c: u32) -> bool {
    c == SLASH || c == BACKSLASH
}

/// `Other_Lowercase` of Unicode 15.1.0: the code points `str.islower`
/// reads as lowercase whose category is not `Ll`.
const OTHER_LOWERCASE: [(u32, u32); 28] = [
    (0xAA, 0xAA),
    (0xBA, 0xBA),
    (0x2B0, 0x2B8),
    (0x2C0, 0x2C1),
    (0x2E0, 0x2E4),
    (0x345, 0x345),
    (0x37A, 0x37A),
    (0x10FC, 0x10FC),
    (0x1D2C, 0x1D6A),
    (0x1D78, 0x1D78),
    (0x1D9B, 0x1DBF),
    (0x2071, 0x2071),
    (0x207F, 0x207F),
    (0x2090, 0x209C),
    (0x2170, 0x217F),
    (0x24D0, 0x24E9),
    (0x2C7C, 0x2C7D),
    (0xA69C, 0xA69D),
    (0xA770, 0xA770),
    (0xA7F2, 0xA7F4),
    (0xA7F8, 0xA7F9),
    (0xAB5C, 0xAB5F),
    (0xAB69, 0xAB69),
    (0x10780, 0x10780),
    (0x10783, 0x10785),
    (0x10787, 0x107B0),
    (0x107B2, 0x107BA),
    (0x1E030, 0x1E06D),
];

/// `str.islower` of a one-character string: the `Lowercase` property
/// (`Ll` and `Other_Lowercase`).
pub fn is_lower(cp: u32) -> bool {
    if cp < 0x80 {
        return (cp as u8).is_ascii_lowercase();
    }
    unicode::category(cp) == "Ll"
        || OTHER_LOWERCASE
            .iter()
            .any(|&(lo, hi)| (lo..=hi).contains(&cp))
}

/// `str.isalpha`: non-empty and every code point alphabetic.
fn is_alpha_str(s: &[u32]) -> bool {
    !s.is_empty() && s.iter().all(|&c| unicode::is_alpha(c))
}

/// `_refused_spelling`: two separators first (either slash in either
/// place), a NUL anywhere, or a URL's `://` anywhere.
pub fn refused_spelling(s: &[u32]) -> bool {
    (s.len() >= 2 && is_sep(s[0]) && is_sep(s[1])) || s.contains(&0) || contains_seq(s, "://")
}

/// `_escapes_root`: refused, rooted, drive-qualified (`_DRIVE_PREFIX_RE`,
/// ASCII letters only), or a `..` that climbs above the root at any
/// point, either slash separating.
pub fn escapes_root(rel: &[u32]) -> bool {
    if refused_spelling(rel) {
        return true;
    }
    let folded: Vec<u32> = rel
        .iter()
        .map(|&c| if c == BACKSLASH { SLASH } else { c })
        .collect();
    let drive = folded.len() >= 2
        && folded[0] < 0x80
        && (folded[0] as u8).is_ascii_alphabetic()
        && folded[1] == COLON;
    if folded.first() == Some(&SLASH) || drive {
        return true;
    }
    let mut depth: i64 = 0;
    for part in folded.split(|&c| c == SLASH) {
        if part.is_empty() || eq(part, ".") {
            continue;
        }
        if eq(part, "..") {
            depth -= 1;
            if depth < 0 {
                return true;
            }
        } else {
            depth += 1;
        }
    }
    false
}

/// `_fold_altsep(p, sep, altsep)`: `p.replace(altsep, sep)` when `altsep`
/// is given and not empty, else `p`.
pub fn fold_altsep(p: &[u32], sep: &[u32], altsep: Option<&[u32]>) -> Vec<u32> {
    match altsep {
        Some(alt) if !alt.is_empty() => replace(p, alt, sep),
        _ => p.to_vec(),
    }
}

/// `_is_placeholder_path`: a listed placeholder or under a listed prefix,
/// as spelled or with one trailing extension cut off.
pub fn is_placeholder_path(s: &[u32]) -> bool {
    let listed = |t: &[u32]| {
        PLACEHOLDER_PATHS.iter().any(|p| eq(t, p))
            || PLACEHOLDER_PREFIXES.iter().any(|p| starts(t, p))
    };
    if listed(s) {
        return true;
    }
    let last_start = s.iter().rposition(|&c| c == SLASH).map_or(0, |i| i + 1);
    if s[last_start..].contains(&('.' as u32)) {
        // The last `.` of `s` is in its last segment, so `stem != s`.
        let dot = s.iter().rposition(|&c| c == '.' as u32).unwrap_or(0);
        let stem = &s[..dot];
        if listed(stem) {
            return true;
        }
    }
    false
}

/// `_is_single_segment_routelike`: exactly one `/`, and `s[1:]` (read
/// whatever `s[0]` is) non-empty and either a well-known web file name
/// once `str.lower`ed, or without a dot.
pub fn is_single_segment_routelike(s: &[u32]) -> bool {
    if count(s, SLASH) != 1 {
        return false;
    }
    let segment = &s[1..];
    if segment.is_empty() {
        return false;
    }
    let lowered = unicode::lower(segment);
    if WELLKNOWN_ROUTE_SEGMENTS.iter().any(|w| eq(&lowered, w)) {
        return true;
    }
    !segment.contains(&('.' as u32))
}

/// `_bounded_scan_text`: a body of at most `_MAX_BODY_SCAN_BYTES` code
/// points whole; a longer one cut to that many, then at its last space,
/// newline or tab when that is past the first code point.
pub fn bounded_scan_text(body: &[u32]) -> &[u32] {
    if body.len() <= MAX_BODY_SCAN_BYTES {
        return body;
    }
    let truncated = &body[..MAX_BODY_SCAN_BYTES];
    let last_ws = truncated
        .iter()
        .rposition(|&c| c == SPACE || c == NEWLINE || c == TAB);
    match last_ws {
        Some(i) if i > 0 => &truncated[..i],
        _ => truncated,
    }
}

/// `_trim_candidate`: stripped, `\ ` read as a space, `$HOME/` and
/// `${HOME}/` spelled `~/`, trailing punctuation cut (a `)` only while
/// the `)`s outnumber the `(`s), trailing slashes cut down to one code
/// point.
pub fn trim_candidate(raw: &[u32]) -> Vec<u32> {
    let mut s = replace(strip(raw), &cps("\\ "), &cps(" "));
    if starts(&s, "$HOME/") {
        s.splice(..5, [u32::from('~')]);
    } else if starts(&s, "${HOME}/") {
        s.splice(..7, [u32::from('~')]);
    }
    let punct = cps(TRAILING_PUNCT);
    let opens = count(&s, '(' as u32);
    let mut closes = count(&s, ')' as u32);
    while let Some(&last) = s.last() {
        if !punct.contains(&last) {
            break;
        }
        if last == ')' as u32 {
            if opens >= closes {
                break;
            }
            closes -= 1;
        }
        s.pop();
    }
    while s.len() > 1 && s.last() == Some(&SLASH) {
        s.pop();
    }
    s
}

fn drive_shaped(s: &[u32]) -> bool {
    s.len() >= 3 && unicode::is_alpha(s[0]) && s[1] == COLON && is_sep(s[2])
}

/// `_command_shaped`: a candidate holding a space or a tab whose first
/// word crosses at most one boundary, or with a later slashless word that
/// is a dash flag or alphabetic and lowercase first. A candidate whose
/// split is empty raises `IndexError`, as the reference does.
pub fn command_shaped(s: &[u32]) -> Result<bool, IndexError> {
    if !s.contains(&SPACE) && !s.contains(&TAB) {
        return Ok(false);
    }
    let parts = split_ws(s);
    let first = *parts.first().ok_or(IndexError)?;
    let mut boundaries = count(first, SLASH) + count(first, BACKSLASH);
    if drive_shaped(first) {
        boundaries += 1;
    }
    if starts(first, "~/") {
        boundaries += 1;
    }
    let arglike = parts[1..].iter().any(|p| {
        !p.contains(&SLASH)
            && !p.contains(&BACKSLASH)
            && (p[0] == '-' as u32 || (is_alpha_str(p) && is_lower(p[0])))
    });
    Ok(boundaries <= 1 || arglike)
}

/// `_anchored_candidate`: `s` read by its home, root or drive anchor, or
/// None.
pub fn anchored_candidate(s: &[u32]) -> Option<&[u32]> {
    if starts(s, "~/") {
        if s.len() <= 2 || is_placeholder_path(s) {
            return None;
        }
        return Some(s);
    }
    if starts(s, "/") {
        if s.len() < 3 || is_placeholder_path(s) || is_single_segment_routelike(s) {
            return None;
        }
        return Some(s);
    }
    if drive_shaped(s) {
        return Some(s);
    }
    None
}

/// `_LINE_SUFFIX_RE.match(s).group(1)`, for
/// `^(.+[\\/][^\\/:]+):\d+(?:[-:]\d+)?$`.
///
/// The tail after the group holds no separator, so the group ends at the
/// first `:` after the LAST separator, whichever start the engine's
/// backtracking tries first; `.+` needs that separator past the first
/// code point with no newline before it, and `[^\\/:]+` (which a newline
/// satisfies) at least one code point between. The tail is `\d+`
/// (Unicode decimal digits), optionally `-` or `:` and `\d+`, then the
/// end or a newline that ends the text (`$`).
pub fn line_suffix(s: &[u32]) -> Option<&[u32]> {
    let k = s.iter().rposition(|&c| is_sep(c))?;
    if k < 1 || s[..k].contains(&NEWLINE) {
        return None;
    }
    let g = k + 1 + s[k + 1..].iter().position(|&c| c == COLON)?;
    if g == k + 1 {
        return None;
    }
    let mut tail = &s[g + 1..];
    if tail.last() == Some(&NEWLINE) {
        tail = &tail[..tail.len() - 1];
    }
    let digits = |t: &[u32]| !t.is_empty() && t.iter().all(|&c| unicode::is_re_digit(c));
    let ok = match tail.iter().position(|&c| c == '-' as u32 || c == COLON) {
        None => digits(tail),
        Some(i) => digits(&tail[..i]) && digits(&tail[i + 1..]),
    };
    ok.then_some(&s[..g])
}

/// `_normalize_candidate`: the spelling a check reads, or None when the
/// candidate is no path claim. Raises (`Err`) where `_command_shaped`
/// does.
pub fn normalize_candidate(raw: &[u32]) -> Result<Option<Vec<u32>>, IndexError> {
    if raw.is_empty() || raw.len() > MAX_PATH_LENGTH + 64 {
        return Ok(None);
    }
    let mut s = trim_candidate(raw);
    if s.is_empty() || s.len() > MAX_PATH_LENGTH {
        return Ok(None);
    }
    if refused_spelling(&s) || contains_seq(&s, "://") {
        return Ok(None);
    }
    let first_slash = index_of(&s, '/');
    if let (Some(at), Some(colon)) = (index_of(&s, '@'), index_of(&s, ':')) {
        if at < colon && first_slash.is_none_or(|f| at < f) {
            return Ok(None);
        }
    }
    if let Some(group) = line_suffix(&s) {
        s = group.to_vec();
    }
    if s.iter()
        .any(|&c| "*?<>{}".chars().any(|g| c == u32::from(g)))
    {
        return Ok(None);
    }
    if command_shaped(&s)? {
        return Ok(None);
    }
    Ok(anchored_candidate(&s).map(<[u32]>::to_vec))
}
