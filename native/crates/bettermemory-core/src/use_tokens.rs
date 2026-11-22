//! The per-turn use-token bookkeeping of `handlers/_shared.py`: the turn
//! bump with its auto-commit, the wall-clock expiry drain and its event,
//! the backward scan of the event log for settled `use` rows, and the
//! tokens attached to search hits.
//!
//! The event log is a [`UseLog`]: the recorder's `record`, its
//! `session_for_request` and its store's `iter_events_backward`. The
//! clock and the random source are `session::Effects`. The reference's
//! re-entrant lock is one guard of [`SessionState::lock`]: the functions
//! that run under `_advance_turn`'s lock take its `&mut Inner`, and only
//! [`advance_turn`], [`already_recorded_pending_ids`] and
//! [`attach_use_tokens`] take the lock themselves.

use std::collections::{BTreeSet, HashMap};

use crate::datetime;
use crate::events::truthy;
use crate::pyvalue::{py_int, PyError, PyStr, PyValue};
use crate::session::{Effects, Inner, PendingUseToken, SessionState, DEFAULT_USE_TOKEN_TTL_TURNS};
use crate::unicode;

/// One row of the event log: a dict's items in order.
pub type Row = Vec<(PyValue, PyValue)>;

/// The recorder and its store, as the bookkeeping reads them.
pub trait UseLog {
    /// `recorder.record(kind, **fields)`, the keywords in call order.
    fn record(&mut self, kind: &str, fields: Row);
    /// `recorder.session_for_request()`.
    fn session_for_request(&mut self) -> PyStr;
    /// `recorder.store.iter_events_backward()`: newest first, each row
    /// pulled only when the scan asks for it.
    fn events_backward(&mut self) -> Box<dyn Iterator<Item = Row> + '_>;
}

/// An exception the reference raises here, with Python's text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum UseTokenError {
    /// `x in allowed_sessions` of an unhashable session value:
    /// `TypeError: unhashable type: '<name>'`.
    Unhashable(&'static str),
    /// `int(now - issued_at)` of NaN (`ValueError`) or an infinity
    /// (`OverflowError`).
    Int(PyError),
    /// `h["id"]` of a hit without one: `KeyError: 'id'`.
    MissingId,
    /// A hit whose id is not a `str`: the session's maps hold `str` keys
    /// only, so this is declined (the reference files any hashable id).
    NotPorted,
}

impl UseTokenError {
    /// `type(e).__name__`.
    pub fn name(&self) -> &'static str {
        match self {
            UseTokenError::Unhashable(_) => "TypeError",
            UseTokenError::Int(e) => e.name(),
            UseTokenError::MissingId => "KeyError",
            UseTokenError::NotPorted => "NotPorted",
        }
    }

    /// `str(e)`.
    pub fn message(&self) -> String {
        match self {
            UseTokenError::Unhashable(t) => format!("unhashable type: '{t}'"),
            UseTokenError::Int(PyError::ValueError) => "cannot convert float NaN to integer".into(),
            UseTokenError::Int(_) => "cannot convert float infinity to integer".into(),
            UseTokenError::MissingId => "'id'".into(),
            UseTokenError::NotPorted => "a hit id that is not a str".into(),
        }
    }
}

fn get<'a>(row: &'a [(PyValue, PyValue)], key: &str) -> Option<&'a PyValue> {
    let key = unicode::cps(key);
    row.iter().rev().find_map(|(k, v)| match k {
        PyValue::Str(s) if *s == key => Some(v),
        _ => None,
    })
}

fn is_str(v: Option<&PyValue>, text: &str) -> bool {
    matches!(v, Some(PyValue::Str(s)) if *s == unicode::cps(text))
}

/// `a or b` of two `dict.get` answers (None for a missing key).
fn or<'a>(a: Option<&'a PyValue>, b: Option<&'a PyValue>) -> Option<&'a PyValue> {
    match a {
        Some(v) if truthy(v) => a,
        _ => b,
    }
}

/// The type `hash(value)` refuses, if any: the first in the order a
/// tuple's hash visits its items, without native recursion.
fn unhashable(value: &PyValue) -> Option<&'static str> {
    let mut work = vec![value];
    while let Some(v) = work.pop() {
        match v {
            PyValue::List(_) => return Some("list"),
            PyValue::Dict(_) => return Some("dict"),
            PyValue::Tuple(items) => work.extend(items.iter().rev()),
            _ => {}
        }
    }
    None
}

/// Python's `min` over `(key, item)` pairs: an item replaces the current
/// one only when its key is `<` the current key.
fn py_min<T>(items: impl IntoIterator<Item = (f64, T)>) -> Option<(f64, T)> {
    let mut best: Option<(f64, T)> = None;
    for (k, item) in items {
        let replaces = match &best {
            Some((b, _)) => k < *b,
            None => true,
        };
        if replaces {
            best = Some((k, item));
        }
    }
    best
}

const EPOCH_US: i128 = 62_135_596_800_000_000;

/// `us / 10**6` as Python's true division of two ints: the double
/// nearest the quotient, ties to even.
fn true_div_million(us: i128) -> f64 {
    if us.unsigned_abs() <= 1 << 53 {
        return us as f64 / 1e6;
    }
    let a = us.unsigned_abs();
    let approx = a as f64 / 1e6;
    // The error of `approx` is under two units in the last place, so the
    // answer is it or a neighbour: the nearest by exact comparison.
    let cands = [
        f64::from_bits(approx.to_bits() - 1),
        approx,
        f64::from_bits(approx.to_bits() + 1),
        f64::from_bits(approx.to_bits() - 2),
        f64::from_bits(approx.to_bits() + 2),
    ];
    // |d * 10**6 - a| scaled by 2**s, with d = m * 2**-s.
    let dist = |d: f64| -> (u128, u64) {
        let bits = d.to_bits();
        let e = ((bits >> 52) & 0x7ff) as i32 - 1075;
        let m = (bits & ((1 << 52) - 1)) | (1 << 52);
        let s = (-e) as u32;
        let lhs = u128::from(m) * 1_000_000;
        let rhs = a << s;
        (lhs.abs_diff(rhs) << (64 - s), m)
    };
    let mut best = cands[1];
    let (mut bd, mut bm) = dist(best);
    for &c in &cands {
        let (d, m) = dist(c);
        if d < bd || (d == bd && m % 2 == 0 && bm % 2 == 1) {
            (best, bd, bm) = (c, d, m);
        }
    }
    if us < 0 {
        -best
    } else {
        best
    }
}

/// `_event_ts_epoch(raw)`: `parse_event_ts(raw).timestamp()`, None for a
/// value that is not a `str` or does not parse.
pub fn event_ts_epoch(raw: Option<&PyValue>) -> Option<f64> {
    let Some(PyValue::Str(text)) = raw else {
        return None;
    };
    let dt = datetime::parse_event_ts(text)?;
    Some(true_div_million(dt.instant_us()? - EPOCH_US))
}

/// `_scan_settled_use_events(recorder, allowed_sessions,
/// pending_issued_at, oldest_pending_issued_at)`.
pub fn scan_settled_use_events(
    log: &mut dyn UseLog,
    allowed_sessions: &BTreeSet<PyStr>,
    pending_issued_at: &HashMap<PyStr, f64>,
    oldest_pending_issued_at: f64,
) -> Result<BTreeSet<PyStr>, UseTokenError> {
    let mut out = BTreeSet::new();
    for event in log.events_backward() {
        let ev_ts = event_ts_epoch(get(&event, "ts"));
        if ev_ts.is_some_and(|t| t < oldest_pending_issued_at) {
            break;
        }
        if !is_str(get(&event, "kind"), "use") {
            continue;
        }
        let session = or(get(&event, "session"), get(&event, "session_id"));
        if let Some(t) = session.and_then(unhashable) {
            return Err(UseTokenError::Unhashable(t));
        }
        let ours = match session {
            Some(PyValue::Str(s)) => {
                allowed_sessions.contains(s) || is_str(get(&event, "triggered_from"), "stop_hook")
            }
            _ => false,
        };
        if !ours {
            continue;
        }
        let Some(ev_ts) = ev_ts else { continue };
        let ids = or(get(&event, "ids"), get(&event, "memory_ids"));
        let ids = match ids {
            Some(v) if truthy(v) => v,
            _ => continue,
        };
        let PyValue::List(ids) = ids else { continue };
        for mid in ids {
            let PyValue::Str(mid) = mid else { continue };
            if let Some(&issued) = pending_issued_at.get(mid) {
                if ev_ts >= issued {
                    out.insert(mid.clone());
                }
            }
        }
    }
    Ok(out)
}

/// `_already_recorded_pending_ids` over the state a caller holds the lock
/// of. `extra_pending` is in its dict's order.
pub fn already_recorded_in(
    st: &Inner,
    log: &mut dyn UseLog,
    extra_session_ids: Option<&BTreeSet<PyStr>>,
    extra_pending: Option<&[(PyStr, f64)]>,
) -> Result<BTreeSet<PyStr>, UseTokenError> {
    let extra = extra_pending.unwrap_or(&[]);
    if st.pending_use_tokens.is_empty() && extra.is_empty() {
        return Ok(BTreeSet::new());
    }
    // The dict's values in its order: an update keeps a key's place.
    let mut order: Vec<f64> = Vec::new();
    let mut place: HashMap<PyStr, usize> = HashMap::new();
    let live = st.pending_use_tokens.iter().map(|(m, t)| (m, t.issued_at));
    for (m, at) in live.chain(extra.iter().map(|(m, at)| (m, *at))) {
        match place.get(m) {
            Some(&i) => order[i] = at,
            None => {
                place.insert(m.clone(), order.len());
                order.push(at);
            }
        }
    }
    let oldest = py_min(order.iter().map(|at| (*at, ()))).map_or(0.0, |(k, ())| k);
    let pending: HashMap<PyStr, f64> = place.into_iter().map(|(m, i)| (m, order[i])).collect();
    let mut allowed = BTreeSet::from([log.session_for_request()]);
    if let Some(x) = extra_session_ids {
        allowed.extend(x.iter().cloned());
    }
    scan_settled_use_events(log, &allowed, &pending, oldest)
}

/// `_already_recorded_pending_ids(state, recorder, extra_session_ids=,
/// extra_pending=)`: the pending map read under the lock, the log scanned
/// after it is released, as in the reference.
pub fn already_recorded_pending_ids(
    state: &SessionState,
    log: &mut dyn UseLog,
    extra_session_ids: Option<&BTreeSet<PyStr>>,
    extra_pending: Option<&[(PyStr, f64)]>,
) -> Result<BTreeSet<PyStr>, UseTokenError> {
    let snapshot = state.lock().clone();
    already_recorded_in(&snapshot, log, extra_session_ids, extra_pending)
}

/// `_hook_attributed_pending_ids`, the legacy alias.
pub fn hook_attributed_pending_ids(
    state: &SessionState,
    log: &mut dyn UseLog,
    extra_session_ids: Option<&BTreeSet<PyStr>>,
    extra_pending: Option<&[(PyStr, f64)]>,
) -> Result<BTreeSet<PyStr>, UseTokenError> {
    already_recorded_pending_ids(state, log, extra_session_ids, extra_pending)
}

/// `_drain_expired_use_tokens(state, recorder, override_ids=)`.
pub fn drain_expired_use_tokens(
    st: &mut Inner,
    log: &mut dyn UseLog,
    override_ids: Option<&BTreeSet<PyStr>>,
) -> Result<Vec<PendingUseToken>, UseTokenError> {
    let mut expired = st.pop_expired_use_tokens();
    if let Some(o) = override_ids.filter(|o| !o.is_empty()) {
        expired.retain(|t| !o.contains(&t.memory_id));
    }
    if st.pending_use_tokens.is_empty() && expired.is_empty() {
        return Ok(expired);
    }
    let extra: Vec<(PyStr, f64)> = expired
        .iter()
        .map(|t| (t.memory_id.clone(), t.issued_at))
        .collect();
    let done = already_recorded_in(st, log, None, Some(&extra))?;
    for mid in &done {
        st.purge_use_token(mid);
    }
    expired.retain(|t| !done.contains(&t.memory_id));
    Ok(expired)
}

fn strs(ids: impl IntoIterator<Item = PyStr>) -> PyValue {
    PyValue::List(ids.into_iter().map(PyValue::Str).collect())
}

/// `_emit_expired_use_tokens(state, recorder, expired)`.
pub fn emit_expired_use_tokens(
    st: &Inner,
    log: &mut dyn UseLog,
    expired: &[PendingUseToken],
    fx: &mut dyn Effects,
) -> Result<(), UseTokenError> {
    let Some((_, oldest)) = py_min(expired.iter().map(|t| (t.issued_at, t))) else {
        return Ok(());
    };
    let now = fx.time();
    let age = py_int(&PyValue::Float(now - oldest.issued_at)).map_err(UseTokenError::Int)?;
    let turns = i128::from(st.turn_counter) - i128::from(oldest.issued_at_turn);
    log.record(
        "use_token_expired",
        vec![
            (
                PyValue::str("ids"),
                strs(expired.iter().map(|t| t.memory_id.clone())),
            ),
            (PyValue::str("age_seconds"), age),
            (PyValue::str("turns_since_issue"), PyValue::int(turns)),
            (PyValue::str("reason"), PyValue::str("wall_clock_ttl")),
        ],
    );
    Ok(())
}

/// `_advance_turn(state, recorder, override_ids=)`: everything under one
/// guard of the state's lock.
pub fn advance_turn(
    state: &SessionState,
    log: &mut dyn UseLog,
    override_ids: Option<&BTreeSet<PyStr>>,
    fx: &mut dyn Effects,
) -> Result<(), UseTokenError> {
    let mut st = state.lock();
    st.advance_turn(fx);
    let lost = drain_expired_use_tokens(&mut st, log, override_ids)?;
    emit_expired_use_tokens(&st, log, &lost, fx)?;
    let auto = st.consume_old_tokens(DEFAULT_USE_TOKEN_TTL_TURNS, None, override_ids, fx);
    if !auto.is_empty() {
        log.record(
            "use",
            vec![
                (PyValue::str("ids"), strs(auto)),
                (PyValue::str("outcome"), PyValue::str("applied")),
                (PyValue::str("auto"), PyValue::Bool(true)),
                (PyValue::str("attribution"), PyValue::str("auto")),
            ],
        );
    }
    Ok(())
}

/// `_attach_use_tokens(out, state)`: each hit dict gets its `use_token`
/// (assigned in place when present, else added last).
pub fn attach_use_tokens(
    out: &mut [Row],
    state: &SessionState,
    fx: &mut dyn Effects,
) -> Result<(), UseTokenError> {
    if out.is_empty() {
        return Ok(());
    }
    // The reference builds the whole id list first, so a hit without an
    // id raises KeyError even when an earlier id is one the port declines.
    if out.iter().any(|hit| get(hit, "id").is_none()) {
        return Err(UseTokenError::MissingId);
    }
    let mut ids = Vec::with_capacity(out.len());
    for hit in out.iter() {
        match get(hit, "id") {
            None => return Err(UseTokenError::MissingId),
            Some(PyValue::Str(s)) => ids.push(s.clone()),
            Some(_) => return Err(UseTokenError::NotPorted),
        }
    }
    let tokens = state.lock().issue_use_tokens(&ids, fx);
    let key = unicode::cps("use_token");
    for (hit, id) in out.iter_mut().zip(&ids) {
        let token = PyValue::str(tokens.get(id).map_or("", String::as_str));
        match hit
            .iter_mut()
            .rev()
            .find(|(k, _)| matches!(k, PyValue::Str(s) if *s == key))
        {
            Some(slot) => slot.1 = token,
            None => hit.push((PyValue::Str(key.clone()), token)),
        }
    }
    Ok(())
}
