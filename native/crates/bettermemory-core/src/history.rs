//! `history.py`, its first unit: the log rows that name a record, with
//! the versions of the records that link to it and the rows that say the
//! store's history was replaced.
//!
//! - [`read_snapshot`]: one read transaction around a report's queries;
//! - [`rows_naming`]: every log row whose payload text contains an id;
//! - [`with_linking_versions`]: those rows parsed, with every version and
//!   delete of each record that ever linked to the id merged in;
//! - [`with_restores`] and [`replaced_rows`]: the `store_restored` and
//!   `history_accepted` rows past the first row found, merged in;
//! - the readers of one row's payload: [`payload`], [`mapping`],
//!   [`subject`], [`names_as_event`], [`link_types`].
//!
//! The store is its connection ([`rusqlite::Connection`]): the read-only
//! one of [`crate::store::StoreFile`], or a writing open's. The file was
//! admitted before any of this runs, so the `log` table is the
//! reference's own: `seq` is the row number, and the other columns hold
//! text, a blob or (the session) NULL. Every statement is the
//! reference's, and SQLite answers it: `instr` is its own, in both
//! builds.
//!
//! Text is a Python `str` as code points ([`PyStr`]), so an id read out
//! of a payload may hold a lone surrogate. Binding one raises
//! UnicodeEncodeError in the reference, and is
//! [`PyError::UnicodeEncodeError`] here.

use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};

use rusqlite::Connection;

use crate::json;
use crate::log::LogRow;
use crate::pyvalue::{py_str, repr, PyError, PyStr, PyValue};
use crate::store::{log_row, StoreError, LOG_SELECT};
use crate::unicode;

/// `_CHANGE_EVENTS`: the telemetry kinds of the tool calls that change a
/// record, each naming it under `id`.
pub const CHANGE_EVENTS: [&str; 5] = ["write", "update", "verify", "remove", "restore"];
/// `_STANDING_GUARD`: its row names every record it guarded under
/// `standing`.
pub const STANDING_GUARD: &str = "standing_guard";
/// `_VERSION_KINDS`: the put rows whose payload carries the whole record
/// under the kind's own prefix.
pub const VERSION_KINDS: [&str; 3] = ["memory_put", "tombstone_put", "episode_put"];
/// `_RECORD_DELETES`: the rows that take a record out of its table.
pub const RECORD_DELETES: [&str; 3] = ["memory_delete", "tombstone_delete", "episode_delete"];
/// `_LINKING_KINDS`: the versions that carry links.
pub const LINKING_KINDS: [&str; 2] = ["memory_put", "tombstone_put"];
/// `_LINK_CARRIED_BY`: the deletes of a record that links, each with the
/// put that carries its links on when it comes right after.
pub const LINK_CARRIED_BY: [(&str, &str); 2] = [
    ("memory_delete", "tombstone_put"),
    ("tombstone_delete", "memory_put"),
];
/// `sorted(log.HISTORY_REPLACED)`: the kinds of the rows that say the
/// store's history was replaced.
pub const HISTORY_REPLACED: [&str; 2] = ["history_accepted", "store_restored"];
/// `log.CAPTURE_MEMBER`.
pub const CAPTURE_MEMBER: &str = "capture_member";
/// `sorted(_LINKING_KINDS | set(_LINK_CARRIED_BY))`: the kinds the scan
/// for a linking record reads.
pub const LINK_SCAN_KINDS: [&str; 4] = [
    "memory_delete",
    "memory_put",
    "tombstone_delete",
    "tombstone_put",
];
/// `sys.int_info.default_max_str_digits`: `str` of an `int` of more
/// digits raises ValueError.
const INT_MAX_STR_DIGITS: usize = 4300;

/// A row and its payload as [`payload`] reads it.
pub type Found = (LogRow, Option<PyValue>);

/// `read_snapshot(conn)` around `body`: one read transaction, so every
/// query of `body` sees the store as of the same commit; rolled back at
/// the end, whatever `body` returns. A connection that already holds a
/// transaction keeps its own: `body` runs in it and nothing is ended.
///
/// The error is what `BEGIN` or the closing `ROLLBACK` raises. The
/// second happens when `body` ended the transaction itself ("cannot
/// rollback - no transaction is active"); it then takes the place of
/// what `body` returned, as the exception of a `finally` block takes the
/// place of the one passing through it. A panic in `body` rolls back on
/// the way out.
pub fn read_snapshot<T>(conn: &Connection, body: impl FnOnce() -> T) -> Result<T, StoreError> {
    struct Unwinding<'a>(Option<&'a Connection>);
    impl Drop for Unwinding<'_> {
        fn drop(&mut self) {
            if let Some(conn) = self.0 {
                let _ = conn.execute_batch("ROLLBACK");
            }
        }
    }
    if !conn.is_autocommit() {
        return Ok(body());
    }
    conn.execute_batch("BEGIN")?;
    let mut guard = Unwinding(Some(conn));
    let out = body();
    guard.0 = None;
    conn.execute_batch("ROLLBACK")?;
    Ok(out)
}

/// One `SELECT` of log rows: the statement compiled, then `texts` bound
/// in order (a lone surrogate is UnicodeEncodeError, at the first text
/// that holds one), then `after`, as `sqlite3` binds a parameter tuple.
/// A row is decoded when it is fetched, column by column, and the
/// statement steps to the next row before the row is visited, as
/// `Cursor.__next__` does.
fn select(
    conn: &Connection,
    tail: &str,
    texts: &[&[u32]],
    after: Option<i64>,
    visit: &mut dyn FnMut(LogRow) -> Result<(), StoreError>,
) -> Result<(), StoreError> {
    let mut statement = conn.prepare(&format!("{LOG_SELECT} {tail}"))?;
    let mut index = 0;
    for text in texts {
        index += 1;
        let text = unicode::to_string(text).ok_or(PyError::UnicodeEncodeError)?;
        statement.raw_bind_parameter(index, text)?;
    }
    if let Some(after) = after {
        statement.raw_bind_parameter(index + 1, after)?;
    }
    let mut rows = statement.raw_query();
    let mut next = rows.next()?;
    while let Some(row) = next {
        let row = log_row(row)?;
        next = rows.next()?;
        visit(row)?;
    }
    Ok(())
}

/// `_rows_naming(store, record_id, kinds=kinds)`: every log row whose
/// payload text contains `record_id`, in seq order; only rows of `kinds`
/// when any are named. The test is SQLite's `instr(payload, ?) > 0`: an
/// empty id is in every payload, and a row of any seq is read (zero and
/// below too).
pub fn rows_naming(
    conn: &Connection,
    record_id: &[u32],
    kinds: &[PyStr],
) -> Result<Vec<LogRow>, StoreError> {
    let mut tail = String::from("WHERE instr(payload, ?) > 0");
    if !kinds.is_empty() {
        tail.push_str(" AND kind IN (");
        tail.push_str(&vec!["?"; kinds.len()].join(","));
        tail.push(')');
    }
    tail.push_str(" ORDER BY seq");
    let mut texts: Vec<&[u32]> = Vec::with_capacity(kinds.len() + 1);
    texts.push(record_id);
    texts.extend(kinds.iter().map(Vec::as_slice));
    let mut out = Vec::new();
    select(conn, &tail, &texts, None, &mut |row| {
        out.push(row);
        Ok(())
    })?;
    Ok(out)
}

/// `_with_linking_versions(store, record_id, rows)`: `rows` parsed, with
/// every `memory_put`, `tombstone_put`, `memory_delete` and
/// `tombstone_delete` row that names a record that ever linked to
/// `record_id` merged in, in seq order; and those records.
///
/// As the reference: a seq given twice keeps its last row; a linking
/// record is the subject of a `memory_put` or `tombstone_put` row of
/// `rows` that is another record's and carries a link to `record_id`;
/// each is scanned for in sorted order by its id as text, so a row that
/// merely contains that text comes too; a row whose seq is already held
/// is not read again.
pub fn with_linking_versions(
    conn: &Connection,
    record_id: &[u32],
    rows: Vec<LogRow>,
) -> Result<(Vec<Found>, BTreeSet<PyStr>), StoreError> {
    let mut parsed: BTreeMap<i64, Found> = BTreeMap::new();
    // A dict's order: each seq where it first came.
    let mut order: Vec<i64> = Vec::new();
    for row in rows {
        let seq = row.seq;
        let read = payload(&row)?;
        if parsed.insert(seq, (row, read)).is_none() {
            order.push(seq);
        }
    }
    let mut sources: BTreeSet<PyStr> = BTreeSet::new();
    for seq in &order {
        let (row, Some(read)) = &parsed[seq] else {
            continue;
        };
        if !LINKING_KINDS.contains(&row.kind.as_str()) {
            continue;
        }
        let Some(named) = subject(&row.kind, read) else {
            continue;
        };
        if named.as_slice() != record_id && !link_types(&row.kind, read, record_id)?.is_empty() {
            sources.insert(named.clone());
        }
    }
    let kinds: Vec<PyStr> = LINK_SCAN_KINDS.iter().map(|k| unicode::cps(k)).collect();
    for source in &sources {
        for row in rows_naming(conn, source, &kinds)? {
            if let Entry::Vacant(slot) = parsed.entry(row.seq) {
                let read = payload(&row)?;
                slot.insert((row, read));
            }
        }
    }
    Ok((parsed.into_values().collect(), sources))
}

/// `_with_restores(store, found)`: `found` with every row that says the
/// store's history was replaced past its first row's seq merged in, in
/// seq order. An empty `found` comes back as it is, and the store is not
/// asked.
///
/// As the reference: the bound is the seq of the first item, not the
/// lowest; a seq `found` already holds keeps its item (the last one given
/// for that seq), but the replaced row's payload is parsed all the same,
/// so its RecursionError is raised.
pub fn with_restores(conn: &Connection, found: Vec<Found>) -> Result<Vec<Found>, StoreError> {
    let Some(first) = found.first() else {
        return Ok(found);
    };
    let after = first.0.seq;
    let mut rows: BTreeMap<i64, Found> = BTreeMap::new();
    for item in found {
        rows.insert(item.0.seq, item);
    }
    replaced_rows(conn, after, &mut |row| {
        let read = payload(&row)?;
        rows.entry(row.seq).or_insert((row, read));
        Ok(())
    })?;
    Ok(rows.into_values().collect())
}

/// `_replaced_rows(store, after)`: the `store_restored` and
/// `history_accepted` rows past seq `after`, in seq order, each handed to
/// `visit` as it is read (the reference's generator).
pub fn replaced_rows(
    conn: &Connection,
    after: i64,
    visit: &mut dyn FnMut(LogRow) -> Result<(), StoreError>,
) -> Result<(), StoreError> {
    let kinds: Vec<PyStr> = HISTORY_REPLACED.iter().map(|k| unicode::cps(k)).collect();
    let texts: Vec<&[u32]> = kinds.iter().map(Vec::as_slice).collect();
    select(
        conn,
        "WHERE kind IN (?, ?) AND seq > ? ORDER BY seq",
        &texts,
        Some(after),
        visit,
    )
}

/// `_payload(row)`: the payload as `json.loads` reads it when it is a
/// JSON object, else None (text that is not JSON, an integer of more
/// than 4,300 digits, a value that is not an object). RecursionError,
/// which the reference does not catch, for a text nested past
/// [`json::MAX_DEPTH`].
pub fn payload(row: &LogRow) -> Result<Option<PyValue>, PyError> {
    match json::loads(&unicode::cps(&row.payload)) {
        Ok(value @ PyValue::Dict(_)) => Ok(Some(value)),
        Ok(_) => Ok(None),
        Err(PyError::RecursionError) => Err(PyError::RecursionError),
        Err(_) => Ok(None),
    }
}

/// `_mapping(value)`: the value when it is a mapping (a `dict`).
pub fn mapping(value: Option<&PyValue>) -> Option<&PyValue> {
    value.filter(|v| matches!(v, PyValue::Dict(_)))
}

/// `_subject(kind, payload)`: the record a row of `kind` changes, or
/// None for a kind that changes no record or a payload that does not say
/// (the name is missing or is not a `str`). `payload` is a `dict`.
pub fn subject<'a>(kind: &str, payload: &'a PyValue) -> Option<&'a PyStr> {
    let named = if VERSION_KINDS.contains(&kind) {
        // `kind.split("_", 1)[0]`: memory, tombstone, episode.
        let prefix = kind.split('_').next().unwrap_or(kind);
        mapping(payload.get(prefix))?.get("id")
    } else if RECORD_DELETES.contains(&kind) {
        payload.get("id")
    } else if matches!(kind, "verification_put" | "verification_delete") {
        payload.get("memory_id")
    } else if matches!(kind, "home_put" | "standing_put" | "returned_put") {
        // `kind[: -len("_put")]`: home, standing, returned.
        let prefix = &kind[..kind.len() - "_put".len()];
        mapping(payload.get(prefix))?.get("record_id")
    } else if matches!(kind, "home_delete" | "standing_delete" | "returned_delete") {
        payload.get("record_id")
    } else if kind == CAPTURE_MEMBER || kind == "import_put" {
        payload.get("memory_id")
    } else {
        None
    };
    match named {
        Some(PyValue::Str(text)) => Some(text),
        _ => None,
    }
}

/// `_names_as_event(kind, payload, record_id)`: a change event whose `id`
/// is the record, or the standing guard's row with the record in its
/// `standing` list. `payload` is a `dict`.
pub fn names_as_event(kind: &str, payload: &PyValue, record_id: &[u32]) -> bool {
    let is_record = |value: &PyValue| matches!(value, PyValue::Str(text) if text == record_id);
    if CHANGE_EVENTS.contains(&kind) {
        return payload.get("id").is_some_and(is_record);
    }
    if kind == STANDING_GUARD {
        return match payload.get("standing") {
            Some(PyValue::List(standing)) => standing.iter().any(is_record),
            _ => false,
        };
    }
    false
}

/// `_link_types(kind, payload, record_id)`: the types of the links to
/// `record_id` a row carries, each as `str()` spells it (a missing type
/// is `None`), in sorted order. A `memory_put` carries its links in its
/// record (`memory.links`); a row of any other kind is read beside its
/// record (`links`), as a tombstone's are. `payload` is a `dict`.
///
/// The error is what `str()` raises: ValueError for an `int` of more
/// than 4,300 digits, RecursionError for a type nested past what `str`
/// writes (9,998 lists around a scalar). Neither comes out of a payload
/// `json.loads` read.
pub fn link_types(
    kind: &str,
    payload: &PyValue,
    record_id: &[u32],
) -> Result<BTreeSet<PyStr>, PyError> {
    let links = if kind == "memory_put" {
        mapping(payload.get("memory")).and_then(|record| record.get("links"))
    } else {
        payload.get("links")
    };
    let mut types = BTreeSet::new();
    let Some(PyValue::List(links)) = links else {
        return Ok(types);
    };
    for link in links {
        if !matches!(link, PyValue::Dict(_)) {
            continue;
        }
        if matches!(link.get("target_id"), Some(PyValue::Str(target)) if target == record_id) {
            types.insert(text_of(link.get("type").unwrap_or(&PyValue::None))?);
        }
    }
    Ok(types)
}

/// `str(value)`, with what CPython 3.13 raises on the way: the first, in
/// the order `repr` writes, of an object nested past
/// [`json::MAX_DEPTH`] + 1 levels (RecursionError) and an `int` of more
/// than 4,300 digits (ValueError). `str` of a container writes one level
/// more than `repr` does: 9,998 lists around a scalar or an empty list
/// are written (9,997 by `repr`), 9,999 raise. A container is spelled
/// here at its top level, each item by [`repr`], so each item has the
/// whole of `repr`'s budget, as it has in `str`.
fn text_of(value: &PyValue) -> Result<PyStr, PyError> {
    let mut work = vec![(value, 1usize)];
    while let Some((value, depth)) = work.pop() {
        if depth > json::MAX_DEPTH + 1 {
            return Err(PyError::RecursionError);
        }
        match value {
            PyValue::Int(digits) if digits.trim_start_matches('-').len() > INT_MAX_STR_DIGITS => {
                return Err(PyError::ValueError);
            }
            PyValue::List(items) | PyValue::Tuple(items) => {
                work.extend(items.iter().rev().map(|item| (item, depth + 1)));
            }
            PyValue::Dict(items) => {
                for (key, item) in items.iter().rev() {
                    work.push((item, depth + 1));
                    work.push((key, depth + 1));
                }
            }
            _ => {}
        }
    }
    let ascii = |out: &mut PyStr, text: &str| out.extend(text.chars().map(u32::from));
    let mut out = PyStr::new();
    match value {
        PyValue::List(items) | PyValue::Tuple(items) => {
            let list = matches!(value, PyValue::List(_));
            ascii(&mut out, if list { "[" } else { "(" });
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    ascii(&mut out, ", ");
                }
                out.extend(repr(item)?);
            }
            if !list && items.len() == 1 {
                ascii(&mut out, ",");
            }
            ascii(&mut out, if list { "]" } else { ")" });
        }
        PyValue::Dict(items) => {
            ascii(&mut out, "{");
            for (i, (key, item)) in items.iter().enumerate() {
                if i > 0 {
                    ascii(&mut out, ", ");
                }
                out.extend(repr(key)?);
                ascii(&mut out, ": ");
                out.extend(repr(item)?);
            }
            ascii(&mut out, "}");
        }
        _ => return py_str(value),
    }
    Ok(out)
}
