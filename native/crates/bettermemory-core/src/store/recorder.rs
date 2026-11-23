//! The recorder's bound `Store.record_event` on a signed store, a caller
//! transaction, or a keyless connection. One binding permits repeated calls.

use super::exception::StoreException;
use super::record_event::{
    record_bound, BoundCall, EventError, EventLogger, EventTransaction, PreparedPayload, Session,
};
use super::write::WriteError;
use crate::event_recorder::{ActorKeyObjects, Dispatch, EventActor, EventSink, PyException};
use crate::event_redact::{redact_event_fields, Spans};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::cps;

/// The ordinary credential detector used for both the recorder and store.
pub fn secret_spans(text: &[u32]) -> Vec<(usize, usize, PyStr)> {
    crate::credentials::spans::secret_spans(text)
        .into_iter()
        .map(|(start, end, name)| (start, end, cps(name)))
        .collect()
}

/// All effects borrowed by the Python bound store method. Pass the open
/// key state's refusal as `Err` when there is no signing key.
pub struct StoreEventSink<'a, 's> {
    pub target: Result<EventTransaction<'a, 's>, &'a [u32]>,
    pub log: &'a mut dyn EventLogger,
    pub spans: Spans<'a>,
}

impl EventSink for StoreEventSink<'_, '_> {
    fn record_event(
        &mut self,
        kind: &[u32],
        session: PyValue,
        actor: EventActor,
        best_effort: Option<bool>,
        attempts: Option<u32>,
        deadline: Option<f64>,
        fields: Vec<(PyStr, PyValue)>,
    ) -> Result<(), PyException> {
        self.record_event_typed(Dispatch {
            actor_keys: ActorKeyObjects::default(),
            kind,
            session,
            actor,
            best_effort,
            attempts,
            deadline,
            fields,
        })
        .map_err(|e| e.exception)
    }

    fn record_event_typed(&mut self, event: Dispatch<'_>) -> Result<(), StoreException> {
        let mut best_effort = event.best_effort.unwrap_or(true);
        let mut attempts = PyValue::int(event.attempts.unwrap_or(1).into());
        let mut deadline = event.deadline.map_or(PyValue::None, PyValue::Float);
        let mut fields = Vec::with_capacity(event.fields.len());
        let names = [cps("best_effort"), cps("attempts"), cps("deadline")];
        for (key, value) in event.fields {
            if key == names[0] {
                best_effort = super::home::truthy(&value);
            } else if key == names[1] {
                attempts = value;
            } else if key == names[2] {
                deadline = value;
            } else {
                fields.push((PyValue::Str(key), value));
            }
        }
        let call = BoundCall {
            kind: event.kind,
            session: Session::Value(&event.session),
            best_effort,
            attempts,
            deadline,
        };
        let target = self
            .target
            .as_mut()
            .map(EventTransaction::reborrow)
            .map_err(|text| *text);
        record_bound(
            target,
            &call,
            || {
                let mut payload = redact_event_fields(&fields, self.spans);
                let (record, actor_keys) = match event.actor {
                    EventActor::None => {
                        return Ok(PreparedPayload {
                            value: PyValue::Dict(payload),
                            actor_keys: ActorKeyObjects::default(),
                        })
                    }
                    EventActor::Published(actor) => (actor.to_record(), ActorKeyObjects::default()),
                    EventActor::Given(record @ PyValue::Dict(_)) => (record, event.actor_keys),
                    EventActor::Given(value) => {
                        actor_record(&value, &event.actor_keys).map_err(EventError::Guard)?
                    }
                };
                if matches!(&record, PyValue::Dict(entries) if !entries.is_empty()) {
                    // Recorder already removed the one actor keyword from the fields.
                    payload.push((PyValue::str("actor"), record));
                }
                Ok(PreparedPayload {
                    value: PyValue::Dict(payload),
                    actor_keys,
                })
            },
            self.log,
        )
        .map(|_| ())
        .map_err(exception)
    }
}

/// Python's `dict(actor)` with the identities of its surviving keys.
pub fn actor_record(
    value: &PyValue,
    objects: &ActorKeyObjects,
) -> Result<(PyValue, ActorKeyObjects), PyException> {
    super::writers::py_dict_with_objects(value, objects).map_err(|e| PyException {
        class: e.class(),
        message: e.text().unwrap_or_default(),
    })
}

fn exception(e: EventError) -> StoreException {
    let error = match e {
        EventError::Value(message) => {
            return PyException {
                class: "ValueError".into(),
                message,
            }
            .into()
        }
        EventError::Log(raised) => return raised,
        EventError::Guard(raised) => return raised.into(),
        EventError::Write(error) => error,
    };
    let class = match error {
        WriteError::Callback(raised) => return raised,
        WriteError::Clock(error) => return StoreException::os(&error, None, None),
        WriteError::Sqlite(ref sql) => {
            let (class, code) = super::reads::failure_of(sql).unwrap_or(("DatabaseError", None));
            let mut raised = StoreException::from(PyException {
                class: class.into(),
                message: cps(&error.to_string()),
            });
            raised.sqlite_code = code;
            return raised;
        }
        WriteError::Refused { class, text } => {
            return PyException {
                class: class.into(),
                message: cps(text.as_deref().unwrap_or("")),
            }
            .into()
        }
        WriteError::LaterWriter { .. } | WriteError::RolledBack(_) => "OperationalError",
        WriteError::Version(_) => "StoreVersionError",
        WriteError::NotAnObject => "TypeError",
        WriteError::NotPorted(_) => "NotImplementedError",
    };
    PyException {
        class: class.into(),
        message: cps(&error.to_string()),
    }
    .into()
}
