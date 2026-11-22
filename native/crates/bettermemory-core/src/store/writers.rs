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
use super::home::{self, Answer, Home};
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
    /// A column read as a Python value that this build does not carry (a
    /// blob where the reference would hold `bytes`).
    Blob(&'static str),
    /// The relink's provenance read met what `store::provenance` does not
    /// port (its own `NotPorted`), or the key ring could not be loaded.
    Provenance(String),
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
        "TombstonedError" | "NotFoundError" | "MemoryNotFoundError" | "NotTombstonedError"
    )
}

impl std::fmt::Display for RecordWriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RecordWriteError::Raised { class, message } => {
                write!(f, "{class}: {}", json::utf8_text(message))
            }
            RecordWriteError::Record(e) => write!(f, "the stored record raised {}", e.class()),
            RecordWriteError::NotPorted(WriterNotPorted::Blob(column)) => {
                write!(f, "the native build does not read a blob in {column}")
            }
            RecordWriteError::NotPorted(WriterNotPorted::Provenance(what)) => {
                write!(f, "the relink's provenance read is not ported: {what}")
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

/// `put_memory`'s keyword arguments.
#[derive(Clone, Copy, Debug, Default)]
pub struct Put<'a> {
    /// None keeps the row's label (`local` for a new row). Any Python
    /// value a caller can pass: a value outside the writable labels is
    /// refused (ValueError, or TypeError where it is unhashable).
    pub provenance: Option<&'a PyValue>,
    /// None keeps the row's name.
    pub filename: Option<&'a str>,
    pub session: Option<&'a str>,
    pub home: Option<&'a Home>,
    pub standing: Option<&'a Answer>,
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
/// label and name kept, the home decided, then `memory_put`, `home_put`
/// and `standing_put` in the reference's order. `marks` are payload fields
/// beside the record (`CORROBORATION_MARK`).
#[allow(clippy::too_many_arguments)]
fn put_in(
    tx: &mut Transaction<'_>,
    memory: &Memory,
    provenance: Option<String>,
    filename: Option<&str>,
    session: Option<&str>,
    home: Option<&Home>,
    standing: Option<PyValue>,
    marks: Vec<(&str, PyValue)>,
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
    let home_row = home::home_row(tx, &id, "memory", home)?;
    let mut items = marks;
    items.extend([
        ("memory", record),
        ("provenance", PyValue::Str(label)),
        ("filename", name),
    ]);
    tx.mutate("memory_put", &dict(items), session)?;
    if let Some(row) = &home_row {
        home::put_home(tx, row, session)?;
    }
    if let Some(row) = &standing {
        home::put_standing_row(tx, row, session)?;
    }
    Ok(())
}

/// `_standing_row` (202-222) as `put_memory` runs it (143), before its
/// transaction begins: the checks, then one wall-clock read.
fn standing_before(
    provider: &mut dyn Provider,
    memory: &Memory,
    put: &Put<'_>,
) -> Result<Option<PyValue>, RecordWriteError> {
    match put.standing {
        Some(answer) => Ok(Some(home::standing_row_at(
            &memory.fields.id,
            &memory.fields.body,
            answer,
            || provider.wall_now().map_err(WriteError::Clock),
        )?)),
        None => Ok(None),
    }
}

/// `put_memory` (114-154) inside `tx`, after its provenance check and
/// `_standing_row`: `_put_memory` with the home and the standing row.
fn put_checked(
    tx: &mut Transaction<'_>,
    memory: &Memory,
    provenance: Option<String>,
    put: Put<'_>,
    standing: Option<PyValue>,
) -> Result<(), RecordWriteError> {
    put_in(
        tx,
        memory,
        provenance,
        put.filename,
        put.session,
        put.home,
        standing,
        Vec::new(),
    )
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
        let standing = standing_before(provider, memory, &put)?;
        let mut tx = start(self, provider, hook)?;
        put_checked(&mut tx, memory, provenance, put, standing)?;
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
            SqlValue::Text(t) => loads_worded(&unicode::cps(t))?,
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

// -- part B: write, update and the active record ----------------------------

/// `Store.write`'s arguments (`store_records.py` 767-782), each as the
/// Python value a caller passes. None is the reference's default:
/// `confidence` `medium`, `source` `explicit-statement`, the rest None.
#[derive(Clone, Copy, Debug)]
pub struct WriteArgs<'a> {
    pub content: &'a PyValue,
    pub scopes: &'a PyValue,
    pub confidence: Option<&'a PyValue>,
    pub source: Option<&'a PyValue>,
    pub origin: Option<&'a PyValue>,
    pub category: Option<&'a PyValue>,
    pub claims: Option<&'a PyValue>,
    pub links: Option<&'a PyValue>,
    pub actor: Option<&'a PyValue>,
    pub session: Option<&'a str>,
    pub home: Option<&'a Home>,
    pub standing: Option<&'a Answer>,
}

/// `Store.update`'s keyword arguments (811-819).
#[derive(Clone, Copy, Debug, Default)]
pub struct Update<'a> {
    pub force: bool,
    pub preserve_verification: bool,
    pub session: Option<&'a str>,
    pub standing: Option<&'a Answer>,
}

fn refused(class: &'static str, text: String) -> RecordWriteError {
    RecordWriteError::raised(class, unicode::cps(&text))
}

/// `list(value)` for the values a caller can pass: a list or tuple's
/// items, a str's characters, a dict's keys; TypeError otherwise.
fn py_list(value: &PyValue) -> Result<Vec<PyValue>, RecordWriteError> {
    Ok(match value {
        PyValue::List(items) | PyValue::Tuple(items) => items.clone(),
        PyValue::Str(s) => s.iter().map(|&c| PyValue::Str(vec![c])).collect(),
        PyValue::Dict(items) => items.iter().map(|(k, _)| k.clone()).collect(),
        other => {
            return Err(refused(
                "TypeError",
                format!("'{}' object is not iterable", home::type_name(other)),
            ))
        }
    })
}

/// `str.strip()`: `str.isspace`'s set (`\x1c`-`\x1f` among it), both ends.
fn py_strip(text: &[u32]) -> &[u32] {
    let Some(a) = text.iter().position(|&c| !unicode::is_space(c)) else {
        return &[];
    };
    let b = text
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .unwrap_or(a);
    &text[a..=b]
}

fn write_error(e: WriteError) -> RecordWriteError {
    RecordWriteError::Write(e)
}

/// `a != b` for two datetimes: aware ones by instant, naive ones by their
/// fields, a naive and an aware one always unequal.
fn dt_differ(a: &DateTime, b: &DateTime) -> bool {
    match (a.offset_us, b.offset_us) {
        (Some(x), Some(y)) => a.wall_us() - i128::from(x) != b.wall_us() - i128::from(y),
        (None, None) => a.wall_us() != b.wall_us(),
        _ => true,
    }
}

/// `Store._active_or_raise(memory_id)` (1011-1026) inside `tx`: the
/// active record; MemoryNotFoundError "invalid id: {id!r}" for a text that
/// is not a ULID, TombstonedError "memory {id} was removed: {reason}" for
/// a removed one, MemoryNotFoundError "no memory with id {id}" otherwise.
pub fn active_or_raise(
    tx: &Transaction<'_>,
    memory_id: &[u32],
) -> Result<Memory, RecordWriteError> {
    if !crate::records::is_valid_ulid(memory_id) {
        let mut message = unicode::cps("invalid id: ");
        message.extend(repr_str(memory_id));
        return Err(RecordWriteError::raised("MemoryNotFoundError", message));
    }
    let id = text_id(memory_id)?;
    if let Some(row) = one_row(tx.connection(), MEMORY_ONE, &id)? {
        return rows::memory(&row).map_err(|e| write_error(home::record_refusal(e)));
    }
    if let Some(reason) = removed_reason(tx.connection(), &id)? {
        let mut message = unicode::cps(&format!("memory {id} was removed: "));
        message.extend(reason);
        return Err(RecordWriteError::raised("TombstonedError", message));
    }
    Err(refused(
        "MemoryNotFoundError",
        format!("no memory with id {id}"),
    ))
}

/// `Memory.model_validate(new_memory.model_dump())` as a check only: its
/// refusal, or nothing.
fn validate_as_check(memory: &Memory) -> Result<(), RecordWriteError> {
    Memory::from_payload(&memory.dump())
        .map(|_| ())
        .map_err(|e| write_error(home::record_refusal(e)))
}

impl WritableStore {
    /// `Store.write(...)` (767-808): one wall-clock read for `created` and
    /// `updated`, one minted id, the record built and validated as the
    /// reference builds it, then `put_memory(provenance="local")`. Returns
    /// the record.
    pub fn write(
        &mut self,
        provider: &mut dyn Provider,
        args: WriteArgs<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        self.write_hooked(provider, args, None)
    }

    /// [`WritableStore::write`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn write_hooked(
        &mut self,
        provider: &mut dyn Provider,
        args: WriteArgs<'_>,
        hook: Hook<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        let memory = build_memory(provider, &args)?;
        let put = Put {
            provenance: None,
            filename: None,
            session: args.session,
            home: args.home,
            standing: args.standing,
        };
        let standing = standing_before(provider, &memory, &put)?;
        let mut tx = start(self, provider, hook)?;
        put_checked(&mut tx, &memory, Some(LOCAL.to_owned()), put, standing)?;
        let committed = tx.commit()?;
        Ok(Done {
            value: memory,
            committed,
        })
    }

    /// `Store.update(memory, force=, preserve_verification=, session=,
    /// standing=)` (811-859): the active record read under the lock, the
    /// snapshot's `updated` compared unless `force`, `updated` from the
    /// wall clock, the rollup (and with `preserve_verification` the
    /// verification fields) carried, the copy validated as a check only,
    /// then `put_memory`. Returns the copy.
    pub fn update(
        &mut self,
        provider: &mut dyn Provider,
        memory: &Memory,
        update: Update<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        self.update_hooked(provider, memory, update, None)
    }

    /// [`WritableStore::update`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn update_hooked(
        &mut self,
        provider: &mut dyn Provider,
        memory: &Memory,
        update: Update<'_>,
        hook: Hook<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        let mut tx = start(self, provider, hook)?;
        let current = active_or_raise(&tx, &memory.fields.id)?;
        if !update.force && dt_differ(&current.fields.updated, &memory.fields.updated) {
            let id = text_id(&memory.fields.id)?;
            return Err(refused(
                "ConcurrentUpdateError",
                format!(
                    "memory {id} was updated concurrently (your snapshot is stale; \
                     stored updated={})",
                    current.fields.updated.isoformat()
                ),
            ));
        }
        let mut new = memory.clone();
        new.fields.updated = tx.wall_now()?;
        new.corroborations = current.corroborations.clone();
        new.last_corroborated = current.last_corroborated;
        if update.preserve_verification {
            let c = &current.fields;
            let n = &mut new.fields;
            n.last_verified_at = c.last_verified_at;
            n.verified_paths = c.verified_paths.clone();
            n.verified_commits = c.verified_commits.clone();
            n.verified_versions = c.verified_versions.clone();
            n.verified_absent_paths = c.verified_absent_paths.clone();
            n.verified_path_targets = c.verified_path_targets.clone();
            n.verified_absent_path_targets = c.verified_absent_path_targets.clone();
            n.claims = c.claims.clone();
            n.verified_head = c.verified_head.clone();
        }
        validate_as_check(&new)?;
        let put = Put {
            session: update.session,
            standing: update.standing,
            ..Put::default()
        };
        // Inside `update`'s own transaction on both sides (`put_memory`
        // nested in `_transaction`, 835-859).
        let standing = match put.standing {
            Some(answer) => Some(home::standing_row(
                &mut tx,
                &new.fields.id,
                &new.fields.body,
                answer,
            )?),
            None => None,
        };
        put_checked(&mut tx, &new, None, put, standing)?;
        let committed = tx.commit()?;
        Ok(Done {
            value: new,
            committed,
        })
    }
}

/// `write`'s record (788-805), in the reference's order of evaluation:
/// `utcnow()`, `generate_ulid()`, then each keyword argument left to right
/// (`list(scopes)`, `content.strip()`, the actor dict, `list(claims)`, each
/// link), then the model's own validation.
fn build_memory(
    provider: &mut dyn Provider,
    args: &WriteArgs<'_>,
) -> Result<Memory, RecordWriteError> {
    let now = provider.wall_now().map_err(WriteError::Clock)?;
    let id = provider.ulid().map_err(WriteError::Clock)?;
    let scopes = PyValue::List(py_list(args.scopes)?);
    let PyValue::Str(content) = args.content else {
        return Err(refused(
            "AttributeError",
            format!(
                "'{}' object has no attribute 'strip'",
                home::type_name(args.content)
            ),
        ));
    };
    let mut body = py_strip(content).to_vec();
    body.push(u32::from('\n'));
    let actor = match args.actor {
        Some(v @ PyValue::Dict(_)) => crate::records::Actor::from_value(v)
            .map_err(|e| write_error(home::record_refusal(e)))?
            .dump(),
        Some(v) => v.clone(),
        None => PyValue::None,
    };
    let claims = match args.claims {
        Some(v) if home::truthy(v) => PyValue::List(py_list(v)?),
        _ => PyValue::List(Vec::new()),
    };
    let mut links = Vec::new();
    if let Some(v) = args.links.filter(|v| home::truthy(v)) {
        for entry in py_list(v)? {
            let link = crate::records::MemoryLink::from_value(&entry)
                .map_err(|e| write_error(home::record_refusal(e)))?;
            links.push(link.dump(false));
        }
    }
    let medium = PyValue::str("medium");
    let explicit = PyValue::str("explicit-statement");
    let none = PyValue::None;
    let values: Vec<(&str, PyValue)> = vec![
        ("id", PyValue::str(&id)),
        ("created", PyValue::DateTime(now)),
        ("updated", PyValue::DateTime(now)),
        ("scopes", scopes),
        ("confidence", args.confidence.unwrap_or(&medium).clone()),
        ("source", args.source.unwrap_or(&explicit).clone()),
        ("body", PyValue::Str(body)),
        ("origin", args.origin.unwrap_or(&none).clone()),
        ("actor", actor),
        ("category", args.category.unwrap_or(&none).clone()),
        ("claims", claims),
        ("links", PyValue::List(links)),
    ];
    let mut fields = crate::records::Fields::default();
    for (name, value) in &values {
        fields.push(name, crate::records::lax::Arg::Py(value));
    }
    Memory::validate(&fields).map_err(|e| write_error(home::record_refusal(e)))
}

// -- part B2: mark_verified and record_corroboration ------------------------

/// `Store.mark_verified`'s keyword arguments (862-878), each the Python
/// value a caller passes; None is the reference's default (None, `False`
/// for `check_expected`).
#[derive(Clone, Copy, Debug, Default)]
pub struct Verify<'a> {
    pub verified_paths: Option<&'a PyValue>,
    pub verified_commits: Option<&'a PyValue>,
    pub verified_versions: Option<&'a PyValue>,
    pub verified_absent_paths: Option<&'a PyValue>,
    pub verified_path_targets: Option<&'a PyValue>,
    pub verified_absent_path_targets: Option<&'a PyValue>,
    pub claims: Option<&'a PyValue>,
    pub verified_head: Option<&'a PyValue>,
    pub expected_last_verified_at: Option<&'a PyValue>,
    pub expected_updated: Option<&'a PyValue>,
    pub check_expected: bool,
    pub session: Option<&'a str>,
}

/// `CORROBORATION_MARK` (85).
pub const CORROBORATION_MARK: &str = "corroboration";

/// `CORROBORATION_CHECK_SQL` (92-95).
pub const CORROBORATION_CHECK_SQL: &str =
    "SELECT 1 FROM log WHERE kind = 'memory_put' AND session = ? \
     AND instr(payload, ?) > 0 LIMIT 1";

fn given(v: Option<&PyValue>) -> Option<&PyValue> {
    v.filter(|v| !matches!(v, PyValue::None))
}

/// `stored != caller's` for an optional datetime and any Python value.
fn opt_dt_differ(stored: Option<&DateTime>, given: Option<&PyValue>) -> bool {
    match (stored, given.unwrap_or(&PyValue::None)) {
        (None, PyValue::None) => false,
        (Some(d), PyValue::DateTime(e)) => dt_differ(d, e),
        _ => true,
    }
}

/// `dict(value)` for the values a caller can pass: a dict as it is, an
/// iterable of pairs; CPython's TypeError or ValueError otherwise.
fn py_dict(value: &PyValue) -> Result<PyValue, RecordWriteError> {
    let items = match value {
        PyValue::Dict(_) => return Ok(value.clone()),
        PyValue::List(_) | PyValue::Tuple(_) | PyValue::Str(_) => py_list(value)?,
        other => {
            return Err(refused(
                "TypeError",
                format!("'{}' object is not iterable", home::type_name(other)),
            ))
        }
    };
    let mut out: Vec<(PyValue, PyValue)> = Vec::new();
    for (i, item) in items.into_iter().enumerate() {
        let pair = match &item {
            PyValue::List(v) | PyValue::Tuple(v) => v.clone(),
            PyValue::Str(s) => s.iter().map(|&c| PyValue::Str(vec![c])).collect(),
            PyValue::Dict(v) => v.iter().map(|(k, _)| k.clone()).collect(),
            _ => {
                return Err(refused(
                    "TypeError",
                    format!("cannot convert dictionary update sequence element #{i} to a sequence"),
                ))
            }
        };
        if pair.len() != 2 {
            return Err(refused(
                "ValueError",
                format!(
                    "dictionary update sequence element #{i} has length {}; 2 is required",
                    pair.len()
                ),
            ));
        }
        let mut pair = pair.into_iter();
        let (k, v) = (
            pair.next().unwrap_or(PyValue::None),
            pair.next().unwrap_or(PyValue::None),
        );
        if let Some(kind) = unhashable(&k) {
            return Err(refused("TypeError", format!("unhashable type: '{kind}'")));
        }
        match out.iter_mut().find(|(old, _)| key_eq(old, &k)) {
            Some(slot) => slot.1 = v,
            None => out.push((k, v)),
        }
    }
    Ok(PyValue::Dict(out))
}

/// A number key as Python's `==` (and so `hash`) reads it: the exact
/// decimal of an int, a bool or an integral finite float (`-0.0` is `0`),
/// or None for a float with a fraction, an infinity or a NaN.
fn integral(value: &PyValue) -> Option<String> {
    match value {
        PyValue::Bool(b) => Some(if *b { "1" } else { "0" }.to_owned()),
        PyValue::Int(d) => Some(d.clone()),
        PyValue::Float(f) if f.is_finite() && f.fract() == 0.0 => {
            // `{:.0}` of an integral f64 is its exact decimal.
            let text = format!("{f:.0}");
            Some(if text == "-0" { "0".to_owned() } else { text })
        }
        _ => None,
    }
}

/// Two hashable keys as a `dict` finds them the same: Python's `==`. `1`,
/// `1.0` and `True` are one key, as are `0`, `0.0`, `-0.0` and `False`,
/// and an int too large for a float equals the float only when the float
/// is exactly that integer. Tuples compare item by item. A NaN equals
/// nothing: `dict()` also matches by identity first, but each NaN decoded
/// from data is an object of its own, so two NaN keys stay two. Aware
/// datetimes compare as instants (by fields where the UTC instant falls
/// outside the years a datetime holds), naive ones by fields, and a naive
/// one never equals an aware one. A str never equals a path.
fn key_eq(a: &PyValue, b: &PyValue) -> bool {
    let mut work = vec![(a, b)];
    while let Some((a, b)) = work.pop() {
        let same = match (a, b) {
            (PyValue::Float(x), PyValue::Float(y)) => x == y,
            (PyValue::Tuple(x), PyValue::Tuple(y)) => {
                if x.len() != y.len() {
                    return false;
                }
                work.extend(x.iter().zip(y.iter()));
                true
            }
            (PyValue::DateTime(x), PyValue::DateTime(y)) => match (x.offset_us, y.offset_us) {
                (Some(_), Some(_)) => match (x.to_utc(), y.to_utc()) {
                    (Some(Ok(u)), Some(Ok(v))) => u == v,
                    _ => x == y,
                },
                (None, None) => x == y,
                _ => false,
            },
            (
                PyValue::Bool(_) | PyValue::Int(_) | PyValue::Float(_),
                PyValue::Bool(_) | PyValue::Int(_) | PyValue::Float(_),
            ) => match (integral(a), integral(b)) {
                (Some(x), Some(y)) => x == y,
                _ => false,
            },
            _ => a == b,
        };
        if !same {
            return false;
        }
    }
    true
}

/// `int + 1` of a canonical non-negative decimal.
fn plus_one(decimal: &str) -> String {
    let mut digits: Vec<u8> = decimal.bytes().collect();
    for d in digits.iter_mut().rev() {
        if *d == b'9' {
            *d = b'0';
        } else {
            *d += 1;
            return String::from_utf8(digits).unwrap_or_default();
        }
    }
    let mut out = String::from("1");
    out.push_str(&String::from_utf8(digits).unwrap_or_default());
    out
}

/// `mark_verified`'s check before its transaction (887-893): the head
/// stripped and lowercased, a full commit hash, or ValueError quoting
/// the normalised value.
fn verified_head(head: Option<&PyValue>) -> Result<PyValue, RecordWriteError> {
    let Some(head) = given(head) else {
        return Ok(PyValue::None);
    };
    let PyValue::Str(text) = head else {
        return Err(refused(
            "AttributeError",
            format!(
                "'{}' object has no attribute 'strip'",
                home::type_name(head)
            ),
        ));
    };
    let norm = unicode::lower(py_strip(text));
    if !crate::records::is_full_commit_sha(&norm) {
        let mut message = unicode::cps(
            "verified_head must be a full commit hash (40 or 64 hex characters), got ",
        );
        message.extend(repr_str(&norm));
        return Err(RecordWriteError::raised("ValueError", message));
    }
    Ok(PyValue::Str(norm))
}

/// `_corroborated(tx, memory_id, session)` (1002-1008).
fn corroborated(
    tx: &Transaction<'_>,
    memory_id: &str,
    session: &str,
) -> Result<bool, RecordWriteError> {
    let mark = dict(vec![(CORROBORATION_MARK, PyValue::str(memory_id))]);
    let text = json::canonical_payload(&mark).map_err(py_failed)?;
    let needle: String = {
        let mut chars = text.chars();
        chars.next();
        chars.next_back();
        chars.as_str().to_owned()
    };
    Ok(tx
        .connection()
        .prepare(CORROBORATION_CHECK_SQL)?
        .exists(params![session, needle])?)
}

impl WritableStore {
    /// `Store.mark_verified(memory_id, ...)` (862-951): the head checked
    /// before the transaction; in it the active record, `check_expected`,
    /// one wall-clock read for `last_verified_at` and the stamp, the lists
    /// and target maps given replaced, the copy validated (as a check: a
    /// copy the check accepts holds the values it validates), `memory_put`
    /// then `verification_put`. Returns the record.
    pub fn mark_verified(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        verify: Verify<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        self.mark_verified_hooked(provider, memory_id, verify, None)
    }

    /// [`WritableStore::mark_verified`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn mark_verified_hooked(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        verify: Verify<'_>,
        hook: Hook<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        let head = verified_head(verify.verified_head)?;
        let mut tx = start(self, provider, hook)?;
        let existing = active_or_raise(&tx, &unicode::cps(memory_id))?;
        if verify.check_expected
            && (opt_dt_differ(
                existing.fields.last_verified_at.as_ref(),
                verify.expected_last_verified_at,
            ) || given(verify.expected_updated)
                .is_some_and(|e| opt_dt_differ(Some(&existing.fields.updated), Some(e))))
        {
            return Err(refused(
                "ConcurrentUpdateError",
                format!(
                    "memory {memory_id} was updated concurrently (your snapshot is stale; \
                     stored updated={})",
                    existing.fields.updated.isoformat()
                ),
            ));
        }
        let now = tx.wall_now()?;
        let dumped = existing.dump();
        let PyValue::Dict(items) = &dumped else {
            return Err(py_failed(PyError::TypeError));
        };
        let mut items = items.clone();
        let mut set = |name: &str, value: PyValue| {
            let key = PyValue::str(name);
            match items.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = value,
                None => items.push((key, value)),
            }
        };
        set("last_verified_at", PyValue::DateTime(now));
        for (name, value) in [
            ("verified_paths", verify.verified_paths),
            ("verified_commits", verify.verified_commits),
            ("verified_versions", verify.verified_versions),
            ("verified_absent_paths", verify.verified_absent_paths),
        ] {
            if let Some(v) = given(value) {
                set(name, PyValue::List(py_list(v)?));
            }
        }
        for (name, targets, inputs) in [
            (
                "verified_path_targets",
                verify.verified_path_targets,
                verify.verified_paths,
            ),
            (
                "verified_absent_path_targets",
                verify.verified_absent_path_targets,
                verify.verified_absent_paths,
            ),
        ] {
            if given(targets).is_some() || given(inputs).is_some() {
                let value = match given(targets) {
                    Some(t) => py_dict(t)?,
                    None => PyValue::None,
                };
                set(name, value);
            }
        }
        if let Some(v) = given(verify.claims) {
            set("claims", PyValue::List(py_list(v)?));
        }
        set("verified_head", head.clone());
        let new = Memory::from_payload(&PyValue::Dict(items))
            .map_err(|e| write_error(home::record_refusal(e)))?;
        let row = one_row(
            tx.connection(),
            "SELECT provenance, filename FROM memories WHERE id = ?",
            memory_id,
        )?
        .ok_or(RecordWriteError::Record(RecordError::Raised("TypeError")))?;
        check_targets(&new.fields)?;
        let record = new.record_payload().map_err(py_failed)?;
        let provenance =
            py_str(&py_value(column(&row, "provenance")?, "provenance")?).map_err(py_failed)?;
        let filename = py_value(column(&row, "filename")?, "filename")?;
        let payload = dict(vec![
            ("memory", record),
            ("provenance", PyValue::Str(provenance)),
            ("filename", filename),
        ]);
        let strs = |v: &[PyStr]| PyValue::List(v.iter().cloned().map(PyValue::Str).collect());
        let f = &new.fields;
        let mut stamp = vec![
            ("memory_id", PyValue::str(memory_id)),
            ("machine_id", PyValue::None),
            ("verified_at", PyValue::str(&now.isoformat())),
            ("verified_head", head),
            ("verified_paths", strs(&f.verified_paths)),
            ("verified_commits", strs(&f.verified_commits)),
            ("verified_versions", strs(&f.verified_versions)),
            ("verified_absent_paths", strs(&f.verified_absent_paths)),
        ];
        let dumped = new.dump();
        let PyValue::Dict(dumped) = &dumped else {
            return Err(py_failed(PyError::TypeError));
        };
        for name in ["verified_path_targets", "verified_absent_path_targets"] {
            let key = PyValue::str(name);
            if let Some((_, v)) = dumped.iter().find(|(k, _)| *k == key) {
                if !matches!(v, PyValue::None) {
                    stamp.push((name, v.clone()));
                }
            }
        }
        tx.mutate("memory_put", &payload, verify.session)?;
        tx.mutate("verification_put", &dict(stamp), verify.session)?;
        let committed = tx.commit()?;
        Ok(Done {
            value: new,
            committed,
        })
    }

    /// `Store.record_corroboration(memory_id, session=)` (966-999): the
    /// active record; with a session that already corroborated it, None and
    /// nothing written (no clock read); else the rollup bumped,
    /// `last_corroborated` from the wall clock, and `memory_put` with the
    /// mark `{"corroboration": id}`. Returns the record.
    pub fn record_corroboration(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        session: Option<&str>,
    ) -> Result<Done<Option<Memory>>, RecordWriteError> {
        self.record_corroboration_hooked(provider, memory_id, session, None)
    }

    /// [`WritableStore::record_corroboration`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn record_corroboration_hooked(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        session: Option<&str>,
        hook: Hook<'_>,
    ) -> Result<Done<Option<Memory>>, RecordWriteError> {
        let mut tx = start(self, provider, hook)?;
        let existing = active_or_raise(&tx, &unicode::cps(memory_id))?;
        if let Some(s) = session {
            if corroborated(&tx, memory_id, s)? {
                let committed = tx.commit()?;
                return Ok(Done {
                    value: None,
                    committed,
                });
            }
        }
        let mut bumped = existing;
        bumped.corroborations = plus_one(&bumped.corroborations);
        bumped.last_corroborated = Some(tx.wall_now()?);
        put_in(
            &mut tx,
            &bumped,
            None,
            None,
            session,
            None,
            None,
            vec![(CORROBORATION_MARK, PyValue::str(memory_id))],
        )?;
        let committed = tx.commit()?;
        Ok(Done {
            value: Some(bumped),
            committed,
        })
    }
}

// -- part W6r: put_tombstone, restore, restore_trimmed and the relink -------

/// `_TOMBSTONE_SELECT` (`store_records.py:105-108`), for one id.
const TOMBSTONE_ONE: &str =
    "SELECT tombstones.*, (SELECT payload FROM log WHERE mac = tombstones.log_mac \
     AND kind = 'tombstone_put' LIMIT 1) AS attestation_payload FROM tombstones WHERE id = ?";

/// `store_returned.RELINK_MARK`.
pub const RELINK_MARK: &str = "relink";

/// `put_tombstone`'s keyword arguments (550-560), each as a caller passes
/// it. None is the reference's default: no label or name (the row's, or
/// `local` and None for a new row), no links, 0 corroborations, None.
#[derive(Clone, Copy, Debug, Default)]
pub struct PutTombstone<'a> {
    pub provenance: Option<&'a PyValue>,
    pub filename: Option<&'a str>,
    /// Any iterable a caller passes; each item through
    /// `MemoryLink.model_validate`.
    pub links: Option<&'a PyValue>,
    /// Through `int()`.
    pub corroborations: Option<&'a PyValue>,
    pub last_corroborated: Option<DateTime>,
    pub session: Option<&'a str>,
}

/// What a restore's relink reads provenance with, which the reference
/// holds on its `Store` and native takes from the caller.
#[derive(Clone, Copy, Debug)]
pub struct Relink<'a> {
    /// `Store.strict_provenance` (`store.py` 1147): as the opener passed
    /// it, else `configured_strict_provenance()`. Reading the config is the
    /// caller's (not ported); a config that cannot be read means strict.
    pub strict: bool,
    /// `_unaccounted_memory_ids`: the ids the caller's last `refold` in
    /// this process found unaccounted; empty for a fresh open.
    pub unaccounted: &'a [String],
}

impl Default for Relink<'_> {
    /// Strict, with no refold's ids: what an unreadable config gives a
    /// fresh open.
    fn default() -> Self {
        Relink {
            strict: true,
            unaccounted: &[],
        }
    }
}

/// `restore_trimmed`'s keyword arguments (1124-1132), each as a caller
/// passes it; None is the default (nothing dropped, False).
#[derive(Clone, Copy, Debug, Default)]
pub struct Trim<'a> {
    pub drop_claims: Option<&'a PyValue>,
    pub drop_verified_paths: Option<&'a PyValue>,
    pub clear_verification: Option<&'a PyValue>,
    pub drop_verified_head: Option<&'a PyValue>,
    pub session: Option<&'a str>,
    /// What the relink reads provenance with.
    pub relink: Relink<'a>,
}

/// `json.loads(text)` with the class the reference raises and, for a
/// JSONDecodeError (a ValueError) and the digit limit's ValueError, its
/// text as CPython words it (`json::decode_failure`); a RecursionError's
/// text is the interpreter's and is not carried.
fn loads_worded(text: &[u32]) -> Result<PyValue, RecordWriteError> {
    json::loads_detailed(text).map_err(|e| {
        let message = match json::decode_failure(text) {
            Some(f @ (json::DecodeFailure::At(..) | json::DecodeFailure::IntDigits(_))) => {
                Some(f.message(text))
            }
            _ => None,
        };
        RecordWriteError::Write(WriteError::Refused {
            class: e.name(),
            text: if e == json::LoadError::Recursion {
                None
            } else {
                message
            },
        })
    })
}

/// `int(value)` with CPython's messages (`fold`'s port).
fn int_worded(v: &PyValue) -> Result<PyValue, RecordWriteError> {
    super::fold::int_worded(v)
        .map_err(|(class, text)| RecordWriteError::Write(WriteError::Refused { class, text }))
}

/// A row's apply refused for an int past SQLite's 64 bits: sqlite3's
/// OverflowError, with its text (`digest::OVERFLOW_TEXT`), which the
/// transaction reports by class only.
fn overflow_worded(e: WriteError) -> RecordWriteError {
    match e {
        WriteError::Refused {
            class: "OverflowError",
            text: None,
        } => RecordWriteError::Write(WriteError::Refused {
            class: "OverflowError",
            text: Some(super::digest::OVERFLOW_TEXT.to_owned()),
        }),
        other => RecordWriteError::Write(other),
    }
}

/// `set(value)`: its items (`list()`'s), TypeError for an unhashable one.
fn py_set(value: &PyValue) -> Result<Vec<PyValue>, RecordWriteError> {
    let items = py_list(value)?;
    for item in &items {
        if let Some(kind) = unhashable(item) {
            return Err(refused("TypeError", format!("unhashable type: '{kind}'")));
        }
    }
    Ok(items)
}

/// `text in gone` for a set of hashables: a str equals only a str.
fn holds(gone: &[PyValue], text: &[u32]) -> bool {
    gone.iter()
        .any(|g| matches!(g, PyValue::Str(s) if s.as_slice() == text))
}

fn provenance_failed(e: super::provenance::ProvenanceError) -> RecordWriteError {
    match e {
        super::provenance::ProvenanceError::Raised { class, .. } => {
            RecordWriteError::Write(WriteError::Refused { class, text: None })
        }
        super::provenance::ProvenanceError::NotPorted(what) => {
            RecordWriteError::NotPorted(WriterNotPorted::Provenance(what))
        }
    }
}

/// The tombstone row's record as an active one, as `restore` builds it
/// (650-662) and `restore_trimmed` (1146-1179), `edit` changing the
/// fields first: the links from `links_json`, each validated; `int()` of
/// the rollup; `_dt_opt` of `last_corroborated`; then `Memory(...)`.
fn restored(
    row: &Row,
    edit: impl FnOnce(&Tombstone, &mut Vec<(PyValue, PyValue)>) -> Result<(), RecordWriteError>,
) -> Result<Memory, RecordWriteError> {
    let dead = rows::tombstone(row).map_err(|e| write_error(home::record_refusal(e)))?;
    let dumped = dead.dump();
    let mut items = match &dumped {
        PyValue::Dict(items) => items.clone(),
        _ => Vec::new(),
    };
    items.retain(|(k, _)| {
        !["removed", "removed_reason", "removed_session"]
            .iter()
            .any(|n| *k == PyValue::str(n))
    });
    edit(&dead, &mut items)?;
    let loaded = match column(row, "links_json")? {
        SqlValue::Text(t) => loads_worded(&unicode::cps(t))?,
        _ => return Err(py_failed(PyError::TypeError)),
    };
    let mut links = Vec::new();
    for entry in py_list(&loaded)? {
        let link = crate::records::MemoryLink::from_value(&entry)
            .map_err(|e| write_error(home::record_refusal(e)))?;
        links.push(link.dump(false));
    }
    let corroborations = int_worded(&py_value(column(row, "corroborations")?, "corroborations")?)?;
    let stamp = py_value(column(row, "last_corroborated")?, "last_corroborated")?;
    let last = match &stamp {
        PyValue::None => PyValue::None,
        PyValue::Str(t) => PyValue::DateTime(crate::datetime::parse_record_dt(t).map_err(|e| {
            RecordWriteError::Write(WriteError::Refused {
                class: e.name(),
                text: None,
            })
        })?),
        // `text.replace` of a non-str: AttributeError.
        _ => {
            return Err(RecordWriteError::Write(WriteError::Refused {
                class: "AttributeError",
                text: None,
            }))
        }
    };
    items.extend([
        (PyValue::str("links"), PyValue::List(links)),
        (PyValue::str("corroborations"), corroborations),
        (PyValue::str("last_corroborated"), last),
    ]);
    Memory::from_payload(&PyValue::Dict(items)).map_err(|e| write_error(home::record_refusal(e)))
}

fn set_field(items: &mut [(PyValue, PyValue)], name: &str, value: PyValue) {
    if let Some(slot) = items.iter_mut().find(|(k, _)| *k == PyValue::str(name)) {
        slot.1 = value;
    }
}

/// `store_returned.linking_sources` (215-233): the active records other
/// than `record_id` whose stored links name it, oldest id first.
fn linking_sources(conn: &Connection, record_id: &str) -> Result<Vec<String>, RecordWriteError> {
    let mut statement = conn.prepare(
        "SELECT id, links_json FROM memories WHERE id != ? AND links_json LIKE ? ORDER BY id",
    )?;
    let mut rows = statement.query(params![record_id, format!("%\"{record_id}\"%")])?;
    let want = PyValue::str(record_id);
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        let id = py_value(&sql_value(row.get_ref(0)?)?, "id")?;
        // `json.loads(row[1] or "[]")`: TypeError and ValueError pass it over.
        let text = match sql_value(row.get_ref(1)?)? {
            SqlValue::Null => "[]".to_owned(),
            SqlValue::Text(t) if t.is_empty() => "[]".to_owned(),
            SqlValue::Text(t) => t,
            _ => continue,
        };
        let links = match json::loads(&unicode::cps(&text)) {
            Ok(v) => v,
            Err(PyError::TypeError | PyError::ValueError) => continue,
            Err(e) => return Err(py_failed(e)),
        };
        // `any(...)` over the value: a number, a bool or None is not
        // iterable (TypeError, not caught).
        let named = py_list(&links)?.iter().any(|link| match link {
            PyValue::Dict(items) => items
                .iter()
                .rev()
                .find(|(k, _)| *k == PyValue::str("target_id"))
                .is_some_and(|(_, v)| *v == want),
            _ => false,
        });
        if named {
            // A column's text is UTF-8, so `str(id)` is too.
            out.push(json::utf8_text(&py_str(&id).map_err(py_failed)?));
        }
    }
    Ok(out)
}

/// What the relink needs to read provenance inside the transaction.
pub(crate) struct Ring {
    keys_dir: std::path::PathBuf,
    store_id: String,
}

/// `_relink` (696-703) inside `tx`: each active record whose links name
/// `record_id` put again unchanged with the relink mark
/// ([`put_relinked`]).
fn relink(
    tx: &mut Transaction<'_>,
    ring: &Ring,
    reads: Relink<'_>,
    record_id: &str,
    session: Option<&str>,
) -> Result<Vec<String>, RecordWriteError> {
    let sources: Vec<(String, String)> = linking_sources(tx.connection(), record_id)?
        .into_iter()
        .map(|source| (source, record_id.to_owned()))
        .collect();
    put_relinked(tx, ring, reads, &sources, session)
}

/// `_put_relinked` (706-741) inside `tx`: each record of `sources` (its
/// id, then the record whose rows it puts back, in the mapping's order)
/// put again unchanged with the relink mark, unless its label reads
/// `unaccounted` or its rows carry fields this build does not define (a
/// payload nested past the parser's limit there raises RecursionError and
/// the transaction writes nothing), or it is no longer active. The labels
/// are read once, before any put, on the transaction's own connection,
/// verified under the ring's current key (the writer's), with the
/// caller's `strict_provenance` and refold's ids. Returns the ids put.
pub(crate) fn put_relinked(
    tx: &mut Transaction<'_>,
    ring: &Ring,
    reads: Relink<'_>,
    sources: &[(String, String)],
    session: Option<&str>,
) -> Result<Vec<String>, RecordWriteError> {
    if sources.is_empty() {
        return Ok(Vec::new());
    }
    let labels = {
        let keys = super::keys::load_key_ring(&ring.keys_dir, &ring.store_id).map_err(|e| {
            RecordWriteError::NotPorted(WriterNotPorted::Provenance(format!("the key ring: {e:?}")))
        })?;
        let source = super::provenance::ConnSource(tx.connection());
        let reader = super::provenance::Provenance::new(&source, &keys, true, reads.strict)
            .map_err(provenance_failed)?
            .with_unaccounted(reads.unaccounted.iter().cloned());
        let ids: Vec<&str> = sources.iter().map(|(id, _)| id.as_str()).collect();
        reader.provenance_for(&ids).map_err(provenance_failed)?
    };
    let mut put = Vec::new();
    for (source_id, record_id) in sources {
        let label = labels.iter().find(|(id, _)| id == source_id);
        if label.is_some_and(|(_, l)| l == super::provenance::UNACCOUNTED) {
            continue;
        }
        let unknown =
            super::fold::unknown_record_fields_raising(tx.connection(), &PyValue::str(source_id))?
                .map_err(py_failed)?;
        if !unknown.is_empty() {
            continue;
        }
        let Some(row) = one_row(tx.connection(), MEMORY_ONE, source_id)? else {
            continue;
        };
        let memory = rows::memory(&row).map_err(|e| write_error(home::record_refusal(e)))?;
        put_in(
            tx,
            &memory,
            None,
            None,
            session,
            None,
            None,
            vec![(RELINK_MARK, PyValue::str(record_id))],
        )?;
        put.push(source_id.clone());
    }
    Ok(put)
}

/// `_put_restored` (669-693) inside `tx`.
fn put_restored(
    tx: &mut Transaction<'_>,
    ring: &Ring,
    reads: Relink<'_>,
    memory: &Memory,
    filename: PyValue,
    session: Option<&str>,
) -> Result<(), RecordWriteError> {
    let id = text_id(&memory.fields.id)?;
    let payload = dict(vec![
        ("memory", memory.record_payload().map_err(py_failed)?),
        ("provenance", PyValue::str(LOCAL)),
        ("filename", filename),
    ]);
    tx.mutate(
        "tombstone_delete",
        &dict(vec![("id", PyValue::str(&id))]),
        session,
    )?;
    tx.mutate("memory_put", &payload, session)?;
    relink(tx, ring, reads, &id, session)?;
    tx.name_returned(&id, &id, session);
    Ok(())
}

impl WritableStore {
    pub(crate) fn ring(&self) -> Ring {
        Ring {
            keys_dir: self.keys_dir().to_path_buf(),
            store_id: self.store_id().to_owned(),
        }
    }

    /// `Store.put_tombstone(dead, provenance=, filename=, links=,
    /// corroborations=, last_corroborated=, session=)` (550-594). Before
    /// the transaction: the label check and each link validated. Inside:
    /// ValueError for an active id, the row's label and name kept, then
    /// `tombstone_put`. Returns `dead`.
    pub fn put_tombstone(
        &mut self,
        provider: &mut dyn Provider,
        dead: &Tombstone,
        args: PutTombstone<'_>,
    ) -> Result<Done<Tombstone>, RecordWriteError> {
        self.put_tombstone_hooked(provider, dead, args, None)
    }

    /// [`WritableStore::put_tombstone`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn put_tombstone_hooked(
        &mut self,
        provider: &mut dyn Provider,
        dead: &Tombstone,
        args: PutTombstone<'_>,
        hook: Hook<'_>,
    ) -> Result<Done<Tombstone>, RecordWriteError> {
        let provenance = check_provenance(args.provenance)?;
        let mut link_rows = Vec::new();
        if let Some(links) = args.links {
            for link in py_list(links)? {
                let link = crate::records::MemoryLink::from_value(&link)
                    .map_err(|e| write_error(home::record_refusal(e)))?;
                link_rows.push(link.dump(true));
            }
        }
        let id = text_id(&dead.fields.id)?;
        let mut tx = start(self, provider, hook)?;
        if one_row(tx.connection(), "SELECT 1 FROM memories WHERE id = ?", &id)?.is_some() {
            return Err(refused(
                "ValueError",
                format!("{id} is active; tombstone it instead"),
            ));
        }
        let existing = one_row(
            tx.connection(),
            "SELECT provenance, filename FROM tombstones WHERE id = ?",
            &id,
        )?;
        let mut label: Option<PyStr> = provenance.map(|p| unicode::cps(&p));
        let mut name: PyValue = args.filename.map_or(PyValue::None, PyValue::str);
        if let Some(row) = &existing {
            if label.as_ref().is_none_or(Vec::is_empty) {
                let stored = py_value(column(row, "provenance")?, "provenance")?;
                label = Some(py_str(&stored).map_err(py_failed)?);
            }
            if args.filename.is_none() {
                name = py_value(column(row, "filename")?, "filename")?;
            }
        }
        let label = match label {
            Some(l) if !l.is_empty() => l,
            _ => unicode::cps(LOCAL),
        };
        let record = dead.record_payload().map_err(py_failed)?;
        let corroborations = match args.corroborations {
            Some(v) => int_worded(v)?,
            None => PyValue::int(0),
        };
        let last = args
            .last_corroborated
            .map_or(PyValue::None, |d| PyValue::str(&d.isoformat()));
        let payload = dict(vec![
            ("tombstone", record),
            ("provenance", PyValue::Str(label)),
            ("filename", name),
            ("links", PyValue::List(link_rows)),
            ("corroborations", corroborations),
            ("last_corroborated", last),
        ]);
        tx.mutate("tombstone_put", &payload, args.session)
            .map_err(overflow_worded)?;
        let committed = tx.commit()?;
        Ok(Done {
            value: dead.clone(),
            committed,
        })
    }

    /// `Store.restore(memory_id, session=)` (642-666): the tombstone read
    /// under the write lock (NotFoundError when there is none), the record
    /// rebuilt from it, then `_put_restored`. Returns the record.
    pub fn restore(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        session: Option<&str>,
        reads: Relink<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        self.restore_hooked(provider, memory_id, session, reads, None)
    }

    /// [`WritableStore::restore`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn restore_hooked(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        session: Option<&str>,
        reads: Relink<'_>,
        hook: Hook<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        let ring = self.ring();
        let mut tx = start(self, provider, hook)?;
        let Some(row) = one_row(tx.connection(), TOMBSTONE_ONE, memory_id)? else {
            return Err(RecordWriteError::raised(
                "NotFoundError",
                unicode::cps(memory_id),
            ));
        };
        let memory = restored(&row, |_, _| Ok(()))?;
        let filename = py_value(column(&row, "filename")?, "filename")?;
        put_restored(&mut tx, &ring, reads, &memory, filename, session)?;
        let committed = tx.commit()?;
        Ok(Done {
            value: memory,
            committed,
        })
    }

    /// `Store.restore_trimmed(memory_id, drop_claims=, drop_verified_paths=,
    /// clear_verification=, drop_verified_head=, session=)` (1124-1182):
    /// the id checked before the transaction; inside, NotTombstonedError
    /// for an active id, MemoryNotFoundError for no tombstone, the record
    /// rebuilt without the dropped trust fields, then `_put_restored`.
    pub fn restore_trimmed(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        trim: Trim<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        self.restore_trimmed_hooked(provider, memory_id, trim, None)
    }

    /// [`WritableStore::restore_trimmed`] with `hook` shown each boundary.
    #[doc(hidden)]
    pub fn restore_trimmed_hooked(
        &mut self,
        provider: &mut dyn Provider,
        memory_id: &str,
        trim: Trim<'_>,
        hook: Hook<'_>,
    ) -> Result<Done<Memory>, RecordWriteError> {
        let cps = unicode::cps(memory_id);
        if !crate::records::is_valid_ulid(&cps) {
            let mut message = unicode::cps("invalid id: ");
            message.extend(repr_str(&cps));
            return Err(RecordWriteError::raised("MemoryNotFoundError", message));
        }
        let ring = self.ring();
        let mut tx = start(self, provider, hook)?;
        if one_row(
            tx.connection(),
            "SELECT 1 FROM memories WHERE id = ?",
            memory_id,
        )?
        .is_some()
        {
            return Err(refused(
                "NotTombstonedError",
                format!("memory {memory_id} is active; nothing to restore"),
            ));
        }
        let Some(row) = one_row(tx.connection(), TOMBSTONE_ONE, memory_id)? else {
            return Err(refused(
                "MemoryNotFoundError",
                format!("no tombstone with id {memory_id}"),
            ));
        };
        let memory = restored(&row, |dead, items| {
            let gone = match trim.drop_claims {
                Some(v) => py_set(v)?,
                None => Vec::new(),
            };
            if !gone.is_empty() {
                let kept = dead
                    .fields
                    .claims
                    .iter()
                    .filter(|c| !holds(&gone, c))
                    .map(|c| PyValue::Str(c.clone()))
                    .collect();
                set_field(items, "claims", PyValue::List(kept));
            }
            let gone = match trim.drop_verified_paths {
                Some(v) => py_set(v)?,
                None => Vec::new(),
            };
            if !gone.is_empty() {
                let kept = dead
                    .fields
                    .verified_paths
                    .iter()
                    .filter(|p| !holds(&gone, p))
                    .map(|p| PyValue::Str(p.clone()))
                    .collect();
                set_field(items, "verified_paths", PyValue::List(kept));
                if let Some(targets) = &dead.fields.verified_path_targets {
                    let kept = targets
                        .iter()
                        .filter(|(raw, _)| !holds(&gone, raw))
                        .map(|(raw, t)| {
                            (
                                PyValue::Str(raw.clone()),
                                t.as_ref()
                                    .map_or(PyValue::None, |t| PyValue::Str(t.clone())),
                            )
                        })
                        .collect();
                    set_field(items, "verified_path_targets", PyValue::Dict(kept));
                }
            }
            if trim.clear_verification.is_some_and(home::truthy) {
                set_field(items, "last_verified_at", PyValue::None);
            }
            if trim.drop_verified_head.is_some_and(home::truthy) {
                set_field(items, "verified_head", PyValue::None);
            }
            Ok(())
        })?;
        let filename = py_value(column(&row, "filename")?, "filename")?;
        put_restored(&mut tx, &ring, trim.relink, &memory, filename, trim.session)?;
        let committed = tx.commit()?;
        Ok(Done {
            value: memory,
            committed,
        })
    }
}
