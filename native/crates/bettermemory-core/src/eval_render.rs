//! `eval_render.py`, its first unit: the plain-text rendering of the rate
//! trio, `render_text` with its helpers (`_text`, `_review_text_lines`,
//! `_rate_from_dict`, `_SURFACE_RATES`, `_surface_text_lines`,
//! `_rule_split_text_lines`, `_format_rate`, `_bar`, `_truncate`,
//! `_humanize_seconds`, `_short_ts`), lines 49-384 of the reference.
//!
//! Text is code points ([`PyStr`]); a width pads by code points, as
//! Python's `format` does. Python's `'0.2f'` and `'g'` are
//! [`float_repr::format_fixed`] and [`float_repr::format_general`], and
//! `round` in `_bar` rounds half to even. The review and surfaces sections
//! read `dict[str, Any]` values ([`PyValue`]) as the reference does:
//! subscription (KeyError for a missing key, TypeError for a value that
//! is not subscriptable), truthiness, `.items()` (AttributeError on a value
//! that is not a dict), `str.join` (TypeError for an element that is not a
//! `str`), `str()` and an f-string's `format(value, '')` through
//! [`py_str_limited`]. Where the reference raises, the answer is its
//! exception class ([`RenderError`]); messages are not compared.

use crate::eval::EvalReport;
use crate::eval_model::{RateCI, EARLIER_RULES};
use crate::events::truthy;
use crate::float_repr::{format_fixed, format_general};
use crate::health_model::HOOKLESS_REASON;
use crate::printable::printable;
use crate::pyvalue::{py_str_limited, PyError, PyStr, PyValue};
use crate::time_utils::isoformat_utc_optional;
use crate::unicode::cps;

/// An exception the reference raises while rendering, by class.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RenderError {
    KeyError,
    TypeError,
    ValueError,
    AttributeError,
    OverflowError,
    RecursionError,
}

impl RenderError {
    pub fn name(self) -> &'static str {
        match self {
            RenderError::KeyError => "KeyError",
            RenderError::TypeError => "TypeError",
            RenderError::ValueError => "ValueError",
            RenderError::AttributeError => "AttributeError",
            RenderError::OverflowError => "OverflowError",
            RenderError::RecursionError => "RecursionError",
        }
    }
}

impl From<PyError> for RenderError {
    fn from(e: PyError) -> Self {
        match e {
            PyError::RecursionError => RenderError::RecursionError,
            PyError::ValueError => RenderError::ValueError,
            PyError::OverflowError => RenderError::OverflowError,
            _ => RenderError::TypeError,
        }
    }
}

type Answer<T> = Result<T, RenderError>;

/// `_SURFACE_RATES`.
pub const SURFACE_RATES: [&str; 4] = [
    "shown_opened_rate",
    "shown_used_rate",
    "ignored_rate",
    "delivery_helped_rate",
];

const TORN_NOTE: &str = "NOTE: numerator > denominator (rate clamped to 1.0). Usually a \
windowing artifact under --since (a use event is in-window while its retrieval aged out), \
or — less often — a log read mid-rotation. Re-run without --since to rule out the window edge.";

/// A line being written: text and code points in turn.
struct Line(PyStr);

impl Line {
    fn new(s: &str) -> Line {
        Line(cps(s))
    }
    fn t(mut self, s: &str) -> Line {
        self.0.extend(s.chars().map(u32::from));
        self
    }
    fn p(mut self, s: &[u32]) -> Line {
        self.0.extend_from_slice(s);
        self
    }
    /// `format(value, '')` of an f-string field.
    fn v(self, value: &PyValue) -> Answer<Line> {
        Ok(self.p(&fstr(value)?))
    }
}

/// `str(value)`, which is also `format(value, '')` for every type a
/// [`PyValue`] holds ([`py_str_limited`]): RecursionError past
/// `json::MAX_DEPTH` or ValueError for an `int` of more than 4,300 digits,
/// the first met in the order `repr` writes.
fn fstr(value: &PyValue) -> Answer<PyStr> {
    Ok(py_str_limited(value)?)
}

/// `format(text, '<Ns')`: `text`, then spaces to `width` code points.
fn ljust(text: &[u32], width: usize) -> PyStr {
    let mut out = text.to_vec();
    if out.len() < width {
        out.resize(width, u32::from(b' '));
    }
    out
}

/// `text[:n]` in code points.
fn head(text: &[u32], n: usize) -> &[u32] {
    &text[..text.len().min(n)]
}

/// `value[key]` for a `str` key.
fn sub<'a>(value: &'a PyValue, key: &str) -> Answer<&'a PyValue> {
    match value {
        PyValue::Dict(_) => value.get(key).ok_or(RenderError::KeyError),
        _ => Err(RenderError::TypeError),
    }
}

/// `value.items()`.
fn items(value: &PyValue) -> Answer<&[(PyValue, PyValue)]> {
    match value {
        PyValue::Dict(pairs) => Ok(pairs),
        _ => Err(RenderError::AttributeError),
    }
}

/// `sep.join(value)`: a list's or tuple's elements, a `str`'s characters,
/// a dict's keys; each must be a `str`.
fn join(sep: &str, value: &PyValue) -> Answer<PyStr> {
    let parts: Vec<&[u32]> = match value {
        PyValue::Str(s) => s.chunks(1).collect(),
        PyValue::List(v) | PyValue::Tuple(v) => v.iter().map(as_str).collect::<Answer<_>>()?,
        PyValue::Dict(pairs) => pairs
            .iter()
            .map(|(k, _)| as_str(k))
            .collect::<Answer<_>>()?,
        _ => return Err(RenderError::TypeError),
    };
    Ok(join_texts(sep, &parts))
}

fn as_str(value: &PyValue) -> Answer<&[u32]> {
    match value {
        PyValue::Str(s) => Ok(s),
        _ => Err(RenderError::TypeError),
    }
}

fn join_texts<T: AsRef<[u32]>>(sep: &str, parts: &[T]) -> PyStr {
    let sep = cps(sep);
    let mut out = Vec::new();
    for (i, part) in parts.iter().enumerate() {
        if i > 0 {
            out.extend_from_slice(&sep);
        }
        out.extend_from_slice(part.as_ref());
    }
    out
}

/// `printable(text) or fallback`.
fn printable_or(text: &[u32], fallback: &str) -> PyStr {
    let out = printable(text);
    if out.is_empty() {
        cps(fallback)
    } else {
        out
    }
}

/// `render_text(report)`.
pub fn render_text(report: &EvalReport) -> Answer<PyStr> {
    let mut lines: Vec<PyStr> = Vec::new();
    let scope_part = match &report.scope_filter {
        Some(scope) if !scope.is_empty() => Line::new(" · scope=").p(&printable(scope)).0,
        _ => Vec::new(),
    };
    if report.window_end.is_some() {
        let start = isoformat_utc_optional(report.window_start.as_ref())
            .unwrap_or_else(|| "the start".to_owned());
        let end = isoformat_utc_optional(report.window_end.as_ref()).unwrap_or_default();
        lines.push(
            Line::new("bettermemory eval — ")
                .t(&start)
                .t(" to ")
                .t(&end)
                .p(&scope_part)
                .0,
        );
    } else {
        let window = match report.window_seconds {
            None => "all time".to_owned(),
            Some(seconds) => humanize_seconds(seconds),
        };
        lines.push(
            Line::new("bettermemory eval — last ")
                .t(&window)
                .p(&scope_part)
                .0,
        );
    }
    lines.push(cps(&"─".repeat(60)));
    let count = |label: &str, n: i64| cps(&format!("{label}{n:>5}"));
    lines.push(count(
        "Events scanned                      ",
        report.events_in_window,
    ));
    lines.push(count(
        "Retrieval occurrences               ",
        report.retrieval_occurrences,
    ));
    lines.push(count(
        "Applied use events (auto+explicit)  ",
        report.applied_total,
    ));
    lines.push(count(
        "Turns audited                       ",
        report.turns_audited,
    ));
    if report.turns_no_signal != 0 {
        lines.push(count(
            "Turns no-signal (excluded)          ",
            report.turns_no_signal,
        ));
    }
    if report.repeat_audits != 0 {
        lines.push(count(
            "Repeat audits (deduped, excluded)   ",
            report.repeat_audits,
        ));
    }
    lines.push(Vec::new());
    for (label, rate) in [
        ("memory_helped_rate", &report.memory_helped_rate),
        ("endorsement_rate ", &report.endorsement_rate),
        ("silent_miss_rate ", &report.silent_miss_rate),
    ] {
        lines.push(format_rate(&cps(label), rate));
    }

    if !report.by_model.is_empty() {
        lines.push(Vec::new());
        lines.push(cps(
            "Per-model audit telemetry (Stop-hook `client_model` stamp):",
        ));
        let mut models: Vec<&(PyStr, Vec<(PyStr, i64)>)> = report.by_model.iter().collect();
        models.sort_by(|a, b| a.0.cmp(&b.0));
        for (model, counts) in models {
            let get = |key: &str| {
                let key = cps(key);
                counts
                    .iter()
                    .rev()
                    .find(|(k, _)| *k == key)
                    .map_or(0, |(_, n)| *n)
            };
            lines.push(
                Line::new("  ")
                    .p(&ljust(&printable(model), 28))
                    .t(&format!(
                        " audited={:<5} misses={:<4} no_signal={}",
                        get("audited"),
                        get("misses"),
                        get("no_signal")
                    ))
                    .0,
            );
        }
    }

    if !report.cold_endorsement_memories_rows.is_empty()
        || report.cold_endorsement_memories_total != 0
    {
        let ratio = report.cold_endorsement_ratio_threshold;
        let rule = if ratio > 0.0 {
            format!("explicit share < {}", format_general(ratio))
        } else {
            "0 explicit applied".to_owned()
        };
        lines.push(Vec::new());
        lines.push(cps(&format!(
            "Cold-endorsement memories (retrievals ≥ {}, {rule}): {}",
            report.endorsement_min_retrievals, report.cold_endorsement_memories_total
        )));
        for row in report.cold_endorsement_memories_rows.iter().take(10) {
            let scopes = printable_or(&join_texts(",", &row.scopes), "—");
            let share = if ratio > 0.0 {
                format!(
                    ", explicit {}/{}",
                    row.explicit_applied_count,
                    i128::from(row.auto_applied_count) + i128::from(row.explicit_applied_count)
                )
            } else {
                String::new()
            };
            lines.push(
                Line::new("  ")
                    .p(&printable(&row.id))
                    .t("  ")
                    .p(&ljust(&scopes, 18))
                    .t("  \"")
                    .p(&printable(&truncate(&row.summary, 50)))
                    .t(&format!("\"  ({} retrievals{share})", row.retrieval_count))
                    .0,
            );
        }
        if report.cold_endorsement_memories_total > 10 {
            lines.push(cps(&format!(
                "  … plus {} more",
                report.cold_endorsement_memories_total - 10
            )));
        }
    } else if report.cold_endorsement_suppressed {
        lines.push(Vec::new());
        lines.push(cps(&format!(
            "Cold-endorsement memories: suppressed — {HOOKLESS_REASON}"
        )));
    }

    if !report.silent_miss_recent.is_empty() {
        lines.push(Vec::new());
        lines.push(cps(&format!(
            "Silent-miss candidates (last {}):",
            report.silent_miss_recent.len()
        )));
        for c in &report.silent_miss_recent {
            let or_dash = |v: &Option<PyStr>| match v {
                Some(s) if !s.is_empty() => printable(s),
                _ => printable(&cps("—")),
            };
            lines.push(
                Line::new("  ")
                    .p(&printable(&short_ts(&c.ts)))
                    .t("  session=")
                    .p(&printable(head(&c.session_id, 12)))
                    .t("…  missed=")
                    .p(&or_dash(&c.top_missed_id))
                    .t(" relevance=")
                    .p(&or_dash(&c.top_missed_relevance))
                    .0,
            );
        }
    }

    if !report.threshold_rule.is_empty() {
        lines.push(Vec::new());
        lines.push(
            Line::new("Threshold rule: ")
                .p(&printable(&report.threshold_rule))
                .0,
        );
    }
    lines.extend(review_text_lines(report.review.as_ref())?);
    lines.extend(surface_text_lines(report.surfaces.as_ref())?);
    if report.memory_helped_rate.torn_read
        || report.endorsement_rate.torn_read
        || report.silent_miss_rate.torn_read
    {
        lines.push(Vec::new());
        lines.push(cps(TORN_NOTE));
    }
    Ok(text(&lines))
}

/// `_text(lines)`: each line through `printable`, joined by newlines,
/// with a final newline.
pub fn text(lines: &[PyStr]) -> PyStr {
    let mut out = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        if i > 0 {
            out.push(u32::from(b'\n'));
        }
        out.extend(printable(line));
    }
    out.push(u32::from(b'\n'));
    out
}

/// `num(value)` of `_review_text_lines`: `n/a` for None, else `str()`.
fn num(value: &PyValue) -> Answer<PyStr> {
    match value {
        PyValue::None => Ok(cps("n/a")),
        other => Ok(fstr(other)?),
    }
}

/// `_review_text_lines(review)`.
pub fn review_text_lines(review: Option<&PyValue>) -> Answer<Vec<PyStr>> {
    let Some(review) = review.filter(|r| truthy(r)) else {
        return Ok(Vec::new());
    };
    if !truthy(sub(sub(review, "chains")?, "issued")?) {
        return Ok(Vec::new());
    }
    let chains = sub(review, "chains")?;
    let mut line = Line::new("  issued=").v(sub(chains, "issued")?)?;
    for key in ["answered", "unanswered", "pending", "closed_by_call"] {
        line = line.t(&format!(" {key}=")).v(sub(chains, key)?)?;
    }
    let totals = line.0;
    let line = Line::new("  pre_answered=")
        .v(sub(review, "pre_answered")?)?
        .t(" pre_answered_zero=")
        .v(sub(sub(review, "pre_answered_zero")?, "total")?)?
        .t(" response_chars_p50=")
        .p(&num(sub(review, "response_chars_p50")?)?)
        .t(" calls_per_committed_write=")
        .p(&num(sub(review, "calls_per_committed_write")?)?);
    let shares = line.0;
    let mut actions = Vec::new();
    for (name, count) in items(sub(review, "actions")?)? {
        actions.push(Line(printable(&fstr(name)?)).t("=").v(count)?.0);
    }
    let mut filings = Vec::new();
    for (name, value) in items(sub(review, "filings_per_100_committed")?)? {
        filings.push(Line(printable(&fstr(name)?)).t("=").p(&num(value)?).0);
    }
    let mut lines = vec![
        Vec::new(),
        cps("Write review (chains issued in the window):"),
        totals,
        shares,
        Line::new("  actions ").p(&join_texts(" ", &actions)).0,
        Line::new("  filings_per_100_committed ")
            .p(&join_texts(" ", &filings))
            .0,
    ];
    for (client, bucket) in items(sub(review, "by_client")?)? {
        lines.push(
            Line::new("  client ")
                .p(&printable(&fstr(client)?))
                .t(": issued=")
                .v(sub(bucket, "issued")?)?
                .t(" unanswered=")
                .v(sub(bucket, "unanswered")?)?
                .t(" pre_answered_zero=")
                .v(sub(bucket, "pre_answered_zero")?)?
                .0,
        );
    }
    let grades = sub(review, "grades")?;
    if truthy(sub(grades, "n")?) {
        lines.push(
            Line::new("  grades n=")
                .v(sub(grades, "n")?)?
                .t(" right_both=")
                .v(sub(grades, "right_both")?)?
                .t(" pre_answered_n=")
                .v(sub(grades, "pre_answered_n")?)?
                .0,
        );
    }
    Ok(lines)
}

/// A rate as `_format_rate` reads it: `numerator`, `denominator`, `rate`,
/// `lower`, `upper` (`torn_read` is not read).
pub struct RateFields<'a> {
    pub numerator: &'a PyValue,
    pub denominator: &'a PyValue,
    pub rate: &'a PyValue,
    pub lower: &'a PyValue,
    pub upper: &'a PyValue,
}

/// `_rate_from_dict(rate)`: the six keys read in turn.
pub fn rate_from_dict(rate: &PyValue) -> Answer<RateFields<'_>> {
    let fields = RateFields {
        numerator: sub(rate, "numerator")?,
        denominator: sub(rate, "denominator")?,
        rate: sub(rate, "rate")?,
        lower: sub(rate, "ci95_lower")?,
        upper: sub(rate, "ci95_upper")?,
    };
    sub(rate, "torn_read")?;
    Ok(fields)
}

/// `_surface_text_lines(surfaces)`.
pub fn surface_text_lines(surfaces: Option<&PyValue>) -> Answer<Vec<PyStr>> {
    let Some(surfaces) = surfaces.filter(|s| !matches!(s, PyValue::None)) else {
        return Ok(Vec::new());
    };
    let rules = printable_or(&join(", ", sub(surfaces, "rules")?)?, "none");
    let mut lines = vec![
        Vec::new(),
        Line::new("Recall surfaces (rule ").p(&rules).t("):").0,
    ];
    for surface in ["brief", "recall"] {
        let tally = sub(surfaces, surface)?;
        lines.push(tally_line(&format!("  {surface}: "), tally)?);
        for name in SURFACE_RATES {
            let fields = rate_from_dict(sub(tally, name)?)?;
            lines.push(
                Line::new("    ")
                    .p(&format_rate_fields(&cps(name), &fields)?)
                    .0,
            );
        }
    }
    let suppressed = sub(sub(surfaces, "recall")?, "suppressed")?;
    if truthy(suppressed) {
        let mut held = Vec::new();
        for (reason, count) in items(suppressed)? {
            held.push(Line(printable(&fstr(reason)?)).t("=").v(count)?.0);
        }
        lines.push(
            Line::new("  recall held back ")
                .p(&join_texts(" ", &held))
                .0,
        );
    }
    let settled = sub(surfaces, "settlements")?;
    let segments = sub(surfaces, "segments")?;
    let unanswered = sub(surfaces, "review_unanswered")?;
    lines.push(
        Line::new("  settled brief=")
            .v(sub(settled, "brief")?)?
            .t(" recall=")
            .v(sub(settled, "recall")?)?
            .t(" · sessions=")
            .v(sub(segments, "sessions")?)?
            .t(" segments_opened=")
            .v(sub(segments, "opened")?)?
            .t(" · review_unanswered turns=")
            .v(sub(unanswered, "turns")?)?
            .t(" chains=")
            .v(sub(unanswered, "chains")?)?
            .0,
    );
    for (stratum, grade) in items(sub(surfaces, "grades")?)? {
        if truthy(sub(grade, "graded")?) {
            let label = Line::new("graded ").v(stratum)?.0;
            let fields = rate_from_dict(sub(grade, "relevant_rate")?)?;
            lines.push(Line::new("  ").p(&format_rate_fields(&label, &fields)?).0);
        }
    }
    lines.extend(rule_split_text_lines(surfaces)?);
    lines.push(Line::new("  ").v(sub(surfaces, "note")?)?.0);
    Ok(lines)
}

/// The counts line of one surface's tally, after `prefix`.
fn tally_line(prefix: &str, tally: &PyValue) -> Answer<PyStr> {
    let mut line = Line::new(prefix)
        .t("deliveries=")
        .v(sub(tally, "deliveries")?)?;
    for (label, key) in [
        ("shown", "shown"),
        ("opened", "opened"),
        ("used", "used"),
        ("ignored", "ignored"),
        ("helped", "deliveries_helped"),
    ] {
        line = line.t(&format!(" {label}=")).v(sub(tally, key)?)?;
    }
    Ok(line.0)
}

/// `_rule_split_text_lines(surfaces)`: `surfaces` is a dict here (the
/// caller has subscripted it), read with `.get`.
pub fn rule_split_text_lines(surfaces: &PyValue) -> Answer<Vec<PyStr>> {
    let by_rule = match surfaces.get("by_rule") {
        Some(PyValue::Dict(pairs)) => pairs,
        _ => return Ok(Vec::new()),
    };
    let mut lines = vec![cps("  By rule:")];
    for (group, split) in by_rule {
        // The earlier group's label is made at once; any other is the key,
        // spelled where each line formats it, after `split[surface]`.
        let earlier = match group {
            PyValue::Str(g) if *g == cps(EARLIER_RULES) => {
                let rules = printable_or(&join(", ", sub(split, "rules")?)?, "none");
                Some(Line(g.clone()).t(" (").p(&rules).t(")").0)
            }
            _ => None,
        };
        for surface in ["brief", "recall"] {
            let tally = sub(split, surface)?;
            let label = match &earlier {
                Some(label) => label.clone(),
                None => fstr(group)?,
            };
            let prefix = Line::new("    ").p(&label).t(&format!(" {surface}: "));
            lines.push(tally_line("", tally).map(|t| prefix.p(&t).0)?);
            if truthy(sub(tally, "deliveries")?) {
                for name in SURFACE_RATES {
                    let fields = rate_from_dict(sub(tally, name)?)?;
                    lines.push(
                        Line::new("      ")
                            .p(&format_rate_fields(&cps(name), &fields)?)
                            .0,
                    );
                }
            }
        }
    }
    if let Some(delta @ PyValue::Dict(_)) = surfaces.get("membership_delta") {
        lines.push(
            Line::new("  membership_delta rows=")
                .v(sub(delta, "rows")?)?
                .t(" differing=")
                .v(sub(delta, "rows_differing")?)?
                .t(" delivered_not_rc3=")
                .v(sub(delta, "delivered_not_rc3")?)?
                .t(" rc3_not_delivered=")
                .v(sub(delta, "rc3_not_delivered")?)?
                .0,
        );
    }
    Ok(lines)
}

/// `_format_rate(label, rate)` of a `RateCI`.
pub fn format_rate(label: &[u32], rate: &RateCI) -> PyStr {
    let opt = |f: Option<f64>| f.map_or(PyValue::None, PyValue::Float);
    let (numerator, denominator) = (
        PyValue::int(rate.numerator.into()),
        PyValue::int(rate.denominator.into()),
    );
    let (r, lower, upper) = (opt(rate.rate), opt(rate.lower), opt(rate.upper));
    let fields = RateFields {
        numerator: &numerator,
        denominator: &denominator,
        rate: &r,
        lower: &lower,
        upper: &upper,
    };
    // Ints and floats: nothing here raises.
    format_rate_fields(label, &fields).unwrap_or_default()
}

/// `_format_rate(label, rate)` over the values a `RateCI` built by
/// `_rate_from_dict` holds, which may be of any type: `_bar` runs first
/// (TypeError for a rate Python cannot compare with a float), then the
/// three `'0.2f'` fields in turn.
pub fn format_rate_fields(label: &[u32], rate: &RateFields<'_>) -> Answer<PyStr> {
    let none = |v: &PyValue| matches!(v, PyValue::None);
    let label = ljust(label, 20);
    if none(rate.rate) || none(rate.lower) || none(rate.upper) {
        return Ok(Line(label)
            .t(" n/a    (k=")
            .v(rate.numerator)?
            .t(", n=")
            .v(rate.denominator)?
            .t(")")
            .0);
    }
    let bar = bar_value(rate.rate)?;
    Ok(Line(label)
        .t(" ")
        .t(&fixed2(rate.rate)?)
        .t(" [")
        .t(&fixed2(rate.lower)?)
        .t(", ")
        .t(&fixed2(rate.upper)?)
        .t("]   ")
        .p(&bar)
        .t("   (k=")
        .v(rate.numerator)?
        .t(", n=")
        .v(rate.denominator)?
        .t(")")
        .0)
}

/// `format(value, '0.2f')`: a float; an int or bool through `float()`
/// (OverflowError past the doubles); a datetime's `strftime('0.2f')`,
/// which is `0.2f`; ValueError for a `str`; TypeError for the rest.
fn fixed2(value: &PyValue) -> Answer<String> {
    // Two places is within every precision `format` takes.
    let two = |f: f64| format_fixed(f, 2).map_err(|_| RenderError::ValueError);
    match value {
        PyValue::Float(f) => two(*f),
        PyValue::Bool(b) => two(f64::from(u8::from(*b))),
        PyValue::Int(digits) => {
            let f: f64 = digits.parse().map_err(|_| RenderError::ValueError)?;
            if f.is_infinite() {
                return Err(RenderError::OverflowError);
            }
            two(f)
        }
        PyValue::DateTime(_) => Ok("0.2f".to_owned()),
        PyValue::Str(_) => Err(RenderError::ValueError),
        _ => Err(RenderError::TypeError),
    }
}

/// `_bar(rate)` of any value: `max(0.0, min(1.0, rate))` compares an int
/// or a bool exactly (one and above is a full bar, zero and below an
/// empty one); any other type but a float raises TypeError.
fn bar_value(value: &PyValue) -> Answer<PyStr> {
    match value {
        PyValue::Float(f) => Ok(bar(*f)),
        PyValue::Bool(b) => Ok(bar(f64::from(u8::from(*b)))),
        PyValue::Int(digits) => Ok(bar(if digits.starts_with('-') || digits == "0" {
            0.0
        } else {
            1.0
        })),
        _ => Err(RenderError::TypeError),
    }
}

/// `_bar(rate)`: `max(0.0, min(1.0, rate))` as Python's `min` and `max`
/// (a NaN rate is not below 1.0, so it is a full bar), then
/// `round(rate * 10)` half to even.
pub fn bar(rate: f64) -> PyStr {
    // The NaN is tested on its own, before the clamp: an optimised build
    // may fold `if rate < 1.0 { rate } else { 1.0 }` into a minimum
    // instruction that passes a signalling NaN through (arm64's `fminnm`),
    // which would then be an empty bar.
    let full = if rate.is_nan() {
        10
    } else {
        (rate.clamp(0.0, 1.0) * 10.0).round_ties_even() as usize
    };
    cps(&format!("{}{}", "▇".repeat(full), "▁".repeat(10 - full)))
}

/// `_truncate(s, n)` in code points: `s[: n - 1] + "…"` past `n`, where
/// `n = 0` slices to `-1`, all but the last.
pub fn truncate(s: &[u32], n: usize) -> PyStr {
    if s.len() <= n {
        return s.to_vec();
    }
    let end = n.checked_sub(1).unwrap_or(s.len() - 1);
    let mut out = s[..end].to_vec();
    out.push(u32::from('…'));
    out
}

/// `_humanize_seconds(seconds)`, with Python's floor `%` and `//`.
pub fn humanize_seconds(seconds: i64) -> String {
    for (unit, suffix) in [(86_400, "d"), (3_600, "h"), (60, "m")] {
        if seconds.rem_euclid(unit) == 0 {
            return format!("{}{suffix}", seconds.div_euclid(unit));
        }
    }
    format!("{seconds}s")
}

/// `_short_ts(ts)`: the text as given when it is shorter than 16 code
/// points or holds no `T`; else the part before the first `T`, a space,
/// and the next five code points after it.
pub fn short_ts(ts: &[u32]) -> PyStr {
    let t = u32::from(b'T');
    match ts.iter().position(|&c| c == t) {
        Some(at) if ts.len() >= 16 => {
            let mut out = ts[..at].to_vec();
            out.push(u32::from(b' '));
            out.extend_from_slice(head(&ts[at + 1..], 5));
            out
        }
        _ => ts.to_vec(),
    }
}
