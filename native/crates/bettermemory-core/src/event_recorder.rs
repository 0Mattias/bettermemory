//! `events.py`'s fourth unit: the per-process telemetry writer,
//! `Recorder` (`session_for_request`, `worktree_for_request`, `records`,
//! `record`), over a store taken as a trait ([`EventSink`]).
//!
//! The reference reads its two context variables (`_SUPPRESSED`,
//! `_DEADLINE`) and the published caller (`identity._CURRENT`) ambiently;
//! here both are arguments of each call ([`HookScope`], [`Published`]).
//! The workspace declaration, `_handlers.capture_origin().worktree_root`
//! and `credentials.secret_spans` are closures the recorder holds.
//! [`Recorder::warn`] emits each warning when the store call fails.

use crate::event_hook::HookScope;
use crate::event_redact::redact_query;
use crate::events::{
    row_class, state_fields, RowClass, HARNESS_SESSION_FIELD, HARNESS_STAMPED_KINDS,
    STATE_WRITE_ATTEMPTS,
};
use crate::identity::{Actor, Published, SOURCE_HEADER, SOURCE_TRANSPORT};
use crate::pyvalue::{PyStr, PyValue};
pub use crate::recall_prompt::PyException;
use crate::unicode::cps;

/// The `actor=` a row is recorded with: the handler's own `actor` field
/// (any value but None), else the published actor, else None.
#[derive(Clone, Debug, PartialEq)]
pub enum EventActor {
    Given(PyValue),
    Published(Actor),
    None,
}

/// `Store.record_event` as the recorder calls it. `best_effort` and
/// `attempts` are passed only on a state row, `deadline` only inside a
/// hook body; a field whose name is one of them and that the recorder
/// did not pass stays among `fields`, where Python's keyword binding
/// hands it to that parameter.
pub trait EventSink {
    #[allow(clippy::too_many_arguments)]
    fn record_event(
        &mut self,
        kind: &[u32],
        session: PyValue,
        actor: EventActor,
        best_effort: Option<bool>,
        attempts: Option<u32>,
        deadline: Option<f64>,
        fields: Vec<(PyStr, PyValue)>,
    ) -> Result<(), PyException>;

    /// The callable's name in CPython's `got multiple values for keyword
    /// argument` text.
    fn call_name(&self) -> String {
        "bettermemory.store_events.record_event".to_owned()
    }
}

/// The detector spans of `credentials.secret_spans`.
pub type SecretSpans = Box<dyn FnMut(&[u32]) -> Vec<(usize, usize, PyStr)>>;
/// `identity.workspace_declaration()` over the published caller.
pub type Declaration = Box<dyn FnMut(&Published) -> Result<Option<(PyStr, PyStr)>, PyException>>;
/// `_handlers.capture_origin().worktree_root`.
pub type CaptureWorktree = Box<dyn FnMut() -> Result<Option<PyStr>, PyException>>;

/// `events.Recorder`.
pub struct Recorder<S: EventSink> {
    pub store: S,
    pub session_id: PyStr,
    pub enabled: bool,
    pub worktree_root: Option<PyStr>,
    pub attribution: PyStr,
    pub triggered_from: PyStr,
    pub secret_spans: SecretSpans,
    pub declaration: Declaration,
    pub capture_worktree: CaptureWorktree,
    /// The event logger's warning handler.
    pub warn: Box<dyn FnMut(PyStr) -> Result<(), PyException>>,
}

fn position(fields: &[(PyStr, PyValue)], name: &str) -> Option<usize> {
    let name = cps(name);
    fields.iter().position(|(k, _)| *k == name)
}

fn set_default(fields: &mut Vec<(PyStr, PyValue)>, name: &str, value: impl FnOnce() -> PyValue) {
    if position(fields, name).is_none() {
        fields.push((cps(name), value()));
    }
}

fn type_error(message: String) -> PyException {
    PyException {
        class: "TypeError".to_owned(),
        message: cps(&message),
    }
}

fn joined(parts: &[&[u32]]) -> PyStr {
    parts.concat()
}

impl<S: EventSink> Recorder<S> {
    /// `session_for_request()`: the published actor's session when its
    /// source is the transport or the header, else this process's.
    pub fn session_for_request(&self, published: &Published) -> PyStr {
        if let Some(actor) = published.current_actor() {
            if let Some(session) = &actor.session {
                let source = actor.source("session");
                if source == Some(&cps(SOURCE_TRANSPORT)[..])
                    || source == Some(&cps(SOURCE_HEADER)[..])
                {
                    return session.clone();
                }
            }
        }
        self.session_id.clone()
    }

    /// `worktree_for_request()`.
    pub fn worktree_for_request(
        &mut self,
        published: &Published,
    ) -> Result<Option<PyStr>, PyException> {
        if (self.declaration)(published)?.is_none() {
            return Ok(self.worktree_root.clone());
        }
        (self.capture_worktree)()
    }

    /// `records(kind)`.
    pub fn records(&self, scope: &HookScope, kind: &[u32]) -> bool {
        if scope.is_suppressed() {
            return false;
        }
        self.enabled || row_class(kind) == RowClass::State
    }

    /// `record(kind, **fields)`. `Ok` when the row was written, skipped,
    /// or a measurement row failed and its warning was emitted. Returns the
    /// logging handler's error if emitting the warning fails, or what the
    /// reference raises: a state row's failure (logged first), and the
    /// declaration's and the capture's errors on either class.
    pub fn record(
        &mut self,
        scope: &HookScope,
        published: &Published,
        kind: &[u32],
        mut fields: Vec<(PyStr, PyValue)>,
    ) -> Result<(), PyException> {
        let positional = [(cps("self"), "self"), (cps("kind"), "kind")];
        for (key, _) in &fields {
            if let Some((_, name)) = positional.iter().find(|(bound, _)| bound == key) {
                return Err(type_error(format!(
                    "Recorder.record() got multiple values for argument '{name}'"
                )));
            }
        }
        if scope.is_suppressed() {
            return Ok(());
        }
        let state = row_class(kind) == RowClass::State;
        if !self.enabled && !state {
            return Ok(());
        }
        if !self.attribution.is_empty() {
            set_default(&mut fields, "attribution", || {
                PyValue::Str(self.attribution.clone())
            });
        }
        if !self.triggered_from.is_empty() {
            set_default(&mut fields, "triggered_from", || {
                PyValue::Str(self.triggered_from.clone())
            });
        }
        if position(&fields, "worktree_root").is_none() {
            if let Some(root) = self.worktree_for_request(published)? {
                fields.push((cps("worktree_root"), PyValue::Str(root)));
            }
        }
        if HARNESS_STAMPED_KINDS.iter().any(|k| cps(k) == kind) {
            if let Some(harness) = published.current_harness_session() {
                set_default(&mut fields, HARNESS_SESSION_FIELD, || {
                    PyValue::Str(harness.to_vec())
                });
            }
        }
        let given = position(&fields, "actor").map(|i| fields.remove(i).1);
        let actor = match given {
            Some(value) if value != PyValue::None => EventActor::Given(value),
            _ => match published.current_actor() {
                Some(actor) => EventActor::Published(actor.clone()),
                None => EventActor::None,
            },
        };
        let session = match position(&fields, "session") {
            Some(i) => fields.remove(i).1,
            None => PyValue::Str(self.session_for_request(published)),
        };
        if state && !self.enabled {
            let items: Vec<(PyValue, PyValue)> = fields
                .into_iter()
                .map(|(k, v)| (PyValue::Str(k), v))
                .collect();
            let spans = &mut self.secret_spans;
            let kept = state_fields(kind, &items, &mut |t| redact_query(t, &mut **spans))
                .expect("a keyword's name is a str");
            fields = kept
                .into_iter()
                .filter_map(|(k, v)| match &k {
                    PyValue::Str(k) => Some((k.clone(), v)),
                    _ => None,
                })
                .collect();
        }
        let deadline = scope.deadline();
        let mut passed = vec!["session", "actor"];
        if state {
            passed.extend(["best_effort", "attempts"]);
        }
        if deadline.is_some() {
            passed.push("deadline");
        }
        let duplicate = fields
            .iter()
            .find_map(|(k, _)| passed.iter().find(|p| cps(p) == *k));
        let (best_effort, attempts) = if state {
            (Some(false), Some(STATE_WRITE_ATTEMPTS))
        } else {
            (None, None)
        };
        let outcome = match duplicate {
            Some(name) => Err(type_error(format!(
                "{}() got multiple values for keyword argument '{name}'",
                self.store.call_name()
            ))),
            None => self.store.record_event(
                kind,
                session,
                actor,
                best_effort,
                attempts,
                deadline,
                fields,
            ),
        };
        let Err(exc) = outcome else {
            return Ok(());
        };
        if !state {
            let text = joined(&[&cps("event "), kind, &cps(" not recorded: "), &exc.message]);
            return (self.warn)(text);
        }
        (self.warn)(joined(&[
            &cps("state row "),
            kind,
            &cps(" not recorded, the call fails: "),
            &cps(&exc.class),
            &cps(": "),
            &exc.message,
        ]))?;
        Err(exc)
    }
}
