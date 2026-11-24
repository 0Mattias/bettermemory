//! `history.py`'s fourth unit: one record's whole history.
//! `RecordHistory` (`known`, `findings`, `exit_code`, `to_dict`),
//! `record_history`, `_read_history` with its `adjacent`, `_table_kind`,
//! `_state`, `replaced_detail`, and the constants `FORMAT`,
//! `FORMAT_VERSION`, `EVENT`, `STORE` and `ACTOR_FROM_EVENT`.
//!
//! The store's sides are arguments: the [`Source`] whose connection runs
//! every statement and whose `meta` names the store's key
//! (`store.key_fingerprint`), the in-memory [`KeyRing`] the row verifier
//! reads, and a [`Provenance`] reader over that same source
//! (`store.provenance_for`, `store.episode_provenance`,
//! `store.can_verify`).
//!
//! The record id is code points. `_rows_naming` binds it before anything
//! else of it is used, so an id holding a lone surrogate is
//! UnicodeEncodeError there, after the verifier's reads and before any
//! row is walked; by the time `_state` binds it again and asks the
//! provenance reader (which takes `&str`), it is valid text.
//!
//! `verifier.verified(row)` is asked once per row written, as the
//! reference asks it. The ring is in memory, so the answer is a pure
//! function of the row and the order of the asks changes nothing a caller
//! can see.

use std::collections::{BTreeSet, HashMap};

use rusqlite::{Connection, OptionalExtension};

use crate::history::{
    self, mapping, names_as_event, rows_naming, subject, with_linking_versions, with_restores,
    Found, RowVerifier, HISTORY_REPLACED, LINKING_KINDS, LINK_CARRIED_BY, RECORD_DELETES,
};
use crate::history_rows::REFERENCE;
use crate::history_rows::{event_detail, link_change_rows, record_row, HistoryRow, RecordWalk};
use crate::log::KeyRing;
use crate::pyvalue::{cmp_int_text, PyError, PyStr, PyValue};
use crate::store::provenance::{Provenance, ProvenanceError, Source, UNACCOUNTED};
use crate::store::StoreError;
use crate::unicode;

/// `history.FORMAT`.
pub const FORMAT: &str = "bettermemory-history";
/// `history.FORMAT_VERSION`.
pub const FORMAT_VERSION: i128 = 1;
/// `history.EVENT`: a telemetry row naming the record.
pub const EVENT: &str = "event";
/// `history.STORE`: a row that says the store's history was replaced.
pub const STORE: &str = "store";
/// `history.ACTOR_FROM_EVENT`: the actor came from the request.
pub const ACTOR_FROM_EVENT: &str = "event";

/// Why no history was read: what the reference raises, typed.
#[derive(Debug)]
pub enum HistoryError {
    /// A SQLite error, or a `PyError` the reads raise (RecursionError
    /// from a payload, UnicodeEncodeError binding the id).
    Store(StoreError),
    /// `store.key_fingerprint`: KeyError, meta names no key.
    NoKeyFingerprint,
    /// What `provenance_for` or `episode_provenance` raised, or a
    /// decline (`NotPorted`) passed on as a decline.
    Provenance(ProvenanceError),
}

impl From<StoreError> for HistoryError {
    fn from(e: StoreError) -> Self {
        HistoryError::Store(e)
    }
}

impl From<PyError> for HistoryError {
    fn from(e: PyError) -> Self {
        HistoryError::Store(StoreError::Py(e))
    }
}

impl From<rusqlite::Error> for HistoryError {
    fn from(e: rusqlite::Error) -> Self {
        HistoryError::Store(StoreError::Sqlite(e))
    }
}

impl From<ProvenanceError> for HistoryError {
    fn from(e: ProvenanceError) -> Self {
        HistoryError::Provenance(e)
    }
}

/// `history.RecordHistory`: a record's rows in log order and what the
/// tables hold of it now.
#[derive(Clone, Debug, PartialEq)]
pub struct RecordHistory {
    pub record_id: PyStr,
    pub record_kind: Option<&'static str>,
    pub state: &'static str,
    pub provenance: Option<String>,
    pub key_present: bool,
    pub rows: Vec<HistoryRow>,
}

impl RecordHistory {
    /// `known`: any state but `unknown`.
    pub fn known(&self) -> bool {
        self.state != "unknown"
    }

    /// `findings`: each row whose MAC fails, then an unaccounted label,
    /// then a missing record, one line each.
    pub fn findings(&self) -> Vec<String> {
        let mut out: Vec<String> = self
            .rows
            .iter()
            .filter(|row| row.verified == Some(false))
            .map(|row| format!("seq {} ({}) fails its MAC", row.seq, row.kind))
            .collect();
        if self.provenance.as_deref() == Some(UNACCOUNTED) {
            out.push(format!(
                "the {} record reads unaccounted: no log row this store signed put it as it stands",
                self.state
            ));
        }
        if self.state == "missing" {
            out.push(
                "the log puts this record but no table holds it: it was deleted outside the store"
                    .to_owned(),
            );
        }
        out
    }

    /// `exit_code`: 1 on any finding.
    pub fn exit_code(&self) -> i32 {
        i32::from(!self.findings().is_empty())
    }

    /// `to_dict(diff=diff)`, its keys in the reference's order.
    pub fn to_dict(&self, diff: bool) -> PyValue {
        let opt = |v: Option<&str>| v.map_or(PyValue::None, PyValue::str);
        let findings = self.findings().iter().map(|f| PyValue::str(f)).collect();
        let rows = self.rows.iter().map(|r| r.to_dict(diff)).collect();
        let items = vec![
            ("format", PyValue::str(FORMAT)),
            ("version", PyValue::int(FORMAT_VERSION)),
            ("id", PyValue::Str(self.record_id.clone())),
            ("record_kind", opt(self.record_kind)),
            ("state", PyValue::str(self.state)),
            ("provenance", opt(self.provenance.as_deref())),
            ("key_present", PyValue::Bool(self.key_present)),
            ("findings", PyValue::List(findings)),
            ("rows", PyValue::List(rows)),
        ];
        PyValue::Dict(
            items
                .into_iter()
                .map(|(k, v)| (PyValue::str(k), v))
                .collect(),
        )
    }
}

/// `replaced_detail(kind, payload)`: when the bundle was made, the seq
/// the log was put back to or accepted at (`head_after`; for a kind
/// other than `store_restored`, `log_end_before` when there is none), and
/// the later of the seqs `head_before` and `log_end_before` reached, each
/// left out when None. `after or {}`: an empty mapping has no seq. Only
/// a seq that is exactly an `int` counts for `from_seq` (a bool is not),
/// compared at any size. `Store::history_replaced` keeps its own copy of
/// this rule (`store::replaced_entry`).
pub fn replaced_detail(kind: &str, payload: &PyValue) -> Vec<(PyValue, PyValue)> {
    let mut after = mapping(payload.get("head_after"));
    if after.is_none() && kind != "store_restored" {
        after = mapping(payload.get("log_end_before"));
    }
    let to_seq = after.and_then(|a| a.get("seq"));
    let mut from_seq: Option<&String> = None;
    for end in [
        mapping(payload.get("head_before")),
        mapping(payload.get("log_end_before")),
    ]
    .into_iter()
    .flatten()
    {
        if let Some(PyValue::Int(seq)) = end.get("seq") {
            // `max` keeps the first of equal values.
            if from_seq.is_none_or(|best| cmp_int_text(seq, best).is_gt()) {
                from_seq = Some(seq);
            }
        }
    }
    let from_seq = from_seq.map(|s| PyValue::Int(s.clone()));
    let mut detail = Vec::new();
    set(
        &mut detail,
        &[
            ("bundle_created_at", payload.get("bundle_created_at")),
            ("to_seq", to_seq),
            ("from_seq", from_seq.as_ref()),
        ],
    );
    detail
}

/// `_set(detail, **values)` over a detail of `PyValue` keys.
fn set(detail: &mut Vec<(PyValue, PyValue)>, values: &[(&str, Option<&PyValue>)]) {
    for (name, value) in values {
        let Some(value) = value.filter(|v| !matches!(v, PyValue::None)) else {
            continue;
        };
        let key = PyValue::str(name);
        match detail.iter_mut().find(|(k, _)| *k == key) {
            Some(slot) => slot.1 = value.clone(),
            None => detail.push((key, value.clone())),
        }
    }
}

/// The record id as the text SQLite binds: UnicodeEncodeError for a
/// lone surrogate (unreachable once `rows_naming` bound it).
fn text(record_id: &[u32]) -> Result<String, PyError> {
    unicode::to_string(record_id).ok_or(PyError::UnicodeEncodeError)
}

fn holds(conn: &Connection, table: &str, id: &str) -> Result<bool, rusqlite::Error> {
    let sql = format!("SELECT 1 FROM {table} WHERE id = ?");
    let mut statement = conn.prepare(&sql)?;
    Ok(statement.query_row([id], |_| Ok(())).optional()?.is_some())
}

/// `_table_kind(store, record_id)`: memories, tombstones, episodes, in
/// that order.
pub fn table_kind(
    conn: &Connection,
    record_id: &[u32],
) -> Result<Option<&'static str>, HistoryError> {
    let id = text(record_id)?;
    for (table, kind) in [
        ("memories", "memory"),
        ("tombstones", "memory"),
        ("episodes", "episode"),
    ] {
        if holds(conn, table, &id)? {
            return Ok(Some(kind));
        }
    }
    Ok(None)
}

/// `_state(store, record_id, last_record_kind)`: where the record is now
/// and, for an active one, its provenance label. Memories, then episodes,
/// then tombstones.
pub fn state(
    conn: &Connection,
    provenance: &Provenance<'_>,
    record_id: &[u32],
    last_record_kind: Option<&str>,
) -> Result<(&'static str, Option<String>), HistoryError> {
    let id = text(record_id)?;
    let label =
        |pairs: Vec<(String, String)>| pairs.into_iter().find(|(k, _)| *k == id).map(|(_, v)| v);
    if holds(conn, "memories", &id)? {
        return Ok(("active", label(provenance.provenance_for(&[&id])?)));
    }
    if holds(conn, "episodes", &id)? {
        return Ok(("active", label(provenance.episode_provenance(&[&id])?)));
    }
    if holds(conn, "tombstones", &id)? {
        if last_record_kind != Some("tombstone_put") {
            return Ok(("tombstoned", Some(UNACCOUNTED.to_owned())));
        }
        return Ok(("tombstoned", None));
    }
    Ok((
        match last_record_kind {
            None => "unknown",
            Some("memory_delete") => "deleted",
            Some(kind) if RECORD_DELETES.contains(&kind) => "pruned",
            Some(_) => "missing",
        },
        None,
    ))
}

/// `_read_history`'s `adjacent(index, step, subject)`: the row one seq
/// before or after `found[index]` in the log, as (kind, payload), when it
/// is the next item of `found`, has a payload and is `subject`'s.
fn adjacent<'f>(
    found: &'f [Found],
    index: usize,
    step: i64,
    subject_id: &[u32],
) -> Option<(&'f str, &'f PyValue)> {
    let other = if step < 0 {
        index.checked_sub(1)?
    } else {
        index + 1
    };
    let (row, payload) = found.get(other)?;
    let payload = payload.as_ref()?;
    if found[index].0.seq.checked_add(step) != Some(row.seq) {
        return None;
    }
    if subject(&row.kind, payload).map(Vec::as_slice) != Some(subject_id) {
        return None;
    }
    Some((row.kind.as_str(), payload))
}

/// `record_history(store, record_id)`: `_read_history` inside one read
/// snapshot of `source`'s connection (`history::read_snapshot`, whose
/// closing ROLLBACK error takes the place of the body's answer).
/// `provenance` reads the same source.
pub fn record_history(
    source: &dyn Source,
    ring: &KeyRing,
    provenance: &Provenance<'_>,
    record_id: &[u32],
) -> Result<RecordHistory, HistoryError> {
    let conn = source.connection();
    history::read_snapshot(conn, || read_history(source, ring, provenance, record_id))?
}

/// `_read_history(store, record_id)`, its effects in the reference's
/// order: the verifier (meta's key fingerprint, then the rekey rows), the
/// rows naming the id, the linking versions, the restores, the walk,
/// `_state`, `_table_kind` when the walk named no kind, `can_verify`.
pub fn read_history(
    source: &dyn Source,
    ring: &KeyRing,
    provenance: &Provenance<'_>,
    record_id: &[u32],
) -> Result<RecordHistory, HistoryError> {
    let conn = source.connection();
    let fingerprint = source
        .meta()?
        .into_iter()
        .rev()
        .find(|(k, _)| k == "key_fingerprint")
        .map(|(_, v)| v)
        .ok_or(HistoryError::NoKeyFingerprint)?;
    let mut verifier = RowVerifier::new(conn, ring, &fingerprint)?;
    let named = rows_naming(conn, record_id, &[])?;
    let (found, linking) = with_linking_versions(conn, record_id, named)?;
    let found = with_restores(conn, found)?;

    let mut rows: Vec<HistoryRow> = Vec::new();
    let mut walk = RecordWalk::default();
    let mut links_by_source: HashMap<PyStr, BTreeSet<PyStr>> = HashMap::new();

    for (index, (row, payload)) in found.iter().enumerate() {
        let Some(payload) = payload else { continue };
        let kind = row.kind.as_str();
        if HISTORY_REPLACED.contains(&kind) {
            let action = if kind == "store_restored" {
                "rewound"
            } else {
                "accepted"
            };
            let mut out = HistoryRow::new(row, STORE, action.to_owned(), verifier.verified(row));
            out.detail = replaced_detail(kind, payload);
            rows.push(out);
            continue;
        }
        let named = subject(kind, payload);
        if named.map(Vec::as_slice) == Some(record_id) {
            let before = adjacent(&found, index, -1, record_id);
            let after = adjacent(&found, index, 1, record_id);
            let verified = verifier.verified(row);
            rows.push(record_row(&mut walk, row, payload, before, after, verified));
            continue;
        }
        if names_as_event(kind, payload, record_id) {
            let mut out = HistoryRow::new(row, EVENT, kind.to_owned(), verifier.verified(row));
            if let Some(actor) = mapping(payload.get("actor"))
                .filter(|a| matches!(a, PyValue::Dict(items) if !items.is_empty()))
            {
                out.actor = Some(actor.clone());
                out.actor_source = Some(ACTOR_FROM_EVENT);
            }
            out.detail = event_detail(payload);
            rows.push(out);
            continue;
        }
        let linked = named.filter(|n| linking.contains(*n));
        if let (Some(source_id), Some((_, carrier))) =
            (linked, LINK_CARRIED_BY.iter().find(|(k, _)| *k == kind))
        {
            let following = adjacent(&found, index, 1, source_id);
            if following.map(|f| f.0) != Some(*carrier) {
                for link_type in links_by_source.remove(source_id).unwrap_or_default() {
                    let mut out = HistoryRow::new(
                        row,
                        REFERENCE,
                        "unlinked".to_owned(),
                        verifier.verified(row),
                    );
                    out.detail = vec![
                        (PyValue::str("source"), PyValue::Str(source_id.clone())),
                        (PyValue::str("type"), PyValue::Str(link_type)),
                    ];
                    rows.push(out);
                }
            }
            continue;
        }
        if let Some(source_id) = linked.filter(|_| LINKING_KINDS.contains(&kind)) {
            let verified = verifier.verified(row);
            rows.extend(link_change_rows(
                row,
                payload,
                source_id,
                record_id,
                &mut links_by_source,
                verified,
            )?);
            continue;
        }
        if kind == "conflict_put" {
            let empty = PyValue::Dict(Vec::new());
            let conflict = mapping(payload.get("conflict")).unwrap_or(&empty);
            let is_record =
                |v: Option<&PyValue>| matches!(v, Some(PyValue::Str(s)) if s == record_id);
            let (a, b) = (conflict.get("a_id"), conflict.get("b_id"));
            if is_record(a) || is_record(b) {
                let other = if is_record(a) { b } else { a };
                let mut out = HistoryRow::new(
                    row,
                    REFERENCE,
                    "conflict".to_owned(),
                    verifier.verified(row),
                );
                set(
                    &mut out.detail,
                    &[
                        ("conflict", conflict.get("id")),
                        ("other", other),
                        ("status", conflict.get("status")),
                    ],
                );
                rows.push(out);
            }
        }
    }

    let (state, label) = state(
        conn,
        provenance,
        record_id,
        walk.last_record_kind.as_deref(),
    )?;
    if matches!(state, "active" | "tombstoned") && walk.record_kind.is_none() {
        walk.record_kind = table_kind(conn, record_id)?;
    }
    Ok(RecordHistory {
        record_id: record_id.to_vec(),
        record_kind: walk.record_kind,
        state,
        provenance: label,
        key_present: provenance.can_verify(),
        rows,
    })
}
