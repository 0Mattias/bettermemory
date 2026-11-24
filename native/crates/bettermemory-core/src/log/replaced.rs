//! The rows that say a store's history was replaced (`log.HISTORY_REPLACED`,
//! `log.history_replaced_kind`, `log.history_replaced_row`,
//! `log.append_history_replaced`).
//!
//! `append_history_replaced` has two entries over one body, as its two
//! reference callers enter it: [`append_history_replaced`] with no
//! transaction open (`key_lifecycle.accept_history`), which begins and
//! commits one of its own, and [`append_history_replaced_in`] inside an
//! open transaction (`restore_rows._settle_one`), which joins it under a
//! savepoint and leaves the commit and the head move to the owner.

use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::store::create::Provider;
use crate::store::open::WritableStore;
use crate::store::tx::Transaction;
use crate::store::write::WriteError;
use crate::unicode;

pub const STORE_RESTORED: &str = "store_restored";
pub const HISTORY_ACCEPTED: &str = "history_accepted";

/// `log.HISTORY_REPLACED` in its insertion order: each kind, the noun it
/// is said with and the action `memory history` shows.
pub const HISTORY_REPLACED: [(&str, (&str, &str)); 2] = [
    (STORE_RESTORED, ("a restore", "rewound")),
    (HISTORY_ACCEPTED, ("an accepted history", "accepted")),
];

fn is_replaced_kind(kind: &[u32]) -> Option<&'static str> {
    HISTORY_REPLACED
        .iter()
        .map(|(name, _)| *name)
        .find(|name| unicode::cps(name) == kind)
}

/// `log.history_replaced_kind`: the entry's own `kind` when it is one of
/// the two, else `history_accepted` when `accepted_history` is the object
/// True (not 1, 1.0 or "true"), else `store_restored`.
pub fn history_replaced_kind(entry: &PyValue) -> &'static str {
    if let Some(PyValue::Str(kind)) = entry.get("kind") {
        if let Some(name) = is_replaced_kind(kind) {
            return name;
        }
    }
    if let Some(PyValue::Bool(true)) = entry.get("accepted_history") {
        return HISTORY_ACCEPTED;
    }
    STORE_RESTORED
}

/// The `{seq}` or `{seq, mac}` a restore record entry names. `seq` is
/// always a `PyValue::Int`, of any size.
#[derive(Clone, Debug, PartialEq)]
pub struct ReplacedRow {
    pub seq: PyValue,
    pub mac: Option<PyStr>,
}

impl ReplacedRow {
    /// The reference's dict: `seq`, then `mac` when there is one.
    pub fn to_value(&self) -> PyValue {
        let mut items = vec![(PyValue::str("seq"), self.seq.clone())];
        if let Some(mac) = &self.mac {
            items.push((PyValue::str("mac"), PyValue::Str(mac.clone())));
        }
        PyValue::Dict(items)
    }
}

/// `log.history_replaced_row`: the entry's `row` when it is a mapping
/// whose `seq` is an int (a bool is not), with its `mac` when that is a
/// str; else the entry's own `seq` when that is an int; else None.
pub fn history_replaced_row(entry: &PyValue) -> Option<ReplacedRow> {
    if let Some(row @ PyValue::Dict(_)) = entry.get("row") {
        if let Some(seq @ PyValue::Int(_)) = row.get("seq") {
            let mac = match row.get("mac") {
                Some(PyValue::Str(mac)) => Some(mac.clone()),
                _ => None,
            };
            return Some(ReplacedRow {
                seq: seq.clone(),
                mac,
            });
        }
    }
    match entry.get("seq") {
        Some(seq @ PyValue::Int(_)) => Some(ReplacedRow {
            seq: seq.clone(),
            mac: None,
        }),
        _ => None,
    }
}

/// The row `append_history_replaced` appended: `{seq, mac}`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Appended {
    pub seq: i64,
    pub mac: String,
}

/// Why `append_history_replaced` appended nothing.
#[derive(Debug)]
pub enum ReplacedError {
    /// ValueError, with the reference's text (code points: the kind's
    /// repr), raised before anything is read.
    NotAKind(PyStr),
    /// The write's own failure (SQLite, the writer check, the key).
    Write(WriteError),
}

impl From<WriteError> for ReplacedError {
    fn from(e: WriteError) -> Self {
        ReplacedError::Write(e)
    }
}

fn same_key(a: &PyValue, b: &PyValue) -> bool {
    match (a, b) {
        (PyValue::Str(a), PyValue::Str(b)) => a == b,
        _ => a == b,
    }
}

/// The checked kind, as the ValueError the reference raises otherwise.
fn checked_kind(kind: &[u32]) -> Result<&'static str, ReplacedError> {
    is_replaced_kind(kind).ok_or_else(|| {
        let mut text = repr_str(kind);
        text.extend(unicode::cps(
            " is not a kind that replaces a store's history",
        ));
        ReplacedError::NotAKind(text)
    })
}

/// `PRAGMA query_only` on the connection, truthy.
fn query_only(conn: &rusqlite::Connection) -> Result<bool, ReplacedError> {
    let value: i64 = conn
        .query_row("PRAGMA query_only", [], |row| row.get(0))
        .map_err(WriteError::from)?;
    Ok(value != 0)
}

/// The entry without `row`, in its order, then `kind` and
/// `engine_version` as `dict.update` sets them: a key already there keeps
/// its place and takes the new value, a new one is appended.
pub fn replaced_payload(entry: &PyValue, kind: &str, engine_version: &str) -> PyValue {
    let mut items: Vec<(PyValue, PyValue)> = Vec::new();
    let set = |items: &mut Vec<(PyValue, PyValue)>, name: &PyValue, value: &PyValue| match items
        .iter_mut()
        .find(|(k, _)| same_key(k, name))
    {
        Some(slot) => slot.1 = value.clone(),
        None => items.push((name.clone(), value.clone())),
    };
    if let PyValue::Dict(pairs) = entry {
        let row = PyValue::str("row");
        for (name, value) in pairs {
            if !same_key(name, &row) {
                set(&mut items, name, value);
            }
        }
    }
    set(&mut items, &PyValue::str("kind"), &PyValue::str(kind));
    set(
        &mut items,
        &PyValue::str("engine_version"),
        &PyValue::str(engine_version),
    );
    PyValue::Dict(items)
}

/// `log.append_history_replaced(store, kind, entry)` with no transaction
/// open: the kind checked, `PRAGMA query_only` read (truthy answers None
/// and writes nothing), then one transaction of its own that appends the
/// row with no session and commits, the head moved to it.
pub fn append_history_replaced(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    kind: &[u32],
    entry: &PyValue,
) -> Result<Option<Appended>, ReplacedError> {
    let kind = checked_kind(kind)?;
    if query_only(store.connection())? {
        return Ok(None);
    }
    let payload = replaced_payload(entry, kind, &provider.engine_version());
    let mut tx = store.begin(provider)?;
    let row = tx.append_at(&unicode::cps(kind), &payload, None, None)?;
    tx.commit()?;
    Ok(Some(Appended {
        seq: row.seq,
        mac: row.mac,
    }))
}

/// `log.append_history_replaced(store, kind, entry)` inside the open
/// transaction `tx` (the reference's inner `_transaction` joins under a
/// savepoint): the same checks and payload, the row appended in a nested
/// scope that a failure takes back; the commit and the head move are the
/// owner's. `engine_version` is the build's version, which the provider
/// the transaction holds answers.
pub fn append_history_replaced_in(
    tx: &mut Transaction<'_>,
    engine_version: &str,
    kind: &[u32],
    entry: &PyValue,
) -> Result<Option<Appended>, ReplacedError> {
    let kind = checked_kind(kind)?;
    if query_only(tx.connection())? {
        return Ok(None);
    }
    let payload = replaced_payload(entry, kind, engine_version);
    let row = tx.scope(|tx| tx.append_at(&unicode::cps(kind), &payload, None, None))?;
    Ok(Some(Appended {
        seq: row.seq,
        mac: row.mac,
    }))
}
