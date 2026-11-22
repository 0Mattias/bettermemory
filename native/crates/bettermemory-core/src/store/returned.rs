//! `store_returned.py`'s backfill half on the writable store: the index
//! rows an earlier build's restore left out (`missing_links`), the marks
//! a store holds (`returned_rows`), the marks an earlier build owed
//! (`backfill`) and the hook a writing open runs (`ensure_backfilled`).
//!
//! `_is_active`, `_targets` and `settle` are built in [`super::write`]
//! and [`super::tx`], `linking_sources` and `Store._put_relinked` in
//! [`super::writers`]. Wiring `ensure_backfilled` into the writing open
//! is a later unit's.

use std::collections::{BTreeSet, HashMap, HashSet};

use rusqlite::Connection;

use super::create::Provider;
use super::home;
use super::open::WritableStore;
use super::schema::RETURNED_BACKFILL_KEY;
use super::tx::Committed;
use super::write::{is_active, WriteError, SUPERSEDES};
use super::writers::{put_relinked, Done, RecordWriteError, Relink, WriterNotPorted};
use super::{sql_value, SqlValue};
use crate::json;
use crate::pyvalue::{PyError, PyValue};
use crate::unicode;

/// Iterating a value that is not iterable, an unhashable value hashed, a
/// missing attribute: the reference's own exception.
static NONE: PyValue = PyValue::None;

fn raised(class: &'static str, text: String) -> RecordWriteError {
    RecordWriteError::Raised {
        class,
        message: unicode::cps(&text),
    }
}

fn py_failed(e: PyError) -> RecordWriteError {
    RecordWriteError::Write(WriteError::Refused {
        class: e.name(),
        text: None,
    })
}

/// `for x in value`: a list's items, a str's characters, a dict's keys;
/// TypeError for anything else.
fn iterate(value: &PyValue) -> Result<Vec<PyValue>, RecordWriteError> {
    Ok(match value {
        PyValue::List(items) | PyValue::Tuple(items) => items.clone(),
        PyValue::Str(s) => s.iter().map(|&c| PyValue::Str(vec![c])).collect(),
        PyValue::Dict(items) => items.iter().map(|(k, _)| k.clone()).collect(),
        other => {
            return Err(raised(
                "TypeError",
                format!("'{}' object is not iterable", home::type_name(other)),
            ))
        }
    })
}

/// `mapping.get(name)`: the last value a parsed object holds for `name`
/// (`json.loads` keeps the last of a repeated key).
fn get<'v>(items: &'v [(PyValue, PyValue)], name: &str) -> Option<&'v PyValue> {
    let key = PyValue::str(name);
    items.iter().rev().find(|(k, _)| *k == key).map(|(_, v)| v)
}

/// `hash(value)` for a parsed JSON value: a list or a dict raises.
fn hashable(value: &PyValue) -> Result<(), RecordWriteError> {
    match value {
        PyValue::List(_) | PyValue::Dict(_) => Err(raised(
            "TypeError",
            format!("unhashable type: '{}'", home::type_name(value)),
        )),
        _ => Ok(()),
    }
}

/// A str's text, or None when it holds a lone surrogate: such a str
/// equals no text read from the database, and sqlite3 refuses to bind it.
fn text(cps: &[u32]) -> Option<String> {
    cps.iter().map(|&c| char::from_u32(c)).collect()
}

/// The UnicodeEncodeError sqlite3 raises binding a str that holds a lone
/// surrogate.
fn unbindable(cps: &[u32]) -> RecordWriteError {
    let at = cps
        .iter()
        .position(|&c| char::from_u32(c).is_none())
        .unwrap_or(0);
    raised(
        "UnicodeEncodeError",
        format!(
            "'utf-8' codec can't encode character '\\u{:04x}' in position {at}: \
             surrogates not allowed",
            cps[at]
        ),
    )
}

/// `bool(value)` for a parsed JSON value.
fn truthy(value: &PyValue) -> bool {
    match value {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(t) => t != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) | PyValue::Path(s) => !s.is_empty(),
        PyValue::Tuple(v) | PyValue::List(v) => !v.is_empty(),
        PyValue::Dict(v) => !v.is_empty(),
        PyValue::DateTime(_) => true,
    }
}

/// An integral float's exact decimal digits, as Python compares an int
/// with a float.
fn integral(f: f64) -> Option<String> {
    if !f.is_finite() || f.fract() != 0.0 {
        return None;
    }
    if f == 0.0 {
        return Some("0".to_owned());
    }
    Some(format!("{f:.0}"))
}

/// `json_value == column_value` as Python compares a parsed JSON value
/// with what sqlite3 returns for a column.
fn py_equal(value: &PyValue, column: &SqlValue) -> bool {
    match (value, column) {
        (PyValue::None, SqlValue::Null) => true,
        (PyValue::Bool(b), SqlValue::Integer(i)) => i64::from(*b) == *i,
        (PyValue::Bool(b), SqlValue::Real(f)) => f64::from(u8::from(*b)) == *f,
        (PyValue::Int(t), SqlValue::Integer(i)) => *t == i.to_string(),
        (PyValue::Int(t), SqlValue::Real(f)) => integral(*f).is_some_and(|d| d == *t),
        (PyValue::Float(x), SqlValue::Integer(i)) => integral(*x) == Some(i.to_string()),
        (PyValue::Float(x), SqlValue::Real(f)) => x == f,
        (PyValue::Str(s), SqlValue::Text(t)) => *s == unicode::cps(t),
        _ => false,
    }
}

/// `json.loads(column)` where the reference passes the column's value
/// straight in: TypeError (passed over by the caller) for a number or a
/// NULL; a blob, which `json.loads` would decode, is not ported.
fn loads_column(
    value: &SqlValue,
    column: &'static str,
) -> Result<Option<PyValue>, RecordWriteError> {
    let text = match value {
        SqlValue::Text(t) => t,
        SqlValue::Blob(_) => {
            return Err(RecordWriteError::NotPorted(WriterNotPorted::Blob(column)))
        }
        _ => return Ok(None),
    };
    match json::loads(&unicode::cps(text)) {
        Ok(v) => Ok(Some(v)),
        Err(PyError::TypeError | PyError::ValueError) => Ok(None),
        Err(e) => Err(py_failed(e)),
    }
}

/// `store_returned.missing_links` (236-267): each active record, in id
/// order, whose stored links name an active record with no index row for
/// that link (its source, type, target and note), with the least such
/// target. A link list that is not iterable, or a link whose type or note
/// cannot be hashed, raises TypeError as the reference does.
pub fn missing_links(conn: &Connection) -> Result<Vec<(String, String)>, RecordWriteError> {
    let mut active = HashSet::new();
    {
        let mut statement = conn.prepare("SELECT id FROM memories")?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            active.insert(sql_value(row.get_ref(0)?)?.py_str());
        }
    }
    // The index rows by their source, type and target, each with its notes.
    let mut indexed: HashMap<(String, String, String), Vec<SqlValue>> = HashMap::new();
    {
        let mut statement =
            conn.prepare("SELECT source_id, type, target_id, note FROM memory_links")?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            indexed
                .entry((
                    sql_value(row.get_ref(0)?)?.py_str(),
                    sql_value(row.get_ref(1)?)?.py_str(),
                    sql_value(row.get_ref(2)?)?.py_str(),
                ))
                .or_default()
                .push(sql_value(row.get_ref(3)?)?);
        }
    }
    let mut out = Vec::new();
    let mut statement = conn.prepare(
        "SELECT id, links_json FROM memories WHERE links_json IS NOT NULL \
         AND links_json != '[]' ORDER BY id",
    )?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let record_id = sql_value(row.get_ref(0)?)?;
        let Some(links) = loads_column(&sql_value(row.get_ref(1)?)?, "links_json")? else {
            continue;
        };
        let mut lost: Vec<String> = Vec::new();
        for link in iterate(&links)? {
            let PyValue::Dict(items) = &link else {
                continue;
            };
            let target = get(items, "target_id").unwrap_or(&NONE);
            hashable(target)?;
            let PyValue::Str(target) = target else {
                continue;
            };
            // A str with a lone surrogate is never in `active`.
            let Some(target) = text(target) else {
                continue;
            };
            if !active.contains(&target) {
                continue;
            }
            // The tuple is hashed in order: the record id, the type, the
            // target (a str), the note.
            let kind = get(items, "type").unwrap_or(&NONE);
            let note = get(items, "note").unwrap_or(&NONE);
            hashable(kind)?;
            hashable(note)?;
            // Only a text id equals the index's `str(source_id)`, and only
            // a str type its `str(type)`.
            let held = match (&record_id, kind) {
                (SqlValue::Text(id), PyValue::Str(t)) => text(t)
                    .and_then(|t| indexed.get(&(id.clone(), t, target.clone())))
                    .is_some_and(|notes| notes.iter().any(|n| py_equal(note, n))),
                _ => false,
            };
            if !held {
                lost.push(target);
            }
        }
        if let Some(first) = lost.into_iter().min() {
            // `out[str(record_id)] = lost[0]`: a key seen before keeps its
            // place and takes the later value.
            let key = record_id.py_str();
            match out
                .iter_mut()
                .find(|(k, _): &&mut (String, String)| *k == key)
            {
                Some(pair) => pair.1 = first,
                None => out.push((key, first)),
            }
        }
    }
    Ok(out)
}

/// `store_returned.returned_rows` (270-298) over `conn`: the marks of
/// `ids` (every mark when None), each as its record id, `via` and
/// `returned`, in the order the reads return them, the ids taken once
/// each in their first order and read 500 at a time. A store whose file
/// lacks the table holds none.
pub fn returned_rows(
    conn: &Connection,
    ids: Option<&[String]>,
) -> Result<Vec<(String, String, String)>, RecordWriteError> {
    let wanted: Option<Vec<&str>> = ids.map(|ids| {
        let mut seen = HashSet::new();
        ids.iter()
            .map(String::as_str)
            .filter(|id| seen.insert(*id))
            .collect()
    });
    if wanted.as_ref().is_some_and(Vec::is_empty) {
        return Ok(Vec::new());
    }
    match marks(conn, wanted.as_deref()) {
        Err(RecordWriteError::Write(WriteError::Sqlite(e)))
            if e.to_string().contains("no such table") =>
        {
            Ok(Vec::new())
        }
        other => other,
    }
}

fn marks(
    conn: &Connection,
    wanted: Option<&[&str]>,
) -> Result<Vec<(String, String, String)>, RecordWriteError> {
    let batches: Vec<(String, Vec<&str>)> = match wanted {
        None => vec![(
            "SELECT record_id, via, returned FROM returned".to_owned(),
            Vec::new(),
        )],
        Some(wanted) => wanted
            .chunks(500)
            .map(|batch| {
                let marks = vec!["?"; batch.len()].join(",");
                (
                    format!(
                        "SELECT record_id, via, returned FROM returned \
                         WHERE record_id IN ({marks})"
                    ),
                    batch.to_vec(),
                )
            })
            .collect(),
    };
    let mut out: Vec<(String, String, String)> = Vec::new();
    let mut at_index: HashMap<String, usize> = HashMap::new();
    for (sql, params) in batches {
        let mut statement = conn.prepare(&sql)?;
        let mut rows = statement.query(rusqlite::params_from_iter(params))?;
        while let Some(row) = rows.next()? {
            let id = sql_value(row.get_ref(0)?)?.py_str();
            let via = sql_value(row.get_ref(1)?)?.py_str();
            let at = sql_value(row.get_ref(2)?)?.py_str();
            // A dict keyed by id: a later row for the same id takes its
            // place where the first one stood.
            match at_index.get(&id) {
                Some(&i) => out[i] = (id, via, at),
                None => {
                    at_index.insert(id.clone(), out.len());
                    out.push((id, via, at));
                }
            }
        }
    }
    Ok(out)
}

fn exists(conn: &Connection, sql: &str, params: &[&str]) -> rusqlite::Result<bool> {
    conn.prepare(sql)?
        .exists(rusqlite::params_from_iter(params.iter()))
}

/// `_superseded`: another active record supersedes `id`.
fn superseded(conn: &Connection, id: &str) -> rusqlite::Result<bool> {
    exists(
        conn,
        "SELECT 1 FROM memory_links l JOIN memories m ON m.id = l.source_id \
         WHERE l.type = ? AND l.target_id = ? AND l.source_id != ? LIMIT 1",
        &[SUPERSEDES, id, id],
    )
}

/// The records the log names as brought back: each `tombstone_delete`
/// row's id, each `supersedes` target of another record's `memory_put`,
/// less each record a `returned_put` or `returned_delete` row names; each
/// id as its code points, in the order Python sorts a str (a target with
/// a lone surrogate kept as it is).
fn named(conn: &Connection) -> Result<BTreeSet<Vec<u32>>, RecordWriteError> {
    let mut named = BTreeSet::new();
    let texts = |sql: &str| -> Result<Vec<String>, RecordWriteError> {
        let mut statement = conn.prepare(sql)?;
        let mut rows = statement.query([])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            if let SqlValue::Text(t) = sql_value(row.get_ref(0)?)? {
                out.push(t);
            }
        }
        Ok(out)
    };
    named.extend(
        texts("SELECT json_extract(payload, '$.id') FROM log WHERE kind = 'tombstone_delete'")?
            .iter()
            .map(|t| unicode::cps(t)),
    );
    let mut statement = conn.prepare("SELECT payload FROM log WHERE kind = 'memory_put'")?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        // `json.loads(payload).get("memory") or {}`: TypeError, ValueError
        // and AttributeError pass the row over.
        let parsed = loads_column(&sql_value(row.get_ref(0)?)?, "payload")?;
        let Some(PyValue::Dict(payload)) = &parsed else {
            continue;
        };
        let record = match get(payload, "memory") {
            Some(v) if truthy(v) => v.clone(),
            _ => PyValue::Dict(Vec::new()),
        };
        let PyValue::Dict(record) = &record else {
            return Err(raised(
                "AttributeError",
                format!(
                    "'{}' object has no attribute 'get'",
                    home::type_name(&record)
                ),
            ));
        };
        let source = get(record, "id").unwrap_or(&NONE);
        let links = match get(record, "links") {
            Some(v) if truthy(v) => iterate(v)?,
            _ => Vec::new(),
        };
        for link in links {
            let PyValue::Dict(items) = &link else {
                continue;
            };
            if get(items, "type") != Some(&PyValue::str(SUPERSEDES)) {
                continue;
            }
            if let Some(target @ PyValue::Str(t)) = get(items, "target_id") {
                if target != source {
                    named.insert(t.clone());
                }
            }
        }
    }
    for id in texts(
        "SELECT coalesce(json_extract(payload, '$.returned.record_id'), \
         json_extract(payload, '$.record_id')) FROM log \
         WHERE kind IN ('returned_put', 'returned_delete')",
    )? {
        named.remove(&unicode::cps(&id));
    }
    Ok(named)
}

/// One line `ensure_backfilled` logs on the `bettermemory.store` logger.
#[derive(Debug)]
pub struct LogLine {
    /// `WARNING` or `INFO`.
    pub level: &'static str,
    /// The message as `%`-formatting makes it; None where it holds the
    /// text of an exception whose wording this build does not reproduce.
    pub message: Option<String>,
}

/// What `ensure_backfilled` did: the ids marked, the lines logged, and the
/// backfill's transaction when it committed.
#[derive(Debug)]
pub struct Ensured {
    pub marked: Vec<String>,
    pub log: Vec<LogLine>,
    pub committed: Option<Committed>,
}

/// `str(exc)` for an exception `ensure_backfilled` catches, where known.
fn text_of(e: &RecordWriteError) -> Option<String> {
    match e {
        RecordWriteError::Write(WriteError::Sqlite(rusqlite::Error::SqliteFailure(
            _,
            Some(message),
        ))) => Some(message.clone()),
        RecordWriteError::Write(WriteError::Sqlite(_)) => None,
        other => other.text().map(|t| json::utf8_text(&t)),
    }
}

/// `except (sqlite3.Error, OSError, ValueError)` by the class the
/// reference raises. What this build does not port, and a clock this
/// build could not read, pass out.
fn caught(e: &RecordWriteError) -> bool {
    match e {
        RecordWriteError::NotPorted(_) => false,
        RecordWriteError::Write(WriteError::Sqlite(_) | WriteError::RolledBack(_)) => true,
        RecordWriteError::Write(WriteError::Refused { class, .. }) => caught_class(class),
        RecordWriteError::Raised { class, .. } => caught_class(class),
        RecordWriteError::Record(r) => caught_class(r.class()),
        RecordWriteError::Write(_) => false,
    }
}

fn caught_class(class: &str) -> bool {
    matches!(
        class,
        // sqlite3.Error and its subclasses.
        "Error"
            | "DatabaseError"
            | "OperationalError"
            | "IntegrityError"
            | "ProgrammingError"
            | "InterfaceError"
            | "DataError"
            | "NotSupportedError"
            | "InternalError"
            // OSError and the subclasses a store write can raise.
            | "OSError"
            | "FileNotFoundError"
            | "FileExistsError"
            | "PermissionError"
            | "IsADirectoryError"
            | "NotADirectoryError"
            | "BlockingIOError"
            | "InterruptedError"
            | "TimeoutError"
            // ValueError and its subclasses.
            | "ValueError"
            | "UnicodeError"
            | "UnicodeDecodeError"
            | "UnicodeEncodeError"
            | "JSONDecodeError"
            | "ValidationError"
            | "UnknownFieldsError"
    )
}

impl WritableStore {
    /// `store_returned.backfill(store, session=)` (301-368): the records
    /// the log names read before the transaction; inside it, the index
    /// rows a restore left out put back (`missing_links`,
    /// `_put_relinked`), the wall clock read once, a `returned_put` row
    /// (`via` the record itself) for each named record, in id order, that
    /// is active, superseded by no other active record and not marked,
    /// then meta's backfill key deleted. Returns the ids marked.
    pub fn backfill(
        &mut self,
        provider: &mut dyn Provider,
        session: Option<&str>,
        reads: Relink<'_>,
    ) -> Result<Done<Vec<String>>, RecordWriteError> {
        let named = named(self.connection())?;
        let ring = self.ring();
        let mut tx = self.begin(provider)?;
        let sources = missing_links(tx.connection())?;
        put_relinked(&mut tx, &ring, reads, &sources, session)?;
        let stamp = tx.wall_now()?.isoformat_utc();
        let mut marked = Vec::new();
        for cps in named {
            // sqlite3 refuses to bind a str with a lone surrogate.
            let Some(id) = text(&cps) else {
                return Err(unbindable(&cps));
            };
            let conn = tx.connection();
            if !is_active(conn, &id)?
                || superseded(conn, &id)?
                || exists(conn, "SELECT 1 FROM returned WHERE record_id = ?", &[&id])?
            {
                continue;
            }
            let mark = PyValue::Dict(vec![
                (PyValue::str("record_id"), PyValue::str(&id)),
                (PyValue::str("via"), PyValue::str(&id)),
                (PyValue::str("returned"), PyValue::str(&stamp)),
            ]);
            let payload = PyValue::Dict(vec![(PyValue::str("returned"), mark)]);
            tx.mutate_returned_put(&payload, session)?;
            marked.push(id);
        }
        tx.connection()
            .execute("DELETE FROM meta WHERE key = ?", [RETURNED_BACKFILL_KEY])?;
        let committed = tx.commit()?;
        Ok(Done {
            value: marked,
            committed,
        })
    }

    /// `store_returned.ensure_backfilled(store)` (371-398), with
    /// `store.has_key` given: nothing without the key or without meta's
    /// backfill key; else [`WritableStore::backfill`] with no session. A
    /// backfill that raises a sqlite3 error, an OSError or a ValueError
    /// is logged as a warning and marks nothing (the key stays); one
    /// that marked records logs their count.
    pub fn ensure_backfilled(
        &mut self,
        provider: &mut dyn Provider,
        has_key: bool,
        reads: Relink<'_>,
    ) -> Result<Ensured, RecordWriteError> {
        let mut ensured = Ensured {
            marked: Vec::new(),
            log: Vec::new(),
            committed: None,
        };
        if !has_key {
            return Ok(ensured);
        }
        let pending = {
            let mut statement = self
                .connection()
                .prepare("SELECT value FROM meta WHERE key = ?")?;
            let mut rows = statement.query([RETURNED_BACKFILL_KEY])?;
            match rows.next()? {
                // `fetchone()` reads the value, so a text that is not
                // UTF-8 raises.
                Some(row) => {
                    sql_value(row.get_ref(0)?)?;
                    true
                }
                None => false,
            }
        };
        if !pending {
            return Ok(ensured);
        }
        match self.backfill(provider, None, reads) {
            Ok(done) => {
                if !done.value.is_empty() {
                    ensured.log.push(LogLine {
                        level: "INFO",
                        message: Some(format!(
                            "returned: {} records an earlier build brought back into force \
                             marked for a standing roster read",
                            done.value.len()
                        )),
                    });
                }
                ensured.marked = done.value;
                ensured.committed = Some(done.committed);
            }
            Err(e) if caught(&e) => ensured.log.push(LogLine {
                level: "WARNING",
                message: text_of(&e).map(|t| {
                    format!("returned: the backfill failed and will run at the next open: {t}")
                }),
            }),
            Err(e) => return Err(e),
        }
        Ok(ensured)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `except (sqlite3.Error, OSError, ValueError)`: a plain ValueError
    /// and its subclasses are caught; a TypeError and a KeyError are not.
    #[test]
    fn caught_by_class() {
        for class in [
            "ValueError",
            "UnicodeEncodeError",
            "OperationalError",
            "OSError",
        ] {
            assert!(caught(&raised(class, String::new())), "{class}");
        }
        for class in ["TypeError", "KeyError", "AttributeError", "RecursionError"] {
            assert!(!caught(&raised(class, String::new())), "{class}");
        }
    }

    /// A str with a lone surrogate has no text, and the error names it by
    /// its position in code points.
    #[test]
    fn surrogate_text() {
        assert_eq!(text(&[0x61, 0x62]), Some("ab".to_owned()));
        assert_eq!(text(&[0x61, 0xd800]), None);
        let RecordWriteError::Raised { class, message } = unbindable(&[0x61, 0x62, 0xdc00]) else {
            panic!("not raised");
        };
        assert_eq!(class, "UnicodeEncodeError");
        assert_eq!(
            json::utf8_text(&message),
            "'utf-8' codec can't encode character '\\udc00' in position 2: surrogates not allowed"
        );
    }
}
