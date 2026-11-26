//! `sessions.py`'s first unit: the session join read off the event log.
//! `_OUT_OF_PROCESS_TRIGGERS`, `_disabled_scopes_from_events`,
//! `_latest_in_process_session`, `_session_of`, `harness_joined_sessions`,
//! `hook_spans`, `runs_beside`, `live_harness_sessions`, `bridged_session`
//! and `joined_sessions`, as pure functions over the events; `event_joined`
//! is [`crate::recall_settle::event_joined`], re-exported here.
//!
//! An event is a `PyValue` (a `dict` as the log reads it); `event.get(k)`
//! reads the last value of a `str` key. The events come as a slice
//! whatever the reference's annotation (a list or an iterable). Every
//! stamp `parse_event_ts` answers is aware, so two stamps compare as
//! instants. `x in _OUT_OF_PROCESS_TRIGGERS` is `frozenset` membership: an
//! unhashable `triggered_from` (a list or a dict from the log) raises
//! `TypeError` there, at the event the walk reaches it, which every
//! function answers as `Err(PyError::TypeError)`.

use std::collections::{BTreeMap, BTreeSet};

use crate::datetime::{parse_event_ts, DateTime};
use crate::events::{truthy, HARNESS_SESSION_FIELD};
use crate::pyvalue::{PyError, PyStr, PyValue};

pub use crate::recall_settle::event_joined;

/// `_OUT_OF_PROCESS_TRIGGERS`, sorted: the `triggered_from` values the
/// client-side hooks stamp.
pub const OUT_OF_PROCESS_TRIGGERS: [&str; 4] = [
    "prompt_hook",
    "session_capture",
    "session_hook",
    "stop_hook",
];

static NONE: PyValue = PyValue::None;

fn field<'a>(event: &'a PyValue, name: &str) -> &'a PyValue {
    event.get(name).unwrap_or(&NONE)
}

/// `event.get("session") or event.get("session_id")`.
fn either(event: &PyValue) -> &PyValue {
    let first = field(event, "session");
    if truthy(first) {
        first
    } else {
        field(event, "session_id")
    }
}

/// Whether Python can hash the value: no list or dict, nor a tuple
/// holding one at any depth (walked without recursion).
fn hashable(value: &PyValue) -> bool {
    let mut stack = vec![value];
    while let Some(v) = stack.pop() {
        match v {
            PyValue::List(_) | PyValue::Dict(_) => return false,
            PyValue::Tuple(items) => stack.extend(items),
            _ => {}
        }
    }
    true
}

/// `event.get("triggered_from") in _OUT_OF_PROCESS_TRIGGERS`: true only
/// for a `str` equal to a member; TypeError for an unhashable value.
pub fn is_out_of_process(event: &PyValue) -> Result<bool, PyError> {
    let value = field(event, "triggered_from");
    if !hashable(value) {
        return Err(PyError::TypeError);
    }
    Ok(match value {
        PyValue::Str(s) => OUT_OF_PROCESS_TRIGGERS.iter().any(|t| spells(s, t)),
        _ => false,
    })
}

/// Whether the code points spell the ASCII `text`.
fn spells(s: &[u32], text: &str) -> bool {
    s.len() == text.len() && s.iter().zip(text.bytes()).all(|(&c, b)| c == u32::from(b))
}

/// `value == text` for a `str` `text`: only a `str` of those code points.
fn str_eq(value: &PyValue, text: &[u32]) -> bool {
    matches!(value, PyValue::Str(s) if s == text)
}

/// The instant of an aware stamp, in microseconds.
fn at(dt: &DateTime) -> i128 {
    dt.instant_us().unwrap_or_else(|| dt.wall_micros())
}

fn stamp_of(event: &PyValue) -> Option<DateTime> {
    match field(event, "ts") {
        PyValue::Str(text) => parse_event_ts(text),
        _ => None,
    }
}

/// `_session_of`: the `session`, else the `session_id`, when that is a
/// `str` (the empty one included where `or` hands it back).
pub fn session_of(event: &PyValue) -> Option<&PyStr> {
    match either(event) {
        PyValue::Str(s) => Some(s),
        _ => None,
    }
}

fn latest_over<'a>(
    events: impl DoubleEndedIterator<Item = &'a PyValue>,
    worktree_root: Option<&[u32]>,
) -> Result<Option<PyStr>, PyError> {
    let mut fallback: Option<&PyStr> = None;
    for event in events.rev() {
        if is_out_of_process(event)? {
            continue;
        }
        let PyValue::Str(session) = either(event) else {
            continue;
        };
        match worktree_root {
            Some(root) if !str_eq(field(event, "worktree_root"), root) => {}
            _ => return Ok(Some(session.clone())),
        }
        if fallback.is_none() {
            fallback = Some(session);
        }
    }
    Ok(fallback.cloned())
}

/// `_latest_in_process_session`: the session of the newest event no hook
/// wrote, the newest one stamped with `worktree_root` first when it is
/// given, else the newest of any worktree.
pub fn latest_in_process_session(
    events: &[PyValue],
    worktree_root: Option<&[u32]>,
) -> Result<Option<PyStr>, PyError> {
    latest_over(events.iter(), worktree_root)
}

/// `_disabled_scopes_from_events`: the scopes the anchor session's
/// `scope_disable` rows add and its later `scope_enable` rows remove, in
/// log order. The anchor is `server_session`, else
/// `_latest_in_process_session`; empty without one.
pub fn disabled_scopes_from_events(
    events: &[PyValue],
    worktree_root: Option<&[u32]>,
    server_session: Option<&[u32]>,
) -> Result<BTreeSet<PyStr>, PyError> {
    let found;
    let anchor = match server_session {
        Some(s) => s,
        None => {
            found = latest_in_process_session(events, worktree_root)?;
            match &found {
                Some(s) => s.as_slice(),
                None => return Ok(BTreeSet::new()),
            }
        }
    };
    let mut disabled = BTreeSet::new();
    for event in events {
        if !str_eq(either(event), anchor) {
            continue;
        }
        let PyValue::Str(kind) = field(event, "kind") else {
            continue;
        };
        let disable = spells(kind, "scope_disable");
        if !disable && !spells(kind, "scope_enable") {
            continue;
        }
        let PyValue::Str(scope) = field(event, "scope") else {
            continue;
        };
        if disable {
            disabled.insert(scope.clone());
        } else {
            disabled.remove(scope);
        }
    }
    Ok(disabled)
}

/// `not harness_session`.
fn given(harness_session: Option<&[u32]>) -> Option<&[u32]> {
    harness_session.filter(|h| !h.is_empty())
}

/// `harness_joined_sessions`: the sessions of the events no hook wrote
/// whose harness stamp is `harness_session`; empty when it is empty.
pub fn harness_joined_sessions(
    events: &[PyValue],
    harness_session: Option<&[u32]>,
) -> Result<BTreeSet<PyStr>, PyError> {
    let mut found = BTreeSet::new();
    let Some(harness) = given(harness_session) else {
        return Ok(found);
    };
    for event in events {
        if is_out_of_process(event)? {
            continue;
        }
        if !str_eq(field(event, HARNESS_SESSION_FIELD), harness) {
            continue;
        }
        if let Some(session) = session_of(event) {
            found.insert(session.clone());
        }
    }
    Ok(found)
}

/// What `hook_spans` answers: per harness session, the first and the last
/// stamp of its hook rows, in the order the sessions first appear.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct HookSpans {
    entries: Vec<(PyStr, (DateTime, DateTime))>,
    index: BTreeMap<PyStr, usize>,
}

impl HookSpans {
    /// `spans.get(session)`.
    pub fn get(&self, session: &[u32]) -> Option<&(DateTime, DateTime)> {
        self.index.get(session).map(|&i| &self.entries[i].1)
    }

    /// `spans[session] = span`, keeping a known session's place.
    pub fn insert(&mut self, session: PyStr, span: (DateTime, DateTime)) {
        match self.index.get(&session) {
            Some(&i) => self.entries[i].1 = span,
            None => {
                self.index.insert(session.clone(), self.entries.len());
                self.entries.push((session, span));
            }
        }
    }

    /// The items in insertion order.
    pub fn entries(&self) -> &[(PyStr, (DateTime, DateTime))] {
        &self.entries
    }
}

/// `hook_spans`: per non-empty `str` session of the hook rows with a
/// stamp, the least and the greatest stamp as instants; on a tie the
/// earlier row's stamp stays (`min` and `max` keep their first argument).
pub fn hook_spans(events: &[PyValue]) -> Result<HookSpans, PyError> {
    let mut spans = HookSpans::default();
    for event in events {
        if !is_out_of_process(event)? {
            continue;
        }
        let PyValue::Str(session) = either(event) else {
            continue;
        };
        let Some(stamp) = stamp_of(event) else {
            continue;
        };
        if session.is_empty() {
            continue;
        }
        let span = match spans.get(session) {
            None => (stamp, stamp),
            Some(&(first, last)) => (
                if at(&stamp) < at(&first) {
                    stamp
                } else {
                    first
                },
                if at(&stamp) > at(&last) { stamp } else { last },
            ),
        };
        spans.insert(session.clone(), span);
    }
    Ok(spans)
}

/// `runs_beside`: `other` has a span, and `conversation` has none or
/// `other`'s last stamp is at or after `conversation`'s first.
pub fn runs_beside(spans: &HookSpans, other: &[u32], conversation: &[u32]) -> bool {
    let Some(span) = spans.get(other) else {
        return false;
    };
    match spans.get(conversation) {
        None => true,
        Some(own) => at(&span.1) >= at(&own.0),
    }
}

/// `live_harness_sessions`: the other sessions of `hook_spans` that run
/// beside `harness_session`; empty when it is empty.
pub fn live_harness_sessions(
    events: &[PyValue],
    harness_session: Option<&[u32]>,
) -> Result<BTreeSet<PyStr>, PyError> {
    let Some(harness) = given(harness_session) else {
        return Ok(BTreeSet::new());
    };
    let spans = hook_spans(events)?;
    Ok(spans
        .entries()
        .iter()
        .map(|(session, _)| session)
        .filter(|s| s.as_slice() != harness && runs_beside(&spans, s, harness))
        .cloned()
        .collect())
}

/// `bridged_session`: the newest in-process session stamped with
/// `harness_session`; else `_latest_in_process_session` over the events
/// of every session not set aside: those stamped with a harness session
/// that runs beside this one, or every stamped session when two or more
/// stamps other than this one's wrote (in `worktree_root`, when given) at
/// or after this conversation's first hook row.
pub fn bridged_session(
    events: &[PyValue],
    harness_session: Option<&[u32]>,
    worktree_root: Option<&[u32]>,
) -> Result<Option<PyStr>, PyError> {
    let Some(harness) = given(harness_session) else {
        return latest_in_process_session(events, worktree_root);
    };
    let mut stamped_by: BTreeMap<&PyStr, &PyStr> = BTreeMap::new();
    for event in events.iter().rev() {
        if is_out_of_process(event)? {
            continue;
        }
        let PyValue::Str(stamped) = field(event, HARNESS_SESSION_FIELD) else {
            continue;
        };
        if stamped.is_empty() {
            continue;
        }
        let Some(session) = session_of(event) else {
            continue;
        };
        if stamped.as_slice() == harness {
            return Ok(Some(session.clone()));
        }
        stamped_by.entry(session).or_insert(stamped);
    }
    if stamped_by.is_empty() {
        return latest_in_process_session(events, worktree_root);
    }
    let spans = hook_spans(events)?;
    let mut foreign: BTreeSet<&PyStr> = stamped_by
        .iter()
        .filter(|(_, stamped)| runs_beside(&spans, stamped, harness))
        .map(|(&session, _)| session)
        .collect();
    if let Some(own) = spans.get(harness) {
        let mut stamps: BTreeSet<&PyStr> = BTreeSet::new();
        for event in events {
            if is_out_of_process(event)? {
                continue;
            }
            let Some(session) = session_of(event) else {
                continue;
            };
            let Some(&stamped) = stamped_by.get(session) else {
                continue;
            };
            if foreign.contains(session) {
                continue;
            }
            if let Some(root) = worktree_root {
                if !str_eq(field(event, "worktree_root"), root) {
                    continue;
                }
            }
            if stamp_of(event).is_some_and(|stamp| at(&stamp) >= at(&own.0)) {
                stamps.insert(stamped);
            }
        }
        if stamps.len() > 1 {
            foreign = stamped_by.keys().copied().collect();
        }
    }
    let kept = events
        .iter()
        .filter(|e| session_of(e).is_none_or(|s| !foreign.contains(s)));
    latest_over(kept, worktree_root)
}

/// `joined_sessions`: the harness join's sessions, else the bridge's one.
pub fn joined_sessions(
    events: &[PyValue],
    harness_session: Option<&[u32]>,
    worktree_root: Option<&[u32]>,
) -> Result<BTreeSet<PyStr>, PyError> {
    let found = harness_joined_sessions(events, harness_session)?;
    if !found.is_empty() {
        return Ok(found);
    }
    Ok(bridged_session(events, harness_session, worktree_root)?
        .into_iter()
        .collect())
}
