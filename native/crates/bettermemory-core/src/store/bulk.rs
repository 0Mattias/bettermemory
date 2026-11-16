//! The record writers that run in chunks (`Store._in_chunks`):
//! `rename_scope` and `prune_tombstones` (`store_records.py` 1185-1311).
//! The rule, with the reference's lines, is the README's "Chunked runs".
//!
//! Each lists its items outside any transaction, then applies them in
//! chunks ([`WritableStore::in_chunks`]); each item is read again under its
//! chunk's lock and written in a nested scope of its own, whose failure of
//! the classes the reference catches is recorded and takes back only that
//! item. Any other failure rolls its chunk back and is returned: the
//! chunks before it stay committed.

use rusqlite::{params, Connection};

use super::create::Provider;
use super::open::WritableStore;
use super::provenance::decode_json_bytes;
use super::tx::{Committed, Transaction};
use super::write::{WriteBoundary, WriteError};
use super::writers::{RecordWriteError, WriterNotPorted};
use super::{sql_value, SqlValue};
use crate::datetime::DateTime;
use crate::json;
use crate::pyvalue::{py_int, py_str, PyError, PyStr, PyValue};
use crate::records::rows::{self, Row};
use crate::records::{Memory, RecordError, RecordFields, Tombstone};
use crate::unicode;

/// `_MEMORY_SELECT` (`store_records.py` 101-104).
const MEMORY_SELECT: &str = "SELECT memories.*, (SELECT payload FROM log WHERE mac = \
     memories.log_mac AND kind = 'memory_put' LIMIT 1) AS attestation_payload FROM memories";

/// `_TOMBSTONE_SELECT` (105-108).
const TOMBSTONE_SELECT: &str = "SELECT tombstones.*, (SELECT payload FROM log WHERE mac = \
     tombstones.log_mac AND kind = 'tombstone_put' LIMIT 1) AS attestation_payload FROM tombstones";

/// A chunked run's value and the commit of each chunk, in order.
#[derive(Debug)]
pub struct Bulk<T> {
    pub value: T,
    pub chunks: Vec<Committed>,
}

/// `rename_scope`'s result: the ids changed, and `failed` (present in the
/// reference only when not empty): each id with `str(exc)`, None where the
/// reference's wording is the interpreter's.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Renamed {
    pub active: Vec<PyStr>,
    pub tombstoned: Vec<PyStr>,
    pub failed: Vec<(PyStr, Option<PyStr>)>,
}

/// A boundary hook, shown each boundary of each chunk.
#[doc(hidden)]
pub type Hook<'h> = Option<&'h mut dyn FnMut(WriteBoundary)>;

fn py_failed(e: PyError) -> RecordWriteError {
    RecordWriteError::Write(WriteError::Refused {
        class: e.name(),
        text: None,
    })
}

fn rows_of(conn: &Connection, sql: &str, key: Option<&str>) -> Result<Vec<Row>, RecordWriteError> {
    let mut statement = conn.prepare(sql)?;
    let names: Vec<String> = statement.column_names().map(str::to_owned).collect();
    let mut rows = match key {
        Some(k) => statement.query(params![k])?,
        None => statement.query([])?,
    };
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

fn one(conn: &Connection, sql: &str, key: &str) -> Result<Option<Row>, RecordWriteError> {
    Ok(rows_of(conn, sql, Some(key))?.into_iter().next())
}

/// The column's value as sqlite3 gives it. A blob is `bytes` in the
/// reference, which goes on into the payload and the applied row as bytes:
/// this build's values do not carry bytes, so it is refused as not ported
/// (as the writers' `put_in` refuses it).
fn py_value(v: &SqlValue, column: &'static str) -> Result<PyValue, RecordWriteError> {
    Ok(match v {
        SqlValue::Null => PyValue::None,
        SqlValue::Integer(i) => PyValue::int(i128::from(*i)),
        SqlValue::Real(f) => PyValue::Float(*f),
        SqlValue::Text(t) => PyValue::str(t),
        SqlValue::Blob(_) => {
            return Err(RecordWriteError::NotPorted(WriterNotPorted::Blob(column)))
        }
    })
}

/// `str(row[column])`: a blob is spelled `b'...'`, as `str(bytes)` does.
fn str_of(v: &SqlValue, column: &'static str) -> Result<PyStr, RecordWriteError> {
    match v {
        SqlValue::Blob(_) => Ok(unicode::cps(&v.py_str())),
        _ => py_str(&py_value(v, column)?).map_err(py_failed),
    }
}

fn column<'r>(row: &'r Row, name: &'static str) -> Result<&'r SqlValue, RecordWriteError> {
    // `row[name]` of a table missing the column: IndexError, as `put_in`.
    row.get(name)
        .ok_or(RecordWriteError::Record(RecordError::Raised("IndexError")))
}

fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn raised(class: &'static str, text: String) -> RecordWriteError {
    RecordWriteError::Raised {
        class,
        message: unicode::cps(&text),
    }
}

/// `_scopes_after_rename` (`store_schema.py` 565-578).
pub fn scopes_after_rename(scopes: &[PyStr], old: &[u32], new: &[u32]) -> Option<Vec<PyStr>> {
    if !scopes.iter().any(|s| s.as_slice() == old) {
        return None;
    }
    let mut out: Vec<PyStr> = Vec::new();
    for scope in scopes {
        let name = if scope.as_slice() == old {
            new.to_vec()
        } else {
            scope.clone()
        };
        if !out.contains(&name) {
            out.push(name);
        }
    }
    Some(out)
}

/// `models.validate_path_targets` as `record_payload` runs it.
fn check_targets(fields: &RecordFields) -> Result<(), RecordWriteError> {
    for (field, targets, inputs) in [
        (
            "verified_path_targets",
            &fields.verified_path_targets,
            &fields.verified_paths,
        ),
        (
            "verified_absent_path_targets",
            &fields.verified_absent_path_targets,
            &fields.verified_absent_paths,
        ),
    ] {
        let Some(targets) = targets else { continue };
        if targets.len() > 64 {
            return Err(raised(
                "ValueError",
                format!("{field} capped at 64 entries (got {})", targets.len()),
            ));
        }
        if !inputs.iter().all(|i| targets.iter().any(|(k, _)| k == i)) {
            return Err(raised(
                "ValueError",
                format!("{field} must cover every legacy attestation"),
            ));
        }
    }
    Ok(())
}

/// `put_memory(memory, session=)` with no provenance, filename, home or
/// standing, inside `tx` (`store_records.py` 139-199): the tombstone
/// check, the row's label and name kept, then `memory_put`.
pub fn put_plain(
    tx: &mut Transaction<'_>,
    memory: &Memory,
    session: Option<&str>,
) -> Result<(), RecordWriteError> {
    let id = unicode::to_string(&memory.fields.id).ok_or(RecordWriteError::Write(
        WriteError::Refused {
            class: "UnicodeEncodeError",
            text: None,
        },
    ))?;
    if let Some(row) = one(
        tx.connection(),
        "SELECT removed_reason FROM tombstones WHERE id = ?",
        &id,
    )? {
        let reason = str_of(column(&row, "removed_reason")?, "removed_reason")?;
        let mut message = unicode::cps(&format!("memory {id} was removed: "));
        message.extend(reason);
        message.extend(unicode::cps("; restore it to bring it back"));
        return Err(RecordWriteError::Raised {
            class: "TombstonedError",
            message,
        });
    }
    let existing = one(
        tx.connection(),
        "SELECT provenance, filename FROM memories WHERE id = ?",
        &id,
    )?;
    let mut label: PyStr = Vec::new();
    let mut name = PyValue::None;
    if let Some(row) = &existing {
        label = str_of(column(row, "provenance")?, "provenance")?;
        name = py_value(column(row, "filename")?, "filename")?;
    }
    if label.is_empty() {
        label = unicode::cps("local");
    }
    check_targets(&memory.fields)?;
    let record = memory.record_payload().map_err(py_failed)?;
    let payload = dict(vec![
        ("memory", record),
        ("provenance", PyValue::Str(label)),
        ("filename", name),
    ]);
    tx.mutate("memory_put", &payload, session)?;
    Ok(())
}

/// The classes `except (ValueError, ValidationError)` catches among those
/// a put raises.
fn is_value_error(class: &str) -> bool {
    matches!(
        class,
        "ValueError"
            | "UnknownFieldsError"
            | "ValidationError"
            | "UnicodeEncodeError"
            | "UnicodeDecodeError"
            | "JSONDecodeError"
    )
}

/// `list(_load_json(text, []))` (`store_rows.py` 98-105).
fn links_list(v: &SqlValue) -> Result<PyValue, RecordWriteError> {
    let loaded = match v {
        SqlValue::Null => return Ok(PyValue::List(Vec::new())),
        SqlValue::Text(t) if t == "[]" => return Ok(PyValue::List(Vec::new())),
        SqlValue::Text(t) => json::loads(&unicode::cps(t)).map_err(py_failed)?,
        // `json.loads(bytes)`: decoded as `json.detect_encoding` reads them.
        SqlValue::Blob(b) => match decode_json_bytes(b) {
            Some(points) => json::loads(&points).map_err(py_failed)?,
            None => {
                return Err(RecordWriteError::Write(WriteError::Refused {
                    class: "UnicodeDecodeError",
                    text: None,
                }))
            }
        },
        // `json.loads` of a number: TypeError.
        _ => return Err(py_failed(PyError::TypeError)),
    };
    Ok(PyValue::List(match &loaded {
        PyValue::List(items) | PyValue::Tuple(items) => items.clone(),
        PyValue::Dict(items) => items.iter().map(|(k, _)| k.clone()).collect(),
        PyValue::Str(s) => s.iter().map(|c| PyValue::Str(vec![*c])).collect(),
        // `list()` of a number, a bool or None.
        _ => return Err(py_failed(PyError::TypeError)),
    }))
}

impl WritableStore {
    /// `Store.rename_scope(old, new, include_tombstones=, session=)`
    /// (`store_records.py` 1185-1275).
    pub fn rename_scope(
        &mut self,
        provider: &mut dyn Provider,
        old: &[u32],
        new: &[u32],
        include_tombstones: bool,
        session: Option<&str>,
    ) -> Result<Bulk<Renamed>, RecordWriteError> {
        self.rename_scope_hooked(provider, old, new, include_tombstones, session, None)
    }

    /// [`WritableStore::rename_scope`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn rename_scope_hooked(
        &mut self,
        provider: &mut dyn Provider,
        old: &[u32],
        new: &[u32],
        include_tombstones: bool,
        session: Option<&str>,
        mut hook: Hook<'_>,
    ) -> Result<Bulk<Renamed>, RecordWriteError> {
        let mut out = Renamed::default();
        let mut chunks = Vec::new();
        if old == new {
            return Ok(Bulk { value: out, chunks });
        }
        let mut ids: Vec<String> = Vec::new();
        for row in rows_of(
            self.connection(),
            &format!("{MEMORY_SELECT} ORDER BY rowid"),
            None,
        )? {
            let memory = rows::memory(&row).map_err(RecordWriteError::Record)?;
            if scopes_after_rename(&memory.fields.scopes, old, new).is_some() {
                ids.push(text(&memory.fields.id)?);
            }
        }
        let mut relay = |b: WriteBoundary| {
            if let Some(h) = hook.as_mut() {
                h(b);
            }
        };
        chunks.extend(self.in_chunks(
            &mut *provider,
            Some(&mut relay),
            &ids,
            |tx, id: &String| rename_active(tx, id, old, new, session, &mut out),
        )?);
        if include_tombstones {
            let mut ids: Vec<String> = Vec::new();
            for row in rows_of(
                self.connection(),
                &format!("{TOMBSTONE_SELECT} ORDER BY removed, rowid"),
                None,
            )? {
                let dead = rows::tombstone(&row).map_err(RecordWriteError::Record)?;
                if scopes_after_rename(&dead.fields.scopes, old, new).is_some() {
                    ids.push(text(&dead.fields.id)?);
                }
            }
            chunks.extend(self.in_chunks(
                &mut *provider,
                Some(&mut relay),
                &ids,
                |tx, id: &String| rename_removed(tx, id, old, new, session, &mut out),
            )?);
        }
        Ok(Bulk { value: out, chunks })
    }

    /// `Store.prune_tombstones(older_than, now=, session=)` (1278-1311):
    /// `older_than` in microseconds (a `timedelta`). Returns the ids
    /// pruned, oldest removal first.
    pub fn prune_tombstones(
        &mut self,
        provider: &mut dyn Provider,
        older_than_us: i128,
        now: Option<DateTime>,
        session: Option<&str>,
    ) -> Result<Bulk<Vec<PyStr>>, RecordWriteError> {
        self.prune_tombstones_hooked(provider, older_than_us, now, session, None)
    }

    /// [`WritableStore::prune_tombstones`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn prune_tombstones_hooked(
        &mut self,
        provider: &mut dyn Provider,
        older_than_us: i128,
        now: Option<DateTime>,
        session: Option<&str>,
        hook: Hook<'_>,
    ) -> Result<Bulk<Vec<PyStr>>, RecordWriteError> {
        let now = match now {
            Some(t) => t,
            None => provider.wall_now().map_err(WriteError::Clock)?,
        };
        let cutoff = now.minus_micros(older_than_us).map_err(|e| {
            RecordWriteError::Write(WriteError::Refused {
                class: e.name(),
                text: None,
            })
        })?;
        let mut doomed: Vec<(i128, String)> = Vec::new();
        for row in rows_of(
            self.connection(),
            &format!("{TOMBSTONE_SELECT} ORDER BY removed, rowid"),
            None,
        )? {
            let dead = rows::tombstone(&row).map_err(RecordWriteError::Record)?;
            let removed = earlier(&dead.removed, &cutoff)?;
            if removed.1 {
                doomed.push((removed.0, text(&dead.fields.id)?));
            }
        }
        doomed.sort();
        let ids: Vec<String> = doomed.into_iter().map(|(_, id)| id).collect();
        let mut pruned: Vec<PyStr> = Vec::new();
        let chunks = self.in_chunks(provider, hook, &ids, |tx, id: &String| {
            let Some(raw) = one(
                tx.connection(),
                &format!("{TOMBSTONE_SELECT} WHERE id = ?"),
                id,
            )?
            else {
                return Ok(());
            };
            let dead = rows::tombstone(&raw).map_err(RecordWriteError::Record)?;
            if !earlier(&dead.removed, &cutoff)?.1 {
                return Ok(());
            }
            let payload = dict(vec![("id", PyValue::str(id))]);
            match tx.scope(|tx| tx.mutate("tombstone_delete", &payload, session).map(drop)) {
                Ok(()) => {
                    pruned.push(unicode::cps(id));
                    Ok(())
                }
                Err(WriteError::Refused {
                    class: "UnknownFieldsError",
                    ..
                }) => Ok(()),
                Err(e) => Err(RecordWriteError::Write(e)),
            }
        })?;
        Ok(Bulk {
            value: pruned,
            chunks,
        })
    }
}

fn text(id: &[u32]) -> Result<String, RecordWriteError> {
    unicode::to_string(id).ok_or(RecordWriteError::Write(WriteError::Refused {
        class: "UnicodeEncodeError",
        text: None,
    }))
}

/// `removed < cutoff`: the instant, and whether it is earlier. A naive
/// value against an aware one is Python's TypeError.
fn earlier(removed: &DateTime, cutoff: &DateTime) -> Result<(i128, bool), RecordWriteError> {
    match (removed.instant_us(), cutoff.instant_us()) {
        (Some(r), Some(c)) => Ok((r, r < c)),
        (None, None) => Ok((
            removed.wall_micros(),
            removed.wall_micros() < cutoff.wall_micros(),
        )),
        _ => Err(RecordWriteError::Write(WriteError::Refused {
            class: "TypeError",
            text: None,
        })),
    }
}

/// `rename_active` (1215-1230).
fn rename_active(
    tx: &mut Transaction<'_>,
    id: &str,
    old: &[u32],
    new: &[u32],
    session: Option<&str>,
    out: &mut Renamed,
) -> Result<(), RecordWriteError> {
    let Some(row) = one(
        tx.connection(),
        &format!("{MEMORY_SELECT} WHERE id = ?"),
        id,
    )?
    else {
        return Ok(());
    };
    let memory = rows::memory(&row).map_err(RecordWriteError::Record)?;
    let Some(renamed) = scopes_after_rename(&memory.fields.scopes, old, new) else {
        return Ok(());
    };
    let mut refreshed = memory.clone();
    refreshed.fields.scopes = renamed;
    refreshed.fields.updated = tx.wall_now()?;
    match tx.scope(|tx| put_plain(tx, &refreshed, session)) {
        Ok(()) => {
            out.active.push(memory.fields.id.clone());
            Ok(())
        }
        Err(e) if is_value_error(&e.class()) => {
            out.failed.push((memory.fields.id.clone(), e.text()));
            Ok(())
        }
        Err(e) => Err(e),
    }
}

/// `rename_removed` (1232-1257).
fn rename_removed(
    tx: &mut Transaction<'_>,
    id: &str,
    old: &[u32],
    new: &[u32],
    session: Option<&str>,
    out: &mut Renamed,
) -> Result<(), RecordWriteError> {
    let Some(raw) = one(
        tx.connection(),
        &format!("{TOMBSTONE_SELECT} WHERE id = ?"),
        id,
    )?
    else {
        return Ok(());
    };
    let dead: Tombstone = rows::tombstone(&raw).map_err(RecordWriteError::Record)?;
    let Some(renamed) = scopes_after_rename(&dead.fields.scopes, old, new) else {
        return Ok(());
    };
    let mut copy = dead.clone();
    copy.fields.scopes = renamed;
    check_targets(&copy.fields)?;
    let record = copy.record_payload().map_err(py_failed)?;
    let provenance = str_of(column(&raw, "provenance")?, "provenance")?;
    let payload = dict(vec![
        ("tombstone", record),
        ("provenance", PyValue::Str(provenance)),
        ("filename", py_value(column(&raw, "filename")?, "filename")?),
        ("links", links_list(column(&raw, "links_json")?)?),
        (
            "corroborations",
            py_int(&py_value(
                column(&raw, "corroborations")?,
                "corroborations",
            )?)
            .map_err(py_failed)?,
        ),
        (
            "last_corroborated",
            py_value(column(&raw, "last_corroborated")?, "last_corroborated")?,
        ),
    ]);
    match tx.scope(|tx| tx.mutate("tombstone_put", &payload, session).map(drop)) {
        Ok(()) => {
            out.tombstoned.push(dead.fields.id.clone());
            Ok(())
        }
        Err(WriteError::Refused {
            class: "UnknownFieldsError",
            text,
        }) => {
            out.failed
                .push((dead.fields.id.clone(), text.as_deref().map(unicode::cps)));
            Ok(())
        }
        Err(e) => Err(RecordWriteError::Write(e)),
    }
}
