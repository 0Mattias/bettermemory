//! The plain public record writers of `store_records.py`, each one
//! [`Transaction`] on a [`WritableStore`]: `put_memory` (with
//! `_put_memory`), `tombstone` and `delete_tombstone` (with
//! `_require_row`). The rule, with the reference's lines, is the README's
//! "The plain record writers".
//!
//! Each returns what the reference returns and the transaction's
//! [`Committed`] (its rows, the `returned` marks the commit settled, the
//! head's move), or a [`RecordWriteError`] carrying the reference's
//! exception class and message with nothing written.

use rusqlite::{params, Connection};

use super::create::Provider;
use super::open::WritableStore;
use super::tx::{Committed, Transaction};
use super::write::{WriteBoundary, WriteError};
use super::{sql_value, SqlValue, StoreError};
use crate::datetime::DateTime;
use crate::json;
use crate::pyvalue::{py_int, py_str, repr, repr_str, PyError, PyStr, PyValue};
use crate::records::rows::{self, Row};
use crate::records::{Memory, RecordError, RecordFields, Tombstone};
use crate::unicode;

/// `_WRITABLE_PROVENANCE` (`store_schema.py:142`), sorted as the refusal
/// spells it.
pub const WRITABLE_PROVENANCE: [&str; 3] = ["imported", "local", "synced"];

/// `LOCAL`: a new row's label when none is given.
pub const LOCAL: &str = "local";

/// `_MEMORY_SELECT` (`store_records.py:101-104`), for one id.
const MEMORY_ONE: &str =
    "SELECT memories.*, (SELECT payload FROM log WHERE mac = memories.log_mac \
     AND kind = 'memory_put' LIMIT 1) AS attestation_payload FROM memories WHERE id = ?";

/// What a writer is asked for that this build does not port.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WriterNotPorted {
    /// `put_memory(home=...)`: `_home_row` and the `home_put` row.
    Home,
    /// `put_memory(standing=...)`: `_standing_row` and the `standing_put` row.
    Standing,
    /// A column read as a Python value that this build does not carry (a
    /// blob where the reference would hold `bytes`).
    Blob(&'static str),
}

/// Why a writer wrote nothing.
#[derive(Debug)]
pub enum RecordWriteError {
    /// The reference's own exception: its class and its message
    /// (`args[0]`). [`RecordWriteError::text`] is `str(exc)`.
    Raised { class: &'static str, message: PyStr },
    /// Reading the stored row raised (`_row_to_memory`).
    Record(RecordError),
    /// [`WriterNotPorted`].
    NotPorted(WriterNotPorted),
    /// The transaction refused or failed (its own refusals included).
    Write(WriteError),
}

impl RecordWriteError {
    fn raised(class: &'static str, message: PyStr) -> Self {
        RecordWriteError::Raised { class, message }
    }

    /// The exception's class name, as the reference names it; a native
    /// refusal is `native:<class>`.
    pub fn class(&self) -> String {
        match self {
            RecordWriteError::Raised { class, .. } => (*class).to_owned(),
            RecordWriteError::Record(e) => e.class().to_owned(),
            RecordWriteError::NotPorted(_) => "native:not_ported".to_owned(),
            RecordWriteError::Write(WriteError::Refused { class, .. }) => (*class).to_owned(),
            RecordWriteError::Write(e) => format!("native:{}", e.class()),
        }
    }

    /// `str(exc)`: a KeyError subclass prints its message's `repr`. None
    /// where the reference's wording is not reproduced.
    pub fn text(&self) -> Option<PyStr> {
        match self {
            RecordWriteError::Raised { class, message } => Some(if is_key_error(class) {
                repr_str(message)
            } else {
                message.clone()
            }),
            RecordWriteError::Write(WriteError::Refused { text, .. }) => {
                text.as_deref().map(unicode::cps)
            }
            _ => None,
        }
    }
}

fn is_key_error(class: &str) -> bool {
    matches!(
        class,
        "TombstonedError" | "NotFoundError" | "MemoryNotFoundError"
    )
}

impl std::fmt::Display for RecordWriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RecordWriteError::Raised { class, message } => {
                write!(f, "{class}: {}", json::utf8_text(message))
            }
            RecordWriteError::Record(e) => write!(f, "the stored record raised {}", e.class()),
            RecordWriteError::NotPorted(WriterNotPorted::Home) => {
                f.write_str("the native build does not write a record's home")
            }
            RecordWriteError::NotPorted(WriterNotPorted::Standing) => {
                f.write_str("the native build does not write a standing answer")
            }
            RecordWriteError::NotPorted(WriterNotPorted::Blob(column)) => {
                write!(f, "the native build does not read a blob in {column}")
            }
            RecordWriteError::Write(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for RecordWriteError {}

impl From<WriteError> for RecordWriteError {
    fn from(e: WriteError) -> Self {
        RecordWriteError::Write(e)
    }
}

impl From<rusqlite::Error> for RecordWriteError {
    fn from(e: rusqlite::Error) -> Self {
        RecordWriteError::Write(WriteError::Sqlite(e))
    }
}

impl From<StoreError> for RecordWriteError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::Sqlite(e) => RecordWriteError::Write(WriteError::Sqlite(e)),
            // A text column that is not UTF-8: sqlite3's OperationalError.
            _ => RecordWriteError::Write(WriteError::Refused {
                class: "OperationalError",
                text: None,
            }),
        }
    }
}

fn py_failed(e: PyError) -> RecordWriteError {
    // The reference's wording of these is the interpreter's; the class
    // is what is compared.
    RecordWriteError::Write(WriteError::Refused {
        class: e.name(),
        text: None,
    })
}

/// What a writer returns, and what its transaction wrote.
#[derive(Debug)]
pub struct Done<T> {
    pub value: T,
    pub committed: Committed,
}

/// `put_memory`'s keyword arguments. `home` and `standing` are refused as
/// not ported when given.
#[derive(Clone, Copy, Debug, Default)]
pub struct Put<'a> {
    /// None keeps the row's label (`local` for a new row). Any Python
    /// value a caller can pass: a value outside the writable labels is
    /// refused (ValueError, or TypeError where it is unhashable).
    pub provenance: Option<&'a PyValue>,
    /// None keeps the row's name.
    pub filename: Option<&'a str>,
    pub session: Option<&'a str>,
    pub home: Option<&'a PyValue>,
    pub standing: Option<&'a PyValue>,
}

/// The first value `hash()` refuses inside `value`, in the order CPython
/// hashes a tuple's items: its type name.
fn unhashable(value: &PyValue) -> Option<&'static str> {
    let mut work = vec![value];
    while let Some(v) = work.pop() {
        match v {
            PyValue::List(_) => return Some("list"),
            PyValue::Dict(_) => return Some("dict"),
            PyValue::Tuple(items) => work.extend(items.iter().rev()),
            _ => {}
        }
    }
    None
}

/// `store_records.put_memory`'s check (139-142): None, or a writable label.
fn check_provenance(provenance: Option<&PyValue>) -> Result<Option<String>, RecordWriteError> {
    let Some(p) = provenance else {
        return Ok(None);
    };
    if let PyValue::None = p {
        return Ok(None);
    }
    if let Some(kind) = unhashable(p) {
        return Err(RecordWriteError::raised(
            "TypeError",
            unicode::cps(&format!("unhashable type: '{kind}'")),
        ));
    }
    if let PyValue::Str(s) = p {
        if let Some(label) = WRITABLE_PROVENANCE.iter().find(|l| unicode::cps(l) == *s) {
            return Ok(Some((*label).to_owned()));
        }
    }
    let mut message =
        unicode::cps("provenance must be one of ['imported', 'local', 'synced'], got ");
    message.extend(repr(p).map_err(py_failed)?);
    Err(RecordWriteError::raised("ValueError", message))
}

/// A column as the Python value `sqlite3` reads.
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

fn column<'r>(row: &'r Row, name: &'static str) -> Result<&'r SqlValue, RecordWriteError> {
    // `row[name]` of a table missing the column: IndexError.
    row.get(name)
        .ok_or(RecordWriteError::Record(RecordError::Raised("IndexError")))
}

/// One row of `select` for `key`, or None.
fn one_row(conn: &Connection, select: &str, key: &str) -> Result<Option<Row>, RecordWriteError> {
    let mut statement = conn.prepare(select)?;
    let names: Vec<String> = statement.column_names().map(str::to_owned).collect();
    let mut rows = statement.query(params![key])?;
    let Some(row) = rows.next()? else {
        return Ok(None);
    };
    let mut columns = Vec::with_capacity(names.len());
    for (i, name) in names.iter().enumerate() {
        columns.push((name.clone(), sql_value(row.get_ref(i)?)?));
    }
    Ok(Some(Row { columns }))
}

/// The `removed_reason` of the id's tombstone, as `str()` spells it in an
/// f-string, or None when there is none.
fn removed_reason(conn: &Connection, id: &str) -> Result<Option<PyStr>, RecordWriteError> {
    let Some(row) = one_row(
        conn,
        "SELECT removed_reason FROM tombstones WHERE id = ?",
        id,
    )?
    else {
        return Ok(None);
    };
    let reason = py_value(column(&row, "removed_reason")?, "removed_reason")?;
    Ok(Some(py_str(&reason).map_err(py_failed)?))
}

fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn text_id(id: &[u32]) -> Result<String, RecordWriteError> {
    // A validated record's id is a ULID: ASCII.
    unicode::to_string(id).ok_or(RecordWriteError::Write(WriteError::Refused {
        class: "UnicodeEncodeError",
        text: None,
    }))
}

/// `models.validate_path_targets` (1157-1175), with its texts: each map
/// in turn, its cap and then its cover of the legacy list.
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
            return Err(RecordWriteError::raised(
                "ValueError",
                unicode::cps(&format!(
                    "{field} capped at 64 entries (got {})",
                    targets.len()
                )),
            ));
        }
        if !inputs.iter().all(|i| targets.iter().any(|(k, _)| k == i)) {
            return Err(RecordWriteError::raised(
                "ValueError",
                unicode::cps(&format!("{field} must cover every legacy attestation")),
            ));
        }
    }
    Ok(())
}

/// `_put_memory` (157-199) inside `tx`: the tombstone check, the row's
/// label and name kept, one `memory_put`. `marks` are payload fields
/// beside the record.
fn put_in(
    tx: &mut Transaction<'_>,
    memory: &Memory,
    provenance: Option<String>,
    filename: Option<&str>,
    session: Option<&str>,
    marks: Vec<(&str, PyValue)>,
    refuse: Option<WriterNotPorted>,
) -> Result<(), RecordWriteError> {
    let id = text_id(&memory.fields.id)?;
    if let Some(reason) = removed_reason(tx.connection(), &id)? {
        let mut message = unicode::cps(&format!("memory {id} was removed: "));
        message.extend(reason);
        message.extend(unicode::cps("; restore it to bring it back"));
        return Err(RecordWriteError::raised("TombstonedError", message));
    }
    let existing = one_row(
        tx.connection(),
        "SELECT provenance, filename FROM memories WHERE id = ?",
        &id,
    )?;
    // `provenance or str(existing["provenance"])`, then `or LOCAL`.
    let mut label: Option<PyStr> = provenance.map(|p| unicode::cps(&p));
    let mut name: PyValue = filename.map_or(PyValue::None, PyValue::str);
    if let Some(row) = &existing {
        if label.as_ref().is_none_or(Vec::is_empty) {
            let stored = py_value(column(row, "provenance")?, "provenance")?;
            label = Some(py_str(&stored).map_err(py_failed)?);
        }
        if filename.is_none() {
            name = py_value(column(row, "filename")?, "filename")?;
        }
    }
    let label = match label {
        Some(l) if !l.is_empty() => l,
        _ => unicode::cps(LOCAL),
    };
    check_targets(&memory.fields)?;
    let record = memory.record_payload().map_err(py_failed)?;
    if let Some(why) = refuse {
        return Err(RecordWriteError::NotPorted(why));
    }
    let mut items = marks;
    items.push(("memory", record));
    items.push(("provenance", PyValue::Str(label)));
    items.push(("filename", name));
    tx.mutate("memory_put", &dict(items), session)?;
    Ok(())
}

/// A boundary hook for the kill test ([`WriteBoundary`]).
#[doc(hidden)]
pub type Hook<'h> = Option<&'h mut dyn FnMut(WriteBoundary)>;

fn start<'s, 'h: 's>(
    store: &'s mut WritableStore,
    provider: &'s mut dyn Provider,
    hook: Hook<'h>,
) -> Result<Transaction<'s>, WriteError> {
    match hook {
        Some(hook) => store.begin_with(provider, hook),
        None => store.begin(provider),
    }
}

impl WritableStore {
    /// `Store.put_memory(memory, provenance=, filename=, session=, home=,
    /// standing=)` (`store_records.py` 114-154): insert or replace the
    /// record; returns it as given.
    pub fn put_memory(
        &mut self,
        provider: &mut dyn Provider,
        memory: &Memory,
        put: Put<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        self.put_memory_hooked(provider, memory, put, None)
    }

    /// [`WritableStore::put_memory`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn put_memory_hooked(
        &mut self,
        provider: &mut dyn Provider,
        memory: &Memory,
        put: Put<'_>,
        hook: Hook<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        let provenance = check_provenance(put.provenance)?;
        // Refused where the reference would first write them: after the
        // tombstone check and the record's payload (`_put_memory` 172-192),
        // before any row, so a call it refuses for another reason gets
        // that reason. `_standing_row`'s own checks (143, 202-222) are not ported.
        let given = |v: Option<&PyValue>| v.is_some_and(|v| !matches!(v, PyValue::None));
        let refuse = if given(put.home) {
            Some(WriterNotPorted::Home)
        } else if given(put.standing) {
            Some(WriterNotPorted::Standing)
        } else {
            None
        };
        if given(put.standing) {
            // `_standing_row` stamps the answer before the transaction
            // (`store_records.py:221`): the wall clock is read once here,
            // whatever the call then does.
            provider.wall_now().map_err(WriteError::Clock)?;
        }
        let mut tx = start(self, provider, hook)?;
        put_in(
            &mut tx,
            memory,
            provenance,
            put.filename,
            put.session,
            Vec::new(),
            refuse,
        )?;
        let committed = tx.commit()?;
        Ok(Done {
            value: memory.clone(),
            committed,
        })
    }

    /// `Store.tombstone(memory_id, reason, session=, now=)` (500-547):
    /// the active record moved to the tombstones. `now` None reads the
    /// wall clock once, after the record is found.
    pub fn tombstone(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        reason: &str,
        session: Option<&str>,
        now: Option<DateTime>,
    ) -> Result<Done<Tombstone>, RecordWriteError> {
        self.tombstone_hooked(provider, memory_id, reason, session, now, None)
    }

    /// [`WritableStore::tombstone`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn tombstone_hooked(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        reason: &str,
        session: Option<&str>,
        now: Option<DateTime>,
        hook: Hook<'_>,
    ) -> Result<Done<Tombstone>, RecordWriteError> {
        let mut tx = start(self, provider, hook)?;
        let Some(row) = one_row(tx.connection(), MEMORY_ONE, memory_id)? else {
            if let Some(gone) = removed_reason(tx.connection(), memory_id)? {
                let mut message = unicode::cps(&format!("memory {memory_id} was removed: "));
                message.extend(gone);
                return Err(RecordWriteError::raised("TombstonedError", message));
            }
            return Err(RecordWriteError::raised(
                "NotFoundError",
                unicode::cps(memory_id),
            ));
        };
        let memory = rows::memory(&row).map_err(RecordWriteError::Record)?;
        let removed = match now {
            Some(t) => t,
            None => tx.wall_now()?,
        };
        let dead = Tombstone {
            fields: memory.fields.clone(),
            removed,
            removed_reason: unicode::cps(reason),
            removed_session: session.map(unicode::cps),
        };
        check_targets(&dead.fields)?;
        let record = dead.record_payload().map_err(py_failed)?;
        let provenance =
            py_str(&py_value(column(&row, "provenance")?, "provenance")?).map_err(py_failed)?;
        let filename = py_value(column(&row, "filename")?, "filename")?;
        let links = match column(&row, "links_json")? {
            SqlValue::Text(t) => json::loads(&unicode::cps(t)).map_err(py_failed)?,
            // `json.loads` of anything but text: TypeError.
            _ => return Err(py_failed(PyError::TypeError)),
        };
        let corroborations = py_int(&py_value(
            column(&row, "corroborations")?,
            "corroborations",
        )?)
        .map_err(py_failed)?;
        let last = py_value(column(&row, "last_corroborated")?, "last_corroborated")?;
        let payload = dict(vec![
            ("tombstone", record),
            ("provenance", PyValue::Str(provenance)),
            ("filename", filename),
            ("links", links),
            ("corroborations", corroborations),
            ("last_corroborated", last),
        ]);
        let id = dict(vec![("id", PyValue::str(memory_id))]);
        tx.mutate("memory_delete", &id, session)?;
        tx.mutate("tombstone_put", &payload, session)?;
        let committed = tx.commit()?;
        Ok(Done {
            value: dead,
            committed,
        })
    }

    /// `Store.delete_tombstone(memory_id, session=)` (744-760):
    /// NotFoundError unless the tombstone is there, then
    /// `tombstone_delete`.
    pub fn delete_tombstone(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        session: Option<&str>,
    ) -> Result<Done<()>, RecordWriteError> {
        self.delete_tombstone_hooked(provider, memory_id, session, None)
    }

    /// [`WritableStore::delete_tombstone`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn delete_tombstone_hooked(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        session: Option<&str>,
        hook: Hook<'_>,
    ) -> Result<Done<()>, RecordWriteError> {
        let mut tx = start(self, provider, hook)?;
        if one_row(
            tx.connection(),
            "SELECT 1 FROM tombstones WHERE id = ?",
            memory_id,
        )?
        .is_none()
        {
            return Err(RecordWriteError::raised(
                "NotFoundError",
                unicode::cps(memory_id),
            ));
        }
        let id = dict(vec![("id", PyValue::str(memory_id))]);
        tx.mutate("tombstone_delete", &id, session)?;
        let committed = tx.commit()?;
        Ok(Done {
            value: (),
            committed,
        })
    }
}
