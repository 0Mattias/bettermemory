//! The store's own side of the document frequency memo: the trait
//! [`DocFreqSource`] over a real store connection (`store_index._memory_head`,
//! `Store._read_doc_freq_inputs` through `store_digest.digest_admission_rows`
//! and `digest_mark(build=False)`, `Store._term_masks`, the patch's
//! statement of `store_index._patched_masks`), and over it
//! `Store.document_frequencies` and `Store.ranking_statistics`.
//!
//! The source reads and, where the digest is behind by rows it can apply,
//! writes the digest in one transaction of its own (`digest_mark`), or
//! inside the open one under a savepoint (`Store._transaction`'s
//! `_joined`), which appends no log row. What `digest_mark` and `digest_admission_rows` log
//! is data: [`DigestWarning`]s the caller reads with
//! [`StoreDocFreq::warnings`]; nothing is printed.

use std::cell::RefCell;
use std::path::{Path, PathBuf};

use rusqlite::types::ValueRef;
use rusqlite::Connection;

use super::digest::{self, DigestError};
use super::docfreq::{
    load_scopes, row_mask, AdmissionRow, DocActor, DocFreqError, DocFreqMemo, DocFreqSource, Head,
    Mask, PyInt, Ranked, Statistics,
};
use super::open::{require_current_writer, WritableStore};
use super::provenance;
use super::reads::{self, failure_of, DocFreqs, ReadError};
use super::tx::Transaction;
use super::write::WriteError;
use super::{digest_query, sql_value, versions, SqlValue, StoreError};
use crate::identity::Actor;
use crate::json;
use crate::pyvalue::{PyError, PyValue};
use crate::records::rows::{self, Row};
use crate::unicode;

mod advance;

/// `_memory_head`'s statement: five probes of `log_by_kind`.
const MEMORY_HEAD: &str = "SELECT seq, mac FROM log WHERE seq = (SELECT MAX(s) FROM (\
     SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_put' \
     UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_delete' \
     UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_put' \
     UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_delete' \
     UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'index_rebuilt'))";

/// `digest_admission_rows`' statement.
const DIGEST_ROWS: &str = "SELECT d.rid, d.scopes, h.project, d.client, d.model, d.dl \
     FROM digest_records d LEFT JOIN homes h ON h.record_id = d.id WHERE d.removed = 0";

/// `store_index._ADMISSION_INPUTS`.
const ADMISSION_INPUTS: &str = "SELECT m.rowid AS rid, m.id AS id, m.scopes_json AS scopes_json, \
     h.project AS home_project, \
     m.actor_client AS actor_client, m.actor_model AS actor_model \
     FROM memories m LEFT JOIN homes h ON h.record_id = m.id";

/// `_patched_masks`' statement.
const FTS_ROWIDS: &str = "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? \
     AND rowid >= ? ORDER BY rowid DESC LIMIT ?";

/// Which of the two warnings the digest's reads log.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WarningKind {
    /// `store %s: the digest could not be brought current (%s)`
    /// (`digest_mark`).
    NotCurrent,
    /// `store %s: the digest could not be read (%s)`
    /// (`digest_admission_rows`).
    NotRead,
    /// `note_residue`'s line (`_clear_residue`): the whole message is the
    /// text; no exception, so no class.
    Residue,
    /// `store %s: a nested write could not be taken back (%s); the owner's
    /// transaction is rolled back with it` (`_joined`).
    NotTakenBack,
}

/// One warning on the `bettermemory.store` logger: the store's path text,
/// the caught exception's class, its SQLite result code where it carries
/// one, and its text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DigestWarning {
    pub kind: WarningKind,
    pub path: String,
    pub class: &'static str,
    pub code: Option<i64>,
    pub text: String,
}

impl DigestWarning {
    /// `record.getMessage()`.
    pub fn message(&self) -> String {
        let (path, text) = (&self.path, &self.text);
        match self.kind {
            WarningKind::NotCurrent => {
                format!("store {path}: the digest could not be brought current ({text})")
            }
            WarningKind::NotRead => format!("store {path}: the digest could not be read ({text})"),
            WarningKind::Residue => text.clone(),
            WarningKind::NotTakenBack => format!(
                "store {path}: a nested write could not be taken back ({text}); \
                 the owner's transaction is rolled back with it"
            ),
        }
    }
}

/// A home project as `admit` receives it: the column's text, or its bytes
/// where the row holds a BLOB (the reference hands either as it is; TEXT
/// affinity leaves no other kind in `homes.project`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Home<'a> {
    Text(&'a str),
    Bytes(&'a [u8]),
}

/// The admission predicate both paths call: `(scopes, home project,
/// actor)`.
pub type StoreAdmit<'a> = dyn FnMut(&[PyValue], Option<Home<'_>>, Option<&DocActor>) -> bool + 'a;

/// The memo groups and hands on a home as text: a BLOB's bytes (one char
/// per byte) and a text that starts with this mark are spelled after it,
/// so two homes the reference tells apart stay apart.
const MARK: char = '\u{F8FF}';

fn home_key(v: &SqlValue) -> Option<String> {
    match v {
        SqlValue::Null => None,
        SqlValue::Text(t) if t.starts_with(MARK) => Some(format!("{MARK}{t}")),
        SqlValue::Blob(b) => Some(
            std::iter::once(MARK)
                .chain("b".chars())
                .chain(b.iter().map(|&c| char::from(c)))
                .collect(),
        ),
        other => Some(other.py_str()),
    }
}

/// `home_key`'s inverse, handed to `with`: the home an [`AdmissionRow`]
/// this source made names.
pub fn with_home<R>(key: Option<&str>, with: impl FnOnce(Option<Home<'_>>) -> R) -> R {
    match key.and_then(|k| k.strip_prefix(MARK)) {
        None => with(key.map(Home::Text)),
        Some(text) if text.starts_with(MARK) => with(Some(Home::Text(text))),
        Some(bytes) => {
            let bytes: Vec<u8> = bytes.chars().skip(1).map(|c| c as u8).collect();
            with(Some(Home::Bytes(&bytes)))
        }
    }
}

/// A store connection as the memo's source.
pub struct StoreDocFreq<'c> {
    conn: &'c Connection,
    path: PathBuf,
    path_text: String,
    store_id: String,
    /// `self._tx_depth`: a transaction of this store is open.
    depth: bool,
    warnings: RefCell<Vec<DigestWarning>>,
}

fn sqlite_class(e: &rusqlite::Error) -> (&'static str, Option<i64>) {
    failure_of(e).unwrap_or(("Error", None))
}

fn source(class: &str) -> DocFreqError {
    DocFreqError::Source(class.to_owned())
}

fn from_store(e: StoreError) -> DocFreqError {
    match e {
        StoreError::Sqlite(e) => source(sqlite_class(&e).0),
        StoreError::NotUtf8 { .. } => source("OperationalError"),
        _ => source("OSError"),
    }
}

fn from_read(e: ReadError) -> DocFreqError {
    source(e.class())
}

/// `json.loads`' failure is JSONDecodeError; the shared `load_scopes`
/// names it by its base class ValueError, which nothing else under the
/// memo raises as `Py` (`py_int`'s is a `Source`).
fn json_class(e: DocFreqError) -> DocFreqError {
    match e {
        DocFreqError::Py(PyError::ValueError) => source("JSONDecodeError"),
        e => e,
    }
}

fn scopes(text: &str) -> Result<Option<Vec<PyValue>>, DocFreqError> {
    load_scopes(text).map_err(json_class)
}

/// The class `digest_mark` catches for a digest read that failed, by what
/// failed (the shared `DigestError` keeps no class).
fn unreadable_class(what: &str) -> &'static str {
    match what {
        "a text that is not UTF-8" => "OperationalError",
        "a payload" => "JSONDecodeError",
        "a payload's id" => "KeyError",
        "a cwd" | "an id" | "a body" => "UnicodeEncodeError",
        _ => "ValueError",
    }
}

/// The class `_mutated_ids` raised for the first log row after the
/// digest's seq whose payload does not read, read again after the
/// rollback: `json.loads`' (TypeError for a value that is not text or
/// bytes, UnicodeDecodeError, JSONDecodeError), then the indexing's
/// (TypeError for a value that is not a dict, KeyError for a key it
/// lacks). Where every payload reads and an id is a text holding a lone
/// surrogate, the class `_ingest` raised: a record named before it that
/// does not load (`record_class_of`), else binding that id
/// (UnicodeEncodeError). None otherwise.
fn payload_class(conn: &Connection) -> Option<&'static str> {
    let seen = digest::built(conn, digest::state(conn).ok()?.as_deref()).ok()??;
    let k = digest::DIGEST_KINDS;
    let mut statement = conn
        .prepare(
            "SELECT kind, payload FROM log WHERE kind IN (?, ?, ?, ?) \
             AND seq > ? ORDER BY seq",
        )
        .ok()?;
    let mut rows = statement
        .query(rusqlite::params![k[0], k[1], k[2], k[3], seen])
        .ok()?;
    let index = |v: &PyValue, key: &str| -> Result<PyValue, &'static str> {
        match v {
            PyValue::Dict(items) => items
                .iter()
                .find(|(k, _)| *k == PyValue::str(key))
                .map(|(_, v)| v.clone())
                .ok_or("KeyError"),
            _ => Err("TypeError"),
        }
    };
    // The ids read, first seen first, up to the first that does not
    // encode; then that id's place is fixed.
    let mut before: Vec<String> = Vec::new();
    let mut unbound = false;
    while let Some(row) = rows.next().ok()? {
        let kind = sql_value(row.get_ref(0).ok()?).ok()?.py_str();
        let text = match sql_value(row.get_ref(1).ok()?).ok()? {
            SqlValue::Text(t) => unicode::cps(&t),
            SqlValue::Blob(b) => match provenance::decode_json_bytes(&b) {
                Some(text) => text,
                None => return Some("UnicodeDecodeError"),
            },
            _ => return Some("TypeError"),
        };
        let payload = match json::loads_detailed(&text) {
            Ok(payload) => payload,
            Err(e) => return Some(e.name()),
        };
        let holder = match kind.as_str() {
            "memory_put" => index(&payload, "memory"),
            "tombstone_put" => index(&payload, "tombstone"),
            _ => Ok(payload),
        };
        match holder.and_then(|h| index(&h, "id")) {
            Err(class) => return Some(class),
            Ok(PyValue::Str(ref id)) if !unbound => match unicode::to_string(id) {
                Some(id) if !before.contains(&id) => before.push(id),
                Some(_) => {}
                None => unbound = true,
            },
            Ok(_) => {}
        }
    }
    unbound.then(|| record_class_of(conn, &before).unwrap_or("UnicodeEncodeError"))
}

const MEMORY_ROW: &str = "SELECT memories.*, (SELECT payload FROM log WHERE mac = \
     memories.log_mac AND kind = 'memory_put' LIMIT 1) AS attestation_payload FROM memories WHERE id = ?";
const TOMBSTONE_ROW: &str = "SELECT tombstones.*, (SELECT payload FROM log WHERE mac = \
     tombstones.log_mac AND kind = 'tombstone_put' LIMIT 1) AS attestation_payload \
     FROM tombstones WHERE id = ?";

/// The class `_row_to_memory` or `_row_to_tombstone` raised for the first
/// record `_ingest` after the digest's seq does not load (ValidationError,
/// JSONDecodeError, ...): read again, after the rollback.
fn record_class(conn: &Connection) -> Option<&'static str> {
    let seen = digest::built(conn, digest::state(conn).ok()?.as_deref()).ok()??;
    record_class_of(conn, &digest::mutated_ids(conn, seen).ok()?.0)
}

/// The class the first of `ids` whose record does not load raised.
fn record_class_of(conn: &Connection, ids: &[String]) -> Option<&'static str> {
    for id in ids {
        for (removed, sql) in [(false, MEMORY_ROW), (true, TOMBSTONE_ROW)] {
            let mut statement = conn.prepare(sql).ok()?;
            let names: Vec<String> = statement.column_names().map(str::to_owned).collect();
            let mut found = statement.query([&id]).ok()?;
            let Some(row) = found.next().ok()? else {
                continue;
            };
            let mut columns = Vec::with_capacity(names.len());
            for (i, name) in names.into_iter().enumerate() {
                columns.push((name, sql_value(row.get_ref(i).ok()?).ok()?));
            }
            let row = Row { columns };
            let failed = if removed {
                rows::tombstone(&row).err()
            } else {
                rows::memory(&row).err()
            };
            if let Some(e) = failed {
                return Some(e.class());
            }
        }
    }
    None
}

fn value(v: ValueRef<'_>) -> Result<SqlValue, DocFreqError> {
    sql_value(v).map_err(from_store)
}

/// `int()` of a column sqlite3 gave: an integer as it is, a real
/// truncated, a numeral's text, each of any size (a Python `int`);
/// TypeError for NULL, ValueError for NaN and what does not read,
/// OverflowError for an infinite real. A value past 64 bits is refused
/// only where it is used: bound (`_patched_masks`) or held in
/// `array('q')` (the grouping).
fn py_int(v: &SqlValue) -> Result<PyInt, DocFreqError> {
    let decimal = match v {
        SqlValue::Integer(i) => return Ok(PyInt::Small(*i)),
        SqlValue::Real(f) if f.is_nan() => return Err(source("ValueError")),
        SqlValue::Real(f) => {
            crate::pyvalue::py_int(&PyValue::Float(*f)).map_err(DocFreqError::Py)?
        }
        SqlValue::Null => return Err(DocFreqError::Py(PyError::TypeError)),
        SqlValue::Text(t) => {
            crate::pyvalue::int_from_str(&unicode::cps(t)).map_err(|p| source(p.name()))?
        }
        SqlValue::Blob(b) => match std::str::from_utf8(b) {
            Ok(t) if t.is_ascii() => {
                crate::pyvalue::int_from_str(&unicode::cps(t)).map_err(|p| source(p.name()))?
            }
            _ => return Err(source("ValueError")),
        },
    };
    match &decimal {
        PyValue::Int(text) => PyInt::from_decimal(text).ok_or_else(|| source("ValueError")),
        _ => Err(source("ValueError")),
    }
}

/// An actor column as pydantic's `str | None` reads it: None, a text,
/// bytes decoded as UTF-8; None for anything else (ValidationError).
fn actor_of(v: &SqlValue) -> Option<Option<String>> {
    match v {
        SqlValue::Null => Some(None),
        SqlValue::Text(t) => Some(Some(t.clone())),
        SqlValue::Blob(b) => std::str::from_utf8(b).ok().map(|t| Some(t.to_owned())),
        _ => None,
    }
}

impl<'c> StoreDocFreq<'c> {
    /// Over any connection to the store at `path`: in a transaction
    /// exactly when the connection is (`depth` false).
    pub fn over(conn: &'c Connection, path: &Path, store_id: &str) -> StoreDocFreq<'c> {
        let text = versions::path_text(path);
        StoreDocFreq {
            conn,
            path: path.to_owned(),
            path_text: unicode::to_string(&text)
                .unwrap_or_else(|| text.iter().filter_map(|&c| char::from_u32(c)).collect()),
            store_id: store_id.to_owned(),
            depth: false,
            warnings: RefCell::new(Vec::new()),
        }
    }

    /// The owner's writing store outside any transaction.
    pub fn of_store(store: &'c WritableStore) -> StoreDocFreq<'c> {
        StoreDocFreq::over(store.connection(), store.path(), store.store_id())
    }

    /// Inside a transaction of the store at `path`: in a transaction
    /// always, a rolled-back one included (`_tx_depth` stays 1 until the
    /// block ends). `digest_mark` joins it under a savepoint.
    pub fn of_transaction(
        tx: &'c Transaction<'_>,
        path: &Path,
        store_id: &str,
    ) -> StoreDocFreq<'c> {
        let mut out = StoreDocFreq::over(tx.connection(), path, store_id);
        out.depth = true;
        out
    }

    /// The warnings logged so far, and none kept after.
    pub fn warnings(&self) -> Vec<DigestWarning> {
        std::mem::take(&mut *self.warnings.borrow_mut())
    }

    fn warn(&self, kind: WarningKind, class: &'static str, code: Option<i64>, text: String) {
        self.warnings.borrow_mut().push(DigestWarning {
            kind,
            path: self.path_text.clone(),
            class,
            code,
            text,
        });
    }

    /// `digest_mark(build=False)`: the digest's mark, the record mutations
    /// it is behind by applied first, in a transaction of its own or under
    /// a savepoint of the open one; None where it has to be built or could
    /// not be brought current (logged). A connection left inside a
    /// transaction no call owns is rolled back first (`_clear_residue`).
    pub fn digest_mark(&self) -> Result<Option<String>, DocFreqError> {
        let conn = self.conn;
        let apply = || -> Result<Option<String>, DigestError> {
            match digest::built(conn, digest::state(conn)?.as_deref())? {
                Some(seen) => {
                    digest::ingest(conn, seen)?;
                    digest_query::mark(conn)
                }
                None => Ok(None),
            }
        };
        let attempt = || -> Result<Option<String>, DigestError> {
            if let Some(mark) = digest_query::mark(conn)? {
                return Ok(Some(mark));
            }
            if digest::built(conn, digest::state(conn)?.as_deref())?.is_none() {
                return Ok(None);
            }
            if self.depth {
                return self.joined(apply);
            }
            if !conn.is_autocommit() {
                if reads::note_residue() {
                    let on = format!("the connection to store {}", self.path_text);
                    let text = reads::residue_message(&on, "it was rolled back");
                    self.warn(WarningKind::Residue, "", None, text);
                }
                conn.execute_batch("ROLLBACK")?;
            }
            conn.execute_batch("BEGIN IMMEDIATE")?;
            let out = require_current_writer(conn, &self.path)
                .map_err(|e| DigestError::Writer(e.error))
                .and_then(|()| apply())
                .and_then(|v| conn.execute_batch("COMMIT").map(|()| v).map_err(Into::into));
            if out.is_err() && !conn.is_autocommit() {
                let _ = conn.execute_batch("ROLLBACK");
            }
            out
        };
        let ((class, code), text) = match attempt() {
            Ok(mark) => return Ok(mark),
            Err(DigestError::Overflow) => return Err(DocFreqError::Py(PyError::OverflowError)),
            // sqlite3.Row's IndexError, which `digest_mark` does not catch.
            Err(DigestError::Unreadable("a record column")) => return Err(source("IndexError")),
            Err(e) => {
                let named = match &e {
                    DigestError::Sqlite(s) | DigestError::Writer(WriteError::Sqlite(s)) => {
                        sqlite_class(s)
                    }
                    DigestError::Writer(WriteError::Version(_)) => ("StoreVersionError", None),
                    DigestError::Writer(_) => ("OperationalError", None),
                    DigestError::Unreadable("a memory" | "a tombstone") => {
                        (record_class(conn).unwrap_or("ValidationError"), None)
                    }
                    DigestError::Unreadable(what @ ("a payload" | "a payload's id")) => {
                        (payload_class(conn).unwrap_or(unreadable_class(what)), None)
                    }
                    DigestError::Unreadable(what) => (unreadable_class(what), None),
                    DigestError::Overflow => ("OverflowError", None),
                };
                (named, e.to_string())
            }
        };
        self.warn(WarningKind::NotCurrent, class, code, text);
        Ok(None)
    }

    /// `_joined`: `apply` under the savepoint `nested_1` inside the open
    /// transaction (`_require_open` first), released on success, rolled
    /// back to on failure; a rollback to it that fails is logged and the
    /// whole transaction rolled back.
    fn joined(
        &self,
        apply: impl FnOnce() -> Result<Option<String>, DigestError>,
    ) -> Result<Option<String>, DigestError> {
        let conn = self.conn;
        if conn.is_autocommit() {
            let path = self.path_text.clone();
            return Err(DigestError::Writer(WriteError::RolledBack(path)));
        }
        conn.execute_batch("SAVEPOINT nested_1")?;
        let out = apply().and_then(|v| {
            conn.execute_batch("RELEASE nested_1")
                .map(|()| v)
                .map_err(Into::into)
        });
        if out.is_err() && !conn.is_autocommit() {
            if let Err(e) = conn.execute_batch("ROLLBACK TO nested_1; RELEASE nested_1") {
                let (class, code) = sqlite_class(&e);
                self.warn(WarningKind::NotTakenBack, class, code, e.to_string());
                let _ = conn.execute_batch("ROLLBACK");
            }
        }
        out
    }

    /// `digest_admission_rows`: the digest's active rows as the reference
    /// hands them on (the home, client and model columns as they are), or
    /// None where it did not answer.
    pub fn digest_admission_rows(&self) -> Result<Option<Vec<DigestRow>>, DocFreqError> {
        if self.digest_mark()?.is_none() {
            return Ok(None);
        }
        let mut statement = match self.conn.prepare_cached(DIGEST_ROWS) {
            Ok(s) => s,
            Err(e) => return Ok(self.not_read(&e)),
        };
        let mut rows = match statement.query([]) {
            Ok(r) => r,
            Err(e) => return Ok(self.not_read(&e)),
        };
        // sqlite3 converts a row's columns, steps to the next row, then
        // hands the row on: a step that fails is caught before the row
        // ahead of it is converted with `int()`, which is not caught.
        let (mut out, mut ahead) = (Vec::new(), None);
        loop {
            let row = match rows.next() {
                Ok(row) => row,
                Err(e) => return Ok(self.not_read(&e)),
            };
            if let Some(c) = ahead.take() {
                out.push(digest_row(c, None)?);
            }
            let Some(row) = row else {
                break;
            };
            match cells(row) {
                Ok(c) => ahead = Some(c),
                Err(StoreError::Sqlite(e)) => return Ok(self.not_read(&e)),
                Err(_) => {
                    let text = "Could not decode to UTF-8";
                    self.warn(WarningKind::NotRead, "OperationalError", None, text.into());
                    return Ok(None);
                }
            }
        }
        Ok(Some(out))
    }

    fn not_read<T>(&self, e: &rusqlite::Error) -> Option<T> {
        let (class, code) = sqlite_class(e);
        self.warn(WarningKind::NotRead, class, code, e.to_string());
        None
    }
}

/// One row of `digest_admission_rows`: the row number, the scopes as
/// `str()` spells them, the home project, client and model columns as
/// they are, and the BM25 length, each number as `int()` reads it (past
/// 64 bits where the column holds one).
pub type DigestRow = (PyInt, String, SqlValue, SqlValue, SqlValue, PyInt);

fn cells(row: &rusqlite::Row<'_>) -> Result<Vec<SqlValue>, StoreError> {
    (0..6)
        .map(|i| {
            row.get_ref(i)
                .map_err(StoreError::Sqlite)
                .and_then(sql_value)
        })
        .collect()
}

/// `(int(rid), str(scopes), home, client, model, int(length))`, the
/// length 0 where `length` names it (`_ADMISSION_INPUTS`' rows).
fn digest_row(c: Vec<SqlValue>, length: Option<PyInt>) -> Result<DigestRow, DocFreqError> {
    let rid = py_int(&c[0])?;
    let length = match length {
        Some(n) => n,
        None => py_int(&c[5])?,
    };
    let [_, scopes, home, client, model, _]: [SqlValue; 6] =
        c.try_into().map_err(|_| source("IndexError"))?;
    Ok((rid, scopes.py_str(), home, client, model, length))
}

/// The grouping's own failures over `rows` in their order, as
/// `_read_doc_freq_inputs` meets them: a scopes text that is not JSON,
/// then, for a row whose scopes are a list, an actor pydantic refuses
/// (ValidationError) and a length past 64 bits, which `array('q')`
/// refuses (OverflowError).
fn grouped(rows: &[DigestRow]) -> Result<(), DocFreqError> {
    for (_, text, _, client, model, length) in rows {
        if scopes(text)?.is_none() {
            continue;
        }
        if actor_of(client).is_none() || actor_of(model).is_none() {
            return Err(source("ValidationError"));
        }
        if length.small().is_none() {
            return Err(DocFreqError::Py(PyError::OverflowError));
        }
    }
    Ok(())
}

/// One row as an `AdmissionRow`: the home as `home_key` spells it, an
/// actor column as pydantic reads it, a length past 64 bits (on a row the
/// grouping skips: `grouped` raised for any other) as 0.
fn admission_row((rid, scopes_text, home, client, model, length): DigestRow) -> AdmissionRow {
    AdmissionRow {
        rid,
        scopes_text,
        home_project: home_key(&home),
        client: actor_of(&client).flatten(),
        model: actor_of(&model).flatten(),
        length: length.small().unwrap_or(0),
    }
}

/// The rows as `AdmissionRow`s, the grouping's failures raised first.
fn typed(rows: Vec<DigestRow>) -> Result<Vec<AdmissionRow>, DocFreqError> {
    grouped(&rows)?;
    Ok(rows.into_iter().map(admission_row).collect())
}

impl DocFreqSource for StoreDocFreq<'_> {
    fn memory_head(&self) -> Result<Head, DocFreqError> {
        let mut statement = self
            .conn
            .prepare_cached(MEMORY_HEAD)
            .map_err(|e| source(sqlite_class(&e).0))?;
        let mut rows = statement
            .query([])
            .map_err(|e| source(sqlite_class(&e).0))?;
        let Some(row) = rows.next().map_err(|e| source(sqlite_class(&e).0))? else {
            return Ok((0, String::new()));
        };
        let seq = row.get_ref(0).map_err(|e| source(sqlite_class(&e).0))?;
        let mac = row.get_ref(1).map_err(|e| source(sqlite_class(&e).0))?;
        let mac = value(mac)?;
        match seq {
            ValueRef::Integer(i) => Ok((i, mac.py_str())),
            _ => Err(DocFreqError::Py(PyError::TypeError)),
        }
    }

    fn store_key(&self) -> (String, String) {
        (self.path_text.clone(), self.store_id.clone())
    }

    fn in_transaction(&self) -> bool {
        self.depth || !self.conn.is_autocommit()
    }

    fn admission_rows(&self) -> Result<(Vec<AdmissionRow>, bool), DocFreqError> {
        if let Some(rows) = self.digest_admission_rows()? {
            return Ok((typed(rows)?, true));
        }
        let sqlite = |e: rusqlite::Error| source(sqlite_class(&e).0);
        let mut statement = self.conn.prepare_cached(ADMISSION_INPUTS).map_err(sqlite)?;
        let mut rows = statement.query([]).map_err(sqlite)?;
        // A generator over sqlite3's rows: a row is grouped once the step
        // to the next one succeeded, so a step or a conversion that fails
        // raises after the rows before it were grouped.
        let (mut raw, mut ahead) = (Vec::new(), None);
        loop {
            let row = rows.next();
            if let (Ok(_), Some(c)) = (&row, ahead.take()) {
                raw.push(digest_row(c, Some(PyInt::Small(0)))?);
            }
            let row = match row {
                Ok(Some(row)) => row,
                Ok(None) => break,
                Err(e) => {
                    grouped(&raw)?;
                    return Err(sqlite(e));
                }
            };
            // rid, scopes, home, client, model (the id is not read).
            match cells(row) {
                Ok(mut c) => {
                    c.remove(1);
                    c.push(SqlValue::Integer(0));
                    ahead = Some(c);
                }
                Err(e) => {
                    grouped(&raw)?;
                    return Err(from_store(e));
                }
            }
        }
        Ok((typed(raw)?, false))
    }

    fn advanced_inputs(
        &self,
        entry: &super::docfreq::DocFreqInputs,
        head: &Head,
    ) -> Result<Option<super::docfreq::DocFreqInputs>, DocFreqError> {
        advance::advanced_inputs(self, entry, head)
    }

    fn term_masks(
        &self,
        term: &str,
        inputs: &super::docfreq::DocFreqInputs,
    ) -> Result<(Mask, Mask), DocFreqError> {
        // `_read_doc_freq_inputs` gives each row read the index's length
        // before it, so a row number read twice holds a position equal to
        // the index's length: the read's masks span every position held,
        // and `row_mask` then answers over the index's width as `_row_mask`
        // does (IndexError past its bytes).
        let width = inputs.index.len();
        let span = inputs.index.values().max().map_or(0, |p| p + 1);
        let position = |rid: i64| inputs.index.get(&PyInt::Small(rid)).copied();
        let (body, either) =
            reads::term_masks_by(self.conn, term, &position, span.max(width)).map_err(from_read)?;
        let mask = |bits: &[bool]| {
            let positions: Vec<usize> = (0..bits.len()).filter(|&p| bits[p]).collect();
            row_mask(&positions, width)
        };
        Ok((mask(&body)?, mask(&either)?))
    }

    fn fts_rowids(&self, expr: &str, low: i64, limit: usize) -> Result<Vec<i64>, DocFreqError> {
        let sqlite = |e: rusqlite::Error| source(sqlite_class(&e).0);
        let limit = i64::try_from(limit).unwrap_or(i64::MAX);
        let mut statement = self.conn.prepare_cached(FTS_ROWIDS).map_err(sqlite)?;
        let mut rows = statement
            .query(rusqlite::params![expr, low, limit])
            .map_err(sqlite)?;
        let mut out = Vec::new();
        while let Some(row) = rows.next().map_err(sqlite)? {
            let rid = py_int(&value(row.get_ref(0).map_err(sqlite)?)?)?;
            out.push(
                rid.small()
                    .ok_or(DocFreqError::Py(PyError::OverflowError))?,
            );
        }
        Ok(out)
    }
}

fn unique_terms(terms: &[String]) -> Vec<String> {
    let mut unique: Vec<String> = terms.iter().filter(|t| !t.is_empty()).cloned().collect();
    unique.sort();
    unique.dedup();
    unique
}

/// The uncached read with the one admit shape: the home column and the
/// actor as the memo's path gives them.
fn uncached(
    src: &StoreDocFreq<'_>,
    terms: &[String],
    admit: &mut StoreAdmit<'_>,
) -> Result<Option<DocFreqs>, DocFreqError> {
    let mut adapter = |scopes: &[PyValue], home: &SqlValue, actor: Option<&Actor>| -> bool {
        let actor = actor.map(|a| DocActor {
            client: a.client.as_deref().and_then(unicode::to_string),
            model: a.model.as_deref().and_then(unicode::to_string),
        });
        let spelled = home.py_str();
        let home = match home {
            SqlValue::Null => None,
            SqlValue::Blob(b) => Some(Home::Bytes(b)),
            SqlValue::Text(t) => Some(Home::Text(t)),
            _ => Some(Home::Text(&spelled)),
        };
        admit(scopes, home, actor.as_ref())
    };
    reads::document_frequencies(src.conn, terms, &mut adapter).map_err(from_read)
}

/// `Store.document_frequencies(terms, admit=admit, memoised=memoised)`:
/// through `memo` outside a transaction when `memoised`, else the
/// uncached read. None for no term or no admitted row.
pub fn document_frequencies(
    src: &StoreDocFreq<'_>,
    memo: &DocFreqMemo,
    terms: &[String],
    admit: &mut StoreAdmit<'_>,
    memoised: bool,
) -> Result<Option<DocFreqs>, DocFreqError> {
    let unique = unique_terms(terms);
    if unique.is_empty() {
        return Ok(None);
    }
    if memoised && !src.in_transaction() {
        let mut keyed =
            |s: &[PyValue], h: Option<&str>, a: Option<&DocActor>| with_home(h, |h| admit(s, h, a));
        let found = memo
            .document_frequencies(src, &unique, &mut keyed)
            .map_err(json_class)?;
        return Ok(found.map(|(size, body, scope, _)| (size, body, scope)));
    }
    uncached(src, &unique, admit)
}

/// `Store.ranking_statistics(terms, admit=admit)`: the memo's four items
/// outside a transaction; inside one, the uncached read and no average.
pub fn ranking_statistics(
    src: &StoreDocFreq<'_>,
    memo: &DocFreqMemo,
    terms: &[String],
    admit: &mut StoreAdmit<'_>,
) -> Result<Option<Statistics>, DocFreqError> {
    let unique = unique_terms(terms);
    if unique.is_empty() {
        return Ok(None);
    }
    let found = if src.in_transaction() {
        Ranked::Uncached
    } else {
        let mut keyed =
            |s: &[PyValue], h: Option<&str>, a: Option<&DocActor>| with_home(h, |h| admit(s, h, a));
        memo.ranking_statistics(src, &unique, &mut keyed)
            .map_err(json_class)?
    };
    Ok(match found {
        Ranked::Found(statistics) => Some(statistics),
        Ranked::Nothing => None,
        Ranked::Uncached => {
            uncached(src, &unique, admit)?.map(|(size, body, scope)| (size, body, scope, None))
        }
    })
}
