//! The plain record reads of `store_records.py` that the search path and
//! `in_force` call, each over a `&Connection` (the reference's
//! `self._conn`): `body_of`, `get_memory`, `has_memory`, `get_many`,
//! `light_many` and `light_by_ids` (without `seed_tokens`), `memory_ids`,
//! `count_memories`, `has_memories`, `filename_for` and `load_one`
//! (`_active_or_raise`). The rule, with the reference's lines, is the
//! README's "The plain reads of the index and the records".

use rusqlite::{Connection, ToSql};

use super::provenance::BATCH;
use super::{sql_value, SqlValue, StoreError};
use crate::float_repr;
use crate::json;
use crate::pyvalue::{repr_str, PyStr};
use crate::records::rows::{self, Row};
use crate::records::{is_valid_ulid, Memory, RecordError};
use crate::unicode;

/// `_MEMORY_SELECT` (`store_records.py:101-104`), for one id.
const MEMORY_ONE: &str =
    "SELECT memories.*, (SELECT payload FROM log WHERE mac = memories.log_mac \
     AND kind = 'memory_put' LIMIT 1) AS attestation_payload FROM memories WHERE id = ?";

/// Why a read answered nothing.
#[derive(Debug)]
pub enum ReadError {
    /// The reference's own exception: its class and its message
    /// (`args[0]`). [`ReadError::text`] is `str(exc)`.
    Raised { class: &'static str, message: PyStr },
    /// Reading the stored row raised (`_row_to_memory`).
    Record(RecordError),
    /// A value this build does not read as the reference does (a blob
    /// where the reference would hold `bytes`, a naive datetime).
    NotPorted(&'static str),
    /// SQLite failed.
    Sqlite(rusqlite::Error),
}

impl ReadError {
    pub(crate) fn raised(class: &'static str, message: &str) -> Self {
        ReadError::Raised {
            class,
            message: unicode::cps(message),
        }
    }

    /// The exception's class name; a native refusal is `native:<what>`.
    pub fn class(&self) -> String {
        match self {
            ReadError::Raised { class, .. } => (*class).to_owned(),
            ReadError::Record(e) => e.class().to_owned(),
            ReadError::NotPorted(_) => "native:not_ported".to_owned(),
            ReadError::Sqlite(_) => "native:sqlite".to_owned(),
        }
    }

    /// `str(exc)`: a KeyError subclass prints its message's `repr`. None
    /// where the reference's wording is not reproduced.
    pub fn text(&self) -> Option<PyStr> {
        match self {
            ReadError::Raised { class, message } => Some(
                if matches!(
                    *class,
                    "NotFoundError" | "MemoryNotFoundError" | "TombstonedError"
                ) {
                    repr_str(message)
                } else {
                    message.clone()
                },
            ),
            _ => None,
        }
    }
}

impl std::fmt::Display for ReadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ReadError::Raised { class, message } => {
                write!(f, "{class}: {}", json::utf8_text(message))
            }
            ReadError::Record(e) => write!(f, "the stored record raised {}", e.class()),
            ReadError::NotPorted(what) => write!(f, "not ported: {what}"),
            ReadError::Sqlite(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for ReadError {}

impl From<rusqlite::Error> for ReadError {
    fn from(e: rusqlite::Error) -> Self {
        ReadError::Sqlite(e)
    }
}

impl From<StoreError> for ReadError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::Sqlite(e) => ReadError::Sqlite(e),
            _ => ReadError::NotPorted("a column value"),
        }
    }
}

/// Every row of `sql` over `params`, each with its column names as
/// `sqlite3.Row` keeps them (a private copy of `writers::one_row`'s read).
pub(crate) fn rows_of(
    conn: &Connection,
    sql: &str,
    params: &[&dyn ToSql],
) -> Result<Vec<Row>, ReadError> {
    let mut statement = conn.prepare(sql)?;
    let names: Vec<String> = statement.column_names().map(str::to_owned).collect();
    let mut rows = statement.query(params)?;
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        let mut columns = Vec::with_capacity(names.len());
        for (i, name) in names.iter().enumerate() {
            columns.push((name.clone(), sql_value(row.get_ref(i)?)?));
        }
        out.push(Row { columns });
    }
    Ok(out)
}

fn first_row(conn: &Connection, sql: &str, key: &str) -> Result<Option<Row>, ReadError> {
    Ok(rows_of(conn, sql, &[&key])?.into_iter().next())
}

/// `str(value)` of a column value as `sqlite3` returns it.
pub(crate) fn py_text(value: &SqlValue) -> Result<String, ReadError> {
    Ok(match value {
        SqlValue::Null => "None".to_owned(),
        SqlValue::Integer(i) => i.to_string(),
        SqlValue::Real(f) => float_repr::repr(*f),
        SqlValue::Text(s) => s.clone(),
        SqlValue::Blob(_) => return Err(ReadError::NotPorted("a blob read as str()")),
    })
}

/// `row[name]` of a row whose statement names the column.
pub(crate) fn col<'r>(row: &'r Row, name: &str) -> &'r SqlValue {
    row.get(name).unwrap_or(&SqlValue::Null)
}

fn int_of(value: &SqlValue) -> Result<i64, ReadError> {
    match value {
        SqlValue::Integer(i) => Ok(*i),
        _ => Err(ReadError::NotPorted("int() of a value that is not an int")),
    }
}

fn to_memory(row: &Row) -> Result<Memory, ReadError> {
    rows::memory(row).map_err(ReadError::Record)
}

fn not_found(memory_id: &str) -> ReadError {
    ReadError::raised("NotFoundError", memory_id)
}

/// `body_of` (305-310): the active record's body, or None.
pub fn body_of(conn: &Connection, record_id: &str) -> Result<Option<String>, ReadError> {
    match first_row(conn, "SELECT body FROM memories WHERE id = ?", record_id)? {
        None => Ok(None),
        Some(row) => py_text(&row.columns[0].1).map(Some),
    }
}

/// `get_memory` (313-317): NotFoundError for an id with no active row.
pub fn get_memory(conn: &Connection, memory_id: &str) -> Result<Memory, ReadError> {
    match first_row(conn, MEMORY_ONE, memory_id)? {
        None => Err(not_found(memory_id)),
        Some(row) => to_memory(&row),
    }
}

/// `has_memory` (320-326).
pub fn has_memory(conn: &Connection, memory_id: &str) -> Result<bool, ReadError> {
    Ok(first_row(conn, "SELECT 1 FROM memories WHERE id = ?", memory_id)?.is_some())
}

/// `get_many` (329-338): the records found, in the order asked, an id
/// asked twice read twice; unknown ids skipped.
pub fn get_many(conn: &Connection, ids: &[String]) -> Result<Vec<Memory>, ReadError> {
    let mut out = Vec::new();
    for id in ids {
        if let Some(row) = first_row(conn, MEMORY_ONE, id)? {
            out.push(to_memory(&row)?);
        }
    }
    Ok(out)
}

fn dedup<T: Clone + Eq + std::hash::Hash>(items: &[T]) -> Vec<T> {
    let mut seen = std::collections::HashSet::new();
    items
        .iter()
        .filter(|item| seen.insert((*item).clone()))
        .cloned()
        .collect()
}

fn marks(n: usize) -> String {
    vec!["?"; n].join(",")
}

/// `light_many(rowids, seed_tokens=False)` (341-369): the memories at
/// `rowids`, each once, in the order first asked, without the signed
/// row's fields; unknown row numbers skipped.
pub fn light_many(conn: &Connection, rowids: &[i64]) -> Result<Vec<Memory>, ReadError> {
    let wanted = dedup(rowids);
    let mut found = std::collections::HashMap::new();
    for chunk in wanted.chunks(BATCH) {
        let sql = format!(
            "SELECT memories.rowid AS rid, memories.*, NULL AS attestation_payload \
             FROM memories WHERE rowid IN ({})",
            marks(chunk.len())
        );
        let params: Vec<&dyn ToSql> = chunk.iter().map(|r| r as &dyn ToSql).collect();
        for row in rows_of(conn, &sql, &params)? {
            let memory = to_memory(&row)?;
            found.insert(int_of(col(&row, "rid"))?, memory);
        }
    }
    Ok(wanted.iter().filter_map(|r| found.remove(r)).collect())
}

/// `light_by_ids(ids, seed_tokens=False)` (372-392): `light_many` for
/// records named by id. A row is filed under its record's id, as the
/// reference files it.
pub fn light_by_ids(conn: &Connection, ids: &[String]) -> Result<Vec<Memory>, ReadError> {
    let wanted = dedup(ids);
    let mut found: std::collections::HashMap<PyStr, Memory> = std::collections::HashMap::new();
    for chunk in wanted.chunks(BATCH) {
        let sql = format!(
            "SELECT memories.*, NULL AS attestation_payload FROM memories WHERE id IN ({})",
            marks(chunk.len())
        );
        let params: Vec<&dyn ToSql> = chunk.iter().map(|r| r as &dyn ToSql).collect();
        for row in rows_of(conn, &sql, &params)? {
            let memory = to_memory(&row)?;
            found.insert(memory.fields.id.clone(), memory);
        }
    }
    Ok(wanted
        .iter()
        .filter_map(|id| found.get(&unicode::cps(id)).cloned())
        .collect())
}

/// `memory_ids` (472-476), in row order.
pub fn memory_ids(conn: &Connection) -> Result<Vec<String>, ReadError> {
    rows_of(conn, "SELECT id FROM memories ORDER BY rowid", &[])?
        .iter()
        .map(|row| py_text(col(row, "id")))
        .collect()
}

/// `count_memories` (479-480).
pub fn count_memories(conn: &Connection) -> Result<i64, ReadError> {
    Ok(conn.query_row("SELECT COUNT(*) FROM memories", [], |r| r.get(0))?)
}

/// `has_memories` (483-485): one row read.
pub fn has_memories(conn: &Connection) -> Result<bool, ReadError> {
    Ok(!rows_of(conn, "SELECT 1 FROM memories LIMIT 1", &[])?.is_empty())
}

/// `filename_for` (488-494): NotFoundError for an id with no active row.
pub fn filename_for(conn: &Connection, memory_id: &str) -> Result<Option<String>, ReadError> {
    match first_row(
        conn,
        "SELECT filename FROM memories WHERE id = ?",
        memory_id,
    )? {
        None => Err(not_found(memory_id)),
        Some(row) => match col(&row, "filename") {
            SqlValue::Null => Ok(None),
            value => py_text(value).map(Some),
        },
    }
}

/// `load_one` (1029-1031), that is `_active_or_raise` (1011-1026) over a
/// plain connection, with `writers::active_or_raise`'s texts (its
/// transaction twin): MemoryNotFoundError for an id that is not a ULID or
/// has no row, TombstonedError naming the removal's reason.
pub fn load_one(conn: &Connection, memory_id: &[u32]) -> Result<Memory, ReadError> {
    if !is_valid_ulid(memory_id) {
        let mut message = unicode::cps("invalid id: ");
        message.extend(repr_str(memory_id));
        return Err(ReadError::Raised {
            class: "MemoryNotFoundError",
            message,
        });
    }
    // A ULID is ASCII.
    let id: String = memory_id
        .iter()
        .filter_map(|&c| char::from_u32(c))
        .collect();
    if let Some(row) = first_row(conn, MEMORY_ONE, &id)? {
        return to_memory(&row);
    }
    let dead = first_row(
        conn,
        "SELECT removed_reason FROM tombstones WHERE id = ?",
        &id,
    )?;
    if let Some(row) = dead {
        let reason = py_text(col(&row, "removed_reason"))?;
        return Err(ReadError::raised(
            "TombstonedError",
            &format!("memory {id} was removed: {reason}"),
        ));
    }
    Err(ReadError::raised(
        "MemoryNotFoundError",
        &format!("no memory with id {id}"),
    ))
}
