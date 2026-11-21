//! `audit.py` 191-737: the report types and the event field builders.
//!
//! Every builder returns the event's fields as a [`PyValue::Dict`] in the
//! reference's insertion order. The two that mint an event id take the
//! minter as an argument (`generate_ulid` in the reference), so a caller
//! pins it. A `ValueError` comes back as [`AuditError::ValueError`] with
//! the reference's message, code point for code point.

use crate::datetime::DateTime;
use crate::pyvalue::{repr, repr_str, PyError, PyStr, PyValue};
use crate::unicode::cps;

use super::{
    HELD_CAUSES, NO_OWNER_PROMPT, THRESHOLD_RULE_V1, VALID_NO_SIGNAL_REASON, VALID_REPLY_SOURCE,
    VALID_TRIGGERED_FROM,
};

/// The `ValueError` a builder raises, by its message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuditError {
    ValueError(PyStr),
    /// `repr` of the refused `settled_count` raised (a list nested past
    /// the interpreter's depth), before the `ValueError` was built.
    Repr(PyError),
}

/// `Verdict`: `"miss"`, `"ok"` or `"no_signal"`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Miss,
    Ok,
    NoSignal,
}

impl Verdict {
    pub fn as_str(self) -> &'static str {
        match self {
            Verdict::Miss => "miss",
            Verdict::Ok => "ok",
            Verdict::NoSignal => "no_signal",
        }
    }
}

/// `MissHit`: one probe hit's identity and ranking metadata.
#[derive(Debug, Clone, PartialEq)]
pub struct MissHit {
    pub id: PyStr,
    pub score: f64,
    pub relevance: PyStr,
    pub scopes: Vec<PyStr>,
    pub snippet: PyStr,
    pub matched_unique: i64,
    pub query_unique: i64,
    pub relevance_v2: PyStr,
}

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn int(value: i64) -> PyValue {
    PyValue::int(i128::from(value))
}

fn opt(value: &Option<PyStr>) -> PyValue {
    value.clone().map_or(PyValue::None, PyValue::Str)
}

impl MissHit {
    /// `MissHit.to_dict`.
    pub fn to_dict(&self) -> PyValue {
        PyValue::Dict(vec![
            (s("id"), PyValue::Str(self.id.clone())),
            (s("score"), PyValue::Float(self.score)),
            (s("relevance"), PyValue::Str(self.relevance.clone())),
            (
                s("scopes"),
                PyValue::List(self.scopes.iter().cloned().map(PyValue::Str).collect()),
            ),
            (s("snippet"), PyValue::Str(self.snippet.clone())),
            (s("matched_unique"), int(self.matched_unique)),
            (s("query_unique"), int(self.query_unique)),
            (s("relevance_v2"), PyValue::Str(self.relevance_v2.clone())),
        ])
    }

    /// `MissHit.to_compact_dict`: no scopes, no snippet.
    pub fn to_compact_dict(&self) -> PyValue {
        PyValue::Dict(vec![
            (s("id"), PyValue::Str(self.id.clone())),
            (s("score"), PyValue::Float(self.score)),
            (s("relevance"), PyValue::Str(self.relevance.clone())),
            (s("relevance_v2"), PyValue::Str(self.relevance_v2.clone())),
            (s("matched_unique"), int(self.matched_unique)),
            (s("query_unique"), int(self.query_unique)),
        ])
    }
}

/// `MissReport`: the probe's structured verdict.
#[derive(Debug, Clone, PartialEq)]
pub struct MissReport {
    pub verdict: Verdict,
    pub checked_at: DateTime,
    pub session_id: PyStr,
    pub lookback_seconds: i64,
    pub recent_retrieval_count: i64,
    pub threshold_rule: PyStr,
    pub top_hits: Vec<MissHit>,
    pub probe_query: Option<PyStr>,
    pub no_signal_reason: Option<PyStr>,
    pub suppressed_by: Option<PyStr>,
}

impl MissReport {
    /// `MissReport.is_miss`.
    pub fn is_miss(&self) -> bool {
        self.verdict == Verdict::Miss
    }

    fn full_hits(&self) -> PyValue {
        PyValue::List(self.top_hits.iter().map(MissHit::to_dict).collect())
    }

    /// `MissReport.to_dict`; `checked_at` through `isoformat_utc`.
    pub fn to_dict(&self) -> PyValue {
        PyValue::Dict(vec![
            (s("verdict"), s(self.verdict.as_str())),
            (s("checked_at"), s(&self.checked_at.isoformat_utc())),
            (s("session_id"), PyValue::Str(self.session_id.clone())),
            (s("lookback_seconds"), int(self.lookback_seconds)),
            (
                s("recent_retrieval_count"),
                int(self.recent_retrieval_count),
            ),
            (
                s("threshold_rule"),
                PyValue::Str(self.threshold_rule.clone()),
            ),
            (s("top_hits"), self.full_hits()),
            (s("probe_query"), opt(&self.probe_query)),
            (s("no_signal_reason"), opt(&self.no_signal_reason)),
            (s("suppressed_by"), opt(&self.suppressed_by)),
        ])
    }
}

/// `no_owner_prompt_report`.
pub fn no_owner_prompt_report(
    session_id: &[u32],
    now: DateTime,
    lookback_seconds: i64,
) -> MissReport {
    MissReport {
        verdict: Verdict::NoSignal,
        checked_at: now,
        session_id: session_id.to_vec(),
        lookback_seconds,
        recent_retrieval_count: 0,
        threshold_rule: cps(THRESHOLD_RULE_V1),
        top_hits: Vec::new(),
        probe_query: None,
        no_signal_reason: Some(cps(NO_OWNER_PROMPT)),
        suppressed_by: None,
    }
}

/// `repr(sorted(set))` of a set of ASCII words, already sorted.
fn list_repr(words: &[&str]) -> String {
    let inner: Vec<String> = words.iter().map(|w| format!("'{w}'")).collect();
    format!("[{}]", inner.join(", "))
}

fn among(set: &[&str], text: &[u32]) -> bool {
    set.iter().any(|w| cps(w) == text)
}

fn value_error(head: &str, got: &[u32]) -> AuditError {
    let mut msg = cps(head);
    msg.extend(got);
    AuditError::ValueError(msg)
}

fn check_triggered_from(triggered_from: &[u32]) -> Result<(), AuditError> {
    if among(VALID_TRIGGERED_FROM, triggered_from) {
        return Ok(());
    }
    let head = format!(
        "triggered_from must be one of {}, got ",
        list_repr(VALID_TRIGGERED_FROM)
    );
    Err(value_error(&head, &repr_str(triggered_from)))
}

/// The keyword arguments of `turn_audited_fields` past the required ones.
#[derive(Debug, Clone, Default)]
pub struct TurnAuditedOptions {
    pub repeat: bool,
    pub client_model: Option<PyStr>,
    pub prompt_id: Option<PyStr>,
    pub reply_source: Option<PyStr>,
    pub rule: Option<PyStr>,
    /// Any Python value: the reference checks it for a non-negative int
    /// (a bool refused) itself.
    pub settled_count: Option<PyValue>,
}

/// `turn_audited_fields`.
pub fn turn_audited_fields(
    report: &MissReport,
    session_id: &[u32],
    probe_mode: &[u32],
    assistant_present: bool,
    triggered_from: &[u32],
    o: &TurnAuditedOptions,
) -> Result<PyValue, AuditError> {
    if let Some(count) = &o.settled_count {
        // Read the text as Python would print the int: a hand-built
        // `Int` that is not canonical decimal (" -1", "-0", "+1") is no
        // value the reference can see, so it is refused.
        let fine = matches!(
            count,
            PyValue::Int(text) if !text.starts_with('-')
                && PyValue::int_from_decimal(text).as_ref() == Some(count)
        );
        if !fine {
            let got = repr(count).map_err(AuditError::Repr)?;
            return Err(value_error(
                "settled_count must be a non-negative int or None, got ",
                &got,
            ));
        }
    }
    check_triggered_from(triggered_from)?;
    if let Some(source) = &o.reply_source {
        if !among(VALID_REPLY_SOURCE, source) {
            let head = format!(
                "reply_source must be one of {} or None, got ",
                list_repr(VALID_REPLY_SOURCE)
            );
            return Err(value_error(&head, &repr_str(source)));
        }
    }
    if let Some(reason) = &report.no_signal_reason {
        if !among(VALID_NO_SIGNAL_REASON, reason) {
            let head = format!(
                "no_signal_reason must be one of {} or None, got ",
                list_repr(VALID_NO_SIGNAL_REASON)
            );
            return Err(value_error(&head, &repr_str(reason)));
        }
    }
    let mut f = vec![
        (s("session_id"), PyValue::Str(session_id.to_vec())),
        (s("verdict"), s(report.verdict.as_str())),
        (s("lookback_seconds"), int(report.lookback_seconds)),
        (
            s("recent_retrieval_count"),
            int(report.recent_retrieval_count),
        ),
        (s("probe_mode"), PyValue::Str(probe_mode.to_vec())),
        (
            s("threshold_rule"),
            PyValue::Str(report.threshold_rule.clone()),
        ),
        (s("assistant_present"), PyValue::Bool(assistant_present)),
        (s("triggered_from"), PyValue::Str(triggered_from.to_vec())),
    ];
    push_some(&mut f, "no_signal_reason", &report.no_signal_reason);
    push_some(&mut f, "probe_query", &report.probe_query);
    if !report.top_hits.is_empty() {
        let hits = report
            .top_hits
            .iter()
            .map(MissHit::to_compact_dict)
            .collect();
        f.push((s("top_hits"), PyValue::List(hits)));
    }
    if o.repeat {
        f.push((s("repeat"), PyValue::Bool(true)));
    }
    push_some(&mut f, "client_model", &o.client_model);
    push_some(&mut f, "prompt_id", &o.prompt_id);
    push_some(&mut f, "reply_source", &o.reply_source);
    push_some(&mut f, "rule", &o.rule);
    if let Some(count) = &o.settled_count {
        f.push((s("settled_count"), count.clone()));
    }
    Ok(PyValue::Dict(f))
}

fn push_some(f: &mut Vec<(PyValue, PyValue)>, name: &str, value: &Option<PyStr>) {
    if let Some(v) = value {
        f.push((s(name), PyValue::Str(v.clone())));
    }
}

/// `search_miss_fields`. `event_id` None mints one through `mint`.
pub fn search_miss_fields(
    report: &MissReport,
    session_id: &[u32],
    triggered_from: &[u32],
    event_id: Option<&[u32]>,
    client_model: Option<&[u32]>,
    rule: Option<&[u32]>,
    mint: &mut dyn FnMut() -> String,
) -> Result<PyValue, AuditError> {
    check_triggered_from(triggered_from)?;
    let id = match event_id {
        Some(id) => id.to_vec(),
        None => cps(&mint()),
    };
    let mut f = vec![
        (s("event_id"), PyValue::Str(id)),
        (s("session_id"), PyValue::Str(session_id.to_vec())),
        (
            s("threshold_rule"),
            PyValue::Str(report.threshold_rule.clone()),
        ),
        (s("lookback_seconds"), int(report.lookback_seconds)),
        (
            s("recent_retrieval_count"),
            int(report.recent_retrieval_count),
        ),
        (s("top_hits"), report.full_hits()),
        (s("probe_query"), opt(&report.probe_query)),
        (s("triggered_from"), PyValue::Str(triggered_from.to_vec())),
    ];
    push_some(&mut f, "client_model", &client_model.map(<[u32]>::to_vec));
    push_some(&mut f, "rule", &rule.map(<[u32]>::to_vec));
    Ok(PyValue::Dict(f))
}

/// A `Mapping[str, str]` entry of `held` or `suppressed`, in its order.
pub type Entry = Vec<(PyStr, PyStr)>;

fn entries(list: &[Entry]) -> PyValue {
    PyValue::List(
        list.iter()
            .map(|e| {
                // As `dict(pairs)`: a repeated key keeps its first place
                // and its last value.
                let mut d: Vec<(PyValue, PyValue)> = Vec::with_capacity(e.len());
                let mut at: std::collections::HashMap<&PyStr, usize> =
                    std::collections::HashMap::with_capacity(e.len());
                for (k, v) in e {
                    if let Some(&i) = at.get(k) {
                        d[i].1 = PyValue::Str(v.clone());
                    } else {
                        at.insert(k, d.len());
                        d.push((PyValue::Str(k.clone()), PyValue::Str(v.clone())));
                    }
                }
                PyValue::Dict(d)
            })
            .collect(),
    )
}

/// The trailing keyword arguments `recall_held_fields` and
/// `prompt_recall_fields` share.
#[derive(Debug, Clone, Default)]
pub struct RecallTail {
    pub segment: Option<i64>,
    pub prompt_id: Option<PyStr>,
    pub transcript_path: Option<PyStr>,
    pub harness: Option<PyStr>,
}

fn push_tail(f: &mut Vec<(PyValue, PyValue)>, t: &RecallTail) {
    if let Some(segment) = t.segment {
        f.push((s("segment"), int(segment)));
    }
    push_some(f, "prompt_id", &t.prompt_id);
    push_some(f, "transcript_path", &t.transcript_path);
    push_some(f, "harness", &t.harness);
}

/// `recall_held_fields`. `triggered_from` defaults to `"prompt_hook"` in
/// the reference; here the caller passes it.
#[allow(clippy::too_many_arguments)]
pub fn recall_held_fields(
    report: &MissReport,
    session_id: &[u32],
    probe_mode: &[u32],
    cause: &[u32],
    held: &[Entry],
    rule: &[u32],
    triggered_from: &[u32],
    tail: &RecallTail,
    mint: &mut dyn FnMut() -> String,
) -> Result<PyValue, AuditError> {
    check_triggered_from(triggered_from)?;
    if !among(HELD_CAUSES, cause) {
        let head = format!("cause must be one of {}, got ", list_repr(HELD_CAUSES));
        return Err(value_error(&head, &repr_str(cause)));
    }
    let mut f = vec![
        (s("event_id"), s(&mint())),
        (s("session_id"), PyValue::Str(session_id.to_vec())),
        (s("verdict"), s(report.verdict.as_str())),
        (
            s("threshold_rule"),
            PyValue::Str(report.threshold_rule.clone()),
        ),
        (s("lookback_seconds"), int(report.lookback_seconds)),
        (
            s("recent_retrieval_count"),
            int(report.recent_retrieval_count),
        ),
        (s("top_hits"), report.full_hits()),
        (s("probe_query"), opt(&report.probe_query)),
        (s("probe_mode"), PyValue::Str(probe_mode.to_vec())),
        (s("triggered_from"), PyValue::Str(triggered_from.to_vec())),
        (s("cause"), PyValue::Str(cause.to_vec())),
        (s("held"), entries(held)),
        (s("rule"), PyValue::Str(rule.to_vec())),
    ];
    push_some(&mut f, "no_signal_reason", &report.no_signal_reason);
    push_tail(&mut f, tail);
    Ok(PyValue::Dict(f))
}

/// The keyword arguments of `prompt_recall_fields` past the required
/// ones. `Default` is not the reference's defaults: `triggered_from` is
/// `"prompt_hook"` and `delivered_reason` `"project"` there
/// ([`PromptRecallOptions::reference`]).
#[derive(Debug, Clone, Default)]
pub struct PromptRecallOptions {
    pub triggered_from: PyStr,
    pub delivered_reason: PyStr,
    pub shown_ids: Option<Vec<PyStr>>,
    pub suppressed: Option<Vec<Entry>>,
    pub rule: Option<PyStr>,
    pub tail: RecallTail,
}

impl PromptRecallOptions {
    /// The reference's defaults.
    pub fn reference() -> Self {
        PromptRecallOptions {
            triggered_from: cps("prompt_hook"),
            delivered_reason: cps("project"),
            ..Default::default()
        }
    }
}

/// `prompt_recall_fields`.
pub fn prompt_recall_fields(
    report: &MissReport,
    session_id: &[u32],
    probe_mode: &[u32],
    injected_chars: i64,
    o: &PromptRecallOptions,
    mint: &mut dyn FnMut() -> String,
) -> Result<PyValue, AuditError> {
    check_triggered_from(&o.triggered_from)?;
    let mut f = vec![
        (s("event_id"), s(&mint())),
        (s("session_id"), PyValue::Str(session_id.to_vec())),
        (
            s("threshold_rule"),
            PyValue::Str(report.threshold_rule.clone()),
        ),
        (s("lookback_seconds"), int(report.lookback_seconds)),
        (
            s("recent_retrieval_count"),
            int(report.recent_retrieval_count),
        ),
        (s("top_hits"), report.full_hits()),
        (s("probe_query"), opt(&report.probe_query)),
        (s("probe_mode"), PyValue::Str(probe_mode.to_vec())),
        (s("injected_chars"), int(injected_chars)),
        (s("triggered_from"), PyValue::Str(o.triggered_from.clone())),
        (
            s("delivered_reason"),
            PyValue::Str(o.delivered_reason.clone()),
        ),
    ];
    if let Some(ids) = &o.shown_ids {
        f.push((
            s("shown_ids"),
            PyValue::List(ids.iter().cloned().map(PyValue::Str).collect()),
        ));
    }
    if let Some(list) = &o.suppressed {
        f.push((s("suppressed_ids"), entries(list)));
    }
    if let Some(rule) = &o.rule {
        f.push((s("rule"), PyValue::Str(rule.clone())));
        f.push((s("chars"), int(injected_chars)));
    }
    push_tail(&mut f, &o.tail);
    Ok(PyValue::Dict(f))
}
