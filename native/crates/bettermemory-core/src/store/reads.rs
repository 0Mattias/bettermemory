//! The connection leaves of `store.py` and its admitted reads that take
//! the caller's admission as a predicate.
//!
//! The leaves: `note_residue` (once per process), `error_code_name` with
//! the SQLite result-code names CPython 3.13 carries, `retryable_code`,
//! and `Store.set_busy_timeout`. The reads: `Store.document_frequencies`
//! without the memo, `Store._term_masks`, `Store.scope_counts`,
//! `Store.scopes_for`, `Store.category_rows` and `Store.brief_rows`, each
//! over a connection with the admission a closure. `StoreSource` and
//! `DefaultStoreSource` are the per-request store seam.
//!
//! A scope list is read with `store_rows._load_json` ([`load_json`]), so
//! its items are any JSON value, as the reference hands them to `admit`.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};

use rusqlite::types::ValueRef;
use rusqlite::Connection;

use super::{sql_value, SqlValue, StoreError};
use crate::identity::Actor;
use crate::pyvalue::{self, PyError, PyStr, PyValue};
use crate::records::rows::load_json;
use crate::records::RecordError;
use crate::{float_repr, unicode};

/// `STORE_BUSY`.
pub const STORE_BUSY: &str = "store_busy";
/// `STORE_FULL`.
pub const STORE_FULL: &str = "store_full";

/// What a read raises: SQLite's error, or another exception by class.
#[derive(Debug)]
pub enum ReadError {
    Sqlite(rusqlite::Error),
    /// An exception by class name (a JSON decode error, a TypeError of an
    /// unhashable scope, an Actor's ValidationError, sqlite3's UTF-8
    /// decode failure).
    Raised(&'static str),
    /// `set_busy_timeout`'s ValueError or OverflowError, with its text.
    Worded(&'static str, String),
}

impl ReadError {
    /// The exception's class name.
    pub fn class(&self) -> &'static str {
        match self {
            ReadError::Sqlite(e) => failure_of(e).map_or("Error", |(class, _)| class),
            ReadError::Raised(class) | ReadError::Worded(class, _) => class,
        }
    }
}

impl From<rusqlite::Error> for ReadError {
    fn from(e: rusqlite::Error) -> Self {
        ReadError::Sqlite(e)
    }
}

impl From<RecordError> for ReadError {
    fn from(e: RecordError) -> Self {
        ReadError::Raised(e.class())
    }
}

impl From<StoreError> for ReadError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::Sqlite(e) => ReadError::Sqlite(e),
            // sqlite3 raises OperationalError for text it cannot decode.
            StoreError::NotUtf8 { .. } => ReadError::Raised("OperationalError"),
            _ => ReadError::Raised("OSError"),
        }
    }
}

static RESIDUE_LOGGED: AtomicBool = AtomicBool::new(false);

/// `note_residue`'s once-per-process latch: true the first time it is
/// asked in this process (the call that logs), false after.
pub fn note_residue() -> bool {
    !RESIDUE_LOGGED.swap(true, Ordering::SeqCst)
}

/// The warning `note_residue(connection, then)` logs.
pub fn residue_message(connection: &str, then: &str) -> String {
    format!("{connection} was left inside a transaction; {then}")
}

/// The result-code names of CPython 3.13's `_sqlite` module
/// (`error_codes`), as `sqlite_errorname` reads them.
const ERROR_NAMES: &[(i32, &str)] = &[
    (0, "SQLITE_OK"),
    (1, "SQLITE_ERROR"),
    (2, "SQLITE_INTERNAL"),
    (3, "SQLITE_PERM"),
    (4, "SQLITE_ABORT"),
    (5, "SQLITE_BUSY"),
    (6, "SQLITE_LOCKED"),
    (7, "SQLITE_NOMEM"),
    (8, "SQLITE_READONLY"),
    (9, "SQLITE_INTERRUPT"),
    (10, "SQLITE_IOERR"),
    (11, "SQLITE_CORRUPT"),
    (12, "SQLITE_NOTFOUND"),
    (13, "SQLITE_FULL"),
    (14, "SQLITE_CANTOPEN"),
    (15, "SQLITE_PROTOCOL"),
    (16, "SQLITE_EMPTY"),
    (17, "SQLITE_SCHEMA"),
    (18, "SQLITE_TOOBIG"),
    (19, "SQLITE_CONSTRAINT"),
    (20, "SQLITE_MISMATCH"),
    (21, "SQLITE_MISUSE"),
    (22, "SQLITE_NOLFS"),
    (23, "SQLITE_AUTH"),
    (24, "SQLITE_FORMAT"),
    (25, "SQLITE_RANGE"),
    (26, "SQLITE_NOTADB"),
    (27, "SQLITE_NOTICE"),
    (28, "SQLITE_WARNING"),
    (100, "SQLITE_ROW"),
    (101, "SQLITE_DONE"),
    (256, "SQLITE_OK_LOAD_PERMANENTLY"),
    (257, "SQLITE_ERROR_MISSING_COLLSEQ"),
    (261, "SQLITE_BUSY_RECOVERY"),
    (262, "SQLITE_LOCKED_SHAREDCACHE"),
    (264, "SQLITE_READONLY_RECOVERY"),
    (266, "SQLITE_IOERR_READ"),
    (267, "SQLITE_CORRUPT_VTAB"),
    (270, "SQLITE_CANTOPEN_NOTEMPDIR"),
    (275, "SQLITE_CONSTRAINT_CHECK"),
    (279, "SQLITE_AUTH_USER"),
    (283, "SQLITE_NOTICE_RECOVER_WAL"),
    (284, "SQLITE_WARNING_AUTOINDEX"),
    (512, "SQLITE_OK_SYMLINK"),
    (513, "SQLITE_ERROR_RETRY"),
    (516, "SQLITE_ABORT_ROLLBACK"),
    (517, "SQLITE_BUSY_SNAPSHOT"),
    (518, "SQLITE_LOCKED_VTAB"),
    (520, "SQLITE_READONLY_CANTLOCK"),
    (522, "SQLITE_IOERR_SHORT_READ"),
    (523, "SQLITE_CORRUPT_SEQUENCE"),
    (526, "SQLITE_CANTOPEN_ISDIR"),
    (531, "SQLITE_CONSTRAINT_COMMITHOOK"),
    (539, "SQLITE_NOTICE_RECOVER_ROLLBACK"),
    (769, "SQLITE_ERROR_SNAPSHOT"),
    (773, "SQLITE_BUSY_TIMEOUT"),
    (776, "SQLITE_READONLY_ROLLBACK"),
    (778, "SQLITE_IOERR_WRITE"),
    (779, "SQLITE_CORRUPT_INDEX"),
    (782, "SQLITE_CANTOPEN_FULLPATH"),
    (787, "SQLITE_CONSTRAINT_FOREIGNKEY"),
    (1032, "SQLITE_READONLY_DBMOVED"),
    (1034, "SQLITE_IOERR_FSYNC"),
    (1038, "SQLITE_CANTOPEN_CONVPATH"),
    (1043, "SQLITE_CONSTRAINT_FUNCTION"),
    (1288, "SQLITE_READONLY_CANTINIT"),
    (1290, "SQLITE_IOERR_DIR_FSYNC"),
    (1294, "SQLITE_CANTOPEN_DIRTYWAL"),
    (1299, "SQLITE_CONSTRAINT_NOTNULL"),
    (1544, "SQLITE_READONLY_DIRECTORY"),
    (1546, "SQLITE_IOERR_TRUNCATE"),
    (1550, "SQLITE_CANTOPEN_SYMLINK"),
    (1555, "SQLITE_CONSTRAINT_PRIMARYKEY"),
    (1802, "SQLITE_IOERR_FSTAT"),
    (1811, "SQLITE_CONSTRAINT_TRIGGER"),
    (2058, "SQLITE_IOERR_UNLOCK"),
    (2067, "SQLITE_CONSTRAINT_UNIQUE"),
    (2314, "SQLITE_IOERR_RDLOCK"),
    (2323, "SQLITE_CONSTRAINT_VTAB"),
    (2570, "SQLITE_IOERR_DELETE"),
    (2579, "SQLITE_CONSTRAINT_ROWID"),
    (2826, "SQLITE_IOERR_BLOCKED"),
    (2835, "SQLITE_CONSTRAINT_PINNED"),
    (3082, "SQLITE_IOERR_NOMEM"),
    (3338, "SQLITE_IOERR_ACCESS"),
    (3594, "SQLITE_IOERR_CHECKRESERVEDLOCK"),
    (3850, "SQLITE_IOERR_LOCK"),
    (4106, "SQLITE_IOERR_CLOSE"),
    (4362, "SQLITE_IOERR_DIR_CLOSE"),
    (4618, "SQLITE_IOERR_SHMOPEN"),
    (4874, "SQLITE_IOERR_SHMSIZE"),
    (5130, "SQLITE_IOERR_SHMLOCK"),
    (5386, "SQLITE_IOERR_SHMMAP"),
    (5642, "SQLITE_IOERR_SEEK"),
    (5898, "SQLITE_IOERR_DELETE_NOENT"),
    (6154, "SQLITE_IOERR_MMAP"),
    (6410, "SQLITE_IOERR_GETTEMPPATH"),
    (6666, "SQLITE_IOERR_CONVPATH"),
    (6922, "SQLITE_IOERR_VNODE"),
    (7178, "SQLITE_IOERR_AUTH"),
    (7434, "SQLITE_IOERR_BEGIN_ATOMIC"),
    (7690, "SQLITE_IOERR_COMMIT_ATOMIC"),
    (7946, "SQLITE_IOERR_ROLLBACK_ATOMIC"),
    (8202, "SQLITE_IOERR_DATA"),
    (8458, "SQLITE_IOERR_CORRUPTFS"),
];

/// The name CPython gives SQLite's result code `code`
/// (`sqlite_errorname`); None for a code its table does not hold.
pub fn sqlite_error_name(code: i64) -> Option<&'static str> {
    ERROR_NAMES
        .iter()
        .find(|(c, _)| i64::from(*c) == code)
        .map(|(_, name)| *name)
}

/// The class sqlite3 raises for a SQLite failure and the extended code it
/// carries (`_pysqlite_seterror`): None for an error that is not SQLite's
/// own result code. NOMEM is a MemoryError, which carries no code.
pub fn failure_of(e: &rusqlite::Error) -> Option<(&'static str, Option<i64>)> {
    let f = match e {
        rusqlite::Error::SqliteFailure(f, _) | rusqlite::Error::SqlInputError { error: f, .. } => f,
        _ => return None,
    };
    let class = match f.extended_code & 0xFF {
        7 => return Some(("MemoryError", None)),
        2 | 12 => "InternalError",
        1 | 3..=6 | 8..=10 | 13..=17 => "OperationalError",
        18 => "DataError",
        19 | 20 => "IntegrityError",
        21 | 25 => "InterfaceError",
        _ => "DatabaseError",
    };
    Some((class, Some(i64::from(f.extended_code))))
}

/// `error_code_name(exc)` of an exception of class `class` carrying
/// `code` as its `sqlite_errorcode` (None when it carries none).
pub fn error_code_name(class: &str, code: Option<i64>) -> String {
    code.and_then(sqlite_error_name).unwrap_or(class).to_owned()
}

/// `retryable_code(exc)`: `STORE_BUSY` for an OperationalError whose code
/// reads as SQLITE_BUSY or SQLITE_LOCKED, `STORE_FULL` for SQLITE_FULL.
pub fn retryable_code(class: &str, code: Option<i64>) -> Option<&'static str> {
    if class != "OperationalError" {
        return None;
    }
    match code? & 0xFF {
        5 | 6 => Some(STORE_BUSY),
        13 => Some(STORE_FULL),
        _ => None,
    }
}

/// `Store.set_busy_timeout(seconds)` on `conn`: ValueError for a value
/// that is not finite or is negative (worded with the float's repr),
/// OverflowError where `seconds * 1000` is infinite; else `PRAGMA
/// busy_timeout = int(seconds * 1000)` as SQLite 3.50.4 reads it: 0 for
/// a value past 32 bits.
pub fn set_busy_timeout(conn: &Connection, seconds: f64) -> Result<(), ReadError> {
    if !seconds.is_finite() || seconds < 0.0 {
        return Err(ReadError::Worded(
            "ValueError",
            format!(
                "a busy timeout is a finite number of seconds, got {}",
                float_repr::repr(seconds)
            ),
        ));
    }
    let millis = match &pyvalue::py_int(&PyValue::Float(seconds * 1000.0)) {
        Ok(PyValue::Int(text)) => text.clone(),
        _ => {
            return Err(ReadError::Worded(
                "OverflowError",
                "cannot convert float infinity to integer".to_owned(),
            ))
        }
    };
    // SQLite 3.50.4, the reference's, reads a value past 32 bits as 0
    // (`sqlite3Atoi`); a later SQLite leaves the timeout as it was.
    let millis: i32 = millis.parse().unwrap_or(0);
    conn.execute_batch(&format!("PRAGMA busy_timeout = {millis}"))?;
    Ok(())
}

fn value(v: ValueRef<'_>) -> Result<SqlValue, ReadError> {
    Ok(sql_value(v)?)
}

/// Every column of `row`, decoded before any is used: sqlite3 converts
/// the whole row when it fetches it, so a text that is not UTF-8 in any
/// selected column raises OperationalError whatever the code does with
/// that column, and before anything the code does with the others.
fn columns(row: &rusqlite::Row<'_>, count: usize) -> Result<Vec<SqlValue>, ReadError> {
    (0..count).map(|i| value(row.get_ref(i)?)).collect()
}

/// A scope list as `_load_json(text, None)` gives it: None when it is not
/// a list.
fn scopes_of(v: &SqlValue) -> Result<Option<Vec<PyValue>>, ReadError> {
    Ok(match &mut load_json(v, PyValue::None)? {
        PyValue::List(items) => Some(std::mem::take(items)),
        _ => None,
    })
}

/// A column pydantic's `str | None` field reads: None, a text, or a
/// ValidationError.
fn actor_field(v: &SqlValue) -> Result<Option<PyStr>, ReadError> {
    match v {
        SqlValue::Null => Ok(None),
        SqlValue::Text(t) => Ok(Some(unicode::cps(t))),
        SqlValue::Blob(b) => std::str::from_utf8(b)
            .map(|t| Some(unicode::cps(t)))
            .map_err(|_| ReadError::Raised("ValidationError")),
        _ => Err(ReadError::Raised("ValidationError")),
    }
}

/// `admit(scopes, home project, actor)` of `document_frequencies`.
pub type AdmitWithActor<'a> = dyn FnMut(&[PyValue], &SqlValue, Option<&Actor>) -> bool + 'a;

/// `(size, body_df, scope_df)`; the maps by term.
pub type DocFreqs = (usize, Vec<(String, usize)>, Vec<(String, usize)>);

/// `Store.document_frequencies(terms, admit=admit)` outside the memo:
/// `admit(scopes, home project, actor)` over every memory row whose scopes
/// are a list, then each unique non-empty term's hits among the admitted,
/// in the body column and in either. None when no term or no admitted row.
pub fn document_frequencies(
    conn: &Connection,
    terms: &[String],
    admit: &mut AdmitWithActor<'_>,
) -> Result<Option<DocFreqs>, ReadError> {
    let unique: BTreeSet<&str> = terms
        .iter()
        .map(String::as_str)
        .filter(|t| !t.is_empty())
        .collect();
    if unique.is_empty() {
        return Ok(None);
    }
    let mut admitted: HashSet<String> = HashSet::new();
    // Both texts through the statement cache, as the reference's
    // `self._conn.execute` sends them.
    let mut statement = conn.prepare_cached(
        "SELECT m.rowid AS rid, m.id AS id, m.scopes_json AS scopes_json, \
         h.project AS home_project, \
         m.actor_client AS actor_client, m.actor_model AS actor_model \
         FROM memories m LEFT JOIN homes h ON h.record_id = m.id",
    )?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let cols = columns(row, 6)?;
        let (client, model) = (&cols[4], &cols[5]);
        let actor = if *client != SqlValue::Null || *model != SqlValue::Null {
            Some(Actor {
                client: actor_field(client)?,
                model: actor_field(model)?,
                ..Actor::default()
            })
        } else {
            None
        };
        let Some(scopes) = scopes_of(&cols[2])? else {
            continue;
        };
        if admit(&scopes, &cols[3], actor.as_ref()) {
            admitted.insert(cols[1].py_str());
        }
    }
    if admitted.is_empty() {
        return Ok(None);
    }
    let mut matching = conn.prepare_cached(
        "SELECT m.id AS id FROM memories m \
         JOIN memories_fts f ON f.rowid = m.rowid \
         WHERE memories_fts MATCH ?",
    )?;
    let mut ids_matching = |expr: &str| -> Result<usize, ReadError> {
        let mut seen = HashSet::new();
        let mut rows = matching.query([expr])?;
        while let Some(row) = rows.next()? {
            let id = value(row.get_ref(0)?)?.py_str();
            if admitted.contains(&id) {
                seen.insert(id);
            }
        }
        Ok(seen.len())
    };
    let mut body_df = Vec::new();
    let mut scope_df = Vec::new();
    for term in unique {
        let quoted = format!("\"{}\"", term.replace('"', "\"\""));
        let body = ids_matching(&format!("body_fts : {quoted}"))?;
        let any = ids_matching(&quoted)?;
        if body > 0 {
            body_df.push((term.to_owned(), body));
        }
        if any > 0 {
            scope_df.push((term.to_owned(), any));
        }
    }
    Ok(Some((admitted.len(), body_df, scope_df)))
}

/// `Store._term_masks(term, inputs)`: the positions (by `index`, row
/// number to position, over `width` rows) whose body matches `term`, and
/// whose body or scopes do, each as a mask.
pub fn term_masks(
    conn: &Connection,
    term: &str,
    index: &HashMap<i64, usize>,
    width: usize,
) -> Result<(Vec<bool>, Vec<bool>), ReadError> {
    term_masks_by(conn, term, &|rid| index.get(&rid).copied(), width)
}

/// [`term_masks`] with the position of a row number given by `index`.
pub fn term_masks_by(
    conn: &Connection,
    term: &str,
    index: &dyn Fn(i64) -> Option<usize>,
    width: usize,
) -> Result<(Vec<bool>, Vec<bool>), ReadError> {
    // Through the statement cache, as `Store._term_masks` sends it.
    let mut statement =
        conn.prepare_cached("SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?")?;
    let mut rows_matching = |expr: &str| -> Result<Vec<bool>, ReadError> {
        let mut mask = vec![false; width];
        let mut rows = statement.query([expr])?;
        while let Some(row) = rows.next()? {
            let rid = match value(row.get_ref(0)?)? {
                SqlValue::Integer(i) => i,
                _ => return Err(ReadError::Raised("TypeError")),
            };
            if let Some(p) = index(rid) {
                mask[p] = true;
            }
        }
        Ok(mask)
    };
    let quoted = format!("\"{}\"", term.replace('"', "\"\""));
    let in_body = rows_matching(&format!("body_fts : {quoted}"))?;
    let scoped = rows_matching(&format!("scopes_fts : {quoted}"))?;
    let either = in_body.iter().zip(&scoped).map(|(a, b)| *a || *b).collect();
    Ok((in_body, either))
}

/// A JSON value as a dict key: equal under Python's `==` exactly when
/// the keys are equal (numbers by value, json's one NaN object its own
/// key, a text only with a text); None for a list or an object, which are
/// unhashable.
fn dict_key(v: &PyValue) -> Option<(u8, String)> {
    Some(match v {
        PyValue::None => (0, String::new()),
        PyValue::Str(s) => (1, s.iter().map(|c| format!("{c:x},")).collect()),
        PyValue::Float(f) if f.is_nan() => (3, String::new()),
        PyValue::Float(f) if !(f.is_finite() && f.fract() == 0.0) => (4, f.to_bits().to_string()),
        PyValue::Bool(_) | PyValue::Int(_) | PyValue::Float(_) => match &pyvalue::py_int(v) {
            Ok(PyValue::Int(text)) => (2, text.clone()),
            _ => return None,
        },
        _ => return None,
    })
}

/// `Store.scope_counts(admit=admit)`: how many rows `admit(scopes, home)`
/// admits and how many of them carry each scope, the scopes in first-seen
/// order (keys Python's dict holds equal are one). TypeError for an
/// admitted row holding a list or an object.
pub fn scope_counts(
    conn: &Connection,
    admit: &mut dyn FnMut(&[PyValue], &SqlValue) -> bool,
) -> Result<(usize, Vec<(PyValue, usize)>), ReadError> {
    let mut statement = conn.prepare_cached(
        "SELECT m.scopes_json AS scopes_json, h.project AS home_project \
         FROM memories m LEFT JOIN homes h ON h.record_id = m.id",
    )?;
    let mut rows = statement.query([])?;
    let mut total = 0;
    let mut counts: Vec<(PyValue, usize)> = Vec::new();
    let mut at: HashMap<(u8, String), usize> = HashMap::new();
    while let Some(row) = rows.next()? {
        let cols = columns(row, 2)?;
        let Some(scopes) = scopes_of(&cols[0])? else {
            continue;
        };
        if !admit(&scopes, &cols[1]) {
            continue;
        }
        total += 1;
        for scope in scopes {
            let key = dict_key(&scope).ok_or(ReadError::Raised("TypeError"))?;
            match at.get(&key) {
                Some(&i) => counts[i].1 += 1,
                None => {
                    at.insert(key, counts.len());
                    counts.push((scope, 1));
                }
            }
        }
    }
    Ok((total, counts))
}

/// `Store.scopes_for(ids)`: each active record's scopes among `ids` (each
/// `str()`, a scope list that is not a list read as none), in the order
/// the queries give them, a later row of an id replacing its value.
pub fn scopes_for(
    conn: &Connection,
    ids: &[String],
) -> Result<Vec<(String, Vec<PyStr>)>, ReadError> {
    let mut wanted: Vec<&str> = Vec::new();
    let mut seen = HashSet::new();
    for id in ids {
        if seen.insert(id.as_str()) {
            wanted.push(id);
        }
    }
    let mut out: Vec<(String, Vec<PyStr>)> = Vec::new();
    let mut at: HashMap<String, usize> = HashMap::new();
    for chunk in wanted.chunks(500) {
        let marks = vec!["?"; chunk.len()].join(", ");
        let mut statement = conn.prepare(&format!(
            "SELECT id, scopes_json FROM memories WHERE id IN ({marks})"
        ))?;
        let mut rows = statement.query(rusqlite::params_from_iter(chunk.iter()))?;
        while let Some(row) = rows.next()? {
            let cols = columns(row, 2)?;
            let scopes = match scopes_of(&cols[1])? {
                Some(items) => items
                    .iter()
                    .map(pyvalue::py_str)
                    .collect::<Result<Vec<_>, PyError>>()
                    .map_err(|e| ReadError::Raised(e.name()))?,
                None => Vec::new(),
            };
            let id = cols[0].py_str();
            match at.get(&id) {
                Some(&i) => out[i].1 = scopes,
                None => {
                    at.insert(id.clone(), out.len());
                    out.push((id, scopes));
                }
            }
        }
    }
    Ok(out)
}

/// The ids of the rows `sql` gives (id, scopes, home) that `admit` takes;
/// a scope list that is not a list is skipped, or read as `[]` with
/// `empty_for_other`.
fn admitted_ids(
    conn: &Connection,
    sql: &str,
    params: &[&str],
    empty_for_other: bool,
    admit: &mut dyn FnMut(&[PyValue], &SqlValue) -> bool,
) -> Result<Vec<String>, ReadError> {
    let mut statement = conn.prepare(sql)?;
    let mut rows = statement.query(rusqlite::params_from_iter(params.iter()))?;
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        let cols = columns(row, 3)?;
        let scopes = match scopes_of(&cols[1])? {
            Some(items) => items,
            None if empty_for_other => Vec::new(),
            None => continue,
        };
        if admit(&scopes, &cols[2]) {
            out.push(cols[0].py_str());
        }
    }
    Ok(out)
}

/// `Store.category_rows(category=category, admit=admit)`: the admitted
/// ids in `category`, in id order.
pub fn category_rows(
    conn: &Connection,
    category: &str,
    admit: &mut dyn FnMut(&[PyValue], &SqlValue) -> bool,
) -> Result<Vec<String>, ReadError> {
    admitted_ids(
        conn,
        "SELECT m.id AS id, m.scopes_json AS scopes_json, \
         h.project AS home_project \
         FROM memories m LEFT JOIN homes h ON h.record_id = m.id \
         WHERE m.category = ? ORDER BY m.id",
        &[category],
        false,
        admit,
    )
}

/// `Store.brief_rows(admit=admit, provenance=provenance,
/// exclude_category=exclude_category)`: the admitted ids whose stored
/// label is in `provenance`, newest `updated` first then id descending;
/// a scope list that is not a list is handed to `admit` as `[]`.
pub fn brief_rows(
    conn: &Connection,
    provenance: &[String],
    exclude_category: Option<&str>,
    admit: &mut dyn FnMut(&[PyValue], &SqlValue) -> bool,
) -> Result<Vec<String>, ReadError> {
    let labels: BTreeSet<&str> = provenance.iter().map(String::as_str).collect();
    if labels.is_empty() {
        return Ok(Vec::new());
    }
    let marks = vec!["?"; labels.len()].join(",");
    let category = if exclude_category.is_some() {
        "AND (m.category IS NULL OR m.category != ?) "
    } else {
        ""
    };
    let mut params: Vec<&str> = labels.into_iter().collect();
    params.extend(exclude_category);
    let sql = format!(
        "SELECT m.id AS id, m.scopes_json AS scopes_json, \
         h.project AS home_project FROM memories m \
         LEFT JOIN homes h ON h.record_id = m.id \
         WHERE m.provenance IN ({marks}) {category}\
         ORDER BY m.updated DESC, m.id DESC"
    );
    admitted_ids(conn, &sql, &params, true, admit)
}

/// `StoreSource`: where a request's store comes from, asked at every
/// tool call with the request's context `C`.
pub trait StoreSource<C> {
    type Store;
    fn for_request(&self, ctx: Option<&C>) -> &Self::Store;
}

/// `DefaultStoreSource`: every request is served from the one store.
pub struct DefaultStoreSource<S> {
    store: S,
}

impl<S> DefaultStoreSource<S> {
    pub fn new(store: S) -> Self {
        DefaultStoreSource { store }
    }

    /// `principal_of(ctx)`: the principal of the actor `bind` resolves
    /// for `ctx` (`identity.bind(ctx).actor.principal`).
    pub fn principal_of<C>(
        &self,
        ctx: Option<&C>,
        bind: impl FnOnce(Option<&C>) -> Actor,
    ) -> Option<PyStr> {
        bind(ctx).principal
    }
}

impl<S, C> StoreSource<C> for DefaultStoreSource<S> {
    type Store = S;
    fn for_request(&self, _ctx: Option<&C>) -> &S {
        &self.store
    }
}
