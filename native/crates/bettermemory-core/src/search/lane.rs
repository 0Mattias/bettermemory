//! The conversational lane (`search_lane`): the query's temporal reading,
//! a record's anchor day, the scaffold class and its numerals.
//!
//! The reference reads the query with `re` patterns over `query.lower()`;
//! here each pattern is matched by hand, position by position, with `re`'s
//! Unicode classes (`\w`, `\d`, `\s`, `\b`) and its backtracking where it
//! changes the result. Dates are proleptic ordinals (0001-01-01 is 1), with
//! `datetime.date`'s range and its errors.

use std::collections::HashSet;
use std::sync::OnceLock;

use crate::datetime::{days_in_month, ord_to_ymd, ymd_to_ord, DateTime};
use crate::lexicon::stem_token;
use crate::unicode;

use super::SearchError;

/// The scaffold surface forms (`_CONV_SCAFFOLD_SURFACE`).
pub const SCAFFOLD_SURFACE: &[&str] = &[
    "day",
    "days",
    "week",
    "weeks",
    "weekend",
    "month",
    "months",
    "year",
    "years",
    "ago",
    "yesterday",
    "today",
    "last",
    "latest",
    "first",
    "earliest",
    "past",
    "recent",
    "recently",
    "current",
    "currently",
    "many",
    "much",
    "long",
    "total",
    "time",
    "times",
    "since",
    "between",
    "order",
    "passed",
    "once",
    "twice",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
];

/// `_CONV_TIME_UNIT_SURFACE`.
pub const TIME_UNIT_SURFACE: &[&str] = &[
    "day", "days", "week", "weeks", "weekend", "month", "months", "year", "years",
];

/// `_MONTH_NAMES`.
pub const MONTH_NAMES: [&str; 12] = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
];

/// `_CONV_MAY_CONTEXT_WORDS`.
pub const MAY_CONTEXT_WORDS: &[&str] = &[
    "in", "last", "during", "by", "until", "since", "for", "next", "this", "early", "late", "mid",
];

/// `_CONV_MAY_MODAL_FOLLOWERS`.
pub const MAY_MODAL_FOLLOWERS: &[&str] = &[
    "be", "have", "has", "not", "also", "still", "never", "just", "only", "even", "already",
    "well", "actually", "need", "needs", "want", "require", "requires", "cause", "take", "help",
    "seem", "seems", "work", "look", "get", "become", "make", "break", "fail", "mean", "differ",
    "vary", "change", "depend", "use", "include", "apply", "affect", "result", "lead", "contain",
    "involve", "run", "return", "throw", "raise", "crash", "hang", "conflict", "explain",
    "indicate", "suggest", "show", "appear", "exist", "occur", "happen", "come", "go", "do",
    "prove", "turn", "end", "start", "stop", "block", "prevent", "allow", "let", "keep", "hold",
    "lose", "miss", "skip", "ignore", "override", "expire", "drop", "leak", "matter", "sound",
    "feel",
];

/// `_CONV_ANCHOR_SCAN_CHARS`.
const ANCHOR_SCAN_CHARS: usize = 200;

struct Stems {
    scaffold: HashSet<String>,
    time_unit: HashSet<String>,
}

fn stems() -> &'static Stems {
    static STEMS: OnceLock<Stems> = OnceLock::new();
    STEMS.get_or_init(|| Stems {
        scaffold: SCAFFOLD_SURFACE.iter().map(|w| stem_token(w)).collect(),
        time_unit: TIME_UNIT_SURFACE.iter().map(|w| stem_token(w)).collect(),
    })
}

/// `_CONV_SCAFFOLD_STEMS`, sorted.
pub fn scaffold_stems() -> Vec<String> {
    let mut v: Vec<String> = stems().scaffold.iter().cloned().collect();
    v.sort();
    v
}

/// `_CONV_TIME_UNIT_STEMS`, sorted.
pub fn time_unit_stems() -> Vec<String> {
    let mut v: Vec<String> = stems().time_unit.iter().cloned().collect();
    v.sort();
    v
}

/// `str.isdigit()` of a token.
fn is_digit_token(t: &str) -> bool {
    !t.is_empty() && t.chars().all(|c| unicode::is_digit(c as u32))
}

/// `_conv_scaffold_terms`: the query tokens, in order, the scaffold floor
/// reprices.
pub fn scaffold_terms(tokens: &[String]) -> Vec<&str> {
    let s = stems();
    let mut out = Vec::new();
    for (i, t) in tokens.iter().enumerate() {
        if s.scaffold.contains(t) {
            out.push(t.as_str());
        } else if is_digit_token(t) && t.chars().count() <= 2 {
            if let Some(next) = tokens.get(i + 1) {
                if s.time_unit.contains(next) {
                    out.push(t.as_str());
                }
            }
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Dates: `datetime.date` as an ordinal.

const MAX_ORDINAL: i64 = 3_652_059; // 9999-12-31

/// `date(y, m, d)`: ValueError outside the calendar or years 1 to 9999.
fn date(y: i64, m: u32, d: u32) -> Result<i64, SearchError> {
    if !(1..=9999).contains(&y) || !(1..=12).contains(&m) || d < 1 || d > days_in_month(y, m) {
        return Err(SearchError::ValueError);
    }
    Ok(ymd_to_ord(y, m, d))
}

/// `day + timedelta(days=n)`: OverflowError outside years 1 to 9999.
fn add_days(day: i64, n: i64) -> Result<i64, SearchError> {
    let r = day + n;
    if !(1..=MAX_ORDINAL).contains(&r) {
        return Err(SearchError::OverflowError);
    }
    Ok(r)
}

fn month_window(month: u32, year: i64) -> Result<(i64, i64), SearchError> {
    let start = date(year, month, 1)?;
    let next = if month == 12 {
        date(year + 1, 1, 1)?
    } else {
        date(year, month + 1, 1)?
    };
    Ok((start, add_days(next, -1)?))
}

/// The date of a datetime, in its own offset (naive: its fields).
pub fn local_date(dt: &DateTime) -> i64 {
    ymd_to_ord(i64::from(dt.year), dt.month, dt.day)
}

// ---------------------------------------------------------------------------
// `re` over code points.

fn word(t: &[u32], i: usize) -> bool {
    t.get(i).is_some_and(|&c| unicode::is_word(c))
}

/// `\b` at `i`.
fn boundary(t: &[u32], i: usize) -> bool {
    let before = i > 0 && word(t, i - 1);
    before != word(t, i)
}

fn space(t: &[u32], i: usize) -> bool {
    t.get(i).is_some_and(|&c| unicode::is_space(c))
}

fn digit(t: &[u32], i: usize) -> bool {
    t.get(i).is_some_and(|&c| unicode::is_re_digit(c))
}

/// The literal (ASCII) `lit` at `i`.
fn lit(t: &[u32], i: usize, lit: &str) -> bool {
    t.len() >= i + lit.len() && lit.bytes().zip(&t[i..]).all(|(b, &c)| u32::from(b) == c)
}

/// `\s+` at `i`, greedy: the end of the run, or None.
fn spaces(t: &[u32], i: usize) -> Option<usize> {
    let mut j = i;
    while space(t, j) {
        j += 1;
    }
    (j > i).then_some(j)
}

/// The decimal value of a `\d` code point: Unicode encodes each decimal
/// digit set as ten contiguous code points from zero, so the value is the
/// count of decimal digits just below it, modulo ten.
fn digit_value(cp: u32) -> i64 {
    let mut k = 0;
    let mut c = cp;
    while c > 0 && unicode::is_re_digit(c - 1) {
        k += 1;
        c -= 1;
    }
    k % 10
}

/// `int()` of a run of `\d` code points.
fn int_of(t: &[u32]) -> i64 {
    t.iter().fold(0i64, |acc, &c| acc * 10 + digit_value(c))
}

/// `\d{n}` at `i`.
fn digits(t: &[u32], i: usize, n: usize) -> bool {
    (i..i + n).all(|j| digit(t, j))
}

fn sep(t: &[u32], i: usize) -> bool {
    matches!(t.get(i), Some(&0x2F) | Some(&0x2D))
}

/// `(\d{4})[/-](\d{2})[/-](\d{2})` at `i` (no boundaries): the groups and
/// the end.
fn ymd_at(t: &[u32], i: usize) -> Option<((i64, i64, i64), usize)> {
    if digits(t, i, 4)
        && sep(t, i + 4)
        && digits(t, i + 5, 2)
        && sep(t, i + 7)
        && digits(t, i + 8, 2)
    {
        let g = (
            int_of(&t[i..i + 4]),
            int_of(&t[i + 5..i + 7]),
            int_of(&t[i + 8..i + 10]),
        );
        return Some((g, i + 10));
    }
    None
}

/// `_CONV_DATE_RE` at `i`.
fn date_re_at(t: &[u32], i: usize) -> Option<((i64, i64, i64), usize)> {
    if !boundary(t, i) {
        return None;
    }
    let (g, end) = ymd_at(t, i)?;
    boundary(t, end).then_some((g, end))
}

/// Every non-overlapping match of a pattern, left to right.
fn find_all<T>(t: &[u32], mut at: impl FnMut(&[u32], usize) -> Option<(T, usize)>) -> Vec<T> {
    let mut out = Vec::new();
    let mut i = 0;
    while i <= t.len() {
        match at(t, i) {
            Some((v, end)) => {
                out.push(v);
                i = if end > i { end } else { i + 1 };
            }
            None => i += 1,
        }
    }
    out
}

fn search(t: &[u32], at: impl Fn(&[u32], usize) -> bool) -> bool {
    (0..=t.len()).any(|i| at(t, i))
}

/// One of `words`, then `\b`, at `i` (alternation in order): its end.
fn one_of(t: &[u32], i: usize, words: &[&str]) -> Option<usize> {
    words
        .iter()
        .find(|w| lit(t, i, w) && boundary(t, i + w.len()))
        .map(|w| i + w.len())
}

/// `_CONV_MONTH_RE` at `i`: (month number, year group, end).
fn month_at(t: &[u32], i: usize) -> Option<((u32, Option<i64>), usize)> {
    if !boundary(t, i) {
        return None;
    }
    let mut found = None;
    for (k, name) in MONTH_NAMES.iter().enumerate() {
        if *name == "may" {
            continue;
        }
        if lit(t, i, name) && boundary(t, i + name.len()) {
            found = Some((k as u32 + 1, i + name.len()));
            break;
        }
    }
    if found.is_none() && may_at(t, i) {
        found = Some((5, i + 3));
    }
    let (month, end) = found?;
    // `(?:\s+(\d{4}))?`
    if let Some(j) = spaces(t, end) {
        if digits(t, j, 4) {
            return Some(((month, Some(int_of(&t[j..j + 4]))), j + 4));
        }
    }
    Some(((month, None), end))
}

/// The may branch at `i`: its guard, `may`, the modal lookahead and `\b`.
fn may_at(t: &[u32], i: usize) -> bool {
    if !lit(t, i, "may") {
        return false;
    }
    let behind = MAY_CONTEXT_WORDS.iter().any(|w| {
        let n = w.len();
        i > n
            && (space(t, i - 1) || t[i - 1] == 0x2D)
            && lit(t, i - 1 - n, w)
            && boundary(t, i - 1 - n)
    });
    let ahead = spaces(t, i + 3).is_some_and(|j| digits(t, j, 4));
    if !(behind || ahead) {
        return false;
    }
    // `(?!\s+(?:followers)\b)`
    if let Some(j) = spaces(t, i + 3) {
        if one_of(t, j, MAY_MODAL_FOLLOWERS).is_some() {
            return false;
        }
    }
    boundary(t, i + 3)
}

/// `\b<first>\s+(<units>)\b` at `i`: the unit's index.
fn period_at(t: &[u32], i: usize, first: &str) -> Option<(usize, usize)> {
    if !boundary(t, i) || !lit(t, i, first) {
        return None;
    }
    let j = spaces(t, i + first.len())?;
    for (k, unit) in ["week", "month", "year"].iter().enumerate() {
        if lit(t, j, unit) && boundary(t, j + unit.len()) {
            return Some((k, j + unit.len()));
        }
    }
    None
}

const AGO_WORDS: [(&str, i64); 12] = [
    ("a", 1),
    ("an", 1),
    ("one", 1),
    ("two", 2),
    ("three", 3),
    ("four", 4),
    ("five", 5),
    ("six", 6),
    ("seven", 7),
    ("eight", 8),
    ("nine", 9),
    ("ten", 10),
];
const AGO_UNITS: [(&str, i64); 4] = [("day", 1), ("week", 7), ("month", 30), ("year", 365)];

/// After the count, at `j`: `\s+(day|week|month|year)s?\s+ago\b`.
fn ago_tail(t: &[u32], j: usize) -> Option<(i64, usize)> {
    let k = spaces(t, j)?;
    let &(unit, days) = AGO_UNITS.iter().find(|(u, _)| lit(t, k, u))?;
    let after = k + unit.len();
    for end in [after + 1, after] {
        if end == after + 1 && t.get(after) != Some(&0x73) {
            continue;
        }
        if let Some(m) = spaces(t, end) {
            if lit(t, m, "ago") && boundary(t, m + 3) {
                return Some((days, m + 3));
            }
        }
    }
    None
}

/// `_CONV_AGO_RE` at `i`: (count, unit days, end).
fn ago_at(t: &[u32], i: usize) -> Option<((i64, i64), usize)> {
    if !boundary(t, i) {
        return None;
    }
    if i >= 9 && (lit(t, i - 9, "how many ") || lit(t, i - 9, "how much ")) {
        return None;
    }
    for (w, n) in AGO_WORDS {
        if lit(t, i, w) {
            if let Some((days, end)) = ago_tail(t, i + w.len()) {
                return Some(((n, days), end));
            }
        }
    }
    for len in [2, 1] {
        if digits(t, i, len) {
            if let Some((days, end)) = ago_tail(t, i + len) {
                // `_CONV_AGO_NUMBERS.get(g) or int(g)`: digits are never
                // in the word table.
                return Some(((int_of(&t[i..i + len]), days), end));
            }
        }
    }
    None
}

/// `\bhow\s+(?:many|much)\s+(?:days?|weeks?|months?|years?|time)\b|\bhow\s+long\b`
fn elapsed(t: &[u32]) -> bool {
    search(t, |t, i| {
        if !boundary(t, i) || !lit(t, i, "how") {
            return false;
        }
        let Some(j) = spaces(t, i + 3) else {
            return false;
        };
        if lit(t, j, "long") && boundary(t, j + 4) {
            return true;
        }
        for q in ["many", "much"] {
            if !lit(t, j, q) {
                continue;
            }
            let Some(k) = spaces(t, j + 4) else {
                continue;
            };
            for unit in ["day", "week", "month", "year"] {
                if lit(t, k, unit) {
                    let e = k + unit.len();
                    if (t.get(e) == Some(&0x73) && boundary(t, e + 1)) || boundary(t, e) {
                        return true;
                    }
                }
            }
            if lit(t, k, "time") && boundary(t, k + 4) {
                return true;
            }
        }
        false
    })
}

/// `_CONV_ORDER_RE`.
fn order(t: &[u32]) -> bool {
    search(t, |t, i| {
        if !boundary(t, i) {
            return false;
        }
        // `(?:the\s+)?order\s+(?:of|from)\b`: where it matches after
        // "the ", it matches at "order" too.
        if lit(t, i, "order") {
            if let Some(j) = spaces(t, i + 5) {
                if one_of(t, j, &["of", "from"]).is_some() {
                    return true;
                }
            }
        }
        for (a, b) in [("earliest", "latest"), ("first", "last")] {
            if lit(t, i, a) {
                if let Some(j) = spaces(t, i + a.len()) {
                    if lit(t, j, "to") {
                        if let Some(k) = spaces(t, j + 2) {
                            if lit(t, k, b) && boundary(t, k + b.len()) {
                                return true;
                            }
                        }
                    }
                }
            }
        }
        if lit(t, i, "chronolog") {
            return true;
        }
        if lit(t, i, "which") && space(t, i + 5) {
            return which_at(t, i + 5);
        }
        false
    })
}

/// `\s+.{0,80}?(?:first|last)\b` from `start`, a `\s` code point: `\s+`
/// takes one to all of the run there, and `.` any code point but `\n`.
fn which_at(t: &[u32], start: usize) -> bool {
    let run_end = spaces(t, start).unwrap_or(start);
    let hit = |p: usize| {
        (lit(t, p, "first") && boundary(t, p + 5)) || (lit(t, p, "last") && boundary(t, p + 4))
    };
    // Inside the run: `\s+` stops short and `.` takes nothing.
    if (start + 1..=run_end).any(hit) {
        return true;
    }
    // Past the run: `\s+` takes it all and `.{0,80}` the rest, no `\n`.
    let mut p = run_end;
    for k in 0..=80 {
        if k > 0 {
            if t.get(p) == Some(&0x0A) || p >= t.len() {
                return false;
            }
            p += 1;
        }
        if k > 0 && hit(p) {
            return true;
        }
    }
    false
}

/// `_CONV_WHEN_RE`.
fn when(t: &[u32]) -> bool {
    if lit(t, 0, "when") && boundary(t, 4) {
        return true;
    }
    search(t, |t, i| {
        if !boundary(t, i) {
            return false;
        }
        if lit(t, i, "when") {
            if let Some(j) = spaces(t, i + 4) {
                if lit(t, j, "did") && boundary(t, j + 3) {
                    return true;
                }
            }
        }
        if lit(t, i, "what") {
            if let Some(j) = spaces(t, i + 4) {
                if one_of(t, j, &["day", "date", "month"]).is_some() {
                    return true;
                }
            }
        }
        false
    })
}

/// `_CONV_LATEST_RE`.
fn latest(t: &[u32]) -> bool {
    search(t, |t, i| {
        if !boundary(t, i) {
            return false;
        }
        for who in ["i", "we"] {
            if lit(t, i, who) {
                if let Some(j) = spaces(t, i + who.len()) {
                    if lit(t, j, "last") && boundary(t, j + 4) {
                        return true;
                    }
                }
            }
        }
        if lit(t, i, "last") {
            if let Some(j) = spaces(t, i + 4) {
                if lit(t, j, "time") && boundary(t, j + 4) {
                    return true;
                }
            }
        }
        if lit(t, i, "most") {
            if let Some(j) = spaces(t, i + 4) {
                if lit(t, j, "recent") {
                    let e = j + 6;
                    if (lit(t, e, "ly") && boundary(t, e + 2)) || boundary(t, e) {
                        return true;
                    }
                }
            }
        }
        false
    })
}

/// Which narration a query's shape selects.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Selector {
    Earliest,
    Latest,
}

/// `_TemporalReading`: a closed window of ordinals, a selector, both, or
/// neither.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TemporalReading {
    pub window: Option<(i64, i64)>,
    pub selector: Option<Selector>,
}

impl TemporalReading {
    pub fn is_temporal(&self) -> bool {
        self.window.is_some() || self.selector.is_some()
    }
}

/// `_temporal_reading(query, now)`, with the errors it raises.
pub fn temporal_reading(query: &[u32], now: &DateTime) -> Result<TemporalReading, SearchError> {
    let t = unicode::lower(query);
    let t = t.as_slice();
    let today = local_date(now);
    let (ty, tm, _) = ord_to_ymd(today);
    let weekday = (today + 6) % 7;
    let mut windows: Vec<(i64, i64)> = Vec::new();

    for (y, mo, d) in find_all(t, date_re_at) {
        if (1..=12).contains(&mo) && (1..=31).contains(&d) {
            if let Ok(day) = date(y, mo as u32, d as u32) {
                windows.push((day, day));
            }
        }
    }
    for (month, year) in find_all(t, month_at) {
        let year = match year {
            Some(y) => y,
            None if month <= tm => ty,
            None => ty - 1,
        };
        if let Ok(w) = month_window(month, year) {
            windows.push(w);
        }
    }
    for unit in find_all(t, |t, i| period_at(t, i, "last")) {
        match unit {
            0 => {
                let start_this = add_days(today, -weekday)?;
                windows.push((add_days(start_this, -7)?, add_days(start_this, -1)?));
            }
            1 => {
                let prev_last = add_days(date(ty, tm, 1)?, -1)?;
                let (py, pm, _) = ord_to_ymd(prev_last);
                windows.push(month_window(pm, py)?);
            }
            _ => windows.push((date(ty - 1, 1, 1)?, date(ty - 1, 12, 31)?)),
        }
    }
    for unit in find_all(t, |t, i| period_at(t, i, "this")) {
        match unit {
            0 => {
                let start_this = add_days(today, -weekday)?;
                windows.push((start_this, add_days(start_this, 6)?));
            }
            1 => windows.push(month_window(tm, ty)?),
            _ => windows.push((date(ty, 1, 1)?, date(ty, 12, 31)?)),
        }
    }
    if search(t, |t, i| {
        boundary(t, i) && lit(t, i, "yesterday") && boundary(t, i + 9)
    }) {
        let yday = add_days(today, -1)?;
        windows.push((yday, yday));
    }
    for (count, unit_days) in find_all(t, ago_at) {
        let target = add_days(today, -(count * unit_days))?;
        let half = (unit_days / 2).max(1);
        windows.push((add_days(target, -half)?, add_days(target, half)?));
    }

    let window = if windows.is_empty() {
        None
    } else {
        Some((
            windows.iter().map(|w| w.0).min().unwrap_or(0),
            windows.iter().map(|w| w.1).max().unwrap_or(0),
        ))
    };
    let selector = if order(t) {
        Some(Selector::Earliest)
    } else if elapsed(t) || when(t) {
        Some(if latest(t) {
            Selector::Latest
        } else {
            Selector::Earliest
        })
    } else if latest(t) {
        Some(Selector::Latest)
    } else {
        None
    };
    Ok(TemporalReading { window, selector })
}

/// `_memory_anchor_day`: the leading bracketed date, else the first date
/// in the first 200 code points, else the date of `created` (in its own
/// offset).
pub fn anchor_day(body: &[u32], created: &DateTime) -> i64 {
    let head = if body.first() == Some(&0x5B) {
        ymd_at(body, 1).map(|(g, _)| g)
    } else {
        None
    };
    let m = match head {
        Some(g) => Some(g),
        None => {
            let scan = &body[..body.len().min(ANCHOR_SCAN_CHARS)];
            (0..=scan.len()).find_map(|i| date_re_at(scan, i).map(|(g, _)| g))
        }
    };
    if let Some((y, mo, d)) = m {
        if (1..=12).contains(&mo) && (1..=31).contains(&d) {
            if let Ok(day) = date(y, mo as u32, d as u32) {
                return day;
            }
        }
    }
    local_date(created)
}

/// `_V2_HIGH_MATCHED_FLOOR`.
pub const V2_HIGH_MATCHED_FLOOR: i64 = 4;

/// `_relevance_label_v2`: the coverage label with an absolute floor of
/// matched tokens on its "high" arm.
pub fn relevance_label_v2(matched_unique: i64, query_unique: i64) -> &'static str {
    if query_unique <= 0 {
        return "low";
    }
    let coverage = super::num::int_true_div(i128::from(matched_unique), i128::from(query_unique));
    if coverage >= 0.75 || matched_unique >= V2_HIGH_MATCHED_FLOOR {
        "high"
    } else if coverage >= 0.40 {
        "medium"
    } else {
        "low"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read(q: &str) -> TemporalReading {
        let now = DateTime::new(2026, 6, 1, 12, 0, 0, 0, Some(0)).unwrap();
        temporal_reading(&unicode::cps(q), &now).unwrap()
    }

    #[test]
    fn decimal_digits_have_their_values() {
        assert_eq!(digit_value(u32::from(b'7')), 7);
        assert_eq!(digit_value(0x0663), 3); // ARABIC-INDIC DIGIT THREE
        assert_eq!(digit_value(0x1D7D8), 0); // MATHEMATICAL DOUBLE-STRUCK ZERO
        assert_eq!(digit_value(0x1D7E1), 9);
    }

    #[test]
    fn shapes() {
        assert!(read("this may need sudo").window.is_none());
        assert!(read("in may we migrated").window.is_some());
        assert!(read("may 2026").window.is_some());
        assert_eq!(read("which atlast").selector, Some(Selector::Earliest));
        assert!(read("which\nfirst").selector.is_some());
        assert_eq!(read("the last time i").selector, Some(Selector::Latest));
        assert!(!read("docker compose").is_temporal());
    }
}
