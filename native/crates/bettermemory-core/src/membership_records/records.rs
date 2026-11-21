//! The manifest's JSON and match, the reader protocols and the records
//! `membership_rows` reads (`membership_records.py` 40-176, 321-445,
//! 540-679, 707-724 and 1152-1156): units 2 and 3 of the module's cut,
//! the portable identity aside.
//!
//! `InitStamp` is [`capture::Stamp`] and `Manifest` is
//! [`capture::Manifest`]; `Location` is [`capture::location::Location`].
//! The manifest's methods are free functions over the capture's record.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet, HashSet};

use crate::capture::location::{Location, ObjectKey};
use crate::capture::{Manifest, Stamp};
use crate::checkout_token::{self, Lineage, Raised};
use crate::pyvalue::{py_int, py_str, PyError, PyStr, PyValue};

/// `PRIMARY`, `LINKED`, `BARE`, `FOLDER`: the checkout kinds a capture
/// names (`capture::Kind`), and `UNRESOLVED` (decision B6), a path
/// written at while nothing could be read there.
pub const PRIMARY: &str = "primary";
pub const LINKED: &str = "linked";
pub const BARE: &str = "bare";
pub const FOLDER: &str = "folder";
pub const UNRESOLVED: &str = "unresolved";
/// `SUPERSEDED`: a checkout's token status while its token is one its
/// history supersedes (decision B15).
pub const SUPERSEDED: &str = "superseded";
/// The proposal kinds and states (design 2.1, 3.4).
pub const FIRST_SIGHT: &str = "first_sight";
pub const FILE_CONTRADICTION: &str = "file_contradiction";
pub const REMOTE_DIVERGED: &str = "remote_diverged";
pub const SUBPROJECT_CONTRADICTION: &str = "subproject_contradiction";
pub const DUPLICATE_CHECKOUT: &str = "duplicate_checkout";
pub const LEGACY: &str = "legacy";
pub const IMPORT: &str = "import";
pub const OPEN: &str = "open";
pub const ANSWERED: &str = "answered";

fn word(text: &[u32], w: &str) -> bool {
    text.iter().copied().eq(w.chars().map(u32::from))
}

/// `Manifest.to_json()`: `{"name": name, "entries": [[path, size], ...]}`.
pub fn manifest_to_json(m: &Manifest) -> PyValue {
    let entries = m
        .entries
        .iter()
        .map(|(path, size)| {
            PyValue::List(vec![PyValue::Str(path.clone()), PyValue::Int(size.clone())])
        })
        .collect();
    PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::Str(m.name.clone())),
        (PyValue::str("entries"), PyValue::List(entries)),
    ])
}

/// Python's truth value of a value JSON parsing yields.
fn truthy(v: &PyValue) -> bool {
    match v {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(t) => t != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) => !s.is_empty(),
        PyValue::Tuple(x) | PyValue::List(x) => !x.is_empty(),
        PyValue::Dict(x) => !x.is_empty(),
        PyValue::DateTime(_) | PyValue::Path(_) => true,
    }
}

/// `iter(v)`, read lazily: a dict yields its keys, a `str` its
/// characters; anything else is not iterable (TypeError). Items are
/// borrowed where they exist and made one at a time otherwise, so a caller
/// that stops early reads no further, as the reference's iterator does.
fn items(v: &PyValue) -> Result<Box<dyn Iterator<Item = Cow<'_, PyValue>> + '_>, PyError> {
    match v {
        PyValue::Tuple(x) | PyValue::List(x) => Ok(Box::new(x.iter().map(Cow::Borrowed))),
        PyValue::Dict(x) => Ok(Box::new(x.iter().map(|(k, _)| Cow::Borrowed(k)))),
        PyValue::Str(s) => Ok(Box::new(
            s.iter().map(|&c| Cow::Owned(PyValue::Str(vec![c]))),
        )),
        _ => Err(PyError::TypeError),
    }
}

/// `a, b = v`: TypeError where `v` is not iterable, ValueError for any
/// length but two. At most three items are read.
fn unpack_two(v: &PyValue) -> Result<(Cow<'_, PyValue>, Cow<'_, PyValue>), PyError> {
    let mut it = items(v)?;
    match (it.next(), it.next(), it.next()) {
        (Some(a), Some(b), None) => Ok((a, b)),
        _ => Err(PyError::ValueError),
    }
}

/// `Manifest.from_json(raw)`: None where `raw` is not a dict or its
/// `name` not a `str`; `raw.get("entries") or ()` read element by element,
/// each unpacked into two, then `str(path)` and `int(size)`. The first
/// raise in that order is the answer.
pub fn manifest_from_json(raw: &PyValue) -> Result<Option<Manifest>, PyError> {
    if !matches!(raw, PyValue::Dict(_)) {
        return Ok(None);
    }
    let Some(PyValue::Str(name)) = raw.get("name") else {
        return Ok(None);
    };
    let mut entries = Vec::new();
    if let Some(given) = raw.get("entries").filter(|v| truthy(v)) {
        for element in items(given)? {
            let (path, size) = unpack_two(&element)?;
            let path = py_str(&path)?;
            let PyValue::Int(ref size) = py_int(&size)? else {
                return Err(PyError::TypeError);
            };
            entries.push((path, size.clone()));
        }
    }
    Ok(Some(Manifest {
        name: name.clone(),
        entries,
    }))
}

/// `manifest_match(a, b)`: the Jaccard of the two (path, size) sets, a
/// float division of the set sizes; 0.0 for a missing manifest, another
/// folder name or two empty trees.
pub fn manifest_match(a: Option<&Manifest>, b: Option<&Manifest>) -> f64 {
    let (Some(a), Some(b)) = (a, b) else {
        return 0.0;
    };
    if a.name != b.name {
        return 0.0;
    }
    let left: HashSet<&(PyStr, String)> = a.entries.iter().collect();
    let right: HashSet<&(PyStr, String)> = b.entries.iter().collect();
    if left.is_empty() && right.is_empty() {
        return 0.0;
    }
    let both = left.intersection(&right).count();
    let either = left.len() + right.len() - both;
    // Both counts are below 2**53, so each converts exactly and the
    // division rounds once, as Python's `int / int` does.
    both as f64 / either as f64
}

/// `HistoryReader`: the repository a location was read from, as the
/// ladder asks it.
pub trait HistoryReader {
    fn holds(&self, commits: &[PyStr]) -> BTreeSet<PyStr>;
    fn holds_objects(&self, ids: &[PyStr]) -> BTreeSet<PyStr>;
    fn fingerprint(&self, commit: &[u32]) -> Option<PyStr>;
    fn parents(&self, commit: &[u32]) -> Vec<PyStr>;
    /// `{commit: the other commits it is reachable from}`, and whether a
    /// bound cut the reading short: `None` where the reader does not say,
    /// which the reference reads as cut short.
    fn reachable_among(
        &self,
        commits: &[PyStr],
    ) -> (BTreeMap<PyStr, BTreeSet<PyStr>>, Option<bool>);
    fn blobs(&self, commit: &[u32]) -> Vec<PyStr>;
}

/// `reachable_among`'s `bounded` as the reference reads it: a reader that
/// does not say was cut short.
pub fn bounded(said: Option<bool>) -> bool {
    said.unwrap_or(true)
}

/// `IndependentReader`: a history reader that can also ask git which of
/// `commits` no other of them reaches; None where git does not answer.
pub trait IndependentReader: HistoryReader {
    fn independent(&self, commits: &[PyStr]) -> Option<BTreeSet<PyStr>>;
}

/// `NeedIndependent`: raised where the most-specific rule wants git's
/// reading of `commits` outside the transaction.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NeedIndependent {
    pub commits: Vec<PyStr>,
}

impl NeedIndependent {
    /// The exception's message.
    pub const MESSAGE: &'static str = "git reads the tied witnesses outside the transaction";
}

/// `Specific`: what the most-specific rule read of tied projects.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Specific {
    pub best: Vec<PyStr>,
    pub top: BTreeSet<PyStr>,
    pub held: i64,
    pub bounded: bool,
}

/// `Machine`: a machine as the store records it.
#[derive(Clone, Debug, PartialEq)]
pub struct Machine {
    pub id: PyStr,
    /// A mapping, kept as the dict value it was read as.
    pub facts: PyValue,
    pub predecessor: Option<PyStr>,
    pub minted: PyStr,
}

/// `View`: a path at which a checkout was seen on a machine.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct View {
    pub checkout: PyStr,
    pub machine: PyStr,
    pub path: PyStr,
    pub first_seen: PyStr,
    pub last_seen: PyStr,
    pub order: i64,
}

/// `Checkout`: one git directory (or folder with a project file) on one
/// machine, or a path written at while nothing could be read there.
/// `kind` and `token_status` are text: `unresolved` is no capture kind.
#[derive(Clone, Debug, PartialEq)]
pub struct Checkout {
    pub id: PyStr,
    pub project: PyStr,
    pub machine: PyStr,
    pub kind: PyStr,
    pub object_key: Option<ObjectKey>,
    pub common_object_key: Option<ObjectKey>,
    pub git_dir: Option<PyStr>,
    pub token: Option<PyStr>,
    pub token_status: PyStr,
    pub stamps: Vec<Stamp>,
    pub last_head: Option<PyStr>,
    pub branch: Option<PyStr>,
    pub remotes: Vec<PyStr>,
    pub manifest: Option<Manifest>,
    pub legacy: bool,
    pub retired: bool,
    pub registered: PyStr,
    pub views: Vec<View>,
    pub sessions: Vec<PyStr>,
    pub replaced_by: Option<PyStr>,
    pub ticks: Option<bool>,
}

fn opt_str(v: &Option<PyStr>) -> PyValue {
    v.as_ref()
        .map_or(PyValue::None, |s| PyValue::Str(s.clone()))
}

impl Checkout {
    /// `token_superseded`: the token status is `superseded`.
    pub fn token_superseded(&self) -> bool {
        word(&self.token_status, SUPERSEDED)
    }

    /// `lineage`: `checkout_token.lineage(token, (device, inode))`, None
    /// without either or where either is malformed.
    pub fn lineage(&self) -> Option<Lineage> {
        let key = self.object_key.as_ref().map_or(PyValue::None, |k| {
            PyValue::Tuple(vec![
                PyValue::Str(k.device.clone()),
                PyValue::Str(k.inode.clone()),
            ])
        });
        checkout_token::lineage(&opt_str(&self.token), &key)
    }

    /// `token_refutes(found)`: false without a token of its own; for a
    /// superseded one, whether none was found; else any other token than
    /// its own and a re-mint of it. A raise of `succeeds` passes through.
    pub fn token_refutes(&self, found: Option<&[u32]>) -> Result<bool, Raised> {
        let Some(token) = self.token.as_deref().filter(|t| !t.is_empty()) else {
            return Ok(false);
        };
        if self.token_superseded() {
            return Ok(found.is_none_or(<[u32]>::is_empty));
        }
        if found == Some(token) {
            return Ok(false);
        }
        let found = found.map_or(PyValue::None, |f| PyValue::Str(f.to_vec()));
        Ok(!checkout_token::succeeds(&found, self.lineage().as_ref())?)
    }

    /// `views_on(machine)`: its views on `machine`, stably sorted by order.
    pub fn views_on(&self, machine: &[u32]) -> Vec<&View> {
        let mut out: Vec<&View> = self.views.iter().filter(|v| v.machine == machine).collect();
        out.sort_by_key(|v| v.order);
        out
    }

    /// `newest_view`: the view of the highest order, the first of equals.
    pub fn newest_view(&self) -> Option<&View> {
        let mut best: Option<&View> = None;
        for v in &self.views {
            if best.is_none_or(|b| v.order > b.order) {
                best = Some(v);
            }
        }
        best
    }
}

/// `_live_key(k, v)`: what a view's liveness depends on, compared as the
/// reference's tuple is.
#[derive(Clone, Debug, PartialEq)]
pub struct LiveKey {
    pub id: PyStr,
    pub kind: PyStr,
    pub object_key: Option<ObjectKey>,
    pub common_object_key: Option<ObjectKey>,
    pub token: Option<PyStr>,
    pub token_superseded: bool,
    pub last_head: Option<PyStr>,
    pub stamps: Vec<Stamp>,
    pub machine: PyStr,
    pub path: PyStr,
}

/// `_live_key(k, v)`.
pub fn live_key(k: &Checkout, v: &View) -> LiveKey {
    LiveKey {
        id: k.id.clone(),
        kind: k.kind.clone(),
        object_key: k.object_key.clone(),
        common_object_key: k.common_object_key.clone(),
        token: k.token.clone(),
        token_superseded: k.token_superseded(),
        last_head: k.last_head.clone(),
        stamps: k.stamps.clone(),
        machine: v.machine.clone(),
        path: v.path.clone(),
    }
}

/// `Project`: a set of checkouts and its evidence.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Project {
    pub id: PyStr,
    pub name: PyStr,
    pub merged_into: Option<PyStr>,
    pub provisional: Option<PyStr>,
    pub parent: Option<PyStr>,
    pub tenant: Option<PyStr>,
    pub folder: bool,
    pub boundary: bool,
    pub minted: PyStr,
}

/// `ProjectFile`: a project file id bound to a project.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProjectFile {
    pub file_id: PyStr,
    pub project: PyStr,
    pub evidence: PyStr,
}

/// `Proposal`: a durable question on one checkout. `evidence` is the
/// mapping as read, `signature` the tuple's items.
#[derive(Clone, Debug, PartialEq)]
pub struct Proposal {
    pub id: PyStr,
    pub checkout: Option<PyStr>,
    pub kind: PyStr,
    pub candidates: Vec<PyStr>,
    pub evidence: PyValue,
    pub reason: PyStr,
    pub line: PyStr,
    pub state: PyStr,
    pub answer: Option<PyStr>,
    pub answered_by: Option<PyStr>,
    pub signature: Option<Vec<PyValue>>,
    pub opened: PyStr,
}

/// `_found_token(k, loc)`: for a superseded token the one found; else the
/// one found or, where that is missing or empty, the one written.
pub fn found_token<'a>(k: &Checkout, loc: &'a Location) -> Option<&'a PyStr> {
    if k.token_superseded() {
        return loc.token.as_ref();
    }
    match &loc.token {
        Some(t) if !t.is_empty() => Some(t),
        _ => loc.token_written.as_ref(),
    }
}
