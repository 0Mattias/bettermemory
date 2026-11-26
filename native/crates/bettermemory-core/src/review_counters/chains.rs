//! The review chains (`review_counters.py` 296-610), unit
//! review_counters-1b: `ReviewChain` and its properties, `build_chains`
//! with the closing rule, `_in_window`, `chains_in_window`, `_share`,
//! `_median`, `_iso_week` and `_Tally` (`add`, `committed`, `refused`,
//! `filings_per_100`, `to_dict`).
//!
//! Times are compared as instants (`DateTime::instant_us`), as Python
//! orders two aware datetimes. Every row `parse_event_ts` gives is aware;
//! `now`, `start`, `end` and `horizon` are the caller's, aware in every
//! caller (`eval` and `review samples` build them in UTC), and a naive one
//! is read as UTC here where the reference raises TypeError on the first
//! comparison. `ts + ANSWER_WINDOW` moves the wall-clock fields and keeps
//! the offset, so past 9999-12-31 it is OverflowError, as in Python.

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};

use super::{CountersError, DecidedRow, IssuedRow, ANSWER_WINDOW_US, DETECTORS, REFUSAL_ACTIONS};
use crate::datetime::{days_in_month, ymd_to_ord, DateTime};
use crate::pyvalue::{cmp_int_text, int_from_str, PyStr, PyValue};
use crate::review::{ACTION_COMMIT, ACTION_STANDING, REVIEW_ISSUED};
use crate::search::num::{int_true_div, round_digits};
use crate::unicode::cps;

type Answer<T> = Result<T, CountersError>;

/// The instant of a datetime in microseconds; a naive one read as UTC.
fn at(ts: &DateTime) -> i128 {
    ts.instant_us().unwrap_or_else(|| ts.wall_micros())
}

/// `ts + ANSWER_WINDOW`: OverflowError past year 9999 (wall time).
fn plus_window(ts: &DateTime) -> Answer<DateTime> {
    ts.minus_micros(-i128::from(ANSWER_WINDOW_US))
        .map_err(|_| CountersError::OverflowError)
}

/// `ReviewChain.state`, spelled as the reference's three words.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChainState {
    Answered,
    Unanswered,
    Pending,
}

impl ChainState {
    /// The reference's word.
    pub fn as_str(self) -> &'static str {
        match self {
            ChainState::Answered => "answered",
            ChainState::Unanswered => "unanswered",
            ChainState::Pending => "pending",
        }
    }
}

/// One review chain and how it ended. `decided` is the write row that
/// answered it, its own or the closing call's (a copy: the reference
/// shares the one row). The properties read `issued[0]`, so they panic on
/// a chain without rows, where the reference raises IndexError;
/// `build_chains` makes none.
#[derive(Clone, Debug, PartialEq)]
pub struct ReviewChain {
    pub key: PyStr,
    pub issued: Vec<IssuedRow>,
    pub decided: Option<DecidedRow>,
    pub closed: bool,
    pub absorbed: bool,
    pub state: ChainState,
    /// Calls refused for a standing record before the chain's last
    /// decision.
    pub standing_retries: u64,
}

impl ReviewChain {
    /// `ReviewChain(key=key)`: no rows, pending.
    pub fn new(key: PyStr) -> ReviewChain {
        ReviewChain {
            key,
            issued: Vec::new(),
            decided: None,
            closed: false,
            absorbed: false,
            state: ChainState::Pending,
            standing_retries: 0,
        }
    }

    pub fn root(&self) -> &IssuedRow {
        &self.issued[0]
    }

    pub fn issued_at(&self) -> &DateTime {
        &self.root().ts
    }

    pub fn client(&self) -> &PyStr {
        &self.root().client
    }

    pub fn model(&self) -> &PyStr {
        &self.root().model
    }

    pub fn session(&self) -> Option<&PyStr> {
        self.root().session.as_ref()
    }

    pub fn pre_answered(&self) -> bool {
        self.root().pre_answered
    }

    /// The key, then every row's review_id that is not the key, repeats
    /// kept.
    pub fn review_ids(&self) -> Vec<PyStr> {
        let mut ids = vec![self.key.clone()];
        ids.extend(
            self.issued
                .iter()
                .filter(|r| r.review_id != self.key)
                .map(|r| r.review_id.clone()),
        );
        ids
    }

    /// Every id a review of the chain showed, first showing first.
    pub fn related(&self) -> Vec<PyStr> {
        let mut out: Vec<PyStr> = Vec::new();
        let mut seen: HashSet<&PyStr> = HashSet::new();
        for row in &self.issued {
            for id in &row.related {
                if seen.insert(id) {
                    out.push(id.clone());
                }
            }
        }
        out
    }

    /// One per row not pre-answered, one for a decision, plus the
    /// standing retries. Panics past `u64::MAX` (see `Tally`).
    pub fn calls(&self) -> u64 {
        let issued = self.issued.iter().filter(|r| !r.pre_answered).count() as u64;
        let mut calls = issued;
        bump(&mut calls, u64::from(self.decided.is_some()));
        bump(&mut calls, self.standing_retries);
        calls
    }

    /// The decided action, or None while not answered.
    pub fn outcome(&self) -> Option<&PyStr> {
        self.decided.as_ref().map(|d| &d.action)
    }

    /// The last row, in list order, issued at or before `ts`; else the
    /// root.
    pub fn last_issued_before(&self, ts: &DateTime) -> &IssuedRow {
        let t = at(ts);
        self.issued
            .iter()
            .rev()
            .find(|r| at(&r.ts) <= t)
            .unwrap_or_else(|| self.root())
    }

    /// `last_issued_before` over rows held in time order, as
    /// `build_chains` holds them: the same row, found by bisection.
    fn last_sorted_before(&self, t: i128) -> &IssuedRow {
        match self.issued.partition_point(|r| at(&r.ts) <= t) {
            0 => self.root(),
            n => &self.issued[n - 1],
        }
    }
}

/// A pre-answered chain whose call committed: its decision's instant and
/// the keys of its relation.
struct Closer {
    chain: usize,
    at: i128,
    keys: HashSet<PyStr>,
}

/// `build_chains`: every chain in `events`, judged from rows up to
/// `horizon` (all when None) as of `now`, in the order their roots were
/// issued. A value that is not a dict is skipped; a write row whose score
/// is an `int` past the float range raises OverflowError, as do the
/// window's sums past year 9999.
pub fn build_chains(
    events: &[PyValue],
    now: &DateTime,
    horizon: Option<&DateTime>,
) -> Answer<Vec<ReviewChain>> {
    let within = |ts: &DateTime| horizon.is_none_or(|h| at(ts) <= at(h));
    let mut issued_rows: Vec<IssuedRow> = Vec::new();
    let mut decided_rows: Vec<DecidedRow> = Vec::new();
    for event in events {
        if !matches!(event, PyValue::Dict(_)) {
            continue;
        }
        let kind = match event.get("kind") {
            Some(PyValue::Str(kind)) => kind,
            _ => continue,
        };
        if *kind == cps(REVIEW_ISSUED) {
            if let Some(row) = IssuedRow::from_event(event)? {
                if within(&row.ts) {
                    issued_rows.push(row);
                }
            }
        } else if *kind == cps("write") && matches!(event.get("review_id"), Some(PyValue::Str(_))) {
            if let Some(row) = DecidedRow::from_event(event)? {
                if within(&row.ts) {
                    decided_rows.push(row);
                }
            }
        }
    }

    // `sorted(key=ts)` is stable; Rust's sort_by_key is too.
    issued_rows.sort_by_key(|r| at(&r.ts));
    decided_rows.sort_by_key(|r| at(&r.ts));
    let mut chains: Vec<ReviewChain> = Vec::new();
    let mut index: HashMap<PyStr, usize> = HashMap::new();
    let mut chain_of: HashMap<PyStr, usize> = HashMap::new();
    for row in issued_rows {
        let key = row.chain_key().clone();
        let i = *index.entry(key.clone()).or_insert_with(|| {
            chains.push(ReviewChain::new(key.clone()));
            chains.len() - 1
        });
        chain_of.insert(row.review_id.clone(), i);
        chain_of.entry(key).or_insert(i);
        chains[i].issued.push(row);
    }

    let standing = cps(ACTION_STANDING);
    for decided in decided_rows {
        let Some(&i) = chain_of.get(&decided.review_id) else {
            continue;
        };
        let chain = &mut chains[i];
        if chain.decided.as_ref().is_some_and(|d| d.action != standing) {
            continue;
        }
        let t = at(&decided.ts);
        if t - at(&chain.last_sorted_before(t).ts) <= i128::from(ANSWER_WINDOW_US) {
            if chain.decided.is_some() {
                chain.standing_retries += 1;
            }
            chain.decided = Some(decided);
            chain.state = ChainState::Answered;
        }
    }

    // The closing rule. The reference walks every closer for every open
    // chain; a closer of another session or client, an absorbed one, and
    // one decided at or before the open row's time are skipped there with
    // no effect, so each chain reads only its own group's closers decided
    // after its last row, in decision order (the window's sum is made at
    // the first such closer not absorbed, where the reference makes it
    // first), and stops at the first one past the window, as every later
    // one is too.
    let mut closers: Vec<Closer> = chains
        .iter()
        .enumerate()
        .filter(|(_, c)| c.pre_answered())
        .filter_map(|(i, c)| {
            let d = c.decided.as_ref().filter(|d| d.committed())?;
            Some(Closer {
                chain: i,
                at: at(&d.ts),
                keys: d.relation.iter().map(|(k, _)| k.clone()).collect(),
            })
        })
        .collect();
    closers.sort_by_key(|c| c.at);
    let mut groups: HashMap<(Option<PyStr>, PyStr), Vec<Closer>> = HashMap::new();
    for closer in closers {
        let c = &chains[closer.chain];
        let key = (c.session().cloned(), c.client().clone());
        groups.entry(key).or_default().push(closer);
    }
    let mut order: Vec<usize> = (0..chains.len()).collect();
    order.sort_by_key(|&i| at(chains[i].issued_at()));
    for i in order {
        let chain = &chains[i];
        if chain.decided.is_some() || chain.pre_answered() {
            continue;
        }
        let last = chain.issued.last().expect("a chain has rows");
        if !last.roster.is_empty() {
            continue;
        }
        let Some(group) = groups.get(&(chain.session().cloned(), chain.client().clone())) else {
            continue;
        };
        let last_at = at(&last.ts);
        let mut limit = None;
        let mut found = None;
        for closer in &group[group.partition_point(|c| c.at <= last_at)..] {
            if chains[closer.chain].absorbed {
                continue;
            }
            let limit = match limit {
                Some(limit) => limit,
                None => *limit.insert(at(&plus_window(&last.ts)?)),
            };
            if closer.at > limit {
                break;
            }
            if last.related.iter().all(|id| closer.keys.contains(id)) {
                found = Some(closer.chain);
                break;
            }
        }
        if let Some(c) = found {
            chains[c].absorbed = true;
            let decided = chains[c].decided.clone();
            let chain = &mut chains[i];
            chain.decided = decided;
            chain.closed = true;
            chain.state = ChainState::Answered;
        }
    }

    let now = at(now);
    for chain in &mut chains {
        if chain.decided.is_some() {
            continue;
        }
        let deadline = plus_window(&chain.issued.last().expect("a chain has rows").ts)?;
        chain.state = if at(&deadline) <= now {
            ChainState::Unanswered
        } else {
            ChainState::Pending
        };
    }
    chains.sort_by_key(|c| at(c.issued_at()));
    Ok(chains)
}

/// `_in_window`: `start <= ts < end`, a side open when None.
pub fn in_window(ts: &DateTime, start: Option<&DateTime>, end: Option<&DateTime>) -> bool {
    let t = at(ts);
    start.is_none_or(|s| t >= at(s)) && end.is_none_or(|e| t < at(e))
}

/// `chains_in_window`: the chains whose root was issued in
/// `[start, end)`, judged from rows up to `ANSWER_WINDOW` after `end`,
/// absorbed closing calls left out.
pub fn chains_in_window(
    events: &[PyValue],
    start: Option<&DateTime>,
    end: Option<&DateTime>,
    now: &DateTime,
) -> Answer<Vec<ReviewChain>> {
    let horizon = end.map(plus_window).transpose()?;
    let mut chains = build_chains(events, now, horizon.as_ref())?;
    chains.retain(|c| !c.absorbed && in_window(c.issued_at(), start, end));
    Ok(chains)
}

/// `round(n / d, k)` of two counts: the true division correctly rounded,
/// then `round` on that binary value. The counts are `i128`, so a sum or
/// a hundredfold of `u64` counts is exact.
fn ratio(n: i128, d: i128, digits: u32) -> f64 {
    round_digits(int_true_div(n, d), digits)
}

/// `_share` of `i128` counts.
fn share_wide(numerator: i128, denominator: i128) -> Option<f64> {
    (denominator != 0).then(|| ratio(numerator, denominator, 4))
}

/// `_share`: `round(n / d, 4)`, None for `d == 0`.
pub fn share(numerator: u64, denominator: u64) -> Option<f64> {
    share_wide(i128::from(numerator), i128::from(denominator))
}

/// The magnitude digits of canonical decimal text, least significant
/// first, and its sign.
fn digits_of(text: &str) -> (bool, Vec<u8>) {
    let (neg, mag) = text.strip_prefix('-').map_or((false, text), |m| (true, m));
    (neg, mag.bytes().rev().map(|b| b - b'0').collect())
}

/// `a + b` of two digit strings (least significant first).
fn add_mag(a: &[u8], b: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(a.len().max(b.len()) + 1);
    let mut carry = 0;
    for i in 0..a.len().max(b.len()) {
        let s = a.get(i).copied().unwrap_or(0) + b.get(i).copied().unwrap_or(0) + carry;
        out.push(s % 10);
        carry = s / 10;
    }
    if carry > 0 {
        out.push(carry);
    }
    out
}

/// `a - b` of two digit strings with `a >= b`.
fn sub_mag(a: &[u8], b: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(a.len());
    let mut borrow = 0;
    for (i, &d) in a.iter().enumerate() {
        let sub = b.get(i).copied().unwrap_or(0) + borrow;
        if d >= sub {
            out.push(d - sub);
            borrow = 0;
        } else {
            out.push(d + 10 - sub);
            borrow = 1;
        }
    }
    while out.len() > 1 && out.last() == Some(&0) {
        out.pop();
    }
    out
}

/// `(a + b) / 2` of two `int` texts, exactly, as decimal text: `k` or
/// `k.5`, with no sign on a zero.
fn half_sum(a: &str, b: &str) -> String {
    let ((an, am), (bn, bm)) = (digits_of(a), digits_of(b));
    let (neg, mag) = if an == bn {
        (an, add_mag(&am, &bm))
    } else {
        let mag_cmp = am
            .len()
            .cmp(&bm.len())
            .then_with(|| am.iter().rev().cmp(bm.iter().rev()));
        if mag_cmp.is_ge() {
            (an, sub_mag(&am, &bm))
        } else {
            (bn, sub_mag(&bm, &am))
        }
    };
    let mut out = String::with_capacity(mag.len() + 3);
    let mut rem = 0;
    for &d in mag.iter().rev() {
        let cur = rem * 10 + d;
        let q = cur / 2;
        rem = cur % 2;
        if q != 0 || !out.is_empty() {
            out.push(char::from(b'0' + q));
        }
    }
    if out.is_empty() {
        out.push('0');
    }
    if rem == 1 {
        out.push_str(".5");
    }
    if neg && out != "0" {
        out.insert(0, '-');
    }
    out
}

/// Whether `text` is an `int`'s canonical decimal text: `-` on a nonzero
/// value only, ASCII digits, no leading zero.
fn is_canonical(text: &str) -> bool {
    let (neg, mag) = text.strip_prefix('-').map_or((false, text), |m| (true, m));
    !mag.is_empty()
        && mag.bytes().all(|b| b.is_ascii_digit())
        && if mag == "0" {
            !neg
        } else {
            !mag.starts_with('0')
        }
}

/// One of the median's texts as canonical decimal text: a canonical one
/// as it is, at any length; any other read as `int()` reads a `str`
/// (surrounding whitespace, a sign, Unicode decimal digits, single
/// underscores between digits, at most 4300 digits). Panics on a text
/// `int()` refuses, in debug and release alike: no `int` has it.
fn int_text(text: &str) -> Cow<'_, str> {
    if is_canonical(text) {
        return Cow::Borrowed(text);
    }
    match int_from_str(&cps(text)) {
        Ok(PyValue::Int(ref canonical)) => Cow::Owned(canonical.clone()),
        _ => panic!("median: {text:?} is not the text of an int"),
    }
}

/// Decimal text read as the nearest float; OverflowError when that is
/// infinite.
fn decimal_to_float(text: &str) -> Answer<f64> {
    match text.parse::<f64>() {
        Ok(x) if x.is_finite() => Ok(x),
        _ => Err(CountersError::OverflowError),
    }
}

/// `_median`: `float(statistics.median(values))` of `int` texts, None
/// for none. The middle one of an odd count is `float(int)`; an even
/// count takes `(a + b) / 2`, Python's int true division, correctly
/// rounded. Both raise OverflowError past the float range. Each text is
/// read as `int_text` reads it, so `"+1"`, `" 2 "` and `"1_000"` are the
/// ints 1, 2 and 1000; a text no `int` has (`""`, `"-"`, `"1e5"`)
/// panics, in debug and release alike.
pub fn median(values: &[String]) -> Answer<Option<f64>> {
    if values.is_empty() {
        return Ok(None);
    }
    let mut sorted: Vec<Cow<'_, str>> = values.iter().map(|v| int_text(v)).collect();
    sorted.sort_by(|a, b| cmp_int_text(a, b));
    let n = sorted.len();
    let text = if n % 2 == 1 {
        sorted[n / 2].to_string()
    } else {
        half_sum(&sorted[n / 2 - 1], &sorted[n / 2])
    };
    decimal_to_float(&text).map(Some)
}

/// `_isoweek1monday`: the ordinal of the Monday of the year's week 1.
fn iso_week1_monday(year: i64) -> i64 {
    let first = ymd_to_ord(year, 1, 1);
    let weekday = (first + 6) % 7;
    first - weekday + if weekday > 3 { 7 } else { 0 }
}

/// `date.isocalendar()`'s year and week, as CPython computes them, of a
/// date `date` holds: years 1 to 9999, months 1 to 12, days 1 to the
/// month's last. None for any other, where `date(year, month, day)`
/// raises ValueError.
pub fn iso_year_week(year: i64, month: u32, day: u32) -> Option<(i64, i64)> {
    if !(1..=9999).contains(&year)
        || !(1..=12).contains(&month)
        || !(1..=days_in_month(year, month)).contains(&day)
    {
        return None;
    }
    let today = ymd_to_ord(year, month, day);
    let mut year = year;
    let mut week = (today - iso_week1_monday(year)).div_euclid(7);
    if week < 0 {
        year -= 1;
        week = (today - iso_week1_monday(year)).div_euclid(7);
    } else if week >= 52 && today >= iso_week1_monday(year + 1) {
        year += 1;
        week = 0;
    }
    Some((year, week + 1))
}

/// `_iso_week`: the UTC date's ISO year and week as `{year}-W{week:02d}`;
/// OverflowError when the UTC date falls outside years 1 to 9999.
pub fn iso_week(ts: &DateTime) -> Answer<String> {
    let utc = match ts.assume_utc().to_utc() {
        Some(Ok(utc)) => utc,
        _ => return Err(CountersError::OverflowError),
    };
    let (year, week) = iso_year_week(i64::from(utc.year), utc.month, utc.day)
        .ok_or(CountersError::OverflowError)?;
    Ok(format!("{year}-W{week:02}"))
}

/// `n += k` of a count held as `u64`: panics past `u64::MAX` in debug
/// and release alike, where the reference's `int` keeps counting.
fn bump(count: &mut u64, k: u64) {
    *count = count
        .checked_add(k)
        .expect("a count past u64::MAX, which build_chains never reaches");
}

/// A `dict[str, int]` of counts in insertion order. Each count is a
/// `u64` where the reference's is an unbounded `int`: `Tally::add` adds
/// one per chain, so only a count set by hand reaches the bound, and `add`
/// past `u64::MAX` panics in debug and release alike.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Counts {
    pub items: Vec<(PyStr, u64)>,
    at: HashMap<PyStr, usize>,
}

impl Counts {
    /// `dict.fromkeys(keys, 0)`.
    pub fn from_keys(keys: &[&str]) -> Counts {
        let mut out = Counts::default();
        for key in keys {
            out.add(&cps(key), 0);
        }
        out
    }

    /// `d[key] = d.get(key, 0) + n`: a new key goes last.
    pub fn add(&mut self, key: &PyStr, n: u64) {
        match self.at.get(key) {
            Some(&i) => bump(&mut self.items[i].1, n),
            None => {
                self.at.insert(key.clone(), self.items.len());
                self.items.push((key.clone(), n));
            }
        }
    }

    /// `d.get(key, 0)`.
    pub fn get(&self, key: &str) -> u64 {
        self.at.get(&cps(key)).map_or(0, |&i| self.items[i].1)
    }
}

/// `_Tally`: the counts one bucket keeps. `response_chars` holds `int`
/// texts (review_counters-2 fills it from ints), read as `median` reads
/// them.
///
/// The counts are `u64` where the reference's are unbounded `int`s.
/// `build_chains` and `add` never come near the bound (it takes more than
/// 1.8e19 chains); only the public fields set by hand do. For any field
/// values, `refused`, the shares, `filings_per_100` and `to_dict` are
/// exact as the reference's, their sums and hundredfolds made in `i128`;
/// `add` taking a count past `u64::MAX` panics, in debug and release
/// alike.
#[derive(Clone, Debug, PartialEq)]
pub struct Tally {
    pub issued: u64,
    pub answered: u64,
    pub unanswered: u64,
    pub pending: u64,
    pub closed_by_call: u64,
    pub pre_answered: u64,
    pub pre_answered_zero: u64,
    pub actions: Counts,
    pub links: u64,
    pub update_suggested: u64,
    pub filings: Counts,
    pub committed_calls: u64,
    pub refused_calls: u64,
    pub unanswered_calls: u64,
    pub response_chars: Vec<String>,
}

impl Default for Tally {
    fn default() -> Tally {
        let mut actions = vec![ACTION_COMMIT];
        actions.extend(REFUSAL_ACTIONS);
        Tally {
            issued: 0,
            answered: 0,
            unanswered: 0,
            pending: 0,
            closed_by_call: 0,
            pre_answered: 0,
            pre_answered_zero: 0,
            actions: Counts::from_keys(&actions),
            links: 0,
            update_suggested: 0,
            filings: Counts::from_keys(&DETECTORS),
            committed_calls: 0,
            refused_calls: 0,
            unanswered_calls: 0,
            response_chars: Vec::new(),
        }
    }
}

fn int(n: u64) -> PyValue {
    PyValue::int(i128::from(n))
}

fn opt_float(x: Option<f64>) -> PyValue {
    x.map_or(PyValue::None, PyValue::Float)
}

fn counts_dict(counts: &Counts) -> PyValue {
    PyValue::Dict(
        counts
            .items
            .iter()
            .map(|(k, n)| (PyValue::Str(k.clone()), int(*n)))
            .collect(),
    )
}

impl Tally {
    /// `add`. Panics on an answered chain without a decision, where the
    /// reference's assert fails; `build_chains` makes none.
    pub fn add(&mut self, chain: &ReviewChain) {
        bump(&mut self.issued, 1);
        if chain.pre_answered() {
            bump(&mut self.pre_answered, 1);
        }
        match chain.state {
            ChainState::Pending => {
                bump(&mut self.pending, 1);
                return;
            }
            ChainState::Unanswered => {
                bump(&mut self.unanswered, 1);
                bump(&mut self.unanswered_calls, chain.calls());
                return;
            }
            ChainState::Answered => {}
        }
        bump(&mut self.answered, 1);
        if chain.closed {
            bump(&mut self.closed_by_call, 1);
        }
        let decided = chain
            .decided
            .as_ref()
            .expect("an answered chain has its decision");
        if chain.pre_answered() && decided.zero_attestation() {
            bump(&mut self.pre_answered_zero, 1);
        }
        self.actions.add(&decided.action, 1);
        if decided.committed() {
            bump(&mut self.committed_calls, chain.calls());
            bump(&mut self.links, decided.links.len() as u64);
            bump(
                &mut self.update_suggested,
                decided.update_suggested.len() as u64,
            );
            for (_, detector) in &decided.filings {
                self.filings.add(detector, 1);
            }
        } else {
            bump(&mut self.refused_calls, chain.calls());
        }
    }

    pub fn committed(&self) -> u64 {
        self.actions.get(ACTION_COMMIT)
    }

    /// The `REFUSAL_ACTIONS` only: `refused_standing` is not one. A sum
    /// of `u64` counts, so `u128`.
    pub fn refused(&self) -> u128 {
        REFUSAL_ACTIONS
            .iter()
            .map(|a| u128::from(self.actions.get(a)))
            .sum()
    }

    /// `filings_per_100`: per detector in `filings`' order, then `total`
    /// (a detector named `total` keeps its place and takes the total).
    pub fn filings_per_100(&self) -> PyValue {
        let committed = i128::from(self.committed());
        let rate = |n: i128| (committed != 0).then(|| ratio(100 * n, committed, 2));
        let mut out: Vec<(PyValue, PyValue)> = self
            .filings
            .items
            .iter()
            .map(|(detector, n)| {
                let rate = rate(i128::from(*n));
                (PyValue::Str(detector.clone()), opt_float(rate))
            })
            .collect();
        let sum = self.filings.items.iter().map(|(_, n)| i128::from(*n)).sum();
        let total = opt_float(rate(sum));
        match self.filings.at.get(&cps("total")) {
            Some(&i) => out[i].1 = total,
            None => out.push((PyValue::str("total"), total)),
        }
        PyValue::Dict(out)
    }

    /// `to_dict`, in the reference's key order; OverflowError from the
    /// median.
    pub fn to_dict(&self) -> Answer<PyValue> {
        let judged = i128::from(self.answered) + i128::from(self.unanswered);
        let committed = self.committed();
        let refused = i128::try_from(self.refused()).expect("three u64 counts fit");
        let fields: Vec<(&str, PyValue)> = vec![
            ("issued", int(self.issued)),
            ("answered", int(self.answered)),
            ("unanswered", int(self.unanswered)),
            ("pending", int(self.pending)),
            ("closed_by_call", int(self.closed_by_call)),
            (
                "unanswered_share",
                opt_float(share_wide(i128::from(self.unanswered), judged)),
            ),
            ("pre_answered", int(self.pre_answered)),
            (
                "pre_answered_share",
                opt_float(share(self.pre_answered, self.issued)),
            ),
            ("pre_answered_zero", int(self.pre_answered_zero)),
            ("committed", int(committed)),
            ("refused", PyValue::int(refused)),
            ("actions", counts_dict(&self.actions)),
            ("links", int(self.links)),
            ("update_suggested", int(self.update_suggested)),
            ("filings", counts_dict(&self.filings)),
            ("filings_per_100_committed", self.filings_per_100()),
            (
                "calls_per_committed_write",
                opt_float(
                    (committed != 0)
                        .then(|| ratio(i128::from(self.committed_calls), i128::from(committed), 3)),
                ),
            ),
            (
                "response_chars_p50",
                opt_float(median(&self.response_chars)?),
            ),
            ("response_chars_n", int(self.response_chars.len() as u64)),
        ];
        Ok(PyValue::Dict(
            fields
                .into_iter()
                .map(|(k, v)| (PyValue::str(k), v))
                .collect(),
        ))
    }
}
