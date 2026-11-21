//! Unit 2: the three extraction patterns of `verify_candidates.py` as
//! hand matchers with `finditer` semantics: `_BACKTICK_RE` (line 87),
//! `_BARE_RE` (107-119) and `_DOMAIN_ROUTE_RE` (184-186). The pyre
//! decision (section 5, rule 3) names these three as hand-written
//! leaves; each gains the engine's cross-check once `pyre::Regex` lands.
//!
//! Each `*_iter` answers what `[m.span() + m.span(1) for m in
//! P.finditer(text)]` answers: one [`Hit`] per match, in order. A search
//! starts at 0 and, after a match, at the match's end (no match here is
//! empty). `\w` is [`is_word`] (`re`'s Unicode word class) and `\s` is
//! [`is_space`]: `re`'s `\s` on a `str` pattern is `Py_UNICODE_ISSPACE`,
//! the same set `str.isspace` reads.
//!
//! Why one path per start equals `re`'s backtracking (the template is
//! `hw/parts_model_det.py` of the pyre design, checked against `re` on
//! 2,146,278 texts, and the golden sweeps of this unit):
//!
//! - `_BACKTICK_RE`, `` `([^`\n]+)` ``: the class excludes the backtick, so
//!   the greedy run ends at the first backtick or newline; giving back
//!   leaves a class character where the closing backtick must be.
//! - `_BARE_RE`: the five prefix alternatives differ in their first two
//!   code points, so at most one applies at a start. The body class
//!   holds no space, so a shorter body ends before a body character, never
//!   before the space a continuation needs, and the greedy body is the only
//!   one that can be followed. In a continuation, `[\w.\-]+` holds no space
//!   and no separator, and a spaced word needs two code points; with fewer
//!   words or a shorter word the next code point is a space or a word
//!   character, never the `[\\/]` that must follow. So a continuation
//!   either completes with its greedy words and greedy body or the `*`
//!   stops before it, giving back to the last complete continuation.
//! - `_DOMAIN_ROUTE_RE`: `[\w-]` and `.` are disjoint, so the greedy
//!   labels end only where the next code point is neither; giving back a
//!   label leaves a `.` where `/` must be. The optional leading `.` taken
//!   or not changes nothing: without it `[\w-]+` must start on that `.`.
//!   The tail `[\w.\-]+` has nothing after it.
//!
//! Linearity: a backtick start that fails scanned a run with no backtick
//! in it. A bare start needs a boundary code point, which no body or
//! continuation word holds, and its continuation scan is consumed by the
//! match it extends or stops at a space followed by a word that cannot
//! open a prefix. A domain start inside a `[\w-]` run or at a later label
//! of a dotted run is refused by a lookbehind in constant time, so a
//! token is scanned from at most two starts.

use crate::unicode::{is_space, is_word};

/// One match: `m.span()` and `m.span(1)`, in code points.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Hit {
    pub start: usize,
    pub end: usize,
    pub group_start: usize,
    pub group_end: usize,
}

const BACKTICK: u32 = 0x60;
const NEWLINE: u32 = 0x0A;
const DOT: u32 = 0x2E;
const SLASH: u32 = 0x2F;
const BACKSLASH: u32 = 0x5C;
const SPACE: u32 = 0x20;
const DASH: u32 = 0x2D;

fn run(t: &[u32], mut i: usize, class: impl Fn(u32) -> bool) -> usize {
    while i < t.len() && class(t[i]) {
        i += 1;
    }
    i
}

/// `finditer` over the starts `at` answers for: a hit resumes the search
/// at its end, a miss at the next code point.
fn scan(t: &[u32], at: impl Fn(&[u32], usize) -> Option<Hit>) -> Vec<Hit> {
    let mut out = Vec::new();
    let mut i = 0;
    while i <= t.len() {
        match at(t, i) {
            Some(hit) => {
                i = hit.end;
                out.push(hit);
            }
            None => i += 1,
        }
    }
    out
}

/// `_BACKTICK_RE.finditer(text)`: `` `([^`\n]+)` ``.
pub fn backtick_iter(t: &[u32]) -> Vec<Hit> {
    scan(t, |t, i| {
        if t.get(i) != Some(&BACKTICK) {
            return None;
        }
        let e = run(t, i + 1, |c| c != BACKTICK && c != NEWLINE);
        (e > i + 1 && t.get(e) == Some(&BACKTICK)).then_some(Hit {
            start: i,
            end: e + 1,
            group_start: i + 1,
            group_end: e,
        })
    })
}

/// `[\w./\-_~\\@+%]`.
fn is_body(c: u32) -> bool {
    is_word(c)
        || matches!(
            char::from_u32(c),
            Some('.' | '/' | '-' | '_' | '~' | '\\' | '@' | '+' | '%')
        )
}

/// `[\w.\-]`.
fn is_tail(c: u32) -> bool {
    is_word(c) || c == DOT || c == DASH
}

/// `[=|\s(\[{<"',;“”‘’]`.
fn is_boundary(c: u32) -> bool {
    is_space(c)
        || matches!(
            char::from_u32(c),
            Some('=' | '|' | '(' | '[' | '{' | '<' | '"' | '\'' | ',' | ';')
                | Some('\u{201C}' | '\u{201D}' | '\u{2018}' | '\u{2019}')
        )
}

fn starts_with(t: &[u32], p: usize, lit: &str) -> bool {
    let n = lit.chars().count();
    p + n <= t.len() && t[p..p + n].iter().copied().eq(lit.chars().map(u32::from))
}

/// Group 1 of `_BARE_RE` starting at `p`: its end, or None.
fn bare_group(t: &[u32], p: usize) -> Option<usize> {
    let prefix = ["~/", "$HOME/", "${HOME}/", "/"]
        .iter()
        .find(|lit| starts_with(t, p, lit))
        .map(|lit| p + lit.len());
    let drive = || {
        let letter = t
            .get(p)
            .is_some_and(|&c| c < 0x80 && (c as u8).is_ascii_alphabetic());
        let sep = matches!(t.get(p + 2), Some(&SLASH | &BACKSLASH));
        (letter && t.get(p + 1) == Some(&u32::from(':')) && sep).then_some(p + 3)
    };
    let q = prefix.or_else(drive)?;
    let mut e = run(t, q, is_body);
    if e == q {
        return None;
    }
    // `(?:(?: [A-Z][\w.\-]+)+[\\/][body]+)*`
    loop {
        let (mut k, mut words) = (e, 0);
        while k + 1 < t.len() && t[k] == SPACE && (0x41..=0x5A).contains(&t[k + 1]) {
            let w = run(t, k + 2, is_tail);
            if w == k + 2 {
                break;
            }
            (k, words) = (w, words + 1);
        }
        if words == 0 || !matches!(t.get(k), Some(&SLASH | &BACKSLASH)) {
            return Some(e);
        }
        let b = run(t, k + 1, is_body);
        if b == k + 1 {
            return Some(e);
        }
        e = b;
    }
}

/// `_BARE_RE.finditer(text)`: a boundary (`^` at 0 only, tried first, or
/// one consumed boundary code point), then the path as group 1.
pub fn bare_iter(t: &[u32]) -> Vec<Hit> {
    scan(t, |t, i| {
        let hit = |g: usize| {
            bare_group(t, g).map(|e| Hit {
                start: i,
                end: e,
                group_start: g,
                group_end: e,
            })
        };
        let at_caret = if i == 0 { hit(0) } else { None };
        at_caret.or_else(|| {
            if t.get(i).is_some_and(|&c| is_boundary(c)) {
                hit(i + 1)
            } else {
                None
            }
        })
    })
}

/// `[\w-]`.
fn is_label(c: u32) -> bool {
    is_word(c) || c == DASH
}

/// `_DOMAIN_ROUTE_RE` at start `i`: `domain_at` of `parts_model_det.py`.
fn domain_at(t: &[u32], i: usize) -> Option<Hit> {
    if i > 0 && is_label(t[i - 1]) {
        return None;
    }
    if i >= 2 && is_label(t[i - 2]) && t[i - 1] == DOT {
        return None;
    }
    let p = if t.get(i) == Some(&DOT) { i + 1 } else { i };
    let mut a = run(t, p, is_label);
    if a == p {
        return None;
    }
    let mut labels = 0;
    while t.get(a) == Some(&DOT) {
        let b = run(t, a + 1, is_label);
        if b == a + 1 {
            break;
        }
        (a, labels) = (b, labels + 1);
    }
    if labels == 0 || t.get(a) != Some(&SLASH) {
        return None;
    }
    let g = run(t, a + 1, is_tail);
    (g > a + 1).then_some(Hit {
        start: i,
        end: g,
        group_start: a + 1,
        group_end: g,
    })
}

/// `_DOMAIN_ROUTE_RE.finditer(text)`:
/// `(?<![\w-])(?<![\w-]\.)\.?[\w-]+(?:\.[\w-]+)+/([\w.\-]+)`.
pub fn domain_route_iter(t: &[u32]) -> Vec<Hit> {
    scan(t, domain_at)
}
