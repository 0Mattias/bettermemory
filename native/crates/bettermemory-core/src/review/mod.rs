//! The sealed tables of `review.py` (unit review-1): the related_for
//! constants, the thresholds and their overrides, the labels, the four
//! statements, `QUESTIONS` with its payload and the seal shas, the action
//! and detector names, and the roster line. The related-set records and
//! the text one related entry shows are in [`related`], re-exported here.
//!
//! `related_for`, `roster_candidates` and `roster_for` read the store and
//! the engine and are left for unit review-3; the answers and the
//! decision for the units that add `answers.rs` and `decision.rs` beside
//! this file.

use std::marker::PhantomData;
use std::sync::{Condvar, Mutex, MutexGuard, OnceLock};
use std::thread::{self, ThreadId};

use sha2::{Digest, Sha256};

use crate::json;
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::unicode;

mod related;
pub use related::*;

/// `RELATED_TOP`: the engine's top hits for the write's content.
pub const RELATED_TOP: usize = 9;
/// `RELATED_CAP`: the most related statements one review shows.
pub const RELATED_CAP: usize = 11;
/// `RELATED_TEXT_CHARS`: the most characters of one related statement.
pub const RELATED_TEXT_CHARS: usize = 600;
/// `ROSTER_TEXT_CHARS`: the most characters of one roster line's text.
pub const ROSTER_TEXT_CHARS: usize = 120;

/// `RELATED_CONSTANTS`, in the reference's key order.
pub fn related_constants() -> PyValue {
    let int = |n: usize| PyValue::Int(n.to_string());
    PyValue::Dict(vec![
        (PyValue::str("top"), int(RELATED_TOP)),
        (PyValue::str("cap"), int(RELATED_CAP)),
        (PyValue::str("text_chars"), int(RELATED_TEXT_CHARS)),
    ])
}

/// The sealed `TAU_UNSAFE`, which the process-wide cell starts at.
pub const TAU_UNSAFE: Option<f64> = Some(0.3);
/// The sealed `TAU_SECRET`, likewise.
pub const TAU_SECRET: Option<f64> = Some(0.3);

/// `Tau`: the two thresholds in force for one call, both inclusive.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Tau {
    pub r#unsafe: f64,
    pub secret: f64,
}

/// The module attributes `TAU_UNSAFE` and `TAU_SECRET` as one cell.
static TAU: Mutex<(Option<f64>, Option<f64>)> = Mutex::new((TAU_UNSAFE, TAU_SECRET));

/// The serializing lock every override holds for its life: the thread
/// that holds it and how many of its guards are alive. A guard of another
/// thread waits on `FREED` until the count is zero. The same thread may
/// nest guards (the reference nests `with` blocks), so the lock counts
/// rather than being a plain `Mutex<()>`, which would deadlock a nested
/// override on its own thread.
static HOLDER: Mutex<Option<(ThreadId, usize)>> = Mutex::new(None);
static FREED: Condvar = Condvar::new();

fn tau_cell() -> MutexGuard<'static, (Option<f64>, Option<f64>)> {
    TAU.lock().unwrap_or_else(|e| e.into_inner())
}

fn hold() {
    let me = thread::current().id();
    let mut holder = HOLDER.lock().unwrap_or_else(|e| e.into_inner());
    loop {
        match *holder {
            None => {
                *holder = Some((me, 1));
                return;
            }
            Some((owner, n)) if owner == me => {
                *holder = Some((owner, n + 1));
                return;
            }
            Some(_) => holder = FREED.wait(holder).unwrap_or_else(|e| e.into_inner()),
        }
    }
}

fn release() {
    let mut holder = HOLDER.lock().unwrap_or_else(|e| e.into_inner());
    if let Some((owner, n)) = *holder {
        *holder = (n > 1).then_some((owner, n - 1));
        if holder.is_none() {
            FREED.notify_all();
        }
    }
}

/// `current_tau()`: the thresholds in force, or None while either is
/// unset, read at call time. Product code reads tau only through this.
pub fn current_tau() -> Option<Tau> {
    match *tau_cell() {
        (Some(u), Some(s)) => Some(Tau {
            r#unsafe: u,
            secret: s,
        }),
        _ => None,
    }
}

/// An override in force: on drop (also during a panic's unwinding) it
/// puts back the pair it found, as the reference's `finally` does, and
/// releases its hold on the serializing lock. The override lasts exactly
/// as long as the guard, so the guard is `#[must_use]` and carries the
/// yielded `Tau` itself: a caller cannot take the `Tau` and drop the
/// guard in one pattern. It is not `Send`: the hold belongs to the thread
/// that took it, and a drop on another thread would release that
/// thread's count.
#[must_use = "the override ends when the guard is dropped"]
pub struct TauGuard {
    saved: (Option<f64>, Option<f64>),
    tau: Option<Tau>,
    _not_send: PhantomData<*const ()>,
}

impl TauGuard {
    /// The `Tau` the reference's `with tau_override(...) as tau` yields;
    /// None for `tau_unset` and for a `tau_patched` pair with a None.
    pub fn tau(&self) -> Option<Tau> {
        self.tau
    }
}

impl Drop for TauGuard {
    fn drop(&mut self) {
        *tau_cell() = self.saved;
        release();
    }
}

/// Sets the pair under the serializing lock: what `mock.patch` of the
/// two module attributes does in the reference's tests.
pub fn tau_patched(unsafe_tau: Option<f64>, secret_tau: Option<f64>) -> TauGuard {
    hold();
    let mut cell = tau_cell();
    let saved = *cell;
    *cell = (unsafe_tau, secret_tau);
    let tau = match (unsafe_tau, secret_tau) {
        (Some(u), Some(s)) => Some(Tau {
            r#unsafe: u,
            secret: s,
        }),
        _ => None,
    };
    TauGuard {
        saved,
        tau,
        _not_send: PhantomData,
    }
}

/// `tau_override(unsafe, secret)`: both thresholds for the guard's life,
/// `secret` defaulting to `unsafe`; the yielded `Tau` is `guard.tau()`.
pub fn tau_override(unsafe_tau: f64, secret_tau: Option<f64>) -> TauGuard {
    tau_patched(Some(unsafe_tau), Some(secret_tau.unwrap_or(unsafe_tau)))
}

/// `tau_unset()`: both thresholds None for the guard's life.
pub fn tau_unset() -> TauGuard {
    tau_patched(None, None)
}

pub const LABEL_UPDATE: &str = "update";
pub const LABEL_EXTENDS: &str = "extends";
pub const LABEL_CONFLICT: &str = "conflict";
pub const LABEL_RESTATES: &str = "restates";
pub const LABEL_UNRELATED: &str = "unrelated";
/// `LABELS`, in the reference's order.
pub const LABELS: [&str; 5] = [
    LABEL_UPDATE,
    LABEL_EXTENDS,
    LABEL_CONFLICT,
    LABEL_RESTATES,
    LABEL_UNRELATED,
];
/// `LABEL_CHOICES`: each label quoted, joined by ` | `.
pub const LABEL_CHOICES: &str = r#""update" | "extends" | "conflict" | "restates" | "unrelated""#;

/// `UNSAFE_STATEMENT`.
pub const UNSAFE_STATEMENT: &str = "Following this write would weaken security, skip a safety or \
review step, expose data or credentials, or mislead the user, and the user did not ask for \
exactly that in this conversation.";
/// `SECRET_STATEMENT`.
pub const SECRET_STATEMENT: &str = "The new statement contains a credential, key, token or \
password value, or part of one, in any spelling or encoding.";
/// `STANDING_STATEMENT`, which `standing` re-exports.
pub const STANDING_STATEMENT: &str = "The statement directs future work in its scope until it \
is replaced: a ruling, rule or standing preference the user gave.";
/// `ROSTER_STATEMENT`, outside `QUESTIONS`.
pub const ROSTER_STATEMENT: &str = "Name every roster record this write restates, amends, \
extends or competes with, including one the write would break without saying so. Named \
records are shown to you in full next.";

fn dict(items: &[(&str, PyValue)]) -> PyValue {
    PyValue::Dict(
        items
            .iter()
            .map(|(k, v)| (PyValue::str(k), v.clone()))
            .collect(),
    )
}

/// `QUESTIONS`, built once, in the reference's key order. The reference
/// freezes it with `MappingProxyType`; here it is only ever lent.
pub fn questions() -> &'static PyValue {
    static QUESTIONS: OnceLock<PyValue> = OnceLock::new();
    QUESTIONS.get_or_init(|| {
        let s = PyValue::str;
        let asked = |text: &str| dict(&[("type", s("probability")), ("instructions", s(text))]);
        dict(&[
            (
                "relation",
                dict(&[
                    ("type", s("choice")),
                    (
                        "instructions",
                        s("For each related id: how does the new statement relate to that \
                           stored statement?"),
                    ),
                    (
                        "criteria",
                        dict(&[
                            (LABEL_UPDATE, s("It says a value changed and gives the new value.")),
                            (LABEL_EXTENDS, s("It adds to that stored statement, which stays in force.")),
                            (
                                LABEL_CONFLICT,
                                s("It gives a different value with no sign that anything changed."),
                            ),
                            (LABEL_RESTATES, s("It repeats what a stored statement already says.")),
                            (
                                LABEL_UNRELATED,
                                s("It is not about the same thing as any stored statement."),
                            ),
                        ]),
                    ),
                ]),
            ),
            ("unsafe", asked(UNSAFE_STATEMENT)),
            ("secret", asked(SECRET_STATEMENT)),
            ("standing", asked(STANDING_STATEMENT)),
        ])
    })
}

/// `questions_payload()`: `QUESTIONS` as plain dicts.
pub fn questions_payload() -> PyValue {
    questions().clone()
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn canonical_sha(value: &PyValue) -> Result<String, PyError> {
    json::review_canonical_json(value).map(|b| sha256_hex(&b))
}

/// `questions_sha256()`.
pub fn questions_sha256() -> String {
    canonical_sha(questions()).expect("QUESTIONS is plain text")
}

/// `constants_sha256()`.
pub fn constants_sha256() -> String {
    canonical_sha(&related_constants()).expect("the constants are ints")
}

/// `seal_shas()`: `questions` then `constants`.
pub fn seal_shas() -> [(&'static str, String); 2] {
    [
        ("questions", questions_sha256()),
        ("constants", constants_sha256()),
    ]
}

pub const ACTION_SECRET: &str = "refused_secret";
pub const ACTION_UNSAFE: &str = "refused_unsafe";
pub const ACTION_STANDING: &str = "refused_standing";
pub const ACTION_RESTATES: &str = "restates";
pub const ACTION_COMMIT: &str = "committed";
pub const DETECTOR_REVIEW: &str = "review";
pub const DETECTOR_REVIEW_DISAGREES: &str = "review-disagrees";
/// `REVIEW_LINK_NOTE`: the note on a supersedes link the review sets.
pub const REVIEW_LINK_NOTE: &str = "set by the write review";

/// `RosterLine`: one standing record as the roster shows it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RosterLine {
    pub id: PyStr,
    pub updated: PyStr,
    pub text: PyStr,
}

impl RosterLine {
    /// `to_line()`: `<id> (<updated>) <text>`.
    pub fn to_line(&self) -> PyStr {
        let mut out = self.id.clone();
        out.extend(unicode::cps(" ("));
        out.extend_from_slice(&self.updated);
        out.extend(unicode::cps(") "));
        out.extend_from_slice(&self.text);
        out
    }
}
