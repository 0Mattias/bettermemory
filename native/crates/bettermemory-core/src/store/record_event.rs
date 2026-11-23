//! The write side of `store_events.py` (lines 69-309): `record_event` with
//! its retry and deadline (`_primary_code`, `_busy`, `_time_left`,
//! `_waiting_until`), the v8 import (`import_event`, `imported_event_keys`,
//! `_named_payload`), `record_migration` and `record_capture`.
//!
//! Effects are arguments: the [`Provider`] stamps the rows (`now`), reads
//! the monotonic clock (`time.monotonic`) and sleeps the retry's pause
//! (`time.sleep`); `secret_spans` is the `spans` argument of
//! [`crate::event_redact`]; the deadline is a `time.monotonic()` reading
//! (the caller reads it from its `HookScope`). [`EventLogger`] emits each
//! store diagnostic at the point the reference invokes its logger.

use std::collections::BTreeSet;
use std::panic::{catch_unwind, resume_unwind, AssertUnwindSafe};
use std::sync::LazyLock;

use rusqlite::Connection;

use super::create::Provider;
use super::event_reads::CAPTURE_KINDS;
use super::exception::{ExceptionFamily, StoreException};
use super::open::WritableStore;
use super::reads::{failure_of, set_busy_timeout, ReadError};
use super::rows::{event_import_payload, not_telemetry};
use super::tx::Transaction;
use super::write::WriteError;
use crate::event_hook::{is_busy, primary_code, BusyTimeout};
use crate::event_redact::{name_fields, redact_event_fields, Spans};
use crate::json::{canonical_payload, loads};
use crate::log::LogRow;
use crate::pyvalue::key_objects::KeyObjects;
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::recall_prompt::PyException;
use crate::unicode;

/// `EVENT_RETRY_PAUSE_SECONDS`: the pause before a retried event write.
pub const EVENT_RETRY_PAUSE_SECONDS: f64 = 0.05;

/// `sqlite3.SQLITE_READONLY`.
pub const SQLITE_READONLY: i64 = 8;

/// One line the reference logs on `bettermemory.store`: its level and,
/// for a retry, the try it announces and the tries in all.
#[derive(Debug, Clone, PartialEq)]
pub enum Logged {
    /// `log.info("event %s: the store was busy (%s); try %d of %d")`.
    Retry {
        reason: PyStr,
        next: i64,
        tries: i64,
    },
    /// A retry whose Python integer arguments exceed the machine range.
    WideRetry {
        reason: PyStr,
        next: String,
        tries: String,
    },
    /// `log.warning("event %s not recorded: %s")`.
    NotRecorded(PyStr),
    /// WARNING, "head checkpoint for %s not written: %s"; the database
    /// path and the OSError are separate logging arguments.
    HeadCheckpoint {
        path: std::path::PathBuf,
        error: StoreException,
    },
}

/// Emit the store's diagnostic before retrying or returning. A Python log
/// handler may raise, in which case the operation stops at this call.
pub trait EventLogger {
    fn emit(&mut self, kind: &[u32], entry: Logged) -> Result<(), PyException>;

    /// A binding can preserve exception subclass information and the real
    /// SQLite code. For retry and terminal failures, cause is the original
    /// exception argument; a key-state refusal instead carries plain text.
    fn emit_typed(
        &mut self,
        kind: &[u32],
        entry: Logged,
        _cause: Option<&WriteError>,
    ) -> Result<(), StoreException> {
        self.emit(kind, entry).map_err(StoreException::from)
    }
}

impl EventLogger for Vec<Logged> {
    fn emit(&mut self, _kind: &[u32], entry: Logged) -> Result<(), PyException> {
        self.push(entry);
        Ok(())
    }
}

/// How a write of this module failed where the reference raises.
#[derive(Debug)]
pub enum EventError {
    /// ValueError with its text: a mutation or control kind given to
    /// `record_event`, a kind that is no capture kind, an event
    /// `event_import_payload` refuses.
    Value(PyStr),
    /// The store's error, raised (`best_effort` off, or a class
    /// `record_event` does not catch).
    Write(WriteError),
    /// What `record_capture`'s guard raised.
    Guard(PyException),
    /// A logging handler failed while reporting a retry or a terminal warning.
    Log(StoreException),
}

impl From<WriteError> for EventError {
    fn from(e: WriteError) -> Self {
        EventError::Write(e)
    }
}

/// `getattr(exc, "sqlite_errorcode", None)` of the OperationalError a
/// [`WriteError`] is in the reference; None for every other error.
pub fn sqlite_code(e: &WriteError) -> Option<i64> {
    match e {
        WriteError::Callback(e) if e.family == ExceptionFamily::Operational => e.sqlite_code,
        WriteError::Sqlite(e) => failure_of(e).and_then(|(class, code)| {
            if class == "OperationalError" {
                code
            } else {
                None
            }
        }),
        _ => None,
    }
}

/// `_busy(exc)`: SQLITE_BUSY or SQLITE_LOCKED. Every SQLite failure with
/// either primary code is an OperationalError in `sqlite3`.
pub fn busy(e: &WriteError) -> bool {
    is_busy("OperationalError", sqlite_code(e))
}

/// Whether `except (sqlite3.Error, OSError, RuntimeError)` catches `e`.
pub fn caught(e: &WriteError) -> bool {
    match e {
        WriteError::Callback(e) => matches!(
            e.family,
            ExceptionFamily::Os
                | ExceptionFamily::Runtime
                | ExceptionFamily::Sqlite
                | ExceptionFamily::Operational
        ),
        WriteError::Sqlite(e) => !matches!(failure_of(e), Some(("MemoryError", _))),
        WriteError::Clock(_) | WriteError::RolledBack(_) | WriteError::LaterWriter { .. } => true,
        WriteError::Refused { class, .. } => {
            matches!(
                *class,
                "OperationalError" | "RecursionError" | "RuntimeError" | "NotImplementedError"
            )
        }
        WriteError::Version(_) | WriteError::NotPorted(_) | WriteError::NotAnObject => false,
    }
}

/// `_time_left(deadline)` with `now` the monotonic reading.
pub fn time_left(deadline: Option<f64>, now: f64) -> bool {
    deadline.is_none_or(|d| d - now > EVENT_RETRY_PAUSE_SECONDS)
}

impl BusyTimeout<ReadError> for WritableStore {
    fn busy_timeout(&self) -> Option<f64> {
        Some(self.busy_timeout_seconds)
    }

    fn set_busy_timeout(&mut self, seconds: f64) -> Result<(), ReadError> {
        set_busy_timeout(self.connection(), seconds)?;
        self.busy_timeout_seconds = seconds;
        Ok(())
    }
}

fn dict_get<'a>(fields: &'a [(PyValue, PyValue)], name: &str) -> Option<&'a PyValue> {
    let name = unicode::cps(name);
    fields.iter().find_map(|(k, v)| match k {
        PyValue::Str(s) if *s == name => Some(v),
        _ => None,
    })
}

/// The payload `record_event` signs: `fields` through
/// `_redact_event_fields`, then `actor` (its record) set when the record
/// is not empty and the fields hold no `actor`.
pub fn event_payload(
    fields: &[(PyValue, PyValue)],
    actor: Option<&[(PyValue, PyValue)]>,
    spans: Spans,
) -> PyValue {
    let mut payload = redact_event_fields(fields, spans);
    if let Some(record) = actor.filter(|r| !r.is_empty()) {
        if dict_get(&payload, "actor").is_none() {
            payload.push((PyValue::str("actor"), PyValue::Dict(record.to_vec())));
        }
    }
    PyValue::Dict(payload)
}

/// What one `record_event` call asks besides the store.
pub struct EventCall<'a> {
    pub kind: &'a [u32],
    pub session: Option<&'a [u32]>,
    pub best_effort: bool,
    pub attempts: i64,
    pub deadline: Option<f64>,
}

// All reserved names are ASCII. A longer Python string can pass the kind
// check without allocating a UTF-8 copy of it.
static MAX_RESERVED_KIND: LazyLock<usize> = LazyLock::new(|| {
    use super::rows::{CONTROL_KINDS, MEMBERSHIP_EVENT_KINDS, MUTATION_TABLES};
    CONTROL_KINDS
        .iter()
        .chain(&MEMBERSHIP_EVENT_KINDS)
        .map(|k| k.len())
        .chain(MUTATION_TABLES.iter().map(|t| t.len() + "_delete".len()))
        .max()
        .unwrap_or(0)
});

fn refuse_kind(kind: &[u32]) -> Result<(), EventError> {
    if kind.len() <= *MAX_RESERVED_KIND
        && unicode::to_string(kind).is_some_and(|kind| not_telemetry(&kind))
    {
        let mut text = repr_str(kind);
        text.extend(unicode::cps(" is a mutation or control kind, not an event"));
        return Err(EventError::Value(text));
    }
    Ok(())
}

/// A caught failure records one warning when the call is best effort or
/// the storage failure carries a primary SQLITE_READONLY code.
pub fn warns_on_failure(e: &WriteError, best_effort: bool) -> bool {
    caught(e) && (best_effort || sqlite_code(e).map(primary_code) == Some(SQLITE_READONLY))
}

/// The end of a failed try: swallowed with its warning, or raised.
fn give_up(
    kind: &[u32],
    e: WriteError,
    best_effort: bool,
    log: &mut dyn EventLogger,
) -> Result<Option<LogRow>, EventError> {
    if !warns_on_failure(&e, best_effort) {
        return Err(EventError::Write(e));
    }
    log.emit_typed(kind, Logged::NotRecorded(reason(&e)), Some(&e))
        .map_err(EventError::Log)?;
    Ok(None)
}

fn reason(error: &WriteError) -> PyStr {
    match error {
        WriteError::Callback(error) => error.exception.message.clone(),
        WriteError::Clock(error) => unicode::cps(&super::restore_lock::os_error_text(error)),
        WriteError::Refused {
            text: Some(text), ..
        } => unicode::cps(text),
        other => unicode::cps(&other.to_string()),
    }
}

/// The transaction a writer owns or the outer transaction it joins.
pub enum EventTransaction<'a, 's> {
    Store(&'a mut WritableStore, &'a mut dyn Provider),
    Joined(&'a mut Transaction<'s>),
}

impl<'s> EventTransaction<'_, 's> {
    /// Borrow the same owner or active caller transaction for one operation.
    pub fn reborrow(&mut self) -> EventTransaction<'_, 's> {
        match self {
            Self::Store(store, provider) => EventTransaction::Store(store, &mut **provider),
            Self::Joined(tx) => EventTransaction::Joined(tx),
        }
    }

    fn write<T, E: From<WriteError>>(
        self,
        body: impl FnOnce(&mut Transaction<'_>) -> Result<T, E>,
    ) -> Result<T, E> {
        match self {
            Self::Store(store, provider) => store.batch(provider, None, body).map(|(v, _)| v),
            Self::Joined(tx) => tx.scope(body),
        }
    }
}

/// `Store.record_event`: a keyless connection passes its key state's
/// refusal. A mutation or control kind is refused before inspecting the
/// key. Payload redaction follows that check and runs once across retries.
pub fn record_event(
    target: Result<EventTransaction<'_, '_>, &[u32]>,
    call: &EventCall<'_>,
    fields: &[(PyValue, PyValue)],
    actor: Option<&[(PyValue, PyValue)]>,
    spans: Spans,
    log: &mut dyn EventLogger,
) -> Result<Option<LogRow>, EventError> {
    let controls = BoundCall {
        kind: call.kind,
        session: Session::Text(call.session),
        best_effort: call.best_effort,
        attempts: PyValue::int(call.attempts.into()),
        deadline: call.deadline.map_or(PyValue::None, PyValue::Float),
    };
    record_bound(
        target,
        &controls,
        || {
            Ok(PreparedPayload {
                value: event_payload(fields, actor, spans),
                actor_keys: KeyObjects::default(),
            })
        },
        log,
    )
}

pub(super) struct PreparedPayload {
    pub value: PyValue,
    pub actor_keys: KeyObjects,
}

pub(super) enum Session<'a> {
    Text(Option<&'a [u32]>),
    Value(&'a PyValue),
}

pub(super) struct BoundCall<'a> {
    pub kind: &'a [u32],
    pub session: Session<'a>,
    pub best_effort: bool,
    pub attempts: PyValue,
    pub deadline: PyValue,
}

impl BoundCall<'_> {
    fn append(
        &self,
        tx: &mut Transaction<'_>,
        payload: &PreparedPayload,
    ) -> Result<LogRow, WriteError> {
        match self.session {
            Session::Text(value) => tx.append_at(self.kind, &payload.value, value, None),
            Session::Value(value) => {
                tx.append_event(self.kind, &payload.value, value, &payload.actor_keys)
            }
        }
    }

    fn time_remaining(&self, provider: &mut dyn Provider) -> Result<Option<f64>, WriteError> {
        if matches!(self.deadline, PyValue::None) {
            return Ok(None);
        }
        let now = provider.event_monotonic()?;
        let deadline = match &self.deadline {
            PyValue::Bool(b) => f64::from(*b),
            PyValue::Float(f) => *f,
            PyValue::Int(s) => {
                s.parse::<f64>()
                    .ok()
                    .filter(|f| f.is_finite())
                    .ok_or_else(|| {
                        refused(
                            "OverflowError",
                            "int too large to convert to float".to_owned(),
                        )
                    })?
            }
            value => {
                return Err(refused(
                    "TypeError",
                    format!(
                        "unsupported operand type(s) for -: '{}' and 'float'",
                        super::home::type_name(value)
                    ),
                ))
            }
        };
        Ok(Some(deadline - now))
    }
}

fn refused(class: &'static str, text: String) -> WriteError {
    WriteError::Refused {
        class,
        text: Some(text),
    }
}

enum TryNumber {
    Small(i64),
    Wide(String),
}

impl TryNumber {
    // The caller's max(1, attempts), then the integer-only range.
    fn total(value: &PyValue) -> Result<Self, WriteError> {
        match value {
            PyValue::Int(s) if s.starts_with('-') => Ok(Self::Small(1)),
            PyValue::Int(s) => Ok(s
                .parse::<i64>()
                .map_or_else(|_| Self::Wide(s.clone()), |i| Self::Small(i.max(1)))),
            PyValue::Bool(_) => Ok(Self::Small(1)),
            PyValue::Float(f) if *f > 1.0 => Err(refused(
                "TypeError",
                "'float' object cannot be interpreted as an integer".into(),
            )),
            PyValue::Float(_) => Ok(Self::Small(1)),
            other => Err(refused(
                "TypeError",
                format!(
                    "'>' not supported between instances of '{}' and 'int'",
                    super::home::type_name(other)
                ),
            )),
        }
    }

    fn advance(&mut self) {
        match self {
            Self::Small(n) if *n < i64::MAX => *n += 1,
            Self::Small(n) => *self = Self::Wide((i128::from(*n) + 1).to_string()),
            Self::Wide(n) => *n = super::writers::plus_one(n),
        }
    }

    fn less_than(&self, total: &Self) -> bool {
        match (self, total) {
            (Self::Small(a), Self::Small(b)) => a < b,
            (Self::Small(_), Self::Wide(_)) => true,
            (Self::Wide(_), Self::Small(_)) => false,
            (Self::Wide(a), Self::Wide(b)) => (a.len(), a) < (b.len(), b),
        }
    }

    fn retry(&self, total: &Self, reason: PyStr) -> Logged {
        let text = |n: &Self| match n {
            Self::Small(i) => i.to_string(),
            Self::Wide(s) => s.clone(),
        };
        match (self, total) {
            (Self::Small(next), Self::Small(tries)) => Logged::Retry {
                reason,
                next: *next,
                tries: *tries,
            },
            _ => Logged::WideRetry {
                reason,
                next: text(self),
                tries: text(total),
            },
        }
    }
}

pub(super) fn record_bound(
    target: Result<EventTransaction<'_, '_>, &[u32]>,
    call: &BoundCall<'_>,
    payload: impl FnOnce() -> Result<PreparedPayload, EventError>,
    log: &mut dyn EventLogger,
) -> Result<Option<LogRow>, EventError> {
    refuse_kind(call.kind)?;
    let target = match target {
        Ok(target) => target,
        Err(refusal) => {
            log.emit_typed(call.kind, Logged::NotRecorded(refusal.to_vec()), None)
                .map_err(EventError::Log)?;
            return Ok(None);
        }
    };
    let payload = payload()?;
    let (store, provider) = match target {
        EventTransaction::Store(store, provider) => (store, provider),
        EventTransaction::Joined(tx) => {
            return match tx.scope(|t| call.append(t, &payload)) {
                Ok(row) => Ok(Some(row)),
                Err(e) => give_up(call.kind, e, call.best_effort, log),
            };
        }
    };
    let tries = TryNumber::total(&call.attempts)?;
    let mut attempt = TryNumber::Small(1);
    loop {
        let e = match one_try(store, provider, call, &payload, log) {
            Ok(row) => return Ok(Some(row)),
            Err(e) => e,
        };
        if attempt.less_than(&tries)
            && busy(&e)
            && call
                .time_remaining(provider)?
                .is_none_or(|left| left > EVENT_RETRY_PAUSE_SECONDS)
        {
            attempt.advance();
            log.emit_typed(call.kind, attempt.retry(&tries, reason(&e)), Some(&e))
                .map_err(EventError::Log)?;
            provider.event_pause(EVENT_RETRY_PAUSE_SECONDS)?;
            continue;
        }
        return give_up(call.kind, e, call.best_effort, log);
    }
}

/// One try under `_waiting_until(store, deadline)`.
fn one_try(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    call: &BoundCall<'_>,
    payload: &PreparedPayload,
    log: &mut dyn EventLogger,
) -> Result<LogRow, WriteError> {
    let mut lowered = None;
    if let Some(remaining) = call.time_remaining(provider)? {
        let held = store.busy_timeout_seconds;
        let left = remaining.max(0.0);
        if left < held {
            store.set_busy_timeout(left).map_err(timeout_error)?;
            lowered = Some(held);
        }
    }
    let out = catch_unwind(AssertUnwindSafe(|| {
        let mut tx = store.begin(provider)?;
        let row = call.append(&mut tx, payload)?;
        tx.commit_with_checkpoint(|_, path, error| {
            log.emit_typed(
                call.kind,
                Logged::HeadCheckpoint {
                    path: path.to_owned(),
                    error: error.clone(),
                },
                None,
            )
        })?;
        Ok(row)
    }));
    if let Some(held) = lowered {
        store.set_busy_timeout(held).map_err(timeout_error)?;
    }
    out.unwrap_or_else(|interrupt| resume_unwind(interrupt))
}

fn timeout_error(e: ReadError) -> WriteError {
    match e {
        ReadError::Sqlite(e) => WriteError::Sqlite(e),
        ReadError::Raised(class) => WriteError::Refused { class, text: None },
        ReadError::Worded(class, text) => WriteError::Refused {
            class,
            text: Some(text),
        },
    }
}

/// `Store.import_event(event, imported_from=)`: the v8 event under its own
/// `ts` (now when it has none) and `session`, in a transaction of its own.
pub fn import_event(
    target: EventTransaction<'_, '_>,
    event: &[(PyValue, PyValue)],
    imported_from: &[u32],
    spans: Spans,
) -> Result<LogRow, EventError> {
    let (ts, session, kind, payload) =
        event_import_payload(event, imported_from, spans).map_err(EventError::Value)?;
    target.write(|tx| {
        Ok(tx.append_at(
            &kind,
            &PyValue::Dict(payload),
            session.as_deref(),
            ts.as_deref(),
        )?)
    })
}

/// A key of `imported_event_keys`: `(ts, session, kind, payload text)`.
pub type ImportKey = (String, Option<String>, String, String);

/// `Store.imported_event_keys(imported_from)`: every telemetry row whose
/// payload text holds `canonical_payload({"imported_from": x})[1:-1]`
/// (a `LIKE`, so `%` and `_` in it match as wildcards), each column as
/// `str()` reads it, the payload named as the writer names it now.
pub fn imported_event_keys(
    conn: &Connection,
    imported_from: &[u32],
) -> Result<BTreeSet<ImportKey>, WriteError> {
    let needle = canonical_payload(&PyValue::Dict(vec![(
        PyValue::str("imported_from"),
        PyValue::Str(imported_from.to_vec()),
    )]))
    .map_err(super::write::py_error)?;
    let needle = &needle[1..needle.len() - 1];
    let mut st = conn
        .prepare("SELECT ts, session, kind, payload FROM log WHERE payload LIKE ? ORDER BY seq")?;
    let mut rows = st.query([format!("%{needle}%")])?;
    let mut out = BTreeSet::new();
    while let Some(row) = rows.next()? {
        let col = |i: usize| -> Result<Option<String>, WriteError> {
            let v = super::sql_value(row.get_ref(i)?).map_err(|_| decode_err())?;
            Ok(if matches!(v, super::SqlValue::Null) && i == 1 {
                None
            } else {
                Some(v.py_str())
            })
        };
        let (ts, session, kind, payload) = (col(0)?, col(1)?, col(2)?, col(3)?);
        let kind = kind.unwrap_or_default();
        if not_telemetry(&kind) {
            continue;
        }
        out.insert((
            ts.unwrap_or_default(),
            session,
            kind,
            named_payload(&payload.unwrap_or_default())?,
        ));
    }
    Ok(out)
}

fn decode_err() -> WriteError {
    WriteError::Refused {
        class: "OperationalError",
        text: None,
    }
}

/// `_named_payload(text)`: the text with its free-text fields named, or
/// the text itself when that changes nothing or it is no JSON object.
pub fn named_payload(text: &str) -> Result<String, WriteError> {
    let Ok(mut value) = loads(&unicode::cps(text)) else {
        return Ok(text.to_owned());
    };
    let PyValue::Dict(fields) = &mut value else {
        return Ok(text.to_owned());
    };
    if !name_fields(fields) {
        return Ok(text.to_owned());
    }
    canonical_payload(&value).map_err(super::write::py_error)
}

/// `Store.record_migration(source=, counts=, session=)`: the `migrate_v8`
/// control row, `engine_version` the provider's.
pub fn record_migration(
    target: EventTransaction<'_, '_>,
    source: &PyValue,
    counts: &[(PyValue, PyValue)],
    session: Option<&[u32]>,
) -> Result<LogRow, WriteError> {
    let version = match &target {
        EventTransaction::Store(_, provider) => provider.engine_version(),
        EventTransaction::Joined(tx) => tx.engine_version(),
    };
    let payload = PyValue::Dict(vec![
        (
            PyValue::str("imported_from"),
            PyValue::str(super::rows::IMPORTED_FROM_V8),
        ),
        (PyValue::str("source"), source.clone()),
        (PyValue::str("counts"), PyValue::Dict(counts.to_vec())),
        (PyValue::str("engine_version"), PyValue::str(&version)),
    ]);
    target.write(|tx| tx.append_at(&unicode::cps("migrate_v8"), &payload, session, None))
}

/// `record_capture`'s guard: what it raises aborts the write.
pub type CaptureGuard<'g> = dyn FnMut(&Transaction<'_>) -> Result<(), PyException> + 'g;

/// `Store.record_capture(kind, payload, session=, ts=, guard=)`: a capture
/// kind's row; `guard` runs inside the transaction, under the lock, and
/// its error aborts the write.
pub fn record_capture(
    target: EventTransaction<'_, '_>,
    kind: &[u32],
    payload: &[(PyValue, PyValue)],
    session: Option<&[u32]>,
    ts: Option<&[u32]>,
    guard: Option<&mut CaptureGuard<'_>>,
) -> Result<LogRow, EventError> {
    if !unicode::to_string(kind).is_some_and(|kind| CAPTURE_KINDS.contains(&kind.as_str())) {
        let mut text = repr_str(kind);
        text.extend(unicode::cps(" is not a capture kind"));
        return Err(EventError::Value(text));
    }
    target.write(|tx| {
        if let Some(guard) = guard {
            guard(tx).map_err(EventError::Guard)?;
        }
        Ok(tx.append_at(kind, &PyValue::Dict(payload.to_vec()), session, ts)?)
    })
}
