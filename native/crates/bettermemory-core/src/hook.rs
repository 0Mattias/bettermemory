//! The Stop and UserPromptSubmit hooks of `hook.py`, their pure leaves
//! (unit hook-1a): the stdin payload reader, the pending-retrieval scan
//! and the Stop's attribution settlement. The store, the recorder, the
//! clock and the in-process session join are arguments.

use std::collections::BTreeSet;

use crate::attribution::attribute_uses;
use crate::datetime::{parse_event_ts, DateTime};
use crate::events::truthy;
use crate::json::{loads_detailed, LoadError};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::{cps, py_strip};
use crate::verify_status::micros_to_seconds;

/// `audit.ATTRIBUTION_LOOKBACK_SECONDS` (audit.py 185), which `hook.py`
/// aliases as `_ATTRIBUTION_LOOKBACK_SECONDS`. Its home is `audit.py`; it
/// is defined here until that module is ported.
pub const ATTRIBUTION_LOOKBACK_SECONDS: i64 = 600;

/// `_STDIN_PAYLOAD_CAP_BYTES`: the most stdin bytes a hook reads.
pub const STDIN_PAYLOAD_CAP_BYTES: usize = 64 * 1024;

/// Microseconds from 0001-01-01 to the Unix epoch.
const EPOCH_US: i128 = 719_162 * 86_400 * 1_000_000;

/// `_read_payload(stdin_text)`: the payload dict, an empty dict for a
/// blank, malformed or non-dict text. Only `JSONDecodeError` is caught,
/// so an integer past 4300 digits (ValueError) and a payload nested past
/// the decoder's budget (RecursionError) are errors, as in the reference.
pub fn read_payload(stdin_text: &[u32]) -> Result<PyValue, LoadError> {
    let raw = py_strip(stdin_text);
    if raw.is_empty() {
        return Ok(PyValue::Dict(Vec::new()));
    }
    match loads_detailed(raw) {
        Ok(loaded @ PyValue::Dict(_)) => Ok(loaded),
        Ok(_) | Err(LoadError::Decode) => Ok(PyValue::Dict(Vec::new())),
        Err(other) => Err(other),
    }
}

/// `_payload_text(payload, name)`: a non-empty `str` field, else None.
pub fn payload_text<'a>(payload: &'a PyValue, name: &str) -> Option<&'a PyStr> {
    match payload.get(name) {
        Some(PyValue::Str(s)) if !s.is_empty() => Some(s),
        _ => None,
    }
}

/// What the scan raises on an event, by the reference's exception.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScanError {
    /// `event.get` on an event that is not a dict: AttributeError.
    NotADict,
    /// `session not in used_session_ids` for an unhashable session:
    /// TypeError.
    Unhashable,
}

impl ScanError {
    pub fn name(self) -> &'static str {
        match self {
            ScanError::NotADict => "AttributeError",
            ScanError::Unhashable => "TypeError",
        }
    }
}

/// Python's `hash(value)` succeeds: a list or a dict, or a tuple holding
/// one, does not.
fn hashable(value: &PyValue) -> bool {
    let mut work = vec![value];
    while let Some(v) = work.pop() {
        match v {
            PyValue::List(_) | PyValue::Dict(_) => return false,
            PyValue::Tuple(items) => work.extend(items.iter()),
            _ => {}
        }
    }
    true
}

fn is_str(value: Option<&PyValue>, text: &[u32]) -> bool {
    matches!(value, Some(PyValue::Str(s)) if s == text)
}

/// `a or b or ...` over dict fields: the first truthy, else the last.
fn first_truthy<'a>(event: &'a PyValue, names: &[&str]) -> Option<&'a PyValue> {
    let mut last = None;
    for name in names {
        last = event.get(name);
        if last.is_some_and(truthy) {
            return last;
        }
    }
    last
}

fn str_items(value: Option<&PyValue>, into: &mut BTreeSet<PyStr>) {
    if let Some(PyValue::List(items)) = value {
        for item in items {
            if let PyValue::Str(s) = item {
                into.insert(s.clone());
            }
        }
    }
}

/// `ts.timestamp()` of an aware instant: seconds since the epoch, the
/// microsecond count divided once (`timedelta.total_seconds`).
fn timestamp(dt: &DateTime) -> f64 {
    let instant = dt.assume_utc().instant_us().unwrap_or(0);
    micros_to_seconds(instant - EPOCH_US)
}

/// `_pending_retrievals(events, retrieval_session_id=...,
/// used_session_ids=..., lookback_seconds=...)` with `utcnow()` as `now`:
/// the ids retrieved in the window less those used in it, in code-point
/// order. The window test is the reference's float comparison.
pub fn pending_retrievals(
    events: &[PyValue],
    now: &DateTime,
    retrieval_session_id: &[u32],
    used_session_ids: &[&[u32]],
    lookback_seconds: i64,
) -> Result<BTreeSet<PyStr>, ScanError> {
    let cutoff_ts = timestamp(now) - lookback_seconds as f64;
    let mut retrieved = BTreeSet::new();
    let mut used = BTreeSet::new();
    for event in events {
        if !matches!(event, PyValue::Dict(_)) {
            return Err(ScanError::NotADict);
        }
        let ts = match event.get("ts") {
            Some(PyValue::Str(s)) => parse_event_ts(s),
            _ => None,
        };
        match ts {
            Some(ts) if timestamp(&ts) >= cutoff_ts => {}
            _ => continue,
        }
        let session = first_truthy(event, &["session", "session_id"]);
        let kind = event.get("kind");
        let of_kind = |k: &str| is_str(kind, &cps(k));
        if of_kind("search") || of_kind("list") || of_kind("list_active") {
            if !is_str(session, retrieval_session_id) {
                continue;
            }
            str_items(
                first_truthy(event, &["returned", "memory_ids", "hit_ids"]),
                &mut retrieved,
            );
        } else if of_kind("show") {
            if !is_str(session, retrieval_session_id) {
                continue;
            }
            if let Some(PyValue::Str(mid)) = event.get("id") {
                retrieved.insert(mid.clone());
            }
        } else if of_kind("use") {
            if session.is_some_and(|s| !hashable(s)) {
                return Err(ScanError::Unhashable);
            }
            if !used_session_ids.iter().any(|u| is_str(session, u)) {
                continue;
            }
            str_items(first_truthy(event, &["ids", "memory_ids"]), &mut used);
        }
    }
    Ok(retrieved.difference(&used).cloned().collect())
}

/// `Recorder.record(kind, **fields)`, the fields in the call's order.
pub trait RowSink {
    type Error;
    fn record(&mut self, kind: &str, fields: Vec<(PyStr, PyValue)>) -> Result<(), Self::Error>;
}

/// What `_emit_hook_attributions` raises.
#[derive(Debug, PartialEq, Eq)]
pub enum EmitError<E> {
    /// The scan of the events raised.
    Scan(ScanError),
    /// The store or the recorder raised.
    Effect(E),
}

/// The arguments of `_emit_hook_attributions` other than its effects.
pub struct Turn<'a> {
    pub recent_events: &'a [PyValue],
    pub session_id: &'a [u32],
    pub assistant_response: &'a [u32],
    pub worktree_root: Option<&'a PyStr>,
    pub client_model: Option<&'a PyStr>,
    pub server_session: Option<&'a PyStr>,
    pub now: &'a DateTime,
}

fn text(s: &[u32]) -> PyValue {
    PyValue::Str(s.to_vec())
}

fn field(name: &str, value: PyValue) -> (PyStr, PyValue) {
    (cps(name), value)
}

/// `_emit_hook_attributions(...)`: settle the turn's pending retrievals.
/// `load_one` answers a memory's body, None where the store raises
/// `MemoryNotFoundError` or `TombstonedError`; `latest_session` is
/// `sessions._latest_in_process_session`, asked only when
/// `server_session` is None. Answers the ids settled.
pub fn emit_hook_attributions<E, S, L, J>(
    turn: &Turn<'_>,
    mut load_one: L,
    recorder: &mut S,
    latest_session: J,
) -> Result<BTreeSet<PyStr>, EmitError<E>>
where
    S: RowSink<Error = E>,
    L: FnMut(&PyStr) -> Result<Option<PyStr>, E>,
    J: Fn(&[PyValue], Option<&PyStr>) -> Option<PyStr>,
{
    let server_session = match turn.server_session {
        Some(s) => Some(s.clone()),
        None => latest_session(turn.recent_events, turn.worktree_root),
    };
    let retrieval_session: &[u32] = match &server_session {
        Some(s) if !s.is_empty() => s,
        _ => turn.session_id,
    };
    let pending = pending_retrievals(
        turn.recent_events,
        turn.now,
        retrieval_session,
        &[retrieval_session, turn.session_id],
        ATTRIBUTION_LOOKBACK_SECONDS,
    )
    .map_err(EmitError::Scan)?;
    let mut bodies: Vec<(PyStr, PyStr)> = Vec::new();
    for memory_id in pending {
        if let Some(body) = load_one(&memory_id).map_err(EmitError::Effect)? {
            bodies.push((memory_id, body));
        }
    }
    if bodies.is_empty() {
        return Ok(BTreeSet::new());
    }
    let matches = attribute_uses(&bodies, turn.assistant_response);
    let model = |fields: &mut Vec<(PyStr, PyValue)>| {
        if let Some(m) = turn.client_model {
            fields.push(field("client_model", text(m)));
        }
    };
    if !matches.is_empty() {
        let mut fields = vec![
            field(
                "ids",
                PyValue::List(matches.iter().map(|m| text(&m.memory_id)).collect()),
            ),
            field("outcome", PyValue::str("applied")),
            field("auto", PyValue::Bool(false)),
            field("attribution", PyValue::str("hook")),
            field(
                "claim_excerpts",
                PyValue::List(matches.iter().map(|m| text(&m.claim_excerpt)).collect()),
            ),
            field("triggered_from", PyValue::str("stop_hook")),
        ];
        model(&mut fields);
        recorder.record("use", fields).map_err(EmitError::Effect)?;
    }
    let matched: BTreeSet<&PyStr> = matches.iter().map(|m| &m.memory_id).collect();
    let unmatched: Vec<PyValue> = bodies
        .iter()
        .filter(|(id, _)| !matched.contains(id))
        .map(|(id, _)| text(id))
        .collect();
    if !unmatched.is_empty() {
        let mut fields = vec![
            field("ids", PyValue::List(unmatched)),
            field("outcome", PyValue::str("applied")),
            field("auto", PyValue::Bool(true)),
            field("attribution", PyValue::str("auto")),
            field("triggered_from", PyValue::str("stop_hook")),
        ];
        model(&mut fields);
        recorder.record("use", fields).map_err(EmitError::Effect)?;
    }
    Ok(bodies.into_iter().map(|(id, _)| id).collect())
}
