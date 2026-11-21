//! `history.py`'s third unit: what one log row of a record's history
//! says. `HistoryRow` and `to_dict`, `_changed`, `_body_diff` (through
//! [`difflib::body_diff`]), `_set`, `_event_detail`, `_RecordWalk`,
//! `_record_row` and `_link_change_rows`.
//!
//! What a later unit owns comes in as arguments: whether a row's MAC
//! checks (`_RowVerifier.verified`, unit 2) as `verified`, and the kinds
//! of a row's neighbours in the found rows (`_read_history`'s `adjacent`,
//! unit 4) as `before` and `after`, already looked up.
//!
//! Payload values come from `json.loads`, which hands every `NaN` it
//! reads as one shared object (`json.decoder.NaN`). Python's `!=` of two
//! values has no identity shortcut, so a top-level `NaN` differs from
//! itself; inside a list or a dict the comparison takes the shortcut
//! (`PyObject_RichCompareBool`), so a nested `NaN` equals a nested `NaN`.
//! [`py_ne`] reproduces both.

use std::collections::{BTreeSet, HashMap};

use crate::difflib;
use crate::history::{self, mapping, CAPTURE_MEMBER, RECORD_DELETES, VERSION_KINDS};
use crate::log::LogRow;
use crate::machine::truthy;
use crate::pyvalue::{lt_keys, PyError, PyStr, PyValue};
use crate::store::provenance::LOCAL;
use crate::store::writers::RELINK_MARK;
use crate::unicode::cps;

/// `store.IMPORTED` (`store_schema.IMPORTED`).
const IMPORTED: &str = "imported";
/// `store.UNACCOUNTED` (`store_schema.UNACCOUNTED`). No branch of this
/// unit reads it; it is named here for unit 4's `_state`.
#[allow(dead_code)]
const UNACCOUNTED: &str = "unaccounted";

/// `history.RECORD`.
pub const RECORD: &str = "record";
/// `history.REFERENCE`.
pub const REFERENCE: &str = "reference";
/// `history.ACTOR_FROM_RECORD`.
pub const ACTOR_FROM_RECORD: &str = "record";

/// `history._UNCOMPARED`.
pub const UNCOMPARED: [&str; 6] = [
    "id",
    "created",
    "updated",
    "removed",
    "removed_reason",
    "removed_session",
];
/// `history._CORROBORATION_FIELDS`.
pub const CORROBORATION_FIELDS: [&str; 2] = ["corroborations", "last_corroborated"];
/// `history._EVENT_DETAIL`, in its order.
pub const EVENT_DETAIL: [&str; 6] = [
    "status",
    "reason",
    "fields",
    "door",
    "attribution",
    "triggered_from",
];

/// `history.HistoryRow`: one log row of a record's history.
#[derive(Clone, Debug, PartialEq)]
pub struct HistoryRow {
    pub seq: i64,
    pub ts: String,
    pub session: Option<String>,
    pub kind: String,
    pub role: &'static str,
    pub action: String,
    pub verified: Option<bool>,
    pub actor: Option<PyValue>,
    pub actor_source: Option<&'static str>,
    pub changed: Vec<PyStr>,
    /// A dict's items in insertion order.
    pub detail: Vec<(PyValue, PyValue)>,
    pub diff: Option<PyStr>,
}

impl HistoryRow {
    /// `HistoryRow(**base, role=role, action=action, verified=verified)`
    /// with the defaults: `base` is the row's seq, ts, session and kind.
    pub fn new(row: &LogRow, role: &'static str, action: String, verified: Option<bool>) -> Self {
        HistoryRow {
            seq: row.seq,
            ts: row.ts.clone(),
            session: row.session.clone(),
            kind: row.kind.clone(),
            role,
            action,
            verified,
            actor: None,
            actor_source: None,
            changed: Vec::new(),
            detail: Vec::new(),
            diff: None,
        }
    }

    /// `HistoryRow.to_dict(diff=diff)`, its keys in the reference's order.
    pub fn to_dict(&self, diff: bool) -> PyValue {
        let opt = |v: &Option<String>| v.as_deref().map_or(PyValue::None, PyValue::str);
        let mut out = vec![
            ("seq", PyValue::int(i128::from(self.seq))),
            ("ts", PyValue::str(&self.ts)),
            ("session", opt(&self.session)),
            ("kind", PyValue::str(&self.kind)),
            ("role", PyValue::str(self.role)),
            ("action", PyValue::str(&self.action)),
            (
                "verified",
                self.verified.map_or(PyValue::None, PyValue::Bool),
            ),
            ("actor", self.actor.clone().unwrap_or(PyValue::None)),
            (
                "actor_source",
                self.actor_source.map_or(PyValue::None, PyValue::str),
            ),
            (
                "changed",
                PyValue::List(self.changed.iter().cloned().map(PyValue::Str).collect()),
            ),
            ("detail", PyValue::Dict(self.detail.clone())),
        ];
        if diff {
            out.push((
                "diff",
                self.diff.clone().map_or(PyValue::None, PyValue::Str),
            ));
        }
        PyValue::Dict(out.into_iter().map(|(k, v)| (PyValue::str(k), v)).collect())
    }
}

/// `history._RecordWalk`: what `_read_history` carries from one of the
/// record's own rows to the next.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct RecordWalk {
    pub record_kind: Option<&'static str>,
    pub previous: Option<PyValue>,
    pub previous_body: Option<PyStr>,
    pub previous_seq: Option<i64>,
    pub homed: bool,
    pub last_record_kind: Option<String>,
}

fn is_nan(v: &PyValue) -> bool {
    matches!(v, PyValue::Float(f) if f.is_nan())
}

/// Python's `a == b` over two scalars (anything but a container);
/// `nested` takes the identity shortcut a container's comparison takes,
/// under which `json.loads`'s one `NaN` equals itself.
fn scalar_eq(a: &PyValue, b: &PyValue, nested: bool) -> bool {
    use PyValue as P;
    match (a, b) {
        (P::None, P::None) => true,
        (P::Bool(_) | P::Int(_) | P::Float(_), P::Bool(_) | P::Int(_) | P::Float(_)) => {
            if is_nan(a) || is_nan(b) {
                return nested && is_nan(a) && is_nan(b);
            }
            lt_keys(a, b) == Some(false) && lt_keys(b, a) == Some(false)
        }
        (P::Str(x), P::Str(y)) | (P::Path(x), P::Path(y)) => x == y,
        (P::DateTime(x), P::DateTime(y)) => x == y,
        _ => false,
    }
}

/// Python's `a == b`, walked with a work list so a value nested as
/// deep as `json.loads` reads compares without recursion. A dict's key
/// is a scalar (`json.loads` makes every key a `str`).
fn py_eq(a: &PyValue, b: &PyValue) -> bool {
    use PyValue as P;
    let mut work = vec![(a, b, false)];
    while let Some((a, b, nested)) = work.pop() {
        match (a, b) {
            (P::List(x), P::List(y)) | (P::Tuple(x), P::Tuple(y)) => {
                if x.len() != y.len() {
                    return false;
                }
                work.extend(x.iter().zip(y).map(|(u, v)| (u, v, true)));
            }
            (P::Dict(x), P::Dict(y)) => {
                if x.len() != y.len() {
                    return false;
                }
                let keyed = str_items(b);
                for (k, u) in x {
                    let found = match k {
                        P::Str(k) => keyed.get(k).copied(),
                        _ => y
                            .iter()
                            .rev()
                            .find(|(j, _)| scalar_eq(k, j, true))
                            .map(|e| &e.1),
                    };
                    match found {
                        Some(v) => work.push((u, v, true)),
                        None => return false,
                    }
                }
            }
            _ => {
                if !scalar_eq(a, b, nested) {
                    return false;
                }
            }
        }
    }
    true
}

/// Python's `a != b` over two payload values (module text on `NaN`).
pub fn py_ne(a: &PyValue, b: &PyValue) -> bool {
    !py_eq(a, b)
}

/// The `str` keys of a dict, each with its value (the last, as a dict
/// keeps it).
fn str_items(record: &PyValue) -> HashMap<&PyStr, &PyValue> {
    let mut out = HashMap::new();
    if let PyValue::Dict(items) = record {
        for (k, v) in items {
            if let PyValue::Str(k) = k {
                out.insert(k, v);
            }
        }
    }
    out
}

/// `history._changed(before, after)`: the fields both carry, outside
/// `_UNCOMPARED`, whose values differ, sorted by code point. A payload
/// record's keys are `str` (`json.loads`).
pub fn changed(before: &PyValue, after: &PyValue) -> Vec<PyStr> {
    let (b, a) = (str_items(before), str_items(after));
    let uncompared: Vec<PyStr> = UNCOMPARED.iter().map(|n| cps(n)).collect();
    let names: BTreeSet<&PyStr> = b
        .iter()
        .filter(|(name, value)| {
            !uncompared.contains(name) && a.get(*name).is_some_and(|other| py_ne(value, other))
        })
        .map(|(name, _)| *name)
        .collect();
    names.into_iter().cloned().collect()
}

/// `history._set(detail, **values)`: each value that is not None,
/// set in place (a key already held keeps its position).
fn set(detail: &mut Vec<(PyValue, PyValue)>, values: &[(&str, Option<&PyValue>)]) {
    for (name, value) in values {
        match value {
            None | Some(PyValue::None) => {}
            Some(value) => put(detail, name, (*value).clone()),
        }
    }
}

fn put(detail: &mut Vec<(PyValue, PyValue)>, name: &str, value: PyValue) {
    let key = PyValue::str(name);
    match detail.iter_mut().find(|(k, _)| *k == key) {
        Some(slot) => slot.1 = value,
        None => detail.push((key, value)),
    }
}

/// `history._event_detail(payload)`. `value not in (None, "", [], {})`
/// drops exactly those four (0, False and `[[]]` are kept); an
/// acknowledgement is read by truthiness. `payload` is a dict.
pub fn event_detail(payload: &PyValue) -> Vec<(PyValue, PyValue)> {
    let mut detail = Vec::new();
    for name in EVENT_DETAIL {
        match payload.get(name) {
            None | Some(PyValue::None) => {}
            Some(PyValue::Str(s)) if s.is_empty() => {}
            Some(PyValue::List(v)) if v.is_empty() => {}
            Some(PyValue::Dict(v)) if v.is_empty() => {}
            Some(value) => put(&mut detail, name, value.clone()),
        }
    }
    if payload.get("forced") == Some(&PyValue::Bool(true)) {
        put(&mut detail, "forced", PyValue::Bool(true));
    }
    let suffix = cps("_acknowledged");
    let acknowledged: BTreeSet<PyStr> = str_items(payload)
        .into_iter()
        .filter(|(name, value)| name.ends_with(&suffix) && truthy(value))
        .map(|(name, _)| name[..name.len() - suffix.len()].to_vec())
        .collect();
    if !acknowledged.is_empty() {
        let list = acknowledged.into_iter().map(PyValue::Str).collect();
        put(&mut detail, "acknowledged", PyValue::List(list));
    }
    detail
}

fn is_str(value: Option<&PyValue>, text: &str) -> bool {
    matches!(value, Some(PyValue::Str(s)) if *s == cps(text))
}

/// `history._record_row`: one of the record's own rows, its versions
/// carried in `walk`. `payload` is the row's dict; `before` and `after`
/// are `adjacent(index, -1)` and `adjacent(index, 1)`; `verified` is
/// `verifier.verified(row)`.
pub fn record_row(
    walk: &mut RecordWalk,
    row: &LogRow,
    payload: &PyValue,
    before: Option<(&str, &PyValue)>,
    after: Option<(&str, &PyValue)>,
    verified: Option<bool>,
) -> HistoryRow {
    let kind = row.kind.as_str();
    let mut out = HistoryRow::new(row, RECORD, kind.to_string(), verified);
    let before = before.map(|b| b.0);
    let after = after.map(|a| a.0);
    let action: &str;
    let mut detail = Vec::new();
    let detail = &mut detail;
    if VERSION_KINDS.contains(&kind) {
        let prefix = kind.split('_').next().unwrap_or(kind);
        let record = mapping(payload.get(prefix))
            .cloned()
            .unwrap_or(PyValue::Dict(Vec::new()));
        walk.record_kind = Some(if prefix == "episode" {
            "episode"
        } else {
            "memory"
        });
        let body = match record.get("body") {
            Some(PyValue::Str(s)) => s.clone(),
            _ => PyStr::new(),
        };
        if let Some(previous) = &walk.previous {
            out.changed = changed(previous, &record);
        }
        if walk.previous_body.as_ref() != Some(&body) {
            let old = walk
                .previous_seq
                .map_or("(none)".to_string(), |s| format!("seq {s}"));
            out.diff = Some(difflib::body_diff(
                walk.previous_body.as_deref(),
                &body,
                &cps(&old),
                &cps(&format!("seq {}", row.seq)),
            ));
        }
        if let Some(author) = mapping(record.get("actor")).filter(|a| truthy(a)) {
            out.actor = Some(author.clone());
            out.actor_source = Some(ACTOR_FROM_RECORD);
        }
        let label = payload.get("provenance");
        if !matches!(label, None | Some(PyValue::None)) && !is_str(label, LOCAL) {
            put(
                detail,
                "provenance",
                label.cloned().unwrap_or(PyValue::None),
            );
        }
        if kind == "memory_put" {
            let corroboration = |c: &PyStr| CORROBORATION_FIELDS.iter().any(|f| cps(f) == *c);
            action = if before == Some("tombstone_delete") {
                "restored"
            } else if !matches!(payload.get(RELINK_MARK), None | Some(PyValue::None)) {
                set(detail, &[("restored", payload.get(RELINK_MARK))]);
                "relinked"
            } else if walk.previous.is_none() {
                if is_str(label, IMPORTED) {
                    "imported"
                } else {
                    "created"
                }
            } else if after == Some("verification_put") {
                "verified"
            } else if !out.changed.is_empty() && out.changed.iter().all(corroboration) {
                "corroborated"
            } else {
                "updated"
            };
        } else if kind == "tombstone_put" {
            set(detail, &[("reason", record.get("removed_reason"))]);
            let removing = before == Some("memory_delete");
            action = if removing || walk.previous.is_none() {
                "tombstoned"
            } else {
                "updated"
            };
        } else {
            action = if walk.previous.is_none() {
                "created"
            } else {
                "updated"
            };
        }
        walk.previous = Some(record);
        walk.previous_body = Some(body);
        walk.previous_seq = Some(row.seq);
    } else {
        let sub = |name: &str| {
            mapping(payload.get(name))
                .cloned()
                .unwrap_or(PyValue::Dict(Vec::new()))
        };
        action = match kind {
            "memory_delete" => {
                walk.record_kind = walk.record_kind.or(Some("memory"));
                if after == Some("tombstone_put") {
                    "removed"
                } else {
                    "deleted"
                }
            }
            "tombstone_delete" => {
                walk.record_kind = walk.record_kind.or(Some("memory"));
                if after == Some("memory_put") {
                    "restored"
                } else {
                    "pruned"
                }
            }
            "episode_delete" => {
                walk.record_kind = walk.record_kind.or(Some("episode"));
                "pruned"
            }
            "verification_put" => {
                let names = [
                    ("machine_id", payload.get("machine_id")),
                    ("verified_head", payload.get("verified_head")),
                ];
                set(detail, &names);
                "verification"
            }
            "verification_delete" => {
                set(detail, &[("machine_id", payload.get("machine_id"))]);
                "verification_removed"
            }
            "home_put" => {
                let home = sub("home");
                let moved = walk.homed;
                walk.homed = true;
                let names = [
                    ("project", home.get("project")),
                    ("checkout", home.get("checkout")),
                    ("rule", home.get("rule")),
                ];
                set(detail, &names);
                if moved {
                    "moved"
                } else {
                    "homed"
                }
            }
            "home_delete" => "unhomed",
            "standing_put" => {
                let standing = sub("standing");
                let names = [
                    ("probability", standing.get("probability")),
                    ("source", standing.get("source")),
                    ("answered_by", standing.get("answered_by")),
                ];
                set(detail, &names);
                "standing"
            }
            "standing_delete" => "standing_removed",
            "returned_put" => {
                set(detail, &[("via", sub("returned").get("via"))]);
                "returned"
            }
            "returned_delete" => {
                let dropped = payload.get("dropped");
                set(detail, &[("dropped", dropped)]);
                if matches!(dropped, None | Some(PyValue::None)) {
                    "released"
                } else {
                    "unmarked"
                }
            }
            CAPTURE_MEMBER => {
                let names = [
                    ("batch", payload.get("batch")),
                    ("joined_by", payload.get("joined_by")),
                ];
                set(detail, &names);
                "captured"
            }
            "import_put" => {
                set(detail, &[("source", payload.get("source"))]);
                "import"
            }
            _ => kind,
        };
    }
    if VERSION_KINDS.contains(&kind) || RECORD_DELETES.contains(&kind) {
        walk.last_record_kind = Some(kind.to_string());
    }
    out.action = action.to_string();
    out.detail = std::mem::take(detail);
    out
}

/// `history._link_change_rows`: the links to `record_id` that a linking
/// record's version adds or drops, advancing `links_by_source` to that
/// version's links. The error is `_link_types`'s.
pub fn link_change_rows(
    row: &LogRow,
    payload: &PyValue,
    subject: &[u32],
    record_id: &[u32],
    links_by_source: &mut HashMap<PyStr, BTreeSet<PyStr>>,
    verified: Option<bool>,
) -> Result<Vec<HistoryRow>, PyError> {
    let now = history::link_types(&row.kind, payload, record_id)?;
    let was = links_by_source
        .insert(subject.to_vec(), now.clone())
        .unwrap_or_default();
    let supersedes = cps("supersedes");
    let row_for = |action: &str, link_type: &PyStr| {
        let mut out = HistoryRow::new(row, REFERENCE, action.to_string(), verified);
        out.detail = vec![
            (PyValue::str("source"), PyValue::Str(subject.to_vec())),
            (PyValue::str("type"), PyValue::Str(link_type.clone())),
        ];
        out
    };
    let mut rows: Vec<HistoryRow> = now
        .difference(&was)
        .map(|t| {
            let action = if *t == supersedes {
                "superseded_by"
            } else {
                "linked_from"
            };
            row_for(action, t)
        })
        .collect();
    rows.extend(was.difference(&now).map(|t| row_for("unlinked", t)));
    Ok(rows)
}
