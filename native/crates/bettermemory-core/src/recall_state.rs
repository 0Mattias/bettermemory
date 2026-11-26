//! `recall_state.py`'s first unit: the vocabulary the recall modules read
//! (`is_settlement`, the surfaces, the subagent fields, `Subagent`,
//! `agent_of`, `taken_back`), the per-session record (`_Signal`,
//! `_SessionRecall`, `SegmentView`) with its views (`_view`,
//! `_closing_view`) and transitions (`_restart`, `_reopen`,
//! `_hold_signal`, `_withdraw`, `_replay`), and `rebuild_session` over the
//! store's session read, which is a trait the caller implements.
//!
//! An event is a `PyValue` (a `dict` as the log reads it). The clock is
//! an argument (`now`). `RULE`, `DEFAULT_HARNESS`, `STATE_IDLE_SECONDS`
//! and `REVIEW_UNANSWERED` are `recall_settle`'s, re-exported;
//! `_DELIVERED_PROVENANCE` and `SUMMARY_CHARS` are `recall::choose`'s.
//! `RecallState`, the process-wide table of sessions, and the state bound
//! to a store for the settlement's and the recall's traits (`Bound`) are
//! the second unit, in `held`.

use std::collections::{BTreeMap, BTreeSet};

use crate::datetime::{parse_event_ts, DateTime};
use crate::events::str_ids;
use crate::pyvalue::{py_str, PyError, PyStr, PyValue};
use crate::session::OrderedMap;
use crate::toml::INT_MAX_STR_DIGITS;
use crate::unicode::cps;

mod held;

pub use crate::recall_settle::{DEFAULT_HARNESS, REVIEW_UNANSWERED, RULE, STATE_IDLE_SECONDS};
pub use held::*;

/// `STATE_MAX_SESSIONS`.
pub const STATE_MAX_SESSIONS: usize = 1_000;
/// `_STATE_KINDS`, in the reference's order.
pub const STATE_KINDS: [&str; 6] = [
    "session_brief",
    "prompt_recall",
    "turn_audited",
    "use",
    "review_unanswered",
    "segment_opened",
];
/// `SURFACE_BRIEF`.
pub const SURFACE_BRIEF: &str = "brief";
/// `SURFACE_RECALL`.
pub const SURFACE_RECALL: &str = "recall";
/// `_SURFACE_OF_KIND`, in the reference's order.
pub const SURFACE_OF_KIND: [(&str, &str); 2] = [
    ("session_brief", SURFACE_BRIEF),
    ("prompt_recall", SURFACE_RECALL),
];
/// `AGENT_FIELD`.
pub const AGENT_FIELD: &str = "agent_id";
/// `AGENT_FROM_PAYLOAD`.
pub const AGENT_FROM_PAYLOAD: &str = "payload";
/// `AGENT_FROM_TRANSCRIPT`.
pub const AGENT_FROM_TRANSCRIPT: &str = "transcript";
/// `WITHDRAWN_FIELD`.
pub const WITHDRAWN_FIELD: &str = "withdrawn";

static NONE: PyValue = PyValue::None;

fn field<'a>(event: &'a PyValue, name: &str) -> &'a PyValue {
    event.get(name).unwrap_or(&NONE)
}

fn is_text(value: &PyValue, text: &str) -> bool {
    matches!(value, PyValue::Str(s) if *s == cps(text))
}

fn text_of(value: &PyValue) -> Option<PyStr> {
    match value {
        PyValue::Str(s) => Some(s.clone()),
        _ => None,
    }
}

/// `hash(value)` succeeds: a list or a dict, also inside a tuple, does not.
fn hashable(value: &PyValue) -> bool {
    let mut work = vec![value];
    while let Some(v) = work.pop() {
        match v {
            PyValue::List(_) | PyValue::Dict(_) => return false,
            PyValue::Tuple(items) => work.extend(items),
            _ => {}
        }
    }
    true
}

/// `is_settlement(event)`: a `use` row whose `surface` is a settlement
/// surface. `x in frozenset` hashes `x`, so an unhashable surface raises
/// `TypeError`, but only on a `use` row (`and` stops first).
pub fn is_settlement(event: &PyValue) -> Result<bool, PyError> {
    if !is_text(field(event, "kind"), "use") {
        return Ok(false);
    }
    let surface = field(event, "surface");
    if !hashable(surface) {
        return Err(PyError::TypeError);
    }
    Ok(is_text(surface, SURFACE_BRIEF) || is_text(surface, SURFACE_RECALL))
}

/// `agent_of(event)`: a non-empty `str` `agent_id`, else None.
pub fn agent_of(event: &PyValue) -> Option<&PyStr> {
    match field(event, AGENT_FIELD) {
        PyValue::Str(s) if !s.is_empty() => Some(s),
        _ => None,
    }
}

/// `taken_back(event)`: a subagent's `segment_opened` row whose
/// `withdrawn` is a list.
pub fn taken_back(event: &PyValue) -> bool {
    is_text(field(event, "kind"), "segment_opened")
        && agent_of(event).is_some()
        && matches!(field(event, WITHDRAWN_FIELD), PyValue::List(_))
}

/// `Subagent`: a subagent of a session as a hook row names it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Subagent {
    pub agent_id: PyStr,
    pub agent_type: Option<PyStr>,
    pub found_by: PyStr,
}

impl Subagent {
    /// `Subagent.from_payload(agent_id, agent_type)`: None for a missing
    /// or empty `agent_id`; an empty `agent_type` is None.
    pub fn from_payload(agent_id: Option<&[u32]>, agent_type: Option<&[u32]>) -> Option<Self> {
        let agent_id = agent_id.filter(|a| !a.is_empty())?;
        Some(Subagent {
            agent_id: agent_id.to_vec(),
            agent_type: agent_type.filter(|t| !t.is_empty()).map(<[u32]>::to_vec),
            found_by: cps(AGENT_FROM_PAYLOAD),
        })
    }

    /// `fields()`: `agent_id`, `agent_from`, and `agent_type` when it is
    /// truthy, in that order.
    pub fn fields(&self) -> Vec<(PyStr, PyStr)> {
        let mut out = vec![
            (cps(AGENT_FIELD), self.agent_id.clone()),
            (cps("agent_from"), self.found_by.clone()),
        ];
        if let Some(t) = self.agent_type.as_ref().filter(|t| !t.is_empty()) {
            out.push((cps("agent_type"), t.clone()));
        }
        out
    }
}

/// `_Signal`: a compact SessionStart whose marker was not read yet.
#[derive(Clone, Debug, PartialEq)]
pub struct Signal {
    pub at: DateTime,
    pub shown: BTreeSet<PyStr>,
    pub deliveries: i128,
    pub event_id: Option<PyStr>,
}

/// A shown id's `(surface, shown_event_id)`.
pub type ShownBy = (PyStr, Option<PyStr>);

/// `_SessionRecall`: one (harness, session id)'s cooldown. `shown_by`
/// keeps the dict's insertion order.
#[derive(Clone, Debug)]
pub struct SessionRecall {
    /// An `int` of any size (`PyValue::Int`).
    pub segment: PyValue,
    pub started_at: DateTime,
    pub shown: BTreeSet<PyStr>,
    pub deliveries: i128,
    pub last_seen: DateTime,
    pub shown_by: OrderedMap<PyStr, ShownBy>,
    pub settled: BTreeSet<PyStr>,
    pub review_roots: BTreeSet<PyStr>,
    pub marker: Option<PyStr>,
    pub signal: Option<Signal>,
    pub closed: Option<SegmentView>,
}

impl SessionRecall {
    /// `_SessionRecall(segment, started_at, shown, deliveries, last_seen)`
    /// with the defaults after.
    pub fn new(segment: i128, started_at: DateTime, last_seen: DateTime) -> Self {
        SessionRecall {
            segment: PyValue::int(segment),
            started_at,
            shown: BTreeSet::new(),
            deliveries: 0,
            last_seen,
            shown_by: OrderedMap::default(),
            settled: BTreeSet::new(),
            review_roots: BTreeSet::new(),
            marker: None,
            signal: None,
            closed: None,
        }
    }
}

/// `SegmentView`: a snapshot of a session's segment. `shown_by` is keyed in
/// code point order; `recall_settle` reads it by key.
#[derive(Clone, Debug, PartialEq)]
pub struct SegmentView {
    /// An `int` of any size (`PyValue::Int`).
    pub segment: PyValue,
    pub started_at: DateTime,
    pub shown: BTreeSet<PyStr>,
    pub deliveries: i128,
    pub shown_by: BTreeMap<PyStr, (PyValue, PyValue)>,
    pub settled: BTreeSet<PyStr>,
    pub review_roots: BTreeSet<PyStr>,
    pub marker: Option<PyStr>,
}

fn by_value((surface, event_id): &ShownBy) -> (PyValue, PyValue) {
    (
        PyValue::Str(surface.clone()),
        event_id.clone().map_or(PyValue::None, PyValue::Str),
    )
}

fn segment_view(entry: &SessionRecall, shown: BTreeSet<PyStr>) -> SegmentView {
    SegmentView {
        segment: entry.segment.clone(),
        started_at: entry.started_at,
        shown_by: entry
            .shown_by
            .iter()
            .filter(|(k, _)| shown.contains(*k))
            .map(|(k, v)| (k.clone(), by_value(v)))
            .collect(),
        settled: entry.settled.intersection(&shown).cloned().collect(),
        shown,
        deliveries: entry.deliveries,
        review_roots: entry.review_roots.clone(),
        marker: entry.marker.clone(),
    }
}

/// `_view(entry)`.
pub fn view(entry: &SessionRecall) -> SegmentView {
    SegmentView {
        segment: entry.segment.clone(),
        started_at: entry.started_at,
        shown: entry.shown.clone(),
        deliveries: entry.deliveries,
        shown_by: entry
            .shown_by
            .iter()
            .map(|(k, v)| (k.clone(), by_value(v)))
            .collect(),
        settled: entry.settled.clone(),
        review_roots: entry.review_roots.clone(),
        marker: entry.marker.clone(),
    }
}

/// `_closing_view(entry, keep)`: the ids shown, or those also in `keep`;
/// `shown_by` and `settled` cut to them.
pub fn closing_view(entry: &SessionRecall, keep: Option<&BTreeSet<PyStr>>) -> SegmentView {
    let shown = match keep {
        None => entry.shown.clone(),
        Some(keep) => entry.shown.intersection(keep).cloned().collect(),
    };
    segment_view(entry, shown)
}

fn kept_by(entry: &SessionRecall, keep: impl Fn(&PyStr) -> bool) -> OrderedMap<PyStr, ShownBy> {
    let mut out = OrderedMap::default();
    for (k, v) in entry.shown_by.iter().filter(|(k, _)| keep(k)) {
        out.insert(k.clone(), v.clone());
    }
    out
}

/// `_restart(entry, segment=, at=, marker=)`.
pub fn restart(entry: &mut SessionRecall, segment: PyValue, at: DateTime, marker: Option<PyStr>) {
    entry.segment = segment;
    entry.started_at = at;
    entry.marker = marker;
    entry.signal = None;
    entry.shown = BTreeSet::new();
    entry.shown_by = OrderedMap::default();
    entry.settled = BTreeSet::new();
    entry.deliveries = 0;
}

/// `_reopen(entry, signal=, segment=, at=, marker=)`: what was shown and
/// delivered since `signal` is the new segment's. The count may go below
/// zero, as the reference's `int` does.
pub fn reopen(
    entry: &mut SessionRecall,
    signal: &Signal,
    segment: PyValue,
    at: DateTime,
    marker: Option<PyStr>,
) {
    let shown: BTreeSet<PyStr> = entry.shown.difference(&signal.shown).cloned().collect();
    entry.segment = segment;
    entry.started_at = at;
    entry.marker = marker;
    entry.signal = None;
    entry.shown_by = kept_by(entry, |k| shown.contains(k));
    entry.settled = entry.settled.intersection(&shown).cloned().collect();
    entry.shown = shown;
    entry.deliveries -= signal.deliveries;
}

/// `_hold_signal(entry, at=, event_id=)`.
pub fn hold_signal(entry: &mut SessionRecall, at: DateTime, event_id: Option<PyStr>) {
    entry.signal = Some(Signal {
        at,
        shown: entry.shown.clone(),
        deliveries: entry.deliveries,
        event_id,
    });
}

/// `_withdraw(entry, ids)`: the signal ends and `ids` leave the segment.
pub fn withdraw(entry: &mut SessionRecall, ids: &[PyStr]) {
    let gone: BTreeSet<&PyStr> = ids.iter().collect();
    entry.signal = None;
    entry.shown.retain(|k| !gone.contains(k));
    entry.shown_by = kept_by(entry, |k| !gone.contains(k));
    entry.settled.retain(|k| !gone.contains(k));
}

/// `str(kind)`: `py_str`, after the two raises CPython's `str` and
/// `repr` add, the first met in the order `repr` writes: ValueError for
/// an `int` past 4,300 digits (the sign aside), also inside a container,
/// and RecursionError past `repr`'s depth bound.
fn kind_name(kind: &PyValue) -> Result<PyStr, PyError> {
    let mut work = vec![(kind, 1usize)];
    while let Some((value, depth)) = work.pop() {
        if depth > crate::json::MAX_DEPTH {
            return Err(PyError::RecursionError);
        }
        match value {
            PyValue::Int(digits) if digits.trim_start_matches('-').len() > INT_MAX_STR_DIGITS => {
                return Err(PyError::ValueError);
            }
            PyValue::List(items) | PyValue::Tuple(items) => {
                work.extend(items.iter().rev().map(|item| (item, depth + 1)));
            }
            PyValue::Dict(items) => {
                for (key, item) in items.iter().rev() {
                    work.push((item, depth + 1));
                    work.push((key, depth + 1));
                }
            }
            _ => {}
        }
    }
    py_str(kind)
}

fn show(entry: &mut SessionRecall, id: PyStr, by: ShownBy) {
    entry.shown.insert(id.clone());
    if entry.shown_by.get(&id).is_none() {
        entry.shown_by.insert(id, by);
    }
}

/// `_replay(entry, event, stamp)`: one of the session's rows, other than
/// a `review_unanswered`, applied in log order. `str(kind)` raises
/// ValueError on an `int` past 4,300 digits and RecursionError on a kind
/// nested past `repr`'s bound ([`kind_name`]).
pub fn replay(entry: &mut SessionRecall, event: &PyValue, stamp: DateTime) -> Result<(), PyError> {
    let number = field(event, "segment");
    if matches!(number, PyValue::Int(_)) && *number != entry.segment {
        let marker = text_of(field(event, "marker")).or_else(|| entry.marker.clone());
        let opened_at = match field(event, "opened_at") {
            PyValue::Str(t) => parse_event_ts(t),
            _ => None,
        };
        match (opened_at, entry.signal.clone()) {
            (Some(at), Some(signal)) => reopen(entry, &signal, number.clone(), at, marker),
            (at, _) => restart(entry, number.clone(), at.unwrap_or(stamp), marker),
        }
    }
    let kind = field(event, "kind");
    let shown_event = text_of(field(event, "event_id"));
    if matches!(field(event, "marker_absent"), PyValue::Bool(true)) {
        let held = if is_text(kind, "session_brief") {
            shown_event.clone()
        } else {
            None
        };
        hold_signal(entry, stamp, held);
    }
    if is_text(kind, "segment_opened") {
        let brief_event = text_of(field(event, "brief_event_id"));
        for id in str_ids(field(event, WITHDRAWN_FIELD)) {
            show(entry, id, (cps(SURFACE_BRIEF), brief_event.clone()));
        }
        return Ok(());
    }
    if is_text(kind, "turn_audited") {
        return Ok(());
    }
    if is_text(kind, "use") {
        entry.settled.extend(str_ids(field(event, "ids")));
        return Ok(());
    }
    let name = kind_name(kind)?;
    let surface = SURFACE_OF_KIND
        .iter()
        .find(|(k, _)| cps(k) == name)
        .map_or(SURFACE_RECALL, |(_, s)| s);
    let surface = cps(surface);
    if is_text(kind, "session_brief") {
        for id in str_ids(field(event, "ids")) {
            show(entry, id, (surface.clone(), shown_event.clone()));
        }
        return Ok(());
    }
    let delivered = match event.get("shown_ids") {
        Some(ids) => str_ids(ids),
        None => match field(event, "top_hits") {
            PyValue::List(hits) => match hits.first() {
                Some(first @ PyValue::Dict(_)) => text_of(field(first, "id")).into_iter().collect(),
                _ => Vec::new(),
            },
            _ => Vec::new(),
        },
    };
    for id in delivered {
        show(entry, id, (surface.clone(), shown_event.clone()));
    }
    entry.deliveries += 1;
    Ok(())
}

/// The store's read `rebuild_session` makes:
/// `store.session_events_of_kinds(session_id, _STATE_KINDS)`, the rows in
/// the order the store yields them and the raise that stopped the read,
/// if any (raised after the rows before it are walked).
pub trait SessionEvents {
    type Error;
    fn session_events_of_kinds(
        &mut self,
        session_id: &[u32],
        kinds: &[&str],
    ) -> (Vec<PyValue>, Option<Self::Error>);
}

/// What `rebuild_session` raises: out of a row, or out of the store's read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RebuildError<E> {
    Raised(PyError),
    Read(E),
}

/// `rebuild_session(store, key, now=, agent_id=)`: the session's cooldown
/// from its rows, in the scope of the session (`agent_id` None) or of one
/// subagent.
pub fn rebuild_session<S: SessionEvents + ?Sized>(
    store: &mut S,
    key: &(PyStr, PyStr),
    now: DateTime,
    agent_id: Option<&[u32]>,
) -> Result<SessionRecall, RebuildError<S::Error>> {
    let (harness, session_id) = key;
    let (rows, failure) = store.session_events_of_kinds(session_id, &STATE_KINDS);
    let mut entry: Option<SessionRecall> = None;
    let mut review_roots = BTreeSet::new();
    for event in &rows {
        if matches!(field(event, "harness"), PyValue::Str(h) if h != harness) {
            continue;
        }
        if agent_of(event).map(Vec::as_slice) != agent_id {
            if let Some(entry) = entry.as_mut().filter(|_| agent_id.is_none()) {
                if taken_back(event) {
                    withdraw(entry, &str_ids(field(event, WITHDRAWN_FIELD)));
                }
            }
            continue;
        }
        let Some(stamp) = (match field(event, "ts") {
            PyValue::Str(t) => parse_event_ts(t),
            _ => None,
        }) else {
            continue;
        };
        let kind = field(event, "kind");
        if is_text(kind, "use") && !is_settlement(event).map_err(RebuildError::Raised)? {
            continue;
        }
        if is_text(kind, REVIEW_UNANSWERED) {
            review_roots.extend(str_ids(field(event, "root_ids")));
            continue;
        }
        let entry = entry.get_or_insert_with(|| SessionRecall::new(0, stamp, now));
        replay(entry, event, stamp).map_err(RebuildError::Raised)?;
    }
    if let Some(e) = failure {
        return Err(RebuildError::Read(e));
    }
    let mut entry = entry.unwrap_or_else(|| SessionRecall::new(0, now, now));
    entry.review_roots = review_roots;
    Ok(entry)
}
