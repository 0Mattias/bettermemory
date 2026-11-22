//! `recall_settle.py`'s first unit: the Stop's settlement of what the
//! brief and the prompt recall showed (`_used_in_segment`,
//! `settle_shown`, `settle_unused`), the previous audit's stamp
//! (`previous_audit_at`) and the count of review chains left open
//! (`review_unanswered`).
//!
//! The store's event reads and record loads, the recorder, the recall
//! state and the in-process session join are traits; `review.find_chain`
//! and the recorder `settle_unused` builds are closures; the clock is an
//! argument. An event is a `PyValue` (a `dict` as the log reads it).
//! Every datetime handed in is aware, as the reference's callers make
//! them (`utcnow` and its arithmetic). The second unit, `_probe_message`
//! and `settle_interrupted`, is [`interrupted`].

pub mod interrupted;

use std::collections::{BTreeMap, BTreeSet};

use crate::attribution::attribute_uses;
use crate::datetime::{parse_event_ts, DateTime};
use crate::events::{str_ids, truthy, HARNESS_SESSION_FIELD};
use crate::pyvalue::{PyStr, PyValue};
use crate::records::is_valid_ulid;
use crate::unicode::cps;

/// `recall_state.RULE` (`recall_state.py`).
pub const RULE: &str = "rs-v8";
/// `recall_state.DEFAULT_HARNESS` (`recall_state.py`).
pub const DEFAULT_HARNESS: &str = "claude-code";
/// `recall_state.STATE_IDLE_SECONDS` (`recall_state.py`).
pub const STATE_IDLE_SECONDS: i64 = 24 * 3600;
/// `recall_state.REVIEW_UNANSWERED` (`recall_state.py`).
pub const REVIEW_UNANSWERED: &str = "review_unanswered";
/// `audit.REAUDIT_DEDUP_WINDOW_SECONDS` (`audit.py`).
pub const REAUDIT_DEDUP_WINDOW_SECONDS: i64 = 3600;
/// `review.REVIEW_ISSUED` (`review.py`).
pub const REVIEW_ISSUED: &str = "review_issued";

/// What the reference raises out of this unit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SettleError {
    /// `now - timedelta(...)` before year 1.
    OverflowError,
    /// An unhashable session value tested against the `sessions` set.
    TypeError,
}

/// Why `store.load_one` refused: `MemoryNotFoundError`, `TombstonedError`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LoadFailure {
    NotFound,
    Tombstoned,
}

/// The store's reads this unit makes.
pub trait SettleStore {
    /// `store.events_of_kinds(kinds, since=since)`.
    fn events_of_kinds(&mut self, kinds: &[&str], since: &DateTime) -> Vec<PyValue>;
    /// `store.events_since(since)`.
    fn events_since(&mut self, since: &DateTime) -> Vec<PyValue>;
    /// `store.load_one(id).body`.
    fn load_one(&mut self, id: &[u32]) -> Result<PyStr, LoadFailure>;
    /// `store.has_memories()`. By default not offered (None).
    fn has_memories(&mut self) -> Option<bool> {
        None
    }
}

/// `events.Recorder.record(kind, **fields)`, the fields in order.
pub trait Recorder {
    fn record(&mut self, kind: &str, fields: Vec<(PyValue, PyValue)>);
    /// Whether `events.suppressed` is in force (`records` answers false).
    fn suppressed(&self) -> bool {
        false
    }
    /// `record` where the reference lets the write raise; the default
    /// never does.
    fn try_record(
        &mut self,
        kind: &str,
        fields: Vec<(PyValue, PyValue)>,
    ) -> Result<(), crate::recall_prompt::PyException> {
        self.record(kind, fields);
        Ok(())
    }
}

/// A session key: `(harness, session_id)`.
pub type Key = (PyStr, PyStr);

/// The segment `RecallState.segment` and `take_closed` answer: its start,
/// its number, the ids settled in it, the review roots counted, and each
/// shown id's `(surface, shown_event_id)`.
#[derive(Clone, Debug, PartialEq)]
pub struct SegmentView {
    pub started_at: DateTime,
    pub segment: PyValue,
    pub settled: BTreeSet<PyStr>,
    pub review_roots: BTreeSet<PyStr>,
    pub shown_by: BTreeMap<PyStr, (PyValue, PyValue)>,
}

/// The `RecallState` calls this unit makes.
pub trait SettleState {
    fn segment(&mut self, key: &Key, now: &DateTime) -> SegmentView;
    fn take_closed(&mut self, key: &Key) -> Option<SegmentView>;
    fn note_settled(&mut self, key: &Key, ids: &[PyStr], now: &DateTime);
    fn note_review_roots(&mut self, key: &Key, roots: &[PyStr], now: &DateTime);
    /// `RecallState.forget(key)`: drop the session's held state.
    fn forget(&mut self, _key: &Key) {}
}

/// `sessions.joined_sessions(events, harness_session=, worktree_root=)`.
pub trait Sessions {
    fn joined_sessions(
        &mut self,
        events: &[PyValue],
        harness_session: &[u32],
        worktree_root: Option<&[u32]>,
    ) -> BTreeSet<PyStr>;
}

static NONE: PyValue = PyValue::None;

fn field<'a>(event: &'a PyValue, name: &str) -> &'a PyValue {
    event.get(name).unwrap_or(&NONE)
}

fn is_str(value: &PyValue, text: &str) -> bool {
    matches!(value, PyValue::Str(s) if *s == cps(text))
}

/// `a or b` of two event fields.
fn either<'a>(event: &'a PyValue, a: &str, b: &str) -> &'a PyValue {
    let first = field(event, a);
    if truthy(first) {
        first
    } else {
        field(event, b)
    }
}

/// An instant for ordering; every value here is aware.
fn at(dt: &DateTime) -> i128 {
    dt.instant_us().unwrap_or_else(|| dt.wall_micros())
}

fn stamp_of(event: &PyValue) -> Option<DateTime> {
    match field(event, "ts") {
        PyValue::Str(text) => parse_event_ts(text),
        _ => None,
    }
}

fn seconds_before(now: &DateTime, seconds: i64) -> Result<DateTime, SettleError> {
    now.minus_micros(i128::from(seconds) * 1_000_000)
        .map_err(|_| SettleError::OverflowError)
}

/// `sessions.event_joined`: by the harness stamp when it names this
/// conversation, else by `_session_of` (a `str` `session`, else
/// `session_id`) among `sessions`.
pub fn event_joined(event: &PyValue, harness_session: &[u32], sessions: &BTreeSet<PyStr>) -> bool {
    if !harness_session.is_empty()
        && matches!(field(event, HARNESS_SESSION_FIELD), PyValue::Str(s) if s == harness_session)
    {
        return true;
    }
    matches!(either(event, "session", "session_id"), PyValue::Str(s) if sessions.contains(s))
}

/// `_used_in_segment`: the ids any joined `use` event at or after `since`
/// names (`ids`, else `memory_ids`).
pub fn used_in_segment(
    events: &[PyValue],
    since: &DateTime,
    harness_session: &[u32],
    sessions: &BTreeSet<PyStr>,
) -> BTreeSet<PyStr> {
    let mut used = BTreeSet::new();
    for event in events {
        if !is_str(field(event, "kind"), "use") {
            continue;
        }
        match stamp_of(event) {
            Some(stamp) if at(&stamp) >= at(since) => {}
            _ => continue,
        }
        if event_joined(event, harness_session, sessions) {
            used.extend(str_ids(either(event, "ids", "memory_ids")));
        }
    }
    used
}

fn key_of(harness: Option<&[u32]>, session_id: &[u32]) -> Key {
    let harness = match harness {
        Some(h) if !h.is_empty() => h.to_vec(),
        _ => cps(DEFAULT_HARNESS),
    };
    (harness, session_id.to_vec())
}

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn ids_value(ids: &[PyStr]) -> PyValue {
    PyValue::List(ids.iter().cloned().map(PyValue::Str).collect())
}

/// The arguments of `settle_shown` past the store, recorder and state.
pub struct Shown<'a> {
    pub session_id: &'a [u32],
    pub assistant_response: &'a [u32],
    pub sessions: &'a [PyStr],
    pub harness: Option<&'a [u32]>,
    pub exclude: &'a [PyStr],
    pub prompt_id: Option<&'a [u32]>,
    pub client_model: Option<&'a [u32]>,
    pub now: &'a DateTime,
}

/// `settle_shown`: the segment's shown ids not settled, not excluded and
/// not used yet, matched against the reply; one `use` row per match; the
/// ids settled.
pub fn settle_shown(
    store: &mut dyn SettleStore,
    recorder: &mut dyn Recorder,
    state: &mut dyn SettleState,
    a: &Shown<'_>,
) -> Vec<PyStr> {
    let key = key_of(a.harness, a.session_id);
    let view = state.segment(&key, a.now);
    let exclude: BTreeSet<&PyStr> = a.exclude.iter().collect();
    let pending: Vec<&PyStr> = view
        .shown_by
        .keys()
        .filter(|id| !view.settled.contains(*id) && !exclude.contains(id))
        .collect();
    if pending.is_empty() || a.assistant_response.is_empty() {
        return Vec::new();
    }
    let mut sessions: BTreeSet<PyStr> = a.sessions.iter().cloned().collect();
    sessions.insert(key.1.clone());
    let events = store.events_of_kinds(&["use"], &view.started_at);
    let used = used_in_segment(&events, &view.started_at, &key.1, &sessions);
    let mut bodies: Vec<(PyStr, PyStr)> = Vec::new();
    for id in pending {
        if used.contains(id) {
            continue;
        }
        if let Ok(body) = store.load_one(id) {
            bodies.push((id.clone(), body));
        }
    }
    if bodies.is_empty() {
        return Vec::new();
    }
    let mut settled = Vec::new();
    for m in attribute_uses(&bodies, a.assistant_response) {
        let (surface, shown_event_id) = view.shown_by[&m.memory_id].clone();
        let mut fields = vec![
            (s("ids"), ids_value(std::slice::from_ref(&m.memory_id))),
            (s("outcome"), s("applied")),
            (s("auto"), PyValue::Bool(false)),
            (s("attribution"), s("hook")),
            (
                s("claim_excerpts"),
                PyValue::List(vec![PyValue::Str(m.claim_excerpt)]),
            ),
            (s("surface"), surface),
            (s("shown_event_id"), shown_event_id),
            (s("segment"), view.segment.clone()),
            (s("harness"), PyValue::Str(key.0.clone())),
            (s("triggered_from"), s("stop_hook")),
            (s("rule"), s(RULE)),
        ];
        if let Some(p) = a.prompt_id.filter(|p| !p.is_empty()) {
            fields.push((s("prompt_id"), PyValue::Str(p.to_vec())));
        }
        if let Some(c) = a.client_model {
            fields.push((s("client_model"), PyValue::Str(c.to_vec())));
        }
        recorder.record("use", fields);
        settled.push(m.memory_id);
    }
    if !settled.is_empty() {
        state.note_settled(&key, &settled, a.now);
    }
    settled
}

/// The recorder `settle_unused` builds: `Recorder(store=, session_id=,
/// enabled=, worktree_root=, triggered_from=)`.
pub struct RecorderSpec<'a> {
    pub session_id: &'a [u32],
    pub enabled: bool,
    pub worktree_root: Option<&'a [u32]>,
    pub triggered_from: &'a [u32],
}

/// `settle_unused`: the closed segment's shown ids that no Stop settled,
/// no joined `show` opened and no joined `use` attested, each recorded as
/// one `unused` row; the ids recorded.
#[allow(clippy::too_many_arguments)]
pub fn settle_unused<R: Recorder>(
    store: &mut dyn SettleStore,
    state: &mut dyn SettleState,
    joins: &mut dyn Sessions,
    key: &Key,
    now: &DateTime,
    worktree_root: Option<&[u32]>,
    triggered_from: &[u32],
    telemetry: bool,
    make_recorder: impl FnOnce(&RecorderSpec<'_>) -> R,
) -> Result<Vec<PyStr>, SettleError> {
    let Some(closed) = state.take_closed(key) else {
        return Ok(Vec::new());
    };
    let pending: Vec<&PyStr> = closed
        .shown_by
        .keys()
        .filter(|id| !closed.settled.contains(*id))
        .collect();
    if pending.is_empty() {
        return Ok(Vec::new());
    }
    let recent = store.events_since(&seconds_before(now, REAUDIT_DEDUP_WINDOW_SECONDS)?);
    let mut sessions = joins.joined_sessions(&recent, &key.1, worktree_root);
    sessions.insert(key.1.clone());
    let events = store.events_of_kinds(&["show", "use"], &closed.started_at);
    let mut opened: BTreeSet<PyStr> = BTreeSet::new();
    for event in &events {
        if !is_str(field(event, "kind"), "show") {
            continue;
        }
        match stamp_of(event) {
            Some(stamp) if at(&stamp) >= at(&closed.started_at) => {}
            _ => continue,
        }
        if event_joined(event, &key.1, &sessions) {
            if let PyValue::Str(id) = field(event, "id") {
                opened.insert(id.clone());
            }
        }
    }
    let attested = used_in_segment(&events, &closed.started_at, &key.1, &sessions);
    let mut recorder = make_recorder(&RecorderSpec {
        session_id: &key.1,
        enabled: telemetry,
        worktree_root,
        triggered_from,
    });
    let mut settled = Vec::new();
    for id in pending {
        if opened.contains(id) || attested.contains(id) {
            continue;
        }
        let (surface, shown_event_id) = closed.shown_by[id].clone();
        recorder.record(
            "use",
            vec![
                (s("ids"), ids_value(std::slice::from_ref(id))),
                (s("outcome"), s("unused")),
                (s("auto"), PyValue::Bool(false)),
                (s("attribution"), s("hook")),
                (s("surface"), surface),
                (s("shown_event_id"), shown_event_id),
                (s("segment"), closed.segment.clone()),
                (s("harness"), PyValue::Str(key.0.clone())),
                (s("rule"), s(RULE)),
            ],
        );
        settled.push(id.clone());
    }
    Ok(settled)
}

/// `previous_audit_at`: the newest stamp of the session's `turn_audited`
/// rows inside the idle window; the first of equal instants.
pub fn previous_audit_at(
    store: &mut dyn SettleStore,
    session_id: &[u32],
    now: &DateTime,
) -> Result<Option<DateTime>, SettleError> {
    let since = seconds_before(now, STATE_IDLE_SECONDS)?;
    let mut latest: Option<DateTime> = None;
    for event in store.events_of_kinds(&["turn_audited"], &since) {
        if !matches!(either(&event, "session", "session_id"), PyValue::Str(x) if x == session_id) {
            continue;
        }
        if let Some(stamp) = stamp_of(&event) {
            if latest.as_ref().is_none_or(|l| at(&stamp) > at(l)) {
                latest = Some(stamp);
            }
        }
    }
    Ok(latest)
}

/// Whether Python can hash the value (`x in set` raises otherwise).
fn hashable(value: &PyValue) -> bool {
    match value {
        PyValue::List(_) | PyValue::Dict(_) => false,
        PyValue::Tuple(items) => items.iter().all(hashable),
        _ => true,
    }
}

/// `review_unanswered`: the chain roots of the joined `review_issued`
/// rows at or after `since`, not counted yet, that `find_chain` reads
/// open and not refused for a standing record; one row when any.
/// `find_chain(root)` answers `(open, standing_refused)`.
#[allow(clippy::too_many_arguments)]
pub fn review_unanswered(
    store: &mut dyn SettleStore,
    recorder: &mut dyn Recorder,
    state: &mut dyn SettleState,
    session_id: &[u32],
    sessions: &BTreeSet<PyStr>,
    since: &DateTime,
    harness: Option<&[u32]>,
    now: &DateTime,
    mut find_chain: impl FnMut(&PyStr) -> (bool, bool),
) -> Result<Vec<PyStr>, SettleError> {
    if sessions.is_empty() {
        return Ok(Vec::new());
    }
    let key = key_of(harness, session_id);
    let view = state.segment(&key, now);
    let mut roots: Vec<PyStr> = Vec::new();
    for event in store.events_of_kinds(&[REVIEW_ISSUED], since) {
        let who = either(&event, "session", "session_id");
        if !hashable(who) {
            return Err(SettleError::TypeError);
        }
        if !matches!(who, PyValue::Str(x) if sessions.contains(x)) {
            continue;
        }
        match stamp_of(&event) {
            Some(stamp) if at(&stamp) >= at(since) => {}
            _ => continue,
        }
        let root = match field(&event, "parent_review_id") {
            PyValue::Str(p) if is_valid_ulid(p) => p,
            _ => match field(&event, "review_id") {
                PyValue::Str(o) if is_valid_ulid(o) => o,
                _ => continue,
            },
        };
        if view.review_roots.contains(root) || roots.contains(root) {
            continue;
        }
        roots.push(root.clone());
    }
    let open_roots: Vec<PyStr> = roots
        .into_iter()
        .filter(|root| {
            let (open, standing_refused) = find_chain(root);
            open && !standing_refused
        })
        .collect();
    if open_roots.is_empty() {
        return Ok(Vec::new());
    }
    let mut review_sessions: Vec<PyStr> = sessions.iter().cloned().collect();
    review_sessions.sort();
    recorder.record(
        REVIEW_UNANSWERED,
        vec![
            (s("count"), PyValue::int(open_roots.len() as i128)),
            (s("root_ids"), ids_value(&open_roots)),
            (s("session_id"), PyValue::Str(key.1.clone())),
            (s("review_sessions"), ids_value(&review_sessions)),
            (s("segment"), view.segment.clone()),
            (s("harness"), PyValue::Str(key.0.clone())),
            (s("triggered_from"), s("stop_hook")),
        ],
    );
    state.note_review_roots(&key, &open_roots, now);
    Ok(open_roots)
}
