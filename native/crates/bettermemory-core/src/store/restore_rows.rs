//! `restore_rows.py` whole (192-725): the `store_restored` row a restore
//! could not write, appended by a later writing open (`append_missing`,
//! `settle`) or by the restore itself (`append_for`), and named in the
//! restore record beside the head (`record_row`, `_fill`).
//!
//! Every function takes the store open for writing ([`WritableStore`])
//! and, where it may append, the caller's [`Provider`] (the log clock for
//! the row and the head, the engine version for its payload). What the
//! reference logs is returned as data ([`Warning`]), pushed onto the
//! caller's list in the order the reference logs it, so a call that
//! raises keeps what it said first. The open's own call of `settle`
//! (`store_open.py` 98, 131, 248) is the writing open's (`store::open`).

use std::path::{Path, PathBuf};

use rusqlite::types::Value as SqlParam;
use rusqlite::Connection;

use super::create::Provider;
use super::open::{UpkeepCause as Cause, WritableStore};
use super::rows::CONTROL_KINDS;
use super::write::{replace_retrying, WriteError};
use super::{sql_value, SqlValue};
use crate::datetime::parse_event_ts;
use crate::fsdata::{Numbered, PyErr};
use crate::fsutil::{bounded_read, flock_excl, BoundedReadError, FlockError};
use crate::history_rows::py_ne;
use crate::json::{self, DumpOptions, LoadError};
use crate::log::replaced::{
    append_history_replaced_in, history_replaced_kind, Appended, ReplacedError, STORE_RESTORED,
};
use crate::pyvalue::{cmp_keys, PyError, PyStr, PyValue};
use crate::store::provenance::decode_json_bytes;
use crate::unicode::cps;

/// `RESTORE_RECORD_SUFFIX`.
pub const RESTORE_RECORD_SUFFIX: &str = ".backup_restore.json";
/// `RECORD_CAP_BYTES`: what a read of the record reads at most.
pub const RECORD_CAP_BYTES: u64 = 1 << 20;
/// `WITHHELD_KEY`.
pub const WITHHELD_KEY: &str = "row_withheld";
pub const BECAUSE_RECORD: &str = "record";
pub const BECAUSE_REPLACED: &str = "replaced";
pub const BECAUSE_ELSEWHERE: &str = "elsewhere";

/// The keys an entry is found by in the record (`_fill`, `append_for`).
const KEYS: [&str; 3] = ["kind", "store_id", "restored_at"];

static NONE: PyValue = PyValue::None;

/// Python's `a == b` over values `json.loads` made: numbers by value
/// (`True == 1 == 1.0`), and a NaN inside a list or dict equal to one
/// there, since every NaN `json.loads` reads is one object and a
/// container compares its items by identity first; a NaN at the top
/// equals nothing (`history_rows`' comparison).
fn py_eq(a: &PyValue, b: &PyValue) -> bool {
    !py_ne(a, b)
}

/// `entry.get(key)`: the value, or None when the key is not there.
fn get<'v>(entry: &'v PyValue, key: &str) -> &'v PyValue {
    entry.get(key).unwrap_or(&NONE)
}

/// What the reference raises out of these functions.
#[derive(Debug)]
pub enum RestoreError {
    /// `sqlite3.Error` of a read.
    Sqlite(rusqlite::Error),
    /// A text column that is not UTF-8 (`sqlite3`'s OperationalError).
    NotUtf8,
    /// The write transaction, the append or its commit failed.
    Write(WriteError),
    /// The record's lock (`flock_excl`) was not taken.
    Lock(FlockError),
    /// RecursionError (a record or a payload nested past `json.loads`'s
    /// limit), OverflowError (a seq past SQLite's INTEGER), TypeError or
    /// ValueError (`append_for`'s round trip of its entry).
    Py(PyError),
}

impl From<rusqlite::Error> for RestoreError {
    fn from(e: rusqlite::Error) -> Self {
        RestoreError::Sqlite(e)
    }
}

impl From<ReplacedError> for RestoreError {
    fn from(e: ReplacedError) -> Self {
        match e {
            ReplacedError::Write(e) => RestoreError::Write(e),
            ReplacedError::NotAKind(_) => RestoreError::Py(PyError::ValueError),
        }
    }
}

fn named(class: &str) -> Cause {
    Cause {
        class: class.to_owned(),
        sqlite: None,
    }
}

fn os_cause(e: &std::io::Error) -> Cause {
    named(Numbered::of_io(e).error.class.name())
}

fn sqlite_cause(e: &rusqlite::Error) -> Option<Cause> {
    let class = super::upgrade::sqlite_class(e);
    let sqlite = match e {
        rusqlite::Error::SqliteFailure(f, _) => {
            super::reads::sqlite_error_name(i64::from(f.extended_code))
        }
        _ => None,
    };
    (class != "MemoryError").then(|| Cause {
        class: class.to_owned(),
        sqlite,
    })
}

/// Whether `settle`'s `except (sqlite3.Error, OSError, RuntimeError,
/// ValueError)` catches an exception of this class.
fn caught(class: &str) -> Option<Cause> {
    const CAUGHT: &str = "Error DatabaseError OperationalError IntegrityError ProgrammingError \
        InterfaceError DataError NotSupportedError InternalError OSError FileNotFoundError \
        FileExistsError PermissionError IsADirectoryError NotADirectoryError BlockingIOError \
        InterruptedError TimeoutError KeyUnreadableError RuntimeError RecursionError \
        NotImplementedError PythonFinalizationError ValueError UnicodeError UnicodeDecodeError \
        UnicodeEncodeError JSONDecodeError StoreVersionError UnsupportedOperation";
    CAUGHT
        .split_whitespace()
        .any(|c| c == class)
        .then(|| named(class))
}

impl RestoreError {
    /// The exception as `settle` logs it, or None for one it lets raise
    /// (OverflowError, TypeError, a refusal of this build).
    pub fn cause(&self) -> Option<Cause> {
        match self {
            RestoreError::Sqlite(e) | RestoreError::Write(WriteError::Sqlite(e)) => sqlite_cause(e),
            RestoreError::NotUtf8 => caught("OperationalError"),
            RestoreError::Write(w) => match w {
                WriteError::LaterWriter { .. } | WriteError::RolledBack(_) => {
                    caught("OperationalError")
                }
                WriteError::Version(_) => caught("StoreVersionError"),
                WriteError::Refused { class, .. } => caught(class),
                WriteError::Callback(e) => caught(&e.exception.class),
                WriteError::Clock(e) => Some(os_cause(e)),
                _ => None,
            },
            RestoreError::Lock(FlockError::Io(e)) => Some(os_cause(e)),
            RestoreError::Lock(FlockError::Path(_)) => caught("ValueError"),
            RestoreError::Lock(FlockError::Timeout(_)) => caught("TimeoutError"),
            RestoreError::Py(e) => caught(e.name()),
        }
    }
}

/// `row_withheld`: the row that decided an entry is kept off the log
/// (`seq`, `kind` and `mac` as `str()` of their columns, None where there
/// is no row), and why.
#[derive(Clone, Debug, PartialEq)]
pub struct Withheld {
    pub seq: PyValue,
    pub kind: Option<String>,
    pub mac: Option<String>,
    pub because: &'static str,
}

impl Withheld {
    fn new(seq: PyValue, row: Option<(String, String)>, because: &'static str) -> Self {
        let (kind, mac) = row.map_or((None, None), |(k, m)| (Some(k), Some(m)));
        Withheld {
            seq,
            kind,
            mac,
            because,
        }
    }

    /// The reference's dict `{seq, kind, mac, because}`.
    pub fn to_value(&self) -> PyValue {
        let text = |t: &Option<String>| t.as_deref().map_or(PyValue::None, PyValue::str);
        PyValue::Dict(vec![
            (PyValue::str("seq"), self.seq.clone()),
            (PyValue::str("kind"), text(&self.kind)),
            (PyValue::str("mac"), text(&self.mac)),
            (PyValue::str("because"), PyValue::str(self.because)),
        ])
    }
}

/// One warning the reference logs (`log.warning` on
/// `bettermemory.restore_rows`).
#[derive(Clone, Debug, PartialEq)]
pub enum Warning {
    /// `pending`: the record does not read.
    RecordUnread { record: PathBuf, cause: Cause },
    /// `_report_withheld`, in the words `_said` picks; `clock` holds
    /// `_clock_reading`'s two parts where the time decided.
    Withheld {
        restored_at: PyValue,
        bundle: PyValue,
        row: Withheld,
        clock: Option<(&'static str, String)>,
    },
    /// `_report_withheld`: the record could not be written.
    RecordNotWritten {
        restored_at: PyValue,
        because: &'static str,
        record: PathBuf,
    },
    /// `settle`: `append_missing` raised.
    NotAppended(Cause),
    /// `settle`: an entry's row appended or named.
    Named {
        restored_at: PyValue,
        bundle: PyValue,
        seq: i64,
    },
}

/// One argument of a warning, as the logger receives it.
#[derive(Clone, Debug, PartialEq)]
pub enum Arg<'a> {
    /// The store's path (`store.path`).
    Store,
    /// The record's path.
    Path(&'a Path),
    /// A value `%s` spells as `str(value)`.
    Value(&'a PyValue),
    Text(String),
    Int(i64),
    Cause(&'a Cause),
}

const SAID_RECORD: &str = "store %s: the restore at %s (from %s) gets no store_restored row while \
     %s at seq %s stands past the head it put back, written since by a build or command that \
     appends no late row: a row appended after it would read as a rewind of that record. The \
     restore record keeps the restore (row_withheld), `log export` reads its rewind from there, \
     and the row is appended if a later restore takes that row off the log";
const SAID_REPLACED: &str = "store %s: this store file holds the history the restore at %s (from \
     %s) replaced, not the one it put in place (%s at seq %s is the replaced history's), so it \
     gets no store_restored row; the restore record keeps the restore (row_withheld), and the row \
     is appended if the restored file comes back";
const SAID_ELSEWHERE: &str = "store %s: this store file does not hold the bundle head the restore \
     at %s (from %s) put back (it has %s at seq %s), so it gets no store_restored row here; the \
     restore record keeps the restore (row_withheld)";
const SAID_BY_CLOCK: &str = "store %s: the restore at %s (from %s) gets no store_restored row \
     here: this store file's first row past the bundle's head (%s at seq %s) is stamped before \
     the restore: %s; with %s to compare, nothing tells which. The restore record keeps the \
     restore (row_withheld), and this file is not placed again while that row stands";

impl Warning {
    /// The reference's format string.
    pub fn format(&self) -> &'static str {
        match self {
            Warning::RecordUnread { .. } => {
                "store %s: the restore record %s does not read (%s); a restore it names with no \
                 row gets none"
            }
            Warning::Withheld { clock: Some(_), .. } => SAID_BY_CLOCK,
            Warning::Withheld { row, .. } => match row.because {
                BECAUSE_RECORD => SAID_RECORD,
                BECAUSE_REPLACED => SAID_REPLACED,
                _ => SAID_ELSEWHERE,
            },
            Warning::RecordNotWritten { .. } => {
                "store %s: the restore at %s gets no store_restored row here (%s), and the \
                 restore record %s could not be written to say so; the next owner open decides \
                 again"
            }
            Warning::NotAppended(_) => {
                "store %s: a restore's store_restored row was not appended (%s); the next \
                 writing open appends it"
            }
            Warning::Named { .. } => {
                "store %s: the store_restored row of the restore at %s (from %s) is at seq %d, \
                 and the restore record names it now"
            }
        }
    }

    /// The format's arguments, in order.
    pub fn args(&self) -> Vec<Arg<'_>> {
        match self {
            Warning::RecordUnread { record, cause } => {
                vec![Arg::Store, Arg::Path(record), Arg::Cause(cause)]
            }
            Warning::Withheld {
                restored_at,
                bundle,
                row,
                clock,
            } => {
                let kind = row
                    .kind
                    .as_ref()
                    .map_or_else(|| "no row".to_owned(), |k| format!("a {k} row"));
                let mut args = vec![
                    Arg::Store,
                    Arg::Value(restored_at),
                    Arg::Value(bundle),
                    Arg::Text(kind),
                    Arg::Value(&row.seq),
                ];
                if let Some((what, why)) = clock {
                    args.push(Arg::Text((*what).to_owned()));
                    args.push(Arg::Text(why.clone()));
                }
                args
            }
            Warning::RecordNotWritten {
                restored_at,
                because,
                record,
            } => vec![
                Arg::Store,
                Arg::Value(restored_at),
                Arg::Text((*because).to_owned()),
                Arg::Path(record),
            ],
            Warning::NotAppended(cause) => vec![Arg::Store, Arg::Cause(cause)],
            Warning::Named {
                restored_at,
                bundle,
                seq,
            } => vec![
                Arg::Store,
                Arg::Value(restored_at),
                Arg::Value(bundle),
                Arg::Int(*seq),
            ],
        }
    }
}

/// `restore_record_path`: `<keys dir>/<store_id>.backup_restore.json`.
pub fn restore_record_path(keys_dir: &Path, store_id: &str) -> PathBuf {
    keys_dir.join(format!("{store_id}{RESTORE_RECORD_SUFFIX}"))
}

/// Why the record gave no document.
pub(super) enum Unread {
    /// FileNotFoundError.
    Missing,
    /// Another OSError or a ValueError, by class.
    Class(&'static str),
    Recursion,
}

/// `json.loads(bounded_read(path, RECORD_CAP_BYTES))`: the bytes read
/// as `json.loads` reads bytes (`json.detect_encoding`: UTF-8 with or
/// without its BOM, UTF-16 and UTF-32).
pub(super) fn read_record(path: &Path) -> Result<PyValue, Unread> {
    let bytes = bounded_read(path, RECORD_CAP_BYTES).map_err(|e| match e {
        BoundedReadError::Open(e) | BoundedReadError::Io(e) => {
            match Numbered::of_io(&e).error.class {
                PyErr::FileNotFound => Unread::Missing,
                class => Unread::Class(class.name()),
            }
        }
        BoundedReadError::Directory(_) => Unread::Class("IsADirectoryError"),
        _ => Unread::Class("ValueError"),
    })?;
    let text = decode_json_bytes(&bytes).ok_or(Unread::Class("UnicodeDecodeError"))?;
    json::loads_detailed(&text).map_err(|e| match e {
        LoadError::Recursion => Unread::Recursion,
        other => Unread::Class(other.name()),
    })
}

/// `_write_private`: `atomic_write_bytes(path, data,
/// mode_before_rename=0o600)`. `fsutil` holds no atomic write, and the
/// two copies of this (`log/ring.rs`, `machine/open.rs`) are private to
/// their units, so this is a third over the same shared write.
fn write_private(path: &Path, data: &[u8]) -> std::io::Result<()> {
    replace_retrying(path, data)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// `_fill`'s outcomes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Fill {
    Written,
    Unchanged,
    Missing,
    Failed,
}

/// Whether the record's `item` is `entry`'s place: a dict whose `row`
/// is None (or not there) and whose kind, store id and time equal the
/// entry's by Python's `==`.
fn is_place(item: &PyValue, entry: &PyValue) -> bool {
    matches!(item, PyValue::Dict(_))
        && matches!(get(item, "row"), PyValue::None)
        && same_place(item, entry)
}

fn key_at(items: &[(PyValue, PyValue)], key: &str) -> Option<usize> {
    let key = cps(key);
    items
        .iter()
        .rposition(|(k, _)| matches!(k, PyValue::Str(s) if *s == key))
}

/// `_fill(path, entry, key, value, drop=drop)` under the record's lock.
fn fill(
    path: &Path,
    entry: &PyValue,
    key: &str,
    value: PyValue,
    drop: Option<&str>,
) -> Result<(Fill, Option<PyValue>), RestoreError> {
    let _lock = flock_excl(path, ".lock").map_err(RestoreError::Lock)?;
    let mut document = match read_record(path) {
        Ok(document) => document,
        Err(Unread::Recursion) => return Err(RestoreError::Py(PyError::RecursionError)),
        Err(_) => return Ok((Fill::Missing, None)),
    };
    // `document["restores"]`: KeyError or TypeError unless a dict holds
    // it; a value that is not a list yields no dict.
    let PyValue::Dict(items) = &mut document else {
        return Ok((Fill::Missing, None));
    };
    let Some(at) = key_at(items, "restores") else {
        return Ok((Fill::Missing, None));
    };
    let PyValue::List(restores) = &mut items[at].1 else {
        return Ok((Fill::Missing, None));
    };
    let last = restores.iter_mut().rev().find(|item| is_place(item, entry));
    let Some(PyValue::Dict(target)) = last else {
        return Ok((Fill::Missing, None));
    };
    let previous = key_at(target, key).map(|i| target[i].1.clone());
    let dropped = drop.is_none_or(|d| key_at(target, d).is_none());
    if py_eq(previous.as_ref().unwrap_or(&NONE), &value) && dropped {
        return Ok((Fill::Unchanged, previous));
    }
    match key_at(target, key) {
        Some(i) => target[i].1 = value,
        None => target.push((PyValue::str(key), value)),
    }
    if let Some(i) = drop.and_then(|d| key_at(target, d)) {
        target.remove(i);
    }
    let encoded =
        json::dumps(&document, DumpOptions::DOCUMENT_INDENT2).map_err(RestoreError::Py)?;
    let mut data = json::utf8_text(&encoded).into_bytes();
    data.push(b'\n');
    Ok(match write_private(path, &data) {
        Ok(()) => (Fill::Written, previous),
        Err(_) => (Fill::Failed, previous),
    })
}

fn row_value(row: &Appended) -> PyValue {
    PyValue::Dict(vec![
        (PyValue::str("seq"), PyValue::int(i128::from(row.seq))),
        (PyValue::str("mac"), PyValue::str(&row.mac)),
    ])
}

/// `record_row(path, entry, row)`: `row` named in `entry`'s place, its
/// `row_withheld` dropped; a record that does not read, holds no such
/// entry or cannot be written is left as it is.
pub fn record_row(path: &Path, entry: &PyValue, row: &Appended) -> Result<(), RestoreError> {
    fill(path, entry, "row", row_value(row), Some(WITHHELD_KEY)).map(|_| ())
}

/// `pending(store)`: the record's entries for this store that a restore
/// wrote and whose `row` is there and None, in the record's order.
pub fn pending(
    store: &WritableStore,
    log: &mut Vec<Warning>,
) -> Result<Vec<PyValue>, RestoreError> {
    let path = restore_record_path(store.keys_dir(), store.store_id());
    let document = match read_record(&path) {
        Ok(document) => document,
        Err(Unread::Missing) => return Ok(Vec::new()),
        Err(Unread::Recursion) => return Err(RestoreError::Py(PyError::RecursionError)),
        Err(Unread::Class(class)) => {
            log.push(Warning::RecordUnread {
                record: path,
                cause: named(class),
            });
            return Ok(Vec::new());
        }
    };
    let Some(PyValue::List(restores)) = document.get("restores") else {
        return Ok(Vec::new());
    };
    let store_id = PyValue::str(store.store_id());
    Ok(restores
        .iter()
        .filter(|e| {
            matches!(e, PyValue::Dict(_))
                && history_replaced_kind(e) == STORE_RESTORED
                && matches!(e.get("row"), Some(PyValue::None))
                && py_eq(get(e, "store_id"), &store_id)
        })
        .cloned()
        .collect())
}

/// `_seq_mac`: a `{seq, mac}` whose seq is an int (a bool is not) and
/// whose mac is a str.
fn seq_mac(value: &PyValue) -> Option<(&PyValue, &PyStr)> {
    match (value.get("seq")?, value.get("mac")?) {
        (seq @ PyValue::Int(_), PyValue::Str(mac)) => Some((seq, mac)),
        _ => None,
    }
}

/// A Python int bound as an SQLite parameter: OverflowError past 64 bits.
fn bound(seq: &PyValue) -> Result<i64, RestoreError> {
    match seq {
        PyValue::Int(text) => text
            .parse()
            .map_err(|_| RestoreError::Py(PyError::OverflowError)),
        _ => Err(RestoreError::Py(PyError::TypeError)),
    }
}

/// The rows `sql` reads, each column decoded as `sqlite3` fetches it,
/// until `each` answers.
fn scan<T>(
    conn: &Connection,
    sql: &str,
    params: impl rusqlite::Params,
    mut each: impl FnMut(Vec<SqlValue>) -> Result<Option<T>, RestoreError>,
) -> Result<Option<T>, RestoreError> {
    let mut statement = conn.prepare(sql)?;
    let width = statement.column_count();
    let mut rows = statement.query(params)?;
    while let Some(row) = rows.next()? {
        let mut values = Vec::with_capacity(width);
        for i in 0..width {
            // `sql_value` refuses only a text that is not UTF-8.
            values.push(sql_value(row.get_ref(i)?).map_err(|_| RestoreError::NotUtf8)?);
        }
        if let Some(found) = each(values)? {
            return Ok(Some(found));
        }
    }
    Ok(None)
}

/// `_row_at`: this log's row at `seq` as `(str(kind), str(mac))`.
fn row_at(conn: &Connection, seq: i64) -> Result<Option<(String, String)>, RestoreError> {
    scan(
        conn,
        "SELECT kind, mac FROM log WHERE seq = ?",
        [seq],
        |v| Ok(Some((v[0].py_str(), v[1].py_str()))),
    )
}

/// `_written_before`: both read as times and the row's the earlier.
fn written_before(ts: &SqlValue, restored_at: &PyValue) -> bool {
    let (SqlValue::Text(ts), PyValue::Str(ran)) = (ts, restored_at) else {
        return false;
    };
    // `parse_event_ts` answers aware values only, so both instants are there.
    match (parse_event_ts(&cps(ts)), parse_event_ts(ran)) {
        (Some(written), Some(ran)) => written.instant_us() < ran.instant_us(),
        _ => false,
    }
}

/// `_replaced_head`: the entry's `head_before` where it is past its
/// `head_after`.
fn replaced_head(entry: &PyValue) -> Option<(&PyValue, &PyStr)> {
    let before = seq_mac(get(entry, "head_before"))?;
    let after = seq_mac(get(entry, "head_after"))?;
    (cmp_keys(before.0, after.0) == Some(std::cmp::Ordering::Greater)).then_some(before)
}

/// `_standing`: None to append, else the row that keeps the entry off.
fn standing(conn: &Connection, entry: &PyValue) -> Result<Option<Withheld>, RestoreError> {
    let Some((after, after_mac)) = seq_mac(get(entry, "head_after")) else {
        return Ok(Some(Withheld::new(PyValue::None, None, BECAUSE_ELSEWHERE)));
    };
    let after_seq = bound(after)?;
    let at_after = row_at(conn, after_seq)?;
    if at_after
        .as_ref()
        .is_none_or(|(_, mac)| cps(mac) != *after_mac)
    {
        return Ok(Some(Withheld::new(
            after.clone(),
            at_after,
            BECAUSE_ELSEWHERE,
        )));
    }
    if let Some((before, before_mac)) = replaced_head(entry) {
        // The replaced head tells the two histories apart by MAC; the
        // clock is not asked.
        let at_before = row_at(conn, bound(before)?)?;
        if at_before
            .as_ref()
            .is_some_and(|(_, mac)| cps(mac) == *before_mac)
        {
            return Ok(Some(Withheld::new(
                before.clone(),
                at_before,
                BECAUSE_REPLACED,
            )));
        }
    } else {
        let sql = "SELECT seq, kind, mac, ts FROM log WHERE seq > ? ORDER BY seq LIMIT 1";
        let first = scan(conn, sql, [after_seq], |v| Ok(Some(v)))?;
        if let Some(v) = first.filter(|v| written_before(&v[3], get(entry, "restored_at"))) {
            return Ok(Some(withheld_by(&v, BECAUSE_REPLACED)));
        }
    }
    let mut control: Vec<&str> = CONTROL_KINDS.to_vec();
    control.sort_unstable();
    let marks = vec!["?"; control.len()].join(", ");
    let sql = format!(
        "SELECT seq, kind, mac FROM log WHERE seq > ? AND kind NOT IN ({marks}) ORDER BY seq LIMIT 1"
    );
    let params = std::iter::once(SqlParam::Integer(after_seq))
        .chain(control.iter().map(|k| SqlParam::Text((*k).to_owned())));
    let record = scan(conn, &sql, rusqlite::params_from_iter(params), |v| {
        Ok(Some(v))
    })?;
    Ok(record.map(|v| withheld_by(&v, BECAUSE_RECORD)))
}

/// The seq column, the INTEGER PRIMARY KEY: always an integer.
fn seq_of(value: &SqlValue) -> i64 {
    match value {
        SqlValue::Integer(seq) => *seq,
        _ => unreachable!("seq is the INTEGER PRIMARY KEY"),
    }
}

/// The row `seq, kind, mac` past the head that keeps the entry off.
fn withheld_by(v: &[SqlValue], because: &'static str) -> Withheld {
    let row = Some((v[1].py_str(), v[2].py_str()));
    Withheld::new(PyValue::int(i128::from(seq_of(&v[0]))), row, because)
}

/// `_still_stands`: the `row_withheld` an entry carries still holds.
fn still_stands(conn: &Connection, withheld: &PyValue) -> Result<bool, RestoreError> {
    if !matches!(withheld, PyValue::Dict(_)) || withheld.get("because").is_none() {
        return Ok(false);
    }
    let (seq, mac) = (get(withheld, "seq"), get(withheld, "mac"));
    if matches!(seq, PyValue::None) {
        return Ok(true);
    }
    let mac = match (seq, mac) {
        (PyValue::Int(_), PyValue::None) => None,
        (PyValue::Int(_), PyValue::Str(mac)) => Some(mac.clone()),
        _ => return Ok(false),
    };
    let found = scan(
        conn,
        "SELECT mac FROM log WHERE seq = ?",
        [bound(seq)?],
        |v| Ok(Some(cps(&v[0].py_str()))),
    )?;
    Ok(found == mac)
}

/// `_payload`: the entry less `row_withheld`.
fn payload(entry: &PyValue) -> PyValue {
    without(entry, WITHHELD_KEY)
}

fn without(entry: &PyValue, key: &str) -> PyValue {
    let key = PyValue::str(key);
    match entry {
        PyValue::Dict(items) => {
            PyValue::Dict(items.iter().filter(|(k, _)| *k != key).cloned().collect())
        }
        other => other.clone(),
    }
}

/// `_logged`: the `store_restored` row past the entry's `head_after`
/// whose payload, less `engine_version`, is the entry's less `row` and
/// `row_withheld`.
fn logged(conn: &Connection, entry: &PyValue) -> Result<Option<Appended>, RestoreError> {
    let Some((after, _)) = seq_mac(get(entry, "head_after")) else {
        return Ok(None);
    };
    let wanted = without(&payload(entry), "row");
    let sql = "SELECT seq, mac, payload FROM log WHERE kind = ? AND seq > ? ORDER BY seq";
    scan(conn, sql, (STORE_RESTORED, bound(after)?), |v| {
        let read = match &v[2] {
            SqlValue::Text(t) => json::loads_detailed(&cps(t)),
            SqlValue::Blob(b) => {
                decode_json_bytes(b).map_or(Err(LoadError::Decode), |t| json::loads_detailed(&t))
            }
            _ => return Ok(None),
        };
        let carried = match read {
            Ok(carried @ PyValue::Dict(_)) => without(&carried, "engine_version"),
            Err(LoadError::Recursion) => return Err(RestoreError::Py(PyError::RecursionError)),
            _ => return Ok(None),
        };
        Ok(py_eq(&carried, &wanted).then(|| Appended {
            seq: seq_of(&v[0]),
            mac: v[1].py_str(),
        }))
    })
}

/// What `_settle_one` found.
enum Verdict {
    Kept,
    /// To append (`_decide`), or appended (`_settle_one`; None when the
    /// store refused writes).
    Append(Option<Appended>),
    Logged(Appended),
    Withheld(Withheld),
}

/// `_decide`: `_standing`, the entry's row looked for first unless the
/// log is not the restore's.
fn decide(conn: &Connection, entry: &PyValue) -> Result<Verdict, RestoreError> {
    let standing = standing(conn, entry)?;
    if let Some(w) = standing.as_ref().filter(|w| w.because != BECAUSE_RECORD) {
        return Ok(Verdict::Withheld(w.clone()));
    }
    if let Some(found) = logged(conn, entry)? {
        return Ok(Verdict::Logged(found));
    }
    Ok(standing.map_or(Verdict::Append(None), Verdict::Withheld))
}

/// `_settle_one`: read outside the write lock, then decided again and
/// appended inside one write transaction, which commits (the head moves
/// once).
fn settle_one(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    entry: &PyValue,
) -> Result<Verdict, RestoreError> {
    if still_stands(store.connection(), get(entry, WITHHELD_KEY))? {
        return Ok(Verdict::Kept);
    }
    let verdict = decide(store.connection(), entry)?;
    if !matches!(verdict, Verdict::Append(_)) {
        return Ok(verdict);
    }
    let engine = provider.engine_version();
    let mut tx = store.begin(provider).map_err(RestoreError::Write)?;
    let verdict = decide(tx.connection(), entry)?;
    if !matches!(verdict, Verdict::Append(_)) {
        tx.commit().map_err(RestoreError::Write)?;
        return Ok(verdict);
    }
    let row = append_history_replaced_in(&mut tx, &engine, &cps(STORE_RESTORED), &payload(entry))?;
    tx.commit().map_err(RestoreError::Write)?;
    Ok(Verdict::Append(row))
}

/// `append_missing(store, entries)`: each entry's row (default: the
/// `pending` entries) put on the log or kept off it, in order; returns
/// each entry settled with its row. Nothing, and nothing read, when the
/// store refuses writes. An entry is a dict (the reference's `Mapping`):
/// another value is TypeError where the reference, reading it with
/// `.get`, raises AttributeError.
pub fn append_missing(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    entries: Option<Vec<PyValue>>,
    log: &mut Vec<Warning>,
) -> Result<Vec<(PyValue, Appended)>, RestoreError> {
    let query_only: i64 = store
        .connection()
        .query_row("PRAGMA query_only", [], |r| r.get(0))?;
    if query_only != 0 {
        return Ok(Vec::new());
    }
    let todo = match entries {
        Some(entries) => entries,
        None => pending(store, log)?,
    };
    let path = restore_record_path(store.keys_dir(), store.store_id());
    let mut settled = Vec::new();
    for entry in todo {
        if !matches!(entry, PyValue::Dict(_)) {
            return Err(RestoreError::Py(PyError::TypeError));
        }
        match settle_one(store, provider, &entry)? {
            Verdict::Kept | Verdict::Append(None) => {}
            Verdict::Withheld(w) => report_withheld(&path, &entry, &w, log)?,
            Verdict::Logged(row) | Verdict::Append(Some(row)) => {
                record_row(&path, &entry, &row)?;
                settled.push((entry, row));
            }
        }
    }
    Ok(settled)
}

/// `_clock_reading`: what the file may be and why the time decided.
fn clock_reading(entry: &PyValue) -> (&'static str, String) {
    let before = seq_mac(get(entry, "head_before"));
    let after = seq_mac(get(entry, "head_after"));
    let what = if !matches!(get(entry, "log_end_before"), PyValue::None) {
        "the history it replaced, or a clock set back"
    } else if before.is_some() {
        "a history written before the restore (the one it replaced, or another copy of the \
         store), or a clock set back"
    } else {
        "another copy of the store, written before the restore (this host held none when it \
         ran: head_before and log_end_before null), or a clock set back"
    };
    let why = match before {
        None => "no head checkpoint at the restore (head_before null)".to_owned(),
        Some((seq, _)) => {
            let text = |v: &PyValue| match v {
                PyValue::Int(t) => t.clone(),
                _ => String::new(),
            };
            let bundle = after.map_or_else(|| "?".to_owned(), |(s, _)| text(s));
            format!(
                "no replaced head past the bundle's (head_before at seq {}, the bundle's head \
                 at seq {bundle})",
                text(seq)
            )
        }
    };
    (what, why)
}

/// `_report_withheld`: `row_withheld` written and said when the reason
/// is new to the entry; a record that cannot be written is said.
fn report_withheld(
    path: &Path,
    entry: &PyValue,
    withheld: &Withheld,
    log: &mut Vec<Warning>,
) -> Result<(), RestoreError> {
    let (outcome, previous) = fill(path, entry, WITHHELD_KEY, withheld.to_value(), None)?;
    let restored_at = get(entry, "restored_at").clone();
    match outcome {
        Fill::Failed => {
            log.push(Warning::RecordNotWritten {
                restored_at,
                because: withheld.because,
                record: path.to_path_buf(),
            });
            return Ok(());
        }
        Fill::Written => {}
        Fill::Unchanged | Fill::Missing => return Ok(()),
    }
    if let Some(previous @ PyValue::Dict(_)) = &previous {
        if py_eq(get(previous, "because"), &PyValue::str(withheld.because)) {
            return Ok(());
        }
    }
    let clock = (withheld.because == BECAUSE_REPLACED && replaced_head(entry).is_none())
        .then(|| clock_reading(entry));
    log.push(Warning::Withheld {
        restored_at,
        bundle: get(entry, "bundle").clone(),
        row: withheld.clone(),
        clock,
    });
    Ok(())
}

/// Whether two entries share their kind, store id and time.
fn same_place(a: &PyValue, b: &PyValue) -> bool {
    KEYS.iter().all(|k| py_eq(get(a, k), get(b, k)))
}

/// `append_for(store, entry)`: the pending entries, then `entry` (read
/// back as `json.loads(json.dumps(entry))` unless one shares its kind,
/// store id and time); returns `entry`'s row. `entry` is a dict, as the
/// reference's `Mapping`; another value is TypeError here, where the
/// reference's `dict(entry)` answers by the value: TypeError for a
/// number, ValueError for a str such as `"ab"`, an empty list read as
/// `{}` (the pending entries settled, None returned) and a list of
/// pairs read as the dict they spell.
pub fn append_for(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    entry: &PyValue,
    log: &mut Vec<Warning>,
) -> Result<Option<Appended>, RestoreError> {
    if !matches!(entry, PyValue::Dict(_)) {
        return Err(RestoreError::Py(PyError::TypeError));
    }
    let dumped = json::dumps(entry, DumpOptions::DEFAULT).map_err(RestoreError::Py)?;
    let own = json::loads(&dumped).map_err(RestoreError::Py)?;
    let mut todo = pending(store, log)?;
    if !todo.iter().any(|e| same_place(e, &own)) {
        todo.push(own.clone());
    }
    // The settled copy of `own` holds `own`'s very values, which compare
    // as any value `json.loads` made: a container equals itself, a NaN at
    // the top does not.
    for (settled, row) in append_missing(store, provider, Some(todo), log)? {
        if same_place(&settled, &own) {
            return Ok(Some(row));
        }
    }
    Ok(None)
}

/// `settle(store)`: `append_missing`, an exception of the four classes
/// logged instead of raised, and each settled entry said.
pub fn settle(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    log: &mut Vec<Warning>,
) -> Result<(), RestoreError> {
    let settled = match append_missing(store, provider, None, log) {
        Ok(settled) => settled,
        Err(error) => match error.cause() {
            Some(cause) => {
                log.push(Warning::NotAppended(cause));
                return Ok(());
            }
            None => return Err(error),
        },
    };
    for (entry, row) in settled {
        log.push(Warning::Named {
            restored_at: get(&entry, "restored_at").clone(),
            bundle: get(&entry, "bundle").clone(),
            seq: row.seq,
        });
    }
    Ok(())
}
