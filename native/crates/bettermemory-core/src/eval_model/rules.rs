//! The eval family's model, unit eval_model-2: the threshold-rule
//! registry (`ThresholdRule`, the rules v1 to v4, `THRESHOLD_RULES`) and
//! `ThresholdSweepRow`, the widening rules (w1, w2, `WIDENING_RULES`), the
//! widening lanes' shared walk (`_ReplayableAudits`,
//! `_collect_replayable_audits`), `_probe_query_display`, and the
//! widening detail types (`WideningFlaggedTurn`, `WideningMemoryRollup`,
//! `WideningRuleDetail`).
//!
//! A rule reads `(top_hits, recent_retrieval_count)`: the hits as the
//! event holds them (any values) and the count as an `int`'s canonical
//! decimal text, of any size. A score is compared as Python compares
//! mixed numbers: a `bool` is 0 or 1, an `int` of any size against a
//! float exactly, a NaN false on every comparison.

use std::cmp::Ordering;

use super::{at_or_after, dict_of, opt_text, Answer, EvalModelError};
use crate::datetime::{parse_event_ts, DateTime};
use crate::events::truthy;
use crate::health_model::{dict, int, opt_float, text};
use crate::pyvalue::{cmp_int_float, cmp_int_text, PyStr, PyValue};
use crate::unicode::cps;

/// A rule's check: `check(top_hits, recent_retrieval_count)`.
pub type RuleCheck = fn(&[PyValue], &str) -> Answer<bool>;

/// `ThresholdRule`: a named decision rule for the silent-miss audit.
#[derive(Clone, Copy, Debug)]
pub struct ThresholdRule {
    pub name: &'static str,
    pub description: &'static str,
    pub check: RuleCheck,
}

/// `THRESHOLD_RULES`, in the reference's insertion order.
pub static THRESHOLD_RULES: [ThresholdRule; 4] = [
    ThresholdRule {
        name: "v1_top1_high",
        description: "Current default: top-1 hit relevance == 'high' AND \
no retrieval (search/show/list) in the lookback window.",
        check: rule_v1_top1_high,
    },
    ThresholdRule {
        name: "v2_top1_high_score_50",
        description: "v1 + top-1 score >= 50.0. Filters single-token \
high-relevance hits that score only on coverage.",
        check: rule_v2_top1_high_score_50,
    },
    ThresholdRule {
        name: "v3_top1_high_dominant",
        description: "v1 + top-1 score >= 2x top-2 score. Distinguishes \
one-clear-match from borderline-ranker-noise.",
        check: rule_v3_top1_high_dominant,
    },
    ThresholdRule {
        name: "v4_top1_high_strict_combined",
        description: "Intersection of v2 and v3: must clear both the \
score floor and the dominance test.",
        check: rule_v4_top1_high_strict_combined,
    },
];

/// `WIDENING_RULES`, in the reference's insertion order.
pub static WIDENING_RULES: [ThresholdRule; 2] = [
    ThresholdRule {
        name: "w1_top1_v2_high",
        description: "Top-1 shadow relevance_v2 == 'high' (coverage >= 0.75 \
OR matched_unique >= 4) AND no retrieval in window.",
        check: rule_w1_top1_v2_high,
    },
    ThresholdRule {
        name: "w2_top1_v2_high_from_medium",
        description: "v1 high, OR top-1 promoted medium\u{2192}high by the shadow \
matched-token floor. Excludes w1's low\u{2192}high promotions (measured \
~20% precision on the 2026-07-08 labeling pass vs ~50% for \
medium\u{2192}high).",
        check: rule_w2_top1_v2_high_from_medium,
    },
];

/// `registry[name]` (`dict.get`): the rule of that name, if any.
pub fn rule_named(
    registry: &'static [ThresholdRule],
    name: &str,
) -> Option<&'static ThresholdRule> {
    registry.iter().find(|r| r.name == name)
}

/// `recent_retrieval_count > 0` of an `int`'s canonical decimal text.
fn positive(recent: &str) -> bool {
    !recent.starts_with('-') && recent != "0"
}

/// `top.get(key) == value`: a `str` of exactly those code points.
fn is_str(v: Option<&PyValue>, value: &str) -> bool {
    matches!(v, Some(PyValue::Str(s)) if *s == cps(value))
}

/// A value `isinstance(_, (int, float))` takes: a `bool` is 0 or 1.
#[derive(Clone, Debug)]
enum Num {
    Int(String),
    Float(f64),
}

fn num(v: Option<&PyValue>) -> Option<Num> {
    match v? {
        PyValue::Bool(b) => Some(Num::Int(if *b { "1" } else { "0" }.into())),
        PyValue::Int(t) => Some(Num::Int(t.clone())),
        PyValue::Float(f) => Some(Num::Float(*f)),
        _ => None,
    }
}

/// `a >= b` as Python compares two numbers: exact across int and float,
/// false against a NaN.
fn ge(a: &Num, b: &Num) -> bool {
    use Ordering::{Equal, Greater, Less};
    match (a, b) {
        (Num::Int(x), Num::Int(y)) => cmp_int_text(x, y) != Less,
        (Num::Float(x), Num::Float(y)) => x >= y,
        (Num::Int(x), Num::Float(y)) => matches!(cmp_int_float(x, *y), Some(Greater | Equal)),
        (Num::Float(x), Num::Int(y)) => matches!(cmp_int_float(y, *x), Some(Less | Equal)),
    }
}

/// `2 * n`: exact doubling of an `int`'s text, float doubling of a float.
fn double(n: &Num) -> Num {
    match n {
        Num::Float(f) => Num::Float(2.0 * f),
        Num::Int(t) => {
            let (sign, digits) = t.strip_prefix('-').map_or(("", t.as_str()), |d| ("-", d));
            let mut out = Vec::with_capacity(digits.len() + 1);
            let mut carry = 0;
            for b in digits.bytes().rev() {
                let v = (b - b'0') * 2 + carry;
                out.push(b'0' + v % 10);
                carry = v / 10;
            }
            if carry > 0 {
                out.push(b'0' + carry);
            }
            out.reverse();
            let body = String::from_utf8(out).expect("ASCII digits");
            Num::Int(if body == "0" {
                body
            } else {
                format!("{sign}{body}")
            })
        }
    }
}

/// `_rule_v1_top1_high`: no recent retrieval, and the first hit's
/// `relevance` is `"high"`; AttributeError when that hit is not a dict.
pub fn rule_v1_top1_high(top_hits: &[PyValue], recent: &str) -> Answer<bool> {
    if positive(recent) {
        return Ok(false);
    }
    let Some(first) = top_hits.first() else {
        return Ok(false);
    };
    Ok(is_str(dict_of(first)?.get("relevance"), "high"))
}

/// `_rule_v2_top1_high_score_50`: v1, and the first hit's score a number
/// at or past 50.0.
pub fn rule_v2_top1_high_score_50(top_hits: &[PyValue], recent: &str) -> Answer<bool> {
    if !rule_v1_top1_high(top_hits, recent)? {
        return Ok(false);
    }
    Ok(num(top_hits[0].get("score")).is_some_and(|s| ge(&s, &Num::Float(50.0))))
}

/// `_rule_v3_top1_high_dominant`: v1, the first hit's score a number, and
/// either one hit, a second score that is not a positive number (`<= 0`;
/// a NaN is not), or a first score at least twice the second.
/// AttributeError when the second hit is read and is not a dict.
pub fn rule_v3_top1_high_dominant(top_hits: &[PyValue], recent: &str) -> Answer<bool> {
    if !rule_v1_top1_high(top_hits, recent)? {
        return Ok(false);
    }
    let Some(top) = num(top_hits[0].get("score")) else {
        return Ok(false);
    };
    if top_hits.len() < 2 {
        return Ok(true);
    }
    let Some(second) = num(dict_of(&top_hits[1])?.get("score")) else {
        return Ok(true);
    };
    if ge(&Num::Int("0".into()), &second) {
        return Ok(true);
    }
    Ok(ge(&top, &double(&second)))
}

/// `_rule_v4_top1_high_strict_combined`: `v2 and v3`, v3 read only when
/// v2 holds.
pub fn rule_v4_top1_high_strict_combined(top_hits: &[PyValue], recent: &str) -> Answer<bool> {
    Ok(rule_v2_top1_high_score_50(top_hits, recent)?
        && rule_v3_top1_high_dominant(top_hits, recent)?)
}

/// `_rule_w1_top1_v2_high`: no recent retrieval, and the first hit's
/// `relevance_v2` is `"high"`; AttributeError when that hit is not a dict.
pub fn rule_w1_top1_v2_high(top_hits: &[PyValue], recent: &str) -> Answer<bool> {
    if positive(recent) {
        return Ok(false);
    }
    let Some(first) = top_hits.first() else {
        return Ok(false);
    };
    Ok(is_str(dict_of(first)?.get("relevance_v2"), "high"))
}

/// `_rule_w2_top1_v2_high_from_medium`: v1, or the first hit promoted
/// from `"medium"` to a `relevance_v2` of `"high"`. v1 is asked first, so
/// a first hit that is not a dict raises there (with no recent retrieval)
/// or answers false (with one) before the reference's own dict guard,
/// which never decides.
pub fn rule_w2_top1_v2_high_from_medium(top_hits: &[PyValue], recent: &str) -> Answer<bool> {
    if rule_v1_top1_high(top_hits, recent)? {
        return Ok(true);
    }
    if positive(recent) {
        return Ok(false);
    }
    let Some(top @ PyValue::Dict(_)) = top_hits.first() else {
        return Ok(false);
    };
    Ok(is_str(top.get("relevance"), "medium") && is_str(top.get("relevance_v2"), "high"))
}

/// `ThresholdSweepRow`.
#[derive(Clone, Debug, PartialEq)]
pub struct ThresholdSweepRow {
    pub rule: PyStr,
    pub description: PyStr,
    pub would_flag: i64,
    pub delta_from_v1: i64,
    pub delta_pct: Option<f64>,
}

impl ThresholdSweepRow {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("rule", text(&self.rule)),
            ("description", text(&self.description)),
            ("would_flag", int(self.would_flag)),
            ("delta_from_v1", int(self.delta_from_v1)),
            ("delta_pct", opt_float(self.delta_pct)),
        ])
    }
}

/// One replayable audit: the event (by its index in the stream and by
/// reference), its `top_hits` list and its `recent_retrieval_count` (an
/// `int` that is not a `bool`, else 0) as canonical decimal text.
#[derive(Clone, Debug, PartialEq)]
pub struct ReplayableRow<'a> {
    pub index: usize,
    pub event: &'a PyValue,
    pub top_hits: &'a [PyValue],
    pub recent: String,
}

/// `_ReplayableAudits`: the walk's counters and rows, in stream order.
#[derive(Clone, Debug, PartialEq)]
pub struct ReplayableAudits<'a> {
    pub total_events_scanned: i64,
    pub events_in_window: i64,
    pub with_features: i64,
    pub without_features: i64,
    pub repeats_skipped: i64,
    pub cutoff_retracted: i64,
    pub rows: Vec<ReplayableRow<'a>>,
}

/// `_parse_ts(value)` as an instant in microseconds: a `str` that
/// `parse_event_ts` reads (always aware), else None.
fn instant(v: Option<&PyValue>) -> Option<i128> {
    match v {
        Some(PyValue::Str(s)) => parse_event_ts(s).and_then(|d| d.instant_us()),
        _ => None,
    }
}

/// `_collect_replayable_audits(events, since=since, now=now)`. `since` is
/// a span in microseconds; `now - since` raises OverflowError outside the
/// calendar before any event is read, and a naive `now` raises TypeError
/// at the first readable stamp compared with it. A value that is not a
/// dict is skipped uncounted. The latest readable `cutoff_ts` of any
/// `silent_miss_cutoff` row, wherever it sits and whatever the window,
/// retracts after the walk every candidate whose stamp is unreadable or
/// strictly earlier.
pub fn collect_replayable_audits<'a>(
    events: &'a [PyValue],
    since: Option<i128>,
    now: &DateTime,
) -> Answer<ReplayableAudits<'a>> {
    let window = since
        .map(|s| {
            i64::try_from(s)
                .ok()
                .and_then(|s| now.minus_micros(i128::from(s)).ok())
                .ok_or(EvalModelError::OverflowError)
        })
        .transpose()?;
    let mut walk = ReplayableAudits {
        total_events_scanned: 0,
        events_in_window: 0,
        with_features: 0,
        without_features: 0,
        repeats_skipped: 0,
        cutoff_retracted: 0,
        rows: Vec::new(),
    };
    let mut latest_cutoff: Option<i128> = None;
    let mut candidates = Vec::new();
    for (index, ev) in events.iter().enumerate() {
        if !matches!(ev, PyValue::Dict(_)) {
            continue;
        }
        walk.total_events_scanned += 1;
        match &window {
            None => walk.events_in_window += 1,
            Some(cut) => {
                if let Some(ts) = instant(ev.get("ts")) {
                    if at_or_after(ts, cut, true)? {
                        walk.events_in_window += 1;
                    }
                }
            }
        }
        let kind = ev.get("kind");
        if is_str(kind, "silent_miss_cutoff") {
            if let Some(c) = instant(ev.get("cutoff_ts")) {
                if latest_cutoff.is_none_or(|l| c > l) {
                    latest_cutoff = Some(c);
                }
            }
            continue;
        }
        if !is_str(kind, "turn_audited") {
            continue;
        }
        let ts = instant(ev.get("ts"));
        if let Some(cut) = &window {
            match ts {
                None => continue,
                Some(t) if at_or_after(t, cut, false)? => continue,
                Some(_) => {}
            }
        }
        if ev.get("repeat").is_some_and(truthy) {
            walk.repeats_skipped += 1;
            continue;
        }
        if is_str(ev.get("verdict"), "no_signal") {
            continue;
        }
        let top_hits = match ev.get("top_hits") {
            Some(PyValue::List(hits)) if matches!(hits.first(), Some(PyValue::Dict(_))) => hits,
            _ => {
                walk.without_features += 1;
                continue;
            }
        };
        let recent = match ev.get("recent_retrieval_count") {
            Some(PyValue::Int(t)) => t.clone(),
            _ => "0".to_string(),
        };
        candidates.push((
            ReplayableRow {
                index,
                event: ev,
                top_hits,
                recent,
            },
            ts,
        ));
    }
    for (row, ts) in candidates {
        if let Some(cutoff) = latest_cutoff {
            if ts.is_none_or(|t| t < cutoff) {
                walk.cutoff_retracted += 1;
                continue;
            }
        }
        walk.rows.push(row);
    }
    walk.with_features = walk.rows.len() as i64;
    Ok(walk)
}

/// `_probe_query_display(value)`: `(preview, length, hash_prefix)`. A
/// `str` is its own preview with its length in code points; a dict gives
/// its `preview` and `hash` when `str` and its `len` when an `int` that is
/// not a `bool` (decimal text, any size); anything else (a missing key
/// too) gives nothing.
pub fn probe_query_display(
    value: Option<&PyValue>,
) -> (Option<PyStr>, Option<String>, Option<PyStr>) {
    let str_of = |v: Option<&PyValue>| match v {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    };
    match value {
        Some(PyValue::Str(s)) => (Some(s.clone()), Some(s.len().to_string()), None),
        Some(d @ PyValue::Dict(_)) => (
            str_of(d.get("preview")),
            match d.get("len") {
                Some(PyValue::Int(t)) => Some(t.clone()),
                _ => None,
            },
            str_of(d.get("hash")),
        ),
        _ => (None, None, None),
    }
}

fn opt_int(t: Option<&String>) -> PyValue {
    t.map_or(PyValue::None, |t| PyValue::Int(t.clone()))
}

fn opt_scopes(s: Option<&Vec<PyStr>>) -> PyValue {
    s.map_or(PyValue::None, |s| {
        PyValue::List(s.iter().map(|x| text(x)).collect())
    })
}

/// `WideningFlaggedTurn`: one audited turn a widening rule would flag.
/// The `int` fields keep any size as decimal text.
#[derive(Clone, Debug, PartialEq)]
pub struct WideningFlaggedTurn {
    pub ts: Option<PyStr>,
    pub session_id: Option<PyStr>,
    pub client_model: Option<PyStr>,
    pub probe_query_preview: Option<PyStr>,
    pub probe_query_len: Option<String>,
    pub probe_query_hash: Option<PyStr>,
    pub top_hit_id: PyStr,
    pub top_hit_score: Option<f64>,
    pub relevance_v1: Option<PyStr>,
    pub relevance_v2: Option<PyStr>,
    pub matched_unique: Option<String>,
    pub query_unique: Option<String>,
    pub v1_also_flagged: bool,
    pub memory_status: PyStr,
    pub memory_summary: Option<PyStr>,
    pub memory_scopes: Option<Vec<PyStr>>,
}

impl WideningFlaggedTurn {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("ts", opt_text(self.ts.as_deref())),
            ("session_id", opt_text(self.session_id.as_deref())),
            ("client_model", opt_text(self.client_model.as_deref())),
            (
                "probe_query_preview",
                opt_text(self.probe_query_preview.as_deref()),
            ),
            ("probe_query_len", opt_int(self.probe_query_len.as_ref())),
            (
                "probe_query_hash",
                opt_text(self.probe_query_hash.as_deref()),
            ),
            ("top_hit_id", text(&self.top_hit_id)),
            ("top_hit_score", opt_float(self.top_hit_score)),
            ("relevance_v1", opt_text(self.relevance_v1.as_deref())),
            ("relevance_v2", opt_text(self.relevance_v2.as_deref())),
            ("matched_unique", opt_int(self.matched_unique.as_ref())),
            ("query_unique", opt_int(self.query_unique.as_ref())),
            ("v1_also_flagged", PyValue::Bool(self.v1_also_flagged)),
            ("memory_status", text(&self.memory_status)),
            ("memory_summary", opt_text(self.memory_summary.as_deref())),
            ("memory_scopes", opt_scopes(self.memory_scopes.as_ref())),
        ])
    }
}

/// `WideningMemoryRollup`: one memory's appearances as a flagged top hit.
#[derive(Clone, Debug, PartialEq)]
pub struct WideningMemoryRollup {
    pub memory_id: PyStr,
    pub count: i64,
    pub distinct_sessions: i64,
    pub status: PyStr,
    pub summary: Option<PyStr>,
    pub scopes: Option<Vec<PyStr>>,
    pub score_min: Option<f64>,
    pub score_max: Option<f64>,
}

impl WideningMemoryRollup {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("memory_id", text(&self.memory_id)),
            ("count", int(self.count)),
            ("distinct_sessions", int(self.distinct_sessions)),
            ("status", text(&self.status)),
            ("summary", opt_text(self.summary.as_deref())),
            ("scopes", opt_scopes(self.scopes.as_ref())),
            ("score_min", opt_float(self.score_min)),
            ("score_max", opt_float(self.score_max)),
        ])
    }
}

/// `WideningRuleDetail`: the flagged cohort of one widening rule.
#[derive(Clone, Debug, PartialEq)]
pub struct WideningRuleDetail {
    pub rule: PyStr,
    pub description: PyStr,
    pub flagged_total: i64,
    pub beyond_v1: i64,
    pub turns: Vec<WideningFlaggedTurn>,
    pub by_memory: Vec<WideningMemoryRollup>,
}

impl WideningRuleDetail {
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("rule", text(&self.rule)),
            ("description", text(&self.description)),
            ("flagged_total", int(self.flagged_total)),
            ("beyond_v1", int(self.beyond_v1)),
            (
                "turns",
                PyValue::List(
                    self.turns
                        .iter()
                        .map(WideningFlaggedTurn::to_dict)
                        .collect(),
                ),
            ),
            (
                "by_memory",
                PyValue::List(
                    self.by_memory
                        .iter()
                        .map(WideningMemoryRollup::to_dict)
                        .collect(),
                ),
            ),
        ])
    }
}
