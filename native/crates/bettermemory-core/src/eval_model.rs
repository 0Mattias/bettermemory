//! The eval family's model (`eval_model.py`), unit eval_model-1: the
//! defaults, `RateCI` with the Wilson interval, the two row types
//! `ColdEndorsementMemoriesRow` and `SilentMissCandidate`, the `--since`
//! and `--until` parsing (`parse_since`, `parse_timestamp_utc`,
//! `parse_window_bound`, `resolve_window`), `EARLIER_RULES`,
//! `_show_in_window`, and the silent-miss readers of one event
//! (`_canonical_top_hit_id`, `_silent_miss_from_event`).
//!
//! Counts are `i64` (every caller counts events). A span is carried as
//! `i128` microseconds, where the reference holds a `timedelta`: the
//! longest span `parse_since` returns is 999,999,999 days and 86,399
//! seconds, past `i64` microseconds. The clock is an argument: `now` is
//! the caller's `datetime.now(timezone.utc)`. An event is a
//! `dict[str, Any]`, read as a [`PyValue`]: a value that is not a dict
//! has no `.get`, and the reference raises AttributeError. `to_dict`
//! answers a [`PyValue::Dict`] in the reference's key order.

use std::sync::OnceLock;

use crate::datetime::{parse_event_ts, DateTime};
use crate::events::truthy;
use crate::health_model::{dict, explicit_ratio, int, is_text, opt_float, text};
use crate::pyre::regex::Regex;
use crate::pyvalue::{int_from_str, repr_str, type_name, PyStr, PyValue};
use crate::search::num::{int_true_div, py_max};
use crate::unicode::{self, cps, py_strip};

/// `DEFAULT_SINCE_SPEC`.
pub const DEFAULT_SINCE_SPEC: &str = "30d";
/// `DEFAULT_ENDORSEMENT_MIN_RETRIEVALS`.
pub const DEFAULT_ENDORSEMENT_MIN_RETRIEVALS: i64 = 30;
/// `DEFAULT_SILENT_MISS_LIMIT`.
pub const DEFAULT_SILENT_MISS_LIMIT: i64 = 20;
/// `_WILSON_Z`: the Wilson score z for a 95% interval.
pub const WILSON_Z: f64 = 1.96;
/// `_SINCE_PATTERN`'s text, compiled with no flags on the engine.
pub const SINCE_PATTERN: &str = r"^(?P<value>\d+)(?P<unit>[smhd])$";
/// `_SINCE_UNIT_TO_SECONDS`.
pub const SINCE_UNIT_TO_SECONDS: [(char, i64); 4] =
    [('s', 1), ('m', 60), ('h', 3600), ('d', 86400)];
/// `EARLIER_RULES`: the group of every show whose rule is not the current one.
pub const EARLIER_RULES: &str = "earlier";
/// `_EARLIEST_START` (0001-01-02T00:00:00+00:00) as microseconds since
/// 0001-01-01T00:00 UTC.
pub const EARLIEST_START_US: i128 = 86_400_000_000;
/// `int()`'s limit on the digits of a decimal string
/// (`sys.int_info.default_max_str_digits`).
const INT_MAX_STR_DIGITS: usize = 4300;
/// The first span in seconds `timedelta` cannot hold: 10**9 days.
const SPAN_LIMIT_SECONDS: u128 = 86_400_000_000_000;

const COMPARE: &str = "can't compare offset-naive and offset-aware datetimes";
const SUBTRACT: &str = "can't subtract offset-naive and offset-aware datetimes";

/// `_EARLIEST_START`.
pub fn earliest_start() -> DateTime {
    DateTime::new(1, 1, 2, 0, 0, 0, 0, Some(0)).expect("a datetime")
}

/// The exception a function of this module raises.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EvalModelError {
    /// ValueError with its text.
    ValueError(PyStr),
    /// TypeError with its text (a naive datetime met an aware one).
    TypeError(&'static str),
    /// AttributeError: `.get` of a value of this type, not a dict.
    AttributeError(&'static str),
    /// OverflowError: `now - span` left the calendar.
    OverflowError,
}

impl EvalModelError {
    /// The exception's class name.
    pub fn name(&self) -> &'static str {
        match self {
            EvalModelError::ValueError(_) => "ValueError",
            EvalModelError::TypeError(_) => "TypeError",
            EvalModelError::AttributeError(_) => "AttributeError",
            EvalModelError::OverflowError => "OverflowError",
        }
    }

    /// `str(exc)`.
    pub fn message(&self) -> PyStr {
        match self {
            EvalModelError::ValueError(text) => text.clone(),
            EvalModelError::TypeError(text) => cps(text),
            EvalModelError::AttributeError(kind) => {
                cps(&format!("'{kind}' object has no attribute 'get'"))
            }
            EvalModelError::OverflowError => cps("date value out of range"),
        }
    }
}

type Answer<T> = Result<T, EvalModelError>;

/// `_wilson_interval(k, n, z)`: `(0.0, 1.0)` for `n <= 0`; else `k`
/// clamped to `[0, n]`, `p = k / n` as Python divides two ints (correctly
/// rounded), every other step in the reference's order of operations with
/// `n` read as a float, and the bounds through Python's `max(0.0, x)` and
/// `min(1.0, x)`; `f64::min` is the latter on every `x` (1.0 for a NaN).
pub fn wilson_interval(k: i64, n: i64, z: f64) -> (f64, f64) {
    if n <= 0 {
        return (0.0, 1.0);
    }
    let k = if k > n { n } else { k.max(0) };
    let p = int_true_div(i128::from(k), i128::from(n));
    let nf = n as f64;
    let denom = 1.0 + z * z / nf;
    let center = (p + z * z / (2.0 * nf)) / denom;
    let margin = z * (p * (1.0 - p) / nf + z * z / (4.0 * nf * nf)).sqrt() / denom;
    (py_max(0.0, center - margin), (center + margin).min(1.0))
}

/// `RateCI`: a rate with its counts and 95% Wilson interval.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RateCI {
    pub numerator: i64,
    pub denominator: i64,
    pub rate: Option<f64>,
    pub lower: Option<f64>,
    pub upper: Option<f64>,
    pub torn_read: bool,
}

impl RateCI {
    /// `RateCI.from_counts`: no rate for a denominator `<= 0`; a numerator
    /// past the denominator is a torn read with the rate clamped to 1.0;
    /// else the rate is `numerator / denominator` (int division).
    pub fn from_counts(numerator: i64, denominator: i64) -> RateCI {
        if denominator <= 0 {
            return RateCI {
                numerator,
                denominator,
                rate: None,
                lower: None,
                upper: None,
                torn_read: false,
            };
        }
        let torn_read = numerator > denominator;
        let rate = if torn_read {
            1.0
        } else {
            int_true_div(i128::from(numerator), i128::from(denominator))
        };
        let (lo, hi) = wilson_interval(numerator, denominator, WILSON_Z);
        RateCI {
            numerator,
            denominator,
            rate: Some(rate),
            lower: Some(lo),
            upper: Some(hi),
            torn_read,
        }
    }

    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("numerator", int(self.numerator)),
            ("denominator", int(self.denominator)),
            ("rate", opt_float(self.rate)),
            ("ci95_lower", opt_float(self.lower)),
            ("ci95_upper", opt_float(self.upper)),
            ("torn_read", PyValue::Bool(self.torn_read)),
        ])
    }
}

/// `ColdEndorsementMemoriesRow`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ColdEndorsementMemoriesRow {
    pub id: PyStr,
    pub scopes: Vec<PyStr>,
    pub summary: PyStr,
    pub retrieval_count: i64,
    pub auto_applied_count: i64,
    pub explicit_applied_count: i64,
}

impl ColdEndorsementMemoriesRow {
    /// The property `endorsement_ratio`:
    /// `explicit_ratio(auto + explicit, explicit)`. The sum is an
    /// unbounded `int` in the reference; past `i64` it is divided here by
    /// the same rule `explicit_ratio` follows.
    pub fn endorsement_ratio(&self) -> Option<f64> {
        let applied = i128::from(self.auto_applied_count) + i128::from(self.explicit_applied_count);
        match i64::try_from(applied) {
            Ok(applied) => explicit_ratio(applied, self.explicit_applied_count),
            Err(_) if applied > 0 => Some(int_true_div(
                i128::from(self.explicit_applied_count),
                applied,
            )),
            Err(_) => None,
        }
    }

    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("id", text(&self.id)),
            (
                "scopes",
                PyValue::List(self.scopes.iter().map(|s| text(s)).collect()),
            ),
            ("summary", text(&self.summary)),
            ("retrieval_count", int(self.retrieval_count)),
            ("auto_applied_count", int(self.auto_applied_count)),
            ("explicit_applied_count", int(self.explicit_applied_count)),
            ("endorsement_ratio", opt_float(self.endorsement_ratio())),
        ])
    }
}

/// `SilentMissCandidate`: one `search_miss` event as the renderer reads
/// it. `recent_retrieval_count` is the event's `int` as its decimal text
/// (`PyValue::Int`'s): the reference keeps any `int`, past `i64` too.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SilentMissCandidate {
    pub ts: PyStr,
    pub session_id: PyStr,
    pub top_missed_id: Option<PyStr>,
    pub top_missed_relevance: Option<PyStr>,
    pub threshold_rule: Option<PyStr>,
    pub recent_retrieval_count: String,
}

impl SilentMissCandidate {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("ts", text(&self.ts)),
            ("session_id", text(&self.session_id)),
            ("top_missed_id", opt_text(self.top_missed_id.as_deref())),
            (
                "top_missed_relevance",
                opt_text(self.top_missed_relevance.as_deref()),
            ),
            ("threshold_rule", opt_text(self.threshold_rule.as_deref())),
            (
                "recent_retrieval_count",
                PyValue::Int(self.recent_retrieval_count.clone()),
            ),
        ])
    }
}

/// A bound `parse_window_bound` returns: a span read back from now (a
/// `timedelta`, here microseconds) or an absolute aware UTC datetime.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Bound {
    Span(i128),
    At(DateTime),
}

fn since_regex() -> &'static Regex {
    static SINCE: OnceLock<Regex> = OnceLock::new();
    SINCE
        .get_or_init(|| Regex::new_str(SINCE_PATTERN, 0).expect("the engine builds _SINCE_PATTERN"))
}

/// `_SINCE_PATTERN.fullmatch(text)`: the spans of `value` and `unit`.
fn since_match(text: &[u32]) -> Option<((usize, usize), (usize, usize))> {
    // No budget is given, so the engine's one failure is memory, which
    // the plain call ends as an allocation failure ends.
    let m = since_regex()
        .fullmatch(text, 0, isize::MAX)
        .ok()
        .flatten()?;
    let span = |g| m.group(g).map(|(a, b)| (a as usize, b as usize));
    Some((span(1)?, span(2)?))
}

/// A ValueError whose text is the pieces joined.
fn value_error(pieces: &[&[u32]]) -> EvalModelError {
    EvalModelError::ValueError(pieces.concat())
}

/// `parse_since(spec)`: None for None, `""` or `"all"`; else the span in
/// microseconds of a `\d+[smhd]` token, Unicode digits read as `int()`
/// reads them. ValueError for any other text, for zero, past `int()`'s
/// limit of 4,300 digits (CPython's own text) and past what `timedelta`
/// holds.
pub fn parse_since(spec: Option<&[u32]>) -> Answer<Option<i128>> {
    let Some(spec) = spec else {
        return Ok(None);
    };
    if spec.is_empty() || is_text(spec, "all") {
        return Ok(None);
    }
    let stripped = py_strip(spec);
    let Some(((vs, ve), (us, _))) = since_match(stripped) else {
        return Err(value_error(&[
            &cps("--since: expected 'Ns'/'Nm'/'Nh'/'Nd' or 'all', got "),
            &repr_str(spec),
            &cps(". Examples: '30d', '12h', '90m', 'all'."),
        ]));
    };
    let digits = &stripped[vs..ve];
    if digits.len() > INT_MAX_STR_DIGITS {
        return Err(EvalModelError::ValueError(cps(&format!(
            "Exceeds the limit ({INT_MAX_STR_DIGITS} digits) for integer string conversion: \
value has {} digits; use sys.set_int_max_str_digits() to increase the limit",
            digits.len()
        ))));
    }
    let parsed = int_from_str(digits);
    let Ok(PyValue::Int(value)) = &parsed else {
        unreachable!("\\d+ is an int's text");
    };
    if value == "0" {
        return Err(value_error(&[&cps(
            "--since: value must be positive, got 0",
        )]));
    }
    let unit = SINCE_UNIT_TO_SECONDS
        .iter()
        .find(|(c, _)| u32::from(*c) == stripped[us])
        .map(|&(_, s)| s as u128)
        .expect("a unit of the pattern");
    let seconds = if value.len() > 20 {
        None
    } else {
        value.parse::<u128>().ok().map(|v| v * unit)
    };
    match seconds {
        Some(s) if s < SPAN_LIMIT_SECONDS => Ok(Some(s as i128 * 1_000_000)),
        _ => Err(value_error(&[
            &cps("--since: value out of range: "),
            &repr_str(spec),
            &cps(". Pick a smaller window (or 'all' for no time filter)."),
        ])),
    }
}

fn expected_timestamp(flag: &str, spec: &[u32]) -> EvalModelError {
    value_error(&[
        &cps(flag),
        &cps(
            ": expected 'Ns'/'Nm'/'Nh'/'Nd', 'all', or an ISO-8601 UTC timestamp such as \
2026-10-01T00:00:00Z, got ",
        ),
        &repr_str(spec),
        &cps("."),
    ])
}

/// `parse_timestamp_utc(spec, flag=flag)`: an ISO-8601 timestamp with an
/// explicit offset (a final `Z` or `z` read as `+00:00`) as an aware UTC
/// datetime; ValueError for text `fromisoformat` refuses, for a naive
/// timestamp and for one outside the calendar once read in UTC.
pub fn parse_timestamp_utc(spec: &[u32], flag: &str) -> Answer<DateTime> {
    let stripped = py_strip(spec);
    let mut raw = stripped.to_vec();
    if matches!(raw.last(), Some(&c) if c == u32::from(b'Z') || c == u32::from(b'z')) {
        raw.pop();
        raw.extend(cps("+00:00"));
    }
    let parsed = DateTime::from_isoformat(&raw).map_err(|_| expected_timestamp(flag, spec))?;
    match parsed.to_utc() {
        Some(Ok(utc)) => Ok(utc),
        Some(Err(_)) => Err(value_error(&[
            &cps(flag),
            &cps(": "),
            &repr_str(stripped),
            &cps(" lies outside the calendar once read in UTC; pick a time inside it."),
        ])),
        None => Err(value_error(&[
            &cps(flag),
            &cps(": "),
            &repr_str(spec),
            &cps(" has no UTC offset; add 'Z' (for example "),
            stripped,
            &cps("Z)."),
        ])),
    }
}

/// `parse_window_bound(spec, flag=flag, now=now)`: None for None or a
/// stripped `""` or `"all"`; a `\d+[smhd]` span (refused when it reaches
/// back past `_EARLIEST_START` from `now`); else, when the raw text holds
/// a `str.isdigit` character and a `-`, an absolute timestamp, refused
/// under `--since` when later than `now`. A naive `now` raises TypeError
/// where the reference subtracts or compares it.
pub fn parse_window_bound(
    spec: Option<&[u32]>,
    flag: &str,
    now: &DateTime,
) -> Answer<Option<Bound>> {
    let Some(spec) = spec else {
        return Ok(None);
    };
    let stripped = py_strip(spec);
    if stripped.is_empty() || is_text(stripped, "all") {
        return Ok(None);
    }
    if since_match(stripped).is_some() {
        let span = parse_since(Some(stripped))?.expect("a span for a token");
        let current = now
            .instant_us()
            .ok_or(EvalModelError::TypeError(SUBTRACT))?;
        if span > current - EARLIEST_START_US {
            return Err(value_error(&[
                &cps(flag),
                &cps(": "),
                &repr_str(stripped),
                &cps(" reaches back past the year 1. Pick a smaller window (or 'all' for no time filter)."),
            ]));
        }
        return Ok(Some(Bound::Span(span)));
    }
    if !spec.iter().any(|&c| unicode::is_digit(c)) || !spec.contains(&u32::from(b'-')) {
        return Err(expected_timestamp(flag, spec));
    }
    let bound = parse_timestamp_utc(spec, flag)?;
    if flag == "--since" {
        let current = now.instant_us().ok_or(EvalModelError::TypeError(COMPARE))?;
        if bound.instant_us().expect("aware") > current {
            return Err(value_error(&[
                &cps(flag),
                &cps(": "),
                &repr_str(stripped),
                &cps(" is later than now, so the window would hold nothing yet. Pick a past time, a span such as '30d', or 'all'."),
            ]));
        }
    }
    Ok(Some(Bound::At(bound)))
}

/// `resolve_window(since, until, now=now)`: the absolute `[start, end)`;
/// a span is `now` minus it (OverflowError out of the calendar).
pub fn resolve_window(
    since: Option<&Bound>,
    until: Option<&Bound>,
    now: &DateTime,
) -> Answer<(Option<DateTime>, Option<DateTime>)> {
    let resolve = |b: Option<&Bound>| -> Answer<Option<DateTime>> {
        match b {
            None => Ok(None),
            Some(Bound::At(at)) => Ok(Some(*at)),
            // A span past `i64` microseconds (about 292,000 years) leaves
            // the calendar from any `now`; it is refused before the
            // subtraction, which would overflow `i128` at its ends.
            Some(Bound::Span(us)) => i64::try_from(*us)
                .ok()
                .and_then(|us| now.minus_micros(i128::from(us)).ok())
                .map(Some)
                .ok_or(EvalModelError::OverflowError),
        }
    };
    let start = resolve(since)?;
    let end = resolve(until)?;
    Ok((start, end))
}

/// The dict itself; AttributeError for `.get` of anything else. Its
/// `.get(key)` is [`PyValue::get`]: the last value of a `str` key, None
/// (here `Option::None`) when missing.
fn dict_of(ev: &PyValue) -> Answer<&PyValue> {
    match ev {
        PyValue::Dict(_) => Ok(ev),
        other => Err(EvalModelError::AttributeError(type_name(other))),
    }
}

/// `stamp >= bound` (`ge`) or `stamp < bound` of an aware stamp, as
/// instants; TypeError against a naive bound.
fn at_or_after(stamp: i128, bound: &DateTime, ge: bool) -> Answer<bool> {
    let b = bound
        .instant_us()
        .ok_or(EvalModelError::TypeError(COMPARE))?;
    Ok(if ge { stamp >= b } else { stamp < b })
}

/// `_show_in_window(show, start, end)`: whether the row's `ts` falls in
/// `[start, end)`; a row without a readable stamp only in an unbounded
/// window.
pub fn show_in_window(
    show: &PyValue,
    start: Option<&DateTime>,
    end: Option<&DateTime>,
) -> Answer<bool> {
    let stamp = match dict_of(show)?.get("ts") {
        Some(PyValue::Str(s)) => parse_event_ts(s),
        _ => None,
    };
    let Some(stamp) = stamp else {
        return Ok(start.is_none() && end.is_none());
    };
    let stamp = stamp.instant_us().expect("parse_event_ts answers aware");
    if let Some(start) = start {
        if !at_or_after(stamp, start, true)? {
            return Ok(false);
        }
    }
    match end {
        None => Ok(true),
        Some(end) => at_or_after(stamp, end, false),
    }
}

/// The first entry of `top_hits` when it is a non-empty list.
fn first_hit(top_hits: Option<&PyValue>) -> Option<&PyValue> {
    match top_hits {
        Some(PyValue::List(v)) => v.first(),
        _ => None,
    }
}

/// A `str` value's text; None for a missing key or any other value.
fn str_of(v: Option<&PyValue>) -> Option<PyStr> {
    match v {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

/// `_canonical_top_hit_id(ev)`: the `id` of the first canonical
/// `top_hits` entry when it is a `str`; no legacy fallback.
pub fn canonical_top_hit_id(ev: &PyValue) -> Answer<Option<PyStr>> {
    Ok(match first_hit(dict_of(ev)?.get("top_hits")) {
        Some(first @ PyValue::Dict(_)) => str_of(first.get("id")),
        _ => None,
    })
}

/// `_silent_miss_from_event(ev)`: the renderer's fields of a
/// `search_miss` event, None when `ts` or the session is not a `str`.
/// The session is `ev.get("session_id") or ev.get("session")` (Python
/// truthiness); the legacy `top_hit_ids` is read when `top_hits` is not
/// a list or is an empty one; `recent_retrieval_count` is an `int` that
/// is not a `bool`, else 0.
pub fn silent_miss_from_event(ev: &PyValue) -> Answer<Option<SilentMissCandidate>> {
    let ev = dict_of(ev)?;
    let first = ev.get("session_id");
    let session = if first.is_some_and(truthy) {
        first
    } else {
        ev.get("session")
    };
    let (Some(PyValue::Str(ts)), Some(PyValue::Str(session_id))) = (ev.get("ts"), session) else {
        return Ok(None);
    };
    let mut top_id = None;
    let mut top_relevance = None;
    match first_hit(ev.get("top_hits")) {
        Some(first @ PyValue::Dict(_)) => {
            top_id = str_of(first.get("id"));
            top_relevance = str_of(first.get("relevance"));
        }
        Some(_) => {}
        None => {
            if let Some(first_id) = first_hit(ev.get("top_hit_ids")) {
                top_id = str_of(Some(first_id));
            }
        }
    }
    let recent = match ev.get("recent_retrieval_count") {
        Some(PyValue::Int(t)) => t.clone(),
        _ => "0".to_string(),
    };
    Ok(Some(SilentMissCandidate {
        ts: ts.clone(),
        session_id: session_id.clone(),
        top_missed_id: top_id,
        top_missed_relevance: top_relevance,
        threshold_rule: str_of(ev.get("threshold_rule")),
        recent_retrieval_count: recent,
    }))
}

fn opt_text(s: Option<&[u32]>) -> PyValue {
    s.map_or(PyValue::None, text)
}
