//! `recall_prompt.py` whole: the prompt recall's turn (`_PromptTurn`),
//! its log window (`_prompt_window`), the settlement of the session's
//! interrupted turns before the probe (`_settle_before_probe`), the probe
//! (`_prompt_probe`) and the two rows it writes (`_record_hold`,
//! `_record_delivery`).
//!
//! The store's reads are `recall_settle::SettleStore`; the session joins
//! (`sessions.bridged_session`, `_disabled_scopes_from_events`) are
//! [`PromptSessions`]; `recall_admission._delivery_membership` and
//! `_rc3_admitted` are [`DeliveryGate`]; `Admission.on`,
//! `recall_settle.settle_interrupted` and `recall_settle._probe_message`
//! are closures; the recorder is built by a factory closure over
//! `recall_settle::RecorderSpec`, and the row's event id is minted by a
//! closure. The warning `_settle_before_probe` logs is its return value.

use std::collections::BTreeSet;

use crate::audit::report::{
    prompt_recall_fields, recall_held_fields, AuditError, Entry, MissHit, MissReport,
    PromptRecallOptions, RecallTail,
};
use crate::datetime::DateTime;
use crate::events::{row_class, RowClass};
use crate::in_force::InForce;
use crate::pyvalue::{PyStr, PyValue};
use crate::recall_settle::{
    previous_audit_at, Key, Recorder, RecorderSpec, SegmentView, SettleError, SettleState,
    SettleStore, REAUDIT_DEDUP_WINDOW_SECONDS, RULE, STATE_IDLE_SECONDS,
};
use crate::unicode::cps;

/// `membership_records.POOL_PREFILTER`.
pub const POOL_PREFILTER: &str = "pool_prefilter";
/// `membership_records.PROMPT_RECALL`.
pub const PROMPT_RECALL: &str = "prompt_recall";

/// An exception a closure or the recorder raised: its class name and
/// `str(exc)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PyException {
    pub class: String,
    pub message: PyStr,
}

/// What `_record_hold` and `_record_delivery` raise.
#[derive(Clone, Debug, PartialEq)]
pub enum PromptError {
    /// A field builder's `ValueError`.
    Audit(AuditError),
    /// What the recorder raised.
    Raised(PyException),
}

/// `_PromptTurn`. The configuration is read through its two switches
/// (`cfg.telemetry.enabled`, `cfg.behavior.search_mode`); the closures
/// that need the rest of it hold it themselves. The resolution, the
/// admission and the caller are opaque here: [`DeliveryGate`] and the
/// closures read them. The worktree root is read from the caller
/// ([`TurnCaller`]), as the reference reads `caller.worktree_root`, so
/// the rows and the rc3 admission cannot see two roots.
/// `caller.worktree_root`: the one root a turn's caller holds.
pub trait TurnCaller {
    fn worktree_root(&self) -> Option<&[u32]>;
}

/// A caller known only by its root.
impl TurnCaller for Option<PyStr> {
    fn worktree_root(&self) -> Option<&[u32]> {
        self.as_deref()
    }
}

#[derive(Clone, Debug)]
pub struct PromptTurn<S, A, R, C> {
    pub store: S,
    pub telemetry: bool,
    pub search_mode: Option<PyStr>,
    /// The `prompt_recall` switch: "project" or "everywhere".
    pub mode: PyStr,
    pub query: PyStr,
    pub resolution: Option<R>,
    /// PROMPT_RECALL or SEARCH_UNSCOPED.
    pub surface: PyStr,
    pub admission: A,
    pub remint: Option<PyValue>,
    pub now: DateTime,
    /// (harness, session id).
    pub key: Key,
    pub caller: C,
    pub prompt_id: Option<PyStr>,
    pub transcript_path: Option<PyStr>,
}

/// The `sessions` joins the window reads.
pub trait PromptSessions {
    /// `bridged_session(events, harness_session=, worktree_root=)`.
    fn bridged_session(
        &mut self,
        events: &[PyValue],
        harness_session: &[u32],
        worktree_root: Option<&[u32]>,
    ) -> Option<PyStr>;
    /// `_disabled_scopes_from_events(events, worktree_root=, server_session=)`.
    fn disabled_scopes(
        &mut self,
        events: &[PyValue],
        worktree_root: Option<&[u32]>,
        server_session: Option<&[u32]>,
    ) -> BTreeSet<PyStr>;
}

/// `recall_admission._delivery_membership` and `_rc3_admitted`.
pub trait DeliveryGate<R, A, C, M> {
    /// The `checkout` and `membership_rule` keys, in order.
    fn delivery_membership(
        &mut self,
        resolution: Option<&R>,
        admission: &A,
    ) -> Vec<(PyValue, PyValue)>;
    fn rc3_admitted(&mut self, memories: &[M], caller: &C, project_only: bool) -> Vec<PyStr>;
}

/// What `_prompt_window` answers: (own cut, log, server session, scopes off).
#[derive(Clone, Debug, PartialEq)]
pub struct PromptWindow {
    pub own_since: DateTime,
    pub recent: Vec<PyValue>,
    pub server_session: Option<PyStr>,
    pub excluded_scopes: BTreeSet<PyStr>,
}

/// The turn as a later step reads it.
pub trait PromptTurnView {
    fn store(&mut self) -> &mut dyn SettleStore;
    fn session(&self) -> &[u32];
    fn key(&self) -> &Key;
    fn now(&self) -> &DateTime;
    /// [`prompt_window`] over this turn.
    fn window(
        &mut self,
        view: &SegmentView,
        sessions: &mut dyn PromptSessions,
    ) -> Result<PromptWindow, SettleError>;
}

impl<S: SettleStore, A, R, C: TurnCaller> PromptTurnView for PromptTurn<S, A, R, C> {
    fn store(&mut self) -> &mut dyn SettleStore {
        &mut self.store
    }
    fn session(&self) -> &[u32] {
        &self.key.1
    }
    fn key(&self) -> &Key {
        &self.key
    }
    fn now(&self) -> &DateTime {
        &self.now
    }
    fn window(
        &mut self,
        view: &SegmentView,
        sessions: &mut dyn PromptSessions,
    ) -> Result<PromptWindow, SettleError> {
        prompt_window(self, view, sessions)
    }
}

fn seconds_before(now: &DateTime, seconds: i64) -> Result<DateTime, SettleError> {
    now.minus_micros(i128::from(seconds) * 1_000_000)
        .map_err(|_| SettleError::OverflowError)
}

/// `view.segment > 0`: a number compares, anything else raises `TypeError`.
fn positive(segment: &PyValue) -> Result<bool, SettleError> {
    match segment {
        PyValue::Bool(b) => Ok(*b),
        PyValue::Int(text) => Ok(!text.starts_with('-') && text != "0"),
        PyValue::Float(x) => Ok(*x > 0.0),
        _ => Err(SettleError::TypeError),
    }
}

/// `_prompt_window`: the dedup cut first, then the own cut (the segment's
/// start past the first segment, else the idle window), the log since the
/// dedup cut, the bridged session and the scopes switched off.
pub fn prompt_window<S: SettleStore, A, R, C: TurnCaller>(
    turn: &mut PromptTurn<S, A, R, C>,
    view: &SegmentView,
    sessions: &mut dyn PromptSessions,
) -> Result<PromptWindow, SettleError> {
    let recent_cut = seconds_before(&turn.now, REAUDIT_DEDUP_WINDOW_SECONDS)?;
    let own_since = if positive(&view.segment)? {
        view.started_at
    } else {
        seconds_before(&turn.now, STATE_IDLE_SECONDS)?
    };
    let recent = turn.store.events_since(&recent_cut);
    let wt = turn.caller.worktree_root();
    let server_session = sessions.bridged_session(&recent, &turn.key.1, wt);
    let excluded_scopes = sessions.disabled_scopes(&recent, wt, server_session.as_deref());
    Ok(PromptWindow {
        own_since,
        recent,
        server_session,
        excluded_scopes,
    })
}

/// The keyword arguments `_settle_before_probe` hands
/// `settle_interrupted` past the store and the configuration.
pub struct SettleArgs<'a, A> {
    pub session_id: &'a [u32],
    pub transcript_path: Option<&'a [u32]>,
    pub admission: A,
    pub excluded_scopes: &'a BTreeSet<PyStr>,
    pub recent: &'a [PyValue],
    pub server_session: Option<&'a [u32]>,
    pub worktree_root: Option<&'a [u32]>,
    pub now: &'a DateTime,
    pub previous_audit: Option<DateTime>,
    pub triggered_from: &'static str,
    pub exclude_prompt_id: Option<&'a [u32]>,
}

/// `settle_interrupted` over the store and its keyword arguments. An
/// `Err` is an `Exception` the reference catches and logs; a
/// `BaseException` it lets through (`SystemExit`, `KeyboardInterrupt`)
/// is never returned here: the closure panics or stops the process.
pub type SettleFn<'f, S, A> = dyn FnMut(&mut S, SettleArgs<'_, A>) -> Result<(), PyException> + 'f;

/// `_settle_before_probe`: `settle_interrupted` under the admission on
/// POOL_PREFILTER, with the previous audit's stamp read inside the same
/// `try`; what it raises comes back as the warning the reference logs.
pub fn settle_before_probe<S: SettleStore, A, R, C: TurnCaller>(
    turn: &mut PromptTurn<S, A, R, C>,
    recent: &[PyValue],
    server_session: Option<&[u32]>,
    excluded_scopes: &BTreeSet<PyStr>,
    on: &mut dyn FnMut(&A, &str) -> A,
    settle: &mut SettleFn<'_, S, A>,
) -> Option<PyStr> {
    let admission = on(&turn.admission, POOL_PREFILTER);
    let raised = match previous_audit_at(&mut turn.store, &turn.key.1, &turn.now) {
        Err(err) => Err(match err {
            SettleError::OverflowError => PyException {
                class: "OverflowError".into(),
                message: cps("date value out of range"),
            },
            // previous_audit_at fails only on the date arithmetic.
            SettleError::TypeError | SettleError::State => {
                unreachable!("previous_audit_at raises only OverflowError")
            }
        }),
        Ok(previous_audit) => settle(
            &mut turn.store,
            SettleArgs {
                session_id: &turn.key.1,
                transcript_path: turn.transcript_path.as_deref(),
                admission,
                excluded_scopes,
                recent,
                server_session,
                worktree_root: turn.caller.worktree_root(),
                now: &turn.now,
                previous_audit,
                triggered_from: "prompt_hook",
                exclude_prompt_id: None,
            },
        ),
    };
    raised.err().map(|exc| {
        let mut text = cps("interrupted turns not settled at the prompt: ");
        text.extend(cps(&exc.class));
        text.extend(cps(": "));
        text.extend(exc.message);
        text
    })
}

/// The keyword arguments `_prompt_probe` hands `_probe_message` past the
/// store and the configuration.
pub struct ProbeArgs<'a, A> {
    pub user_message: &'a [u32],
    pub session_id: &'a [u32],
    pub server_session: Option<&'a [u32]>,
    pub admission: A,
    pub excluded_scopes: &'a BTreeSet<PyStr>,
    pub recent: &'a [PyValue],
    pub worktree_root: Option<&'a [u32]>,
    pub now: &'a DateTime,
    pub floor: bool,
}

/// `_prompt_probe`: the probe over the owner's words, under the admission
/// on POOL_PREFILTER when the surface is PROMPT_RECALL, else under the
/// turn's own; never floored.
pub fn prompt_probe<S, A: Clone, R, C: TurnCaller, E>(
    turn: &mut PromptTurn<S, A, R, C>,
    recent: &[PyValue],
    server_session: Option<&[u32]>,
    excluded_scopes: &BTreeSet<PyStr>,
    on: &mut dyn FnMut(&A, &str) -> A,
    probe: impl FnOnce(&mut S, ProbeArgs<'_, A>) -> Result<MissReport, E>,
) -> Result<MissReport, E> {
    let admission = if turn.surface == cps(PROMPT_RECALL) {
        on(&turn.admission, POOL_PREFILTER)
    } else {
        turn.admission.clone()
    };
    probe(
        &mut turn.store,
        ProbeArgs {
            user_message: &turn.query,
            session_id: &turn.key.1,
            server_session,
            admission,
            excluded_scopes,
            recent,
            worktree_root: turn.caller.worktree_root(),
            now: &turn.now,
            floor: false,
        },
    )
}

fn spec<S, A, R, C: TurnCaller>(turn: &PromptTurn<S, A, R, C>) -> RecorderSpec<'_> {
    RecorderSpec {
        session_id: &turn.key.1,
        enabled: turn.telemetry,
        worktree_root: turn.caller.worktree_root(),
        triggered_from: &[],
    }
}

fn probe_mode(search_mode: &Option<PyStr>) -> PyStr {
    match search_mode {
        Some(mode) if !mode.is_empty() => mode.clone(),
        _ => cps("hybrid"),
    }
}

fn tail<S, A, R, C>(turn: &PromptTurn<S, A, R, C>, segment: i64) -> RecallTail {
    RecallTail {
        segment: Some(segment),
        prompt_id: turn.prompt_id.clone(),
        transcript_path: turn.transcript_path.clone(),
        harness: Some(turn.key.0.clone()),
    }
}

/// `fields[key] = value`: an existing key keeps its place.
fn set(fields: &mut Vec<(PyValue, PyValue)>, key: PyValue, value: PyValue) {
    match fields.iter_mut().find(|(k, _)| *k == key) {
        Some(slot) => slot.1 = value,
        None => fields.push((key, value)),
    }
}

fn dict(value: &PyValue) -> Vec<(PyValue, PyValue)> {
    match value {
        PyValue::Dict(pairs) => pairs.clone(),
        _ => Vec::new(),
    }
}

/// `_record_hold`: built only when the recorder records `recall_held`,
/// the row is `recall_held_fields` with the surface as `admission` and
/// the delivery's membership keys.
#[allow(clippy::too_many_arguments)]
pub fn record_hold<S, A, R, C: TurnCaller, M, Rec: Recorder>(
    turn: &PromptTurn<S, A, R, C>,
    report: &MissReport,
    segment: i64,
    cause: &[u32],
    entries: &[Entry],
    gate: &mut dyn DeliveryGate<R, A, C, M>,
    make_recorder: impl FnOnce(&RecorderSpec<'_>) -> Rec,
    mint: &mut dyn FnMut() -> String,
) -> Result<(), PromptError> {
    let mut recorder = make_recorder(&spec(turn));
    let records = !recorder.suppressed()
        && (turn.telemetry || row_class(&cps("recall_held")) == RowClass::State);
    if !records {
        return Ok(());
    }
    let built = recall_held_fields(
        report,
        &turn.key.1,
        &probe_mode(&turn.search_mode),
        cause,
        entries,
        &cps(RULE),
        &cps("prompt_hook"),
        &tail(turn, segment),
        mint,
    )
    .map_err(PromptError::Audit)?;
    let mut fields = dict(&built);
    set(
        &mut fields,
        PyValue::str("admission"),
        PyValue::Str(turn.surface.clone()),
    );
    for (k, v) in gate.delivery_membership(turn.resolution.as_ref(), &turn.admission) {
        set(&mut fields, k, v);
    }
    recorder
        .try_record("recall_held", fields)
        .map_err(PromptError::Raised)
}

/// A chosen hit: the hit, the record that takes its slot, that record's
/// trust row, and its in-force entry.
pub type Chosen<M, T> = (MissHit, M, T, Option<InForce>);

/// `_record_delivery`'s keyword arguments.
pub struct Delivery<'a, M, T> {
    pub block: &'a [u32],
    pub shown_ids: &'a [PyStr],
    pub event_id: &'a [u32],
    pub segment: i64,
    pub chosen: &'a [Chosen<M, T>],
    pub suppressed: &'a [Entry],
    pub rc3_candidates: &'a [M],
}

/// `_record_delivery`: the `prompt_recall` row with the delivery's event
/// id, its admission, its membership keys and the rc3 column less the
/// chosen pointers not shown; a row that cannot be recorded forgets the
/// session's held state and raises.
#[allow(clippy::too_many_arguments)]
pub fn record_delivery<S, A, R, C: TurnCaller, M, T, Rec: Recorder>(
    turn: &PromptTurn<S, A, R, C>,
    report: &MissReport,
    state: &mut dyn SettleState,
    d: &Delivery<'_, M, T>,
    gate: &mut dyn DeliveryGate<R, A, C, M>,
    make_recorder: impl FnOnce(&RecorderSpec<'_>) -> Rec,
    mint: &mut dyn FnMut() -> String,
) -> Result<PyValue, PromptError> {
    let mut recorder = make_recorder(&spec(turn));
    let options = PromptRecallOptions {
        triggered_from: cps("prompt_hook"),
        delivered_reason: turn.mode.clone(),
        shown_ids: Some(d.shown_ids.to_vec()),
        suppressed: Some(d.suppressed.to_vec()),
        rule: Some(cps(RULE)),
        tail: tail(turn, d.segment),
    };
    let built = prompt_recall_fields(
        report,
        &turn.key.1,
        &probe_mode(&turn.search_mode),
        d.block.len() as i64,
        &options,
        mint,
    )
    .map_err(PromptError::Audit)?;
    let mut fields = dict(&built);
    set(
        &mut fields,
        PyValue::str("event_id"),
        PyValue::Str(d.event_id.to_vec()),
    );
    set(
        &mut fields,
        PyValue::str("admission"),
        PyValue::Str(turn.surface.clone()),
    );
    for (k, v) in gate.delivery_membership(turn.resolution.as_ref(), &turn.admission) {
        set(&mut fields, k, v);
    }
    let shown: BTreeSet<&PyStr> = d.shown_ids.iter().collect();
    let dropped: BTreeSet<&PyStr> = d
        .chosen
        .iter()
        .map(|(hit, ..)| &hit.id)
        .filter(|id| !shown.contains(id))
        .collect();
    let project_only = turn.surface == cps(PROMPT_RECALL);
    let admitted: Vec<PyValue> = gate
        .rc3_admitted(d.rc3_candidates, &turn.caller, project_only)
        .into_iter()
        .filter(|id| !dropped.contains(id))
        .map(PyValue::Str)
        .collect();
    set(
        &mut fields,
        PyValue::str("rc3_admitted"),
        PyValue::List(admitted),
    );
    if let Err(exc) = recorder.try_record("prompt_recall", fields.clone()) {
        state.forget(&turn.key);
        return Err(PromptError::Raised(exc));
    }
    Ok(PyValue::Dict(fields))
}
