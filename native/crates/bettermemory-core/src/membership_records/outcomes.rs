//! The remaining frozen records of `membership_records.py` (725-768 and
//! 805-889): `Declaration`, `Declared`, `Why`, `UnresolvedProject`,
//! `Health`, `EpisodeRef`, `Handoff` and `RecallAdmission`. Plain
//! records; a field with a default in the reference has a constructor
//! that takes the reference's default. `Resolution` is
//! `store::caller::Resolution`.

use crate::datetime::DateTime;
use crate::log::LogRow;
use crate::membership_records::records::{Project, Proposal};
use crate::pyvalue::PyStr;

/// `Declaration` (design 8.8): a statement that outranks evidence.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Declaration {
    pub action: PyStr,
    pub principal: PyStr,
    pub checkout: Option<PyStr>,
    pub proposal: Option<PyStr>,
    pub target: Option<PyStr>,
    pub file_id: Option<PyStr>,
    pub name: Option<PyStr>,
    pub directory: Option<PyStr>,
    pub record: Option<PyStr>,
    pub boundary: bool,
    pub outer_tenant: Option<PyStr>,
    pub replaced: Option<PyStr>,
    pub said: Option<PyStr>,
}

impl Declaration {
    /// `Declaration(action, principal)`: every other field at its default.
    pub fn new(action: PyStr, principal: PyStr) -> Self {
        Declaration {
            action,
            principal,
            checkout: None,
            proposal: None,
            target: None,
            file_id: None,
            name: None,
            directory: None,
            record: None,
            boundary: false,
            outer_tenant: None,
            replaced: None,
            said: None,
        }
    }
}

/// `Declared`: what a declaration changed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Declared {
    pub action: PyStr,
    pub proposal: Option<PyStr>,
    pub project: Option<PyStr>,
    pub rows: Vec<i64>,
    pub remint_checkout: Option<PyStr>,
}

impl Declared {
    /// `Declared(action, proposal, project, rows)`: no remint checkout.
    pub fn new(
        action: PyStr,
        proposal: Option<PyStr>,
        project: Option<PyStr>,
        rows: Vec<i64>,
    ) -> Self {
        Declared {
            action,
            proposal,
            project,
            rows,
            remint_checkout: None,
        }
    }
}

/// `Why`: a record's home and the log rows that decided it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Why {
    pub record_id: PyStr,
    pub project: Option<PyStr>,
    pub checkout: Option<PyStr>,
    pub rule: Option<PyStr>,
    pub rows: Vec<LogRow>,
    pub text: PyStr,
}

/// `UnresolvedProject`: a provisional project of decision B6.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UnresolvedProject<N = i64> {
    pub project: PyStr,
    pub name: PyStr,
    pub state: PyStr,
    pub checkout: Option<PyStr>,
    pub path: Option<PyStr>,
    pub memories: N,
    pub episodes: N,
}

/// `Health`: the membership section of health. `suppressed_turns` keeps
/// the mapping's order. `unresolved` defaults to empty.
#[derive(Clone, Debug, PartialEq)]
pub struct Health<N = i64> {
    pub open_proposals: Vec<Proposal>,
    pub provisional: Vec<Project>,
    pub suppressed_turns: Vec<(PyStr, N)>,
    pub indeterminate: N,
    pub deferred: N,
    pub without_live_view: Vec<PyStr>,
    pub unresolved: Vec<UnresolvedProject<N>>,
}

impl<N> Health<N> {
    /// `Health(open_proposals, provisional, suppressed_turns,
    /// indeterminate, deferred, without_live_view)`: nothing unresolved.
    pub fn new(
        open_proposals: Vec<Proposal>,
        provisional: Vec<Project>,
        suppressed_turns: Vec<(PyStr, N)>,
        indeterminate: N,
        deferred: N,
        without_live_view: Vec<PyStr>,
    ) -> Self {
        Health {
            open_proposals,
            provisional,
            suppressed_turns,
            indeterminate,
            deferred,
            without_live_view,
            unresolved: Vec::new(),
        }
    }
}

/// `EpisodeRef`: an episode as the handoff selects it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EpisodeRef {
    pub id: PyStr,
    pub session_id: PyStr,
    pub created: DateTime,
}

/// `Handoff`: the handoff's selection (design 3.6).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Handoff {
    pub session_id: Option<PyStr>,
    pub rule: PyStr,
    pub episodes: Vec<PyStr>,
    pub label: Option<PyStr>,
}

impl Handoff {
    /// `Handoff(session_id, rule)`: no episodes, no label.
    pub fn new(session_id: Option<PyStr>, rule: PyStr) -> Self {
        Handoff {
            session_id,
            rule,
            episodes: Vec::new(),
            label: None,
        }
    }
}

/// `RecallAdmission`: a prompt recall's admission over its hits.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RecallAdmission {
    pub delivered: Vec<PyStr>,
    pub suppressed: Vec<(PyStr, PyStr)>,
}
