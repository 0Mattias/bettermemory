//! `recall_state.py`'s second unit: `RecallState`, the process-wide table
//! of sessions behind one lock, and the two traits its callers read it
//! through (`recall_settle::SettleState`, `recall::choose::ClaimState`),
//! implemented on a state bound to a store ([`Bound`]).
//!
//! The table is in last-seen order, least recent first, as the
//! reference's `OrderedDict` is: a sweep drops idle sessions from the
//! front and stops at the first live one, and the cap drops from the
//! front. The store is an argument of each call that may rebuild a
//! session, and the rebuild runs outside the lock. A lock poisoned by a
//! panic on another thread is recovered as it stands: a panic is the
//! caller's fault, not an answer of the reference, whose lock an
//! exception releases.

use std::collections::{BTreeMap, HashMap};
use std::sync::{Mutex, MutexGuard, PoisonError};

use super::{
    closing_view, hold_signal, rebuild_session, reopen, restart, view, withdraw, RebuildError,
    SegmentView, SessionEvents, SessionRecall, STATE_IDLE_SECONDS, STATE_MAX_SESSIONS,
    SURFACE_BRIEF, SURFACE_RECALL,
};
use crate::datetime::DateTime;
use crate::pyvalue::{integer, PyError, PyStr, PyValue};
use crate::recall::choose::ClaimState;
use crate::recall_settle::{Key, SettleError, SettleState};
use crate::transcript::CompactMarker;
use crate::unicode::cps;

/// The sessions in last-seen order: each key's place in the order is a
/// sequence number, so moving a key to the back and reading the front
/// are logarithmic, as the reference's linked `OrderedDict` makes them
/// constant.
#[derive(Default)]
struct Table {
    next: u64,
    order: BTreeMap<u64, Key>,
    held: HashMap<Key, (u64, SessionRecall)>,
    /// The entry `_held` answers when the cap dropped it at once.
    dropped: Option<SessionRecall>,
}

impl Table {
    fn get_mut(&mut self, key: &Key) -> Option<&mut SessionRecall> {
        self.held.get_mut(key).map(|(_, entry)| entry)
    }

    /// A key the table holds marked seen at `now` and moved to the back.
    fn seen(&mut self, key: &Key, now: DateTime) {
        if let Some((place, entry)) = self.held.get_mut(key) {
            entry.last_seen = now;
            self.order.remove(place);
            *place = self.next;
            self.order.insert(self.next, key.clone());
            self.next += 1;
        }
    }

    /// `table[key] = entry` for a key the table does not hold: at the back.
    fn insert(&mut self, key: Key, entry: SessionRecall) {
        self.order.insert(self.next, key.clone());
        self.held.insert(key, (self.next, entry));
        self.next += 1;
    }

    fn front(&self) -> Option<&SessionRecall> {
        let (_, key) = self.order.first_key_value()?;
        self.held.get(key).map(|(_, entry)| entry)
    }

    fn pop_front(&mut self) -> Option<(Key, SessionRecall)> {
        let (_, key) = self.order.pop_first()?;
        self.held.remove(&key).map(|(_, entry)| (key, entry))
    }

    /// `popitem(last=False)` while more than `max` are held: `key`'s entry
    /// when it went too.
    fn cap(&mut self, max: usize, key: &Key) -> Option<SessionRecall> {
        let mut gone = None;
        while self.held.len() > max {
            let (front, entry) = self.pop_front()?;
            gone = gone.or((front == *key).then_some(entry));
        }
        gone
    }

    fn remove(&mut self, key: &Key) {
        if let Some((place, _)) = self.held.remove(key) {
            self.order.remove(&place);
        }
    }
}

/// `a >= b` of two datetimes: by instant when both are aware, by wall
/// clock when both are naive; a naive against an aware raises
/// `TypeError`, as Python's ordering does.
fn at_or_after(a: &DateTime, b: &DateTime) -> Result<bool, PyError> {
    match (a.instant_us(), b.instant_us()) {
        (Some(x), Some(y)) => Ok(x >= y),
        (None, None) => Ok(a.wall_micros() >= b.wall_micros()),
        _ => Err(PyError::TypeError),
    }
}

/// `a == b` of two datetimes: by instant when both are aware (equal
/// across offsets), by wall clock when both are naive, and false for a
/// naive against an aware, as Python's equality answers.
fn same_moment(a: &DateTime, b: &DateTime) -> bool {
    match (a.instant_us(), b.instant_us()) {
        (Some(x), Some(y)) => x == y,
        (None, None) => a.wall_micros() == b.wall_micros(),
        _ => false,
    }
}

/// `segment + 1` on an `int` of any size.
fn next_segment(segment: &PyValue) -> PyValue {
    match segment {
        PyValue::Int(text) => PyValue::Int(integer::sum(text, "1")),
        other => other.clone(),
    }
}

fn show(entry: &mut SessionRecall, id: &PyStr, surface: &[u32], event_id: Option<&[u32]>) {
    entry.shown.insert(id.clone());
    if entry.shown_by.get(id).is_none() {
        entry.shown_by.insert(
            id.clone(),
            (surface.to_vec(), event_id.map(<[u32]>::to_vec)),
        );
    }
}

/// `RecallState`: one cooldown per (harness, session id), least recently
/// seen first, evicted after `idle_seconds` without a hook and beyond
/// `max_sessions`; a state built with `agent_id` is that subagent's scope.
pub struct RecallState {
    table: Mutex<Table>,
    max_sessions: usize,
    idle_seconds: i64,
    agent_id: Option<PyStr>,
}

impl Default for RecallState {
    /// `RecallState()`: the reference's bounds, the session's own scope.
    fn default() -> Self {
        RecallState::new(STATE_MAX_SESSIONS, STATE_IDLE_SECONDS, None)
    }
}

impl RecallState {
    /// `RecallState(max_sessions=, idle_seconds=, agent_id=)`. The cap is
    /// a `usize`: a negative one, under which the reference's eviction
    /// pops an empty table and raises `KeyError` on every call, is not
    /// representable.
    pub fn new(max_sessions: usize, idle_seconds: i64, agent_id: Option<PyStr>) -> Self {
        RecallState {
            table: Mutex::new(Table::default()),
            max_sessions,
            idle_seconds,
            agent_id,
        }
    }

    fn lock(&self) -> MutexGuard<'_, Table> {
        self.table.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// `len(state)`.
    pub fn len(&self) -> usize {
        self.lock().held.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// `list(state._sessions)`: the keys held, least recently seen first.
    pub fn keys(&self) -> Vec<Key> {
        self.lock().order.values().cloned().collect()
    }

    /// `list(state._sessions.items())`, copied: each held entry whole.
    pub fn entries(&self) -> Vec<(Key, SessionRecall)> {
        let table = self.lock();
        let held = |key: &Key| table.held.get(key).map(|(_, e)| (key.clone(), e.clone()));
        table.order.values().filter_map(held).collect()
    }

    /// `_sweep(now)`: `now - timedelta(seconds=idle)` (OverflowError out
    /// of the years 1 to 9999, also on an empty table), then the front
    /// dropped while it was last seen before that, stopping at the first
    /// entry that was not.
    fn sweep(&self, table: &mut Table, now: &DateTime) -> Result<(), PyError> {
        let cutoff = now
            .minus_micros(i128::from(self.idle_seconds) * 1_000_000)
            .map_err(|_| PyError::OverflowError)?;
        while let Some(entry) = table.front() {
            if at_or_after(&entry.last_seen, &cutoff)? {
                break;
            }
            table.pop_front();
        }
        Ok(())
    }

    /// `_entry(store, key, now)`: the key held, rebuilt outside the lock
    /// when it is not; a session another call made meanwhile is kept and
    /// only marked seen.
    fn entry<S: SessionEvents + ?Sized>(
        &self,
        store: &mut S,
        key: &Key,
        now: DateTime,
    ) -> Result<(), RebuildError<S::Error>> {
        {
            let mut table = self.lock();
            self.sweep(&mut table, &now).map_err(RebuildError::Raised)?;
            if table.held.contains_key(key) {
                table.seen(key, now);
                return Ok(());
            }
        }
        let rebuilt = rebuild_session(store, key, now, self.agent_id.as_deref())?;
        let mut table = self.lock();
        if table.held.contains_key(key) {
            table.seen(key, now);
        } else {
            table.insert(key.clone(), rebuilt);
        }
        table.cap(self.max_sessions, key);
        Ok(())
    }

    /// `_held(key, now)` (lock held): the entry, created empty when it is
    /// not held (never rebuilt), marked seen and moved to the back, then
    /// the cap applied. A cap of zero drops the entry at once and the
    /// call still works on it, as the reference's reference to it does.
    fn held<'t>(&self, table: &'t mut Table, key: &Key, now: DateTime) -> &'t mut SessionRecall {
        if !table.held.contains_key(key) {
            table.insert(key.clone(), SessionRecall::new(0, now, now));
        }
        table.seen(key, now);
        table.dropped = table.cap(self.max_sessions, key);
        if table.dropped.is_some() {
            return table
                .dropped
                .get_or_insert_with(|| SessionRecall::new(0, now, now));
        }
        match table.held.get_mut(key) {
            Some((_, entry)) => entry,
            None => table.dropped.insert(SessionRecall::new(0, now, now)),
        }
    }

    /// `segment(store, key, now=)`: the session's current segment.
    pub fn segment<S: SessionEvents + ?Sized>(
        &self,
        store: &mut S,
        key: &Key,
        now: DateTime,
    ) -> Result<SegmentView, RebuildError<S::Error>> {
        self.entry(store, key, now)?;
        let mut table = self.lock();
        Ok(view(self.held(&mut table, key, now)))
    }

    /// `open_segment(store, key, source=, now=, marker=)`: the segment a
    /// SessionStart leaves the session in and whether it opened it. A
    /// `clear` opens one on the entry's own marker; a `compact` opens one
    /// when `marker`'s key differs from the entry's, else holds the
    /// signal; any other source keeps the segment.
    pub fn open_segment<S: SessionEvents + ?Sized>(
        &self,
        store: &mut S,
        key: &Key,
        source: &[u32],
        now: DateTime,
        marker: Option<&CompactMarker>,
    ) -> Result<(SegmentView, bool), RebuildError<S::Error>> {
        self.entry(store, key, now)?;
        let mut table = self.lock();
        let entry = self.held(&mut table, key, now);
        if source == cps("clear") {
            entry.closed = Some(closing_view(entry, None));
            let (segment, kept) = (next_segment(&entry.segment), entry.marker.clone());
            restart(entry, segment, now, kept);
            return Ok((view(entry), true));
        }
        if source != cps("compact") {
            return Ok((view(entry), false));
        }
        if let Some(m) = marker.filter(|m| Some(&m.key) != entry.marker.as_ref()) {
            entry.closed = Some(closing_view(entry, None));
            let next = next_segment(&entry.segment);
            restart(entry, next, now, Some(m.key.clone()));
            return Ok((view(entry), true));
        }
        hold_signal(entry, now, None);
        Ok((view(entry), false))
    }

    /// `pending_compaction(store, key, now=)`: when the held compact
    /// signal came, or None.
    pub fn pending_compaction<S: SessionEvents + ?Sized>(
        &self,
        store: &mut S,
        key: &Key,
        now: DateTime,
    ) -> Result<Option<DateTime>, RebuildError<S::Error>> {
        self.entry(store, key, now)?;
        let mut table = self.lock();
        let signal = &self.held(&mut table, key, now).signal;
        Ok(signal.as_ref().map(|s| s.at))
    }

    /// `resolve_compaction(key, at=, marker=)`: the segment the signal held
    /// since `at` opens when `marker`'s key differs from the entry's; None
    /// without such a marker, and when no signal held since `at` waits.
    /// The session is not touched (no sweep, not marked seen).
    pub fn resolve_compaction(
        &self,
        key: &Key,
        at: DateTime,
        marker: Option<&CompactMarker>,
    ) -> Option<SegmentView> {
        let mut table = self.lock();
        let entry = table.get_mut(key)?;
        let signal = entry.signal.clone().filter(|s| same_moment(&s.at, &at))?;
        let marker = marker.filter(|m| Some(&m.key) != entry.marker.as_ref())?;
        entry.closed = Some(closing_view(entry, Some(&signal.shown)));
        let segment = next_segment(&entry.segment);
        reopen(entry, &signal, segment, at, Some(marker.key.clone()));
        Some(view(entry))
    }

    /// `attribute_compaction(key, at=)`: the signal held since `at` was a
    /// subagent's; it ends, the ids shown by its brief's event leave the
    /// segment, and those ids (sorted) and the event id are answered.
    pub fn attribute_compaction(
        &self,
        key: &Key,
        at: DateTime,
    ) -> Option<(Vec<PyStr>, Option<PyStr>)> {
        let mut table = self.lock();
        let entry = table.get_mut(key)?;
        let signal = entry.signal.clone().filter(|s| same_moment(&s.at, &at))?;
        let mut ids: Vec<PyStr> = match &signal.event_id {
            None => Vec::new(),
            Some(event) => entry
                .shown_by
                .iter()
                .filter(|(_, (surface, by))| {
                    *surface == cps(SURFACE_BRIEF) && by.as_ref() == Some(event)
                })
                .map(|(id, _)| id.clone())
                .collect(),
        };
        ids.sort();
        withdraw(entry, &ids);
        Some((ids, signal.event_id))
    }

    /// `take_closed(key)`: the segment the session last left, once.
    pub fn take_closed(&self, key: &Key) -> Option<SegmentView> {
        self.lock().get_mut(key)?.closed.take()
    }

    /// `note_shown(key, ids, delivery=, now=, surface=, event_id=)`: an
    /// empty or absent surface names `recall` for a delivery, `brief`
    /// otherwise; an id's first show is kept; a delivery counts one.
    pub fn note_shown(
        &self,
        key: &Key,
        ids: &[PyStr],
        delivery: bool,
        now: DateTime,
        surface: Option<&[u32]>,
        event_id: Option<&[u32]>,
    ) {
        let mut table = self.lock();
        let entry = self.held(&mut table, key, now);
        let named = match surface.filter(|s| !s.is_empty()) {
            Some(s) => s.to_vec(),
            None => cps(if delivery {
                SURFACE_RECALL
            } else {
                SURFACE_BRIEF
            }),
        };
        for id in ids {
            show(entry, id, &named, event_id);
        }
        if delivery {
            entry.deliveries += 1;
        }
    }

    /// `claim_delivery`, or with `in_i64` the segment answered as an
    /// `i64`, nothing claimed when it is past one (`Err` with it).
    fn claim(
        &self,
        key: &Key,
        ids: &[PyStr],
        now: DateTime,
        event_id: Option<&[u32]>,
        in_i64: bool,
    ) -> Result<(Vec<PyStr>, PyValue), PyValue> {
        let mut table = self.lock();
        let entry = self.held(&mut table, key, now);
        if in_i64 && as_i64(&entry.segment).is_none() {
            return Err(entry.segment.clone());
        }
        let kept: Vec<PyStr> = ids
            .iter()
            .filter(|id| !entry.shown.contains(*id))
            .cloned()
            .collect();
        if !kept.is_empty() {
            let recall = cps(SURFACE_RECALL);
            for id in &kept {
                show(entry, id, &recall, event_id);
            }
            entry.deliveries += 1;
        }
        Ok((kept, entry.segment.clone()))
    }

    /// `claim_delivery(key, ids, now=, event_id=)`: the ids not yet shown
    /// in the segment, in order (an id repeated in `ids` is kept twice),
    /// noted as the recall's, and the segment; nothing kept counts no
    /// delivery.
    pub fn claim_delivery(
        &self,
        key: &Key,
        ids: &[PyStr],
        now: DateTime,
        event_id: Option<&[u32]>,
    ) -> (Vec<PyStr>, PyValue) {
        match self.claim(key, ids, now, event_id, false) {
            Ok(answer) => answer,
            Err(segment) => (Vec::new(), segment),
        }
    }

    /// `claim_brief(key, standing, work, now=, event_id=)`: each list's ids
    /// not yet shown, both filtered before either is noted (an id in both,
    /// or repeated in one, is kept each time), noted as the brief's, and
    /// the segment. A brief counts no delivery.
    pub fn claim_brief(
        &self,
        key: &Key,
        standing: &[PyStr],
        work: &[PyStr],
        now: DateTime,
        event_id: Option<&[u32]>,
    ) -> (Vec<PyStr>, Vec<PyStr>, PyValue) {
        let mut table = self.lock();
        let entry = self.held(&mut table, key, now);
        let unseen = |ids: &[PyStr]| -> Vec<PyStr> {
            ids.iter()
                .filter(|id| !entry.shown.contains(*id))
                .cloned()
                .collect()
        };
        let (kept_standing, kept_work) = (unseen(standing), unseen(work));
        let brief = cps(SURFACE_BRIEF);
        for id in kept_standing.iter().chain(&kept_work) {
            show(entry, id, &brief, event_id);
        }
        (kept_standing, kept_work, entry.segment.clone())
    }

    /// `forget(key)`: drop the session's held state.
    pub fn forget(&self, key: &Key) {
        self.lock().remove(key);
    }

    /// `note_settled(key, ids, now=)`.
    pub fn note_settled(&self, key: &Key, ids: &[PyStr], now: DateTime) {
        let mut table = self.lock();
        self.held(&mut table, key, now)
            .settled
            .extend(ids.iter().cloned());
    }

    /// `note_review_roots(key, roots, now=)`.
    pub fn note_review_roots(&self, key: &Key, roots: &[PyStr], now: DateTime) {
        let mut table = self.lock();
        self.held(&mut table, key, now)
            .review_roots
            .extend(roots.iter().cloned());
    }

    /// The state bound to one caller's store, for the two traits.
    pub fn bind<'a, S: SessionEvents + ?Sized>(&'a self, store: &'a mut S) -> Bound<'a, S> {
        Bound {
            state: self,
            store,
            pending: None,
        }
    }
}

/// What a [`Bound`] pair holds for its caller: the store's read or a
/// row's raise met by a rebuild, or a segment `ClaimState` cannot answer
/// in an `i64` (the log's int is any size).
#[derive(Clone, Debug, PartialEq)]
pub enum BoundError<E> {
    Rebuild(RebuildError<E>),
    SegmentPastI64(PyValue),
}

/// A [`RecallState`] bound to one caller's store: `SettleState` and
/// `ClaimState`, whose calls take no store. The first error met is kept
/// for [`Bound::take_error`] (as `origin_git`'s `OriginRunner` keeps its
/// deferred error): `segment` answers `SettleError::State`, so its caller
/// raises before recording anything, as the reference's raise stops it;
/// `claim_delivery` answers no ids. Until the error is taken `segment`
/// answers `SettleError::State` again and every other call is a no-op.
pub struct Bound<'a, S: SessionEvents + ?Sized> {
    state: &'a RecallState,
    store: &'a mut S,
    pending: Option<BoundError<S::Error>>,
}

impl<S: SessionEvents + ?Sized> Bound<'_, S> {
    /// The error the pair met, once; calls go through again after it.
    pub fn take_error(&mut self) -> Option<BoundError<S::Error>> {
        self.pending.take()
    }
}

impl<S: SessionEvents + ?Sized> SettleState for Bound<'_, S> {
    /// `segment`, or `SettleError::State` when the rebuild raised (the
    /// error kept) or an error is still kept: the caller raises there.
    fn segment(&mut self, key: &Key, now: &DateTime) -> Result<SegmentView, SettleError> {
        if self.pending.is_some() {
            return Err(SettleError::State);
        }
        self.state.segment(self.store, key, *now).map_err(|e| {
            self.pending = Some(BoundError::Rebuild(e));
            SettleError::State
        })
    }

    fn take_closed(&mut self, key: &Key) -> Option<SegmentView> {
        self.pending
            .is_none()
            .then(|| self.state.take_closed(key))?
    }

    fn note_settled(&mut self, key: &Key, ids: &[PyStr], now: &DateTime) {
        if self.pending.is_none() {
            self.state.note_settled(key, ids, *now);
        }
    }

    fn note_review_roots(&mut self, key: &Key, roots: &[PyStr], now: &DateTime) {
        if self.pending.is_none() {
            self.state.note_review_roots(key, roots, *now);
        }
    }

    fn forget(&mut self, key: &Key) {
        if self.pending.is_none() {
            self.state.forget(key);
        }
    }
}

fn as_i64(segment: &PyValue) -> Option<i64> {
    match segment {
        PyValue::Int(text) => text.parse().ok(),
        _ => None,
    }
}

impl<S: SessionEvents + ?Sized> ClaimState for Bound<'_, S> {
    /// `claim_delivery` with `event_id` given. A segment past `i64` is
    /// found before anything is claimed: the pair keeps it as its error
    /// and claims nothing.
    fn claim_delivery(
        &mut self,
        key: &Key,
        ids: &[PyStr],
        now: &DateTime,
        event_id: &[u32],
    ) -> (Vec<PyStr>, i64) {
        if self.pending.is_some() {
            return (Vec::new(), 0);
        }
        match self.state.claim(key, ids, *now, Some(event_id), true) {
            Ok((kept, segment)) => (kept, as_i64(&segment).unwrap_or_default()),
            Err(segment) => {
                self.pending = Some(BoundError::SegmentPastI64(segment));
                (Vec::new(), 0)
            }
        }
    }
}
