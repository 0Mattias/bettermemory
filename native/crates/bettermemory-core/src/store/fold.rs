//! The fold check, read only: `store_events.refold` and `log_verify`.
//!
//! [`StoreFile::refold`] replays every mutation row of the log into a
//! scratch SQLite database in memory (the reference's `SCHEMA` and
//! `MEMBERSHIP_SCHEMA`, less the FTS5 table and its three triggers, which
//! the fold never compares and which cannot change what it compares), the
//! way `store_rows._apply_mutation` applies each kind: the same statements,
//! the same values bound in the same order, so SQLite's affinities, keys
//! and the `memory_links_cleanup` trigger act alike. A row whose payload
//! raises what the reference catches (ValueError and its subclasses,
//! KeyError, TypeError, pydantic's ValidationError) is a `payload_invalid`
//! problem whose detail is the first line of `str(exc)`, at most 200
//! characters; what is applied before the raise stays applied, as in the
//! reference, which has no savepoint per row. Anything else the reference
//! raises out of `refold` is [`FoldError::Raised`] with its class.
//!
//! Then each folded and membership table is read on both sides over the
//! columns the reference's schema gives it (`_table_rows`), keyed by its key
//! columns, and compared as Python compares the tuples sqlite3 returns
//! (an int equals a float of the same value; text never equals a blob; a
//! row whose key another row already has replaces its values and keeps the
//! first key's spelling, as a dict does). The index text columns are left
//! out on both sides while the signed log names another tokenizer than the
//! reference's (`tokenizer_drift`; the reference's digest,
//! [`lexicon::python_tokenizer_fingerprint`], since the fold spells the
//! text as the reference does).
//!
//! Not ported: the reference keeps the unaccounted memory ids on its
//! `Store` (`_unaccounted_memory_ids`) for later reads; nothing here keeps
//! state between calls.

use std::collections::HashMap;

use rusqlite::types::{Value, ValueRef};
use rusqlite::Connection;

use super::{
    bytes_repr, log_row, KeyLoader, StoreError, StoreFile, LOAD_TRIES, LOG_SELECT, VERIFY_BATCH,
};
use crate::json::{self, LoadError};
use crate::lexicon;
use crate::log::{ChainReport, KeyRing, LogRow, LogSource, ReportDict, ReportValue, VerifyError};
use crate::pyvalue::{self, repr_str, PyError, PyStr, PyValue};
use crate::records::{self, Episode, Memory, RecordError, RecordFields, Tombstone};
use crate::{float_repr, unicode};

/// `FOLDED_TABLES`, in the reference's order.
pub const FOLDED_TABLES: [&str; 8] = [
    "memories",
    "memory_links",
    "tombstones",
    "episodes",
    "verifications",
    "conflicts",
    "imports",
    "quarantine",
];

/// `MEMBERSHIP_TABLES`, in the reference's order.
pub const MEMBERSHIP_TABLES: [&str; 14] = [
    "machines",
    "projects",
    "project_witnesses",
    "project_blobs",
    "project_remotes",
    "project_files",
    "declared_remotes",
    "checkouts",
    "checkout_views",
    "homes",
    "proposals",
    "standing",
    "returned",
    "native_membership_rows",
];

/// `INDEX_TEXT_COLUMNS`.
pub const INDEX_TEXT_COLUMNS: [&str; 2] = ["body_fts", "scopes_fts"];

/// `_TABLE_KEYS`.
pub fn table_keys(table: &str) -> &'static [&'static str] {
    match table {
        "memories" | "tombstones" | "episodes" | "conflicts" | "machines" | "projects"
        | "checkouts" | "proposals" => &["id"],
        "memory_links" => &["source_id", "type", "target_id", "note"],
        "verifications" => &["memory_id", "machine_id"],
        "imports" => &["source"],
        "quarantine" => &["name"],
        "project_witnesses" => &["project", "commit_id"],
        "project_blobs" => &["project", "blob"],
        "project_remotes" => &["project", "remote_key"],
        "project_files" => &["file_id"],
        "declared_remotes" => &["remote_key"],
        "checkout_views" => &["checkout", "machine", "path"],
        "homes" | "standing" | "returned" => &["record_id"],
        "native_membership_rows" => &["member_kind", "key_text"],
        _ => &[],
    }
}

/// One `_MembershipTable`.
struct Spec {
    table: &'static str,
    kind: &'static str,
    key: &'static [&'static str],
    columns: &'static [&'static str],
    optional: &'static [&'static str],
    floats: &'static [&'static str],
}

const SPECS: [Spec; 14] = [
    Spec {
        table: "native_membership_rows",
        kind: "native_member",
        key: &["member_kind", "key_text"],
        columns: &["member_kind", "key_text", "row_text"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "machines",
        kind: "machine",
        key: &["id"],
        columns: &["id", "facts_json", "predecessor", "minted"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "projects",
        kind: "project",
        key: &["id"],
        columns: &[
            "id",
            "name",
            "merged_into",
            "provisional",
            "parent",
            "tenant",
            "folder",
            "boundary",
            "minted",
        ],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "project_witnesses",
        kind: "project_witness",
        key: &["project", "commit_id"],
        columns: &["project", "commit_id", "arrival", "source", "fingerprint"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "project_blobs",
        kind: "project_blob",
        key: &["project", "blob"],
        columns: &["project", "blob", "witness"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "project_remotes",
        kind: "project_remote",
        key: &["project", "remote_key"],
        columns: &["project", "remote_key"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "project_files",
        kind: "project_file",
        key: &["file_id"],
        columns: &["file_id", "project", "evidence"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "declared_remotes",
        kind: "declared_remote",
        key: &["remote_key"],
        columns: &["remote_key", "project", "principal"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "checkouts",
        kind: "checkout",
        key: &["id"],
        columns: &[
            "id",
            "project",
            "machine",
            "kind",
            "object_device",
            "object_inode",
            "object_birth",
            "common_device",
            "common_inode",
            "common_birth",
            "git_dir",
            "token",
            "token_status",
            "stamps_json",
            "last_head",
            "branch",
            "remotes_json",
            "manifest_json",
            "legacy",
            "retired",
            "registered",
            "unresolved_json",
        ],
        optional: &["unresolved_json"],
        floats: &[],
    },
    Spec {
        table: "checkout_views",
        kind: "checkout_view",
        key: &["checkout", "machine", "path"],
        columns: &[
            "checkout",
            "machine",
            "path",
            "first_seen",
            "last_seen",
            "seen_order",
        ],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "homes",
        kind: "home",
        key: &["record_id"],
        columns: &[
            "record_id",
            "kind",
            "project",
            "checkout",
            "rule",
            "decided",
        ],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "proposals",
        kind: "proposal",
        key: &["id"],
        columns: &[
            "id",
            "checkout",
            "kind",
            "candidates_json",
            "evidence_json",
            "reason",
            "line",
            "state",
            "answer",
            "answered_by",
            "signature_json",
            "opened",
        ],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "standing",
        kind: "standing",
        key: &["record_id"],
        columns: &[
            "record_id",
            "probability",
            "body_sha256",
            "source",
            "answered_by",
            "answered",
        ],
        optional: &[],
        floats: &["probability"],
    },
    Spec {
        table: "returned",
        kind: "returned",
        key: &["record_id"],
        columns: &["record_id", "via", "returned"],
        optional: &[],
        floats: &[],
    },
];

const MEMORY_COLUMNS: [&str; 30] = [
    "id",
    "created",
    "updated",
    "last_verified_at",
    "confidence",
    "category",
    "body",
    "body_fts",
    "scopes_text",
    "scopes_fts",
    "scopes_json",
    "filename",
    "origin_repo",
    "origin_worktree",
    "provenance",
    "verified_head",
    "actor_client",
    "actor_model",
    "source",
    "origin_json",
    "actor_json",
    "verified_paths_json",
    "verified_commits_json",
    "verified_versions_json",
    "verified_absent_paths_json",
    "claims_json",
    "links_json",
    "corroborations",
    "last_corroborated",
    "log_mac",
];

const TOMBSTONE_COLUMNS: [&str; 30] = [
    "id",
    "created",
    "updated",
    "last_verified_at",
    "confidence",
    "category",
    "body",
    "scopes_json",
    "filename",
    "origin_repo",
    "origin_worktree",
    "provenance",
    "verified_head",
    "actor_client",
    "actor_model",
    "source",
    "origin_json",
    "actor_json",
    "verified_paths_json",
    "verified_commits_json",
    "verified_versions_json",
    "verified_absent_paths_json",
    "claims_json",
    "links_json",
    "corroborations",
    "last_corroborated",
    "removed",
    "removed_reason",
    "removed_session",
    "log_mac",
];

const CONFLICT_COLUMNS: [&str; 14] = [
    "id",
    "a_id",
    "b_id",
    "summary_a",
    "summary_b",
    "similarity",
    "method",
    "detector",
    "created",
    "status",
    "verdict_ts",
    "note",
    "verdict_hash_a",
    "verdict_hash_b",
];

const VERIFICATION_LISTS: [&str; 4] = [
    "verified_paths",
    "verified_commits",
    "verified_versions",
    "verified_absent_paths",
];

fn upsert(table: &str, columns: &[&str], key: &str) -> String {
    let updates: Vec<String> = columns
        .iter()
        .filter(|c| **c != key)
        .map(|c| format!("{c} = excluded.{c}"))
        .collect();
    format!(
        "INSERT INTO {table}({}) VALUES ({}) ON CONFLICT({key}) DO UPDATE SET {}",
        columns.join(", "),
        vec!["?"; columns.len()].join(", "),
        updates.join(", ")
    )
}

const EPISODE_UPSERT: &str = "INSERT INTO episodes(id, session_id, created, body, scopes_json, \
     takeaway, origin_json, is_floor, swarm_id, log_mac) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) \
     ON CONFLICT(id) DO UPDATE SET session_id = excluded.session_id, \
     created = excluded.created, body = excluded.body, \
     scopes_json = excluded.scopes_json, takeaway = excluded.takeaway, \
     origin_json = excluded.origin_json, is_floor = excluded.is_floor, \
     swarm_id = excluded.swarm_id, log_mac = excluded.log_mac";

// ---------------------------------------------------------------------------
// The report
// ---------------------------------------------------------------------------

/// A column value as Python's sqlite3 returns it.
#[derive(Clone, Debug, PartialEq)]
pub enum Cell {
    Null,
    Int(i64),
    Real(f64),
    Text(String),
    Blob(Vec<u8>),
}

impl Cell {
    /// `str()` of the value.
    pub fn py_str(&self) -> String {
        match self {
            Cell::Null => "None".to_owned(),
            Cell::Int(i) => i.to_string(),
            Cell::Real(f) => float_repr::repr(*f),
            Cell::Text(t) => t.clone(),
            Cell::Blob(b) => bytes_repr(b),
        }
    }

    /// `repr()` of the value.
    pub fn py_repr(&self) -> String {
        match self {
            Cell::Text(t) => unicode::to_string(&repr_str(&unicode::cps(t))).unwrap_or_default(),
            other => other.py_str(),
        }
    }
}

/// A value as a dict key: hashing and equality as Python's.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum KeyCell {
    Null,
    Int(i128),
    Float(u64),
    Text(String),
    Blob(Vec<u8>),
}

fn key_cell(c: &Cell) -> KeyCell {
    match c {
        Cell::Null => KeyCell::Null,
        Cell::Int(i) => KeyCell::Int(i128::from(*i)),
        Cell::Real(f) => {
            if f.is_finite() && f.fract() == 0.0 && f.abs() < 1.0e38 {
                KeyCell::Int(*f as i128)
            } else {
                KeyCell::Float(f.to_bits())
            }
        }
        Cell::Text(t) => KeyCell::Text(t.clone()),
        Cell::Blob(b) => KeyCell::Blob(b.clone()),
    }
}

/// A row's key as the report names it (`_key_label`): the one value, or
/// the list of them.
#[derive(Clone, Debug, PartialEq)]
pub enum Label {
    One(Cell),
    Many(Vec<Cell>),
}

impl Label {
    fn of(key: &[Cell]) -> Label {
        if key.len() == 1 {
            Label::One(key[0].clone())
        } else {
            Label::Many(key.to_vec())
        }
    }

    /// `str()` of the label, which the reference sorts by.
    pub fn py_str(&self) -> String {
        match self {
            Label::One(c) => c.py_str(),
            Label::Many(cells) => format!(
                "[{}]",
                cells
                    .iter()
                    .map(Cell::py_repr)
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        }
    }
}

/// One table's comparison.
#[derive(Clone, Debug, PartialEq)]
pub struct TableFold {
    pub table: &'static str,
    /// Live rows the replay does not produce, sorted by `str`.
    pub unaccounted: Vec<Label>,
    /// Replayed rows the live table lacks, sorted by `str`.
    pub missing: Vec<Label>,
    /// The live table's rows, by distinct key.
    pub rows: usize,
}

/// A mutation row whose payload could not be applied.
#[derive(Clone, Debug, PartialEq)]
pub struct PayloadProblem {
    pub seq: i64,
    pub kind: String,
    /// `str(exc).splitlines()[0][:200]`.
    pub detail: String,
}

/// `refold()`.
#[derive(Clone, Debug, PartialEq)]
pub struct FoldReport {
    /// `ok` or `diverged`.
    pub status: &'static str,
    pub tables: Vec<TableFold>,
    pub membership: Vec<TableFold>,
    pub problems: Vec<PayloadProblem>,
}

impl FoldReport {
    /// The memories table's unaccounted ids (`unaccounted_ids`).
    pub fn unaccounted_ids(&self) -> &[Label] {
        self.tables
            .iter()
            .find(|t| t.table == "memories")
            .map_or(&[], |t| &t.unaccounted)
    }
}

/// Why no report: what the reference raises out of `refold` or
/// `log_verify` (by class), or a read the native store refused.
#[derive(Debug)]
pub enum FoldError {
    Raised(&'static str),
    Store(StoreError),
}

impl From<StoreError> for FoldError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::NotUtf8 { .. } => FoldError::Raised("OperationalError"),
            StoreError::Sqlite(e) => FoldError::Raised(sqlite_class(&e)),
            StoreError::Py(e) => FoldError::Raised(e.name()),
            other => FoldError::Store(other),
        }
    }
}

impl std::fmt::Display for FoldError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FoldError::Raised(class) => write!(f, "the reference raises {class}"),
            FoldError::Store(e) => write!(f, "{e}"),
        }
    }
}

/// The exception class Python's sqlite3 raises for an SQLite error
/// (`_pysqlite_seterror`).
fn sqlite_class(e: &rusqlite::Error) -> &'static str {
    use rusqlite::ffi::ErrorCode;
    match e {
        rusqlite::Error::SqliteFailure(err, _) => match err.code {
            ErrorCode::ConstraintViolation | ErrorCode::TypeMismatch => "IntegrityError",
            ErrorCode::TooBig => "DataError",
            ErrorCode::DatabaseCorrupt | ErrorCode::NotADatabase => "DatabaseError",
            ErrorCode::InternalMalfunction | ErrorCode::NotFound => "InternalError",
            ErrorCode::OutOfMemory => "MemoryError",
            ErrorCode::ApiMisuse | ErrorCode::ParameterOutOfRange => "InterfaceError",
            _ => "OperationalError",
        },
        rusqlite::Error::Utf8Error(..) | rusqlite::Error::FromSqlConversionFailure(..) => {
            "OperationalError"
        }
        _ => "OperationalError",
    }
}

// ---------------------------------------------------------------------------
// Applying one row
// ---------------------------------------------------------------------------

/// What applying a row raised: an exception `refold` catches, with
/// `str(exc)`, or one it lets out.
enum Fail {
    Caught(&'static str, String),
    Raised(&'static str),
}

type Applied = Result<(), Fail>;

fn key_error(name: &str) -> Fail {
    Fail::Caught(
        "KeyError",
        unicode::to_string(&repr_str(&unicode::cps(name))).unwrap_or_default(),
    )
}

fn py_fail(e: PyError) -> Fail {
    Fail::Raised(e.name())
}

fn text(s: PyStr) -> String {
    unicode::to_string(&s).unwrap_or_default()
}

fn item<'a>(payload: &'a PyValue, name: &str) -> Result<&'a PyValue, Fail> {
    payload.get(name).ok_or_else(|| key_error(name))
}

fn get<'a>(payload: &'a PyValue, name: &str) -> &'a PyValue {
    static NONE: PyValue = PyValue::None;
    payload.get(name).unwrap_or(&NONE)
}

fn py_str(v: &PyValue) -> Result<PyValue, Fail> {
    pyvalue::py_str(v).map(PyValue::Str).map_err(py_fail)
}

fn type_name(v: &PyValue) -> &'static str {
    match v {
        PyValue::None => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::Str(_) => "str",
        PyValue::List(_) => "list",
        PyValue::Dict(_) => "dict",
        PyValue::Tuple(_) => "tuple",
        PyValue::DateTime(_) => "datetime",
        PyValue::Path(_) => "PurePosixPath",
    }
}

/// Python's truth value.
fn truthy(v: &PyValue) -> bool {
    match v {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(t) => t != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) | PyValue::Path(s) => !s.is_empty(),
        PyValue::List(i) | PyValue::Tuple(i) => !i.is_empty(),
        PyValue::Dict(i) => !i.is_empty(),
        PyValue::DateTime(_) => true,
    }
}

/// The message of the UnicodeEncodeError binding `s` raises, if it holds a
/// surrogate.
fn encode_error(s: &[u32]) -> Option<String> {
    let surrogate = |c: &u32| (0xD800..=0xDFFF).contains(c);
    let start = s.iter().position(surrogate)?;
    let end = start + s[start..].iter().take_while(|c| surrogate(c)).count();
    Some(if end - start == 1 {
        format!(
            "'utf-8' codec can't encode character '\\u{:04x}' in position {start}: surrogates not allowed",
            s[start]
        )
    } else {
        format!(
            "'utf-8' codec can't encode characters in position {start}-{}: surrogates not allowed",
            end - 1
        )
    })
}

/// A parameter as Python's sqlite3 binds it.
fn bind(v: &PyValue) -> Result<Value, Fail> {
    Ok(match v {
        PyValue::None => Value::Null,
        PyValue::Bool(b) => Value::Integer(i64::from(*b)),
        PyValue::Int(t) => Value::Integer(
            t.parse::<i64>()
                .map_err(|_| Fail::Raised("OverflowError"))?,
        ),
        PyValue::Float(f) => {
            if f.is_nan() {
                Value::Null
            } else {
                Value::Real(*f)
            }
        }
        // sqlite3's default datetime adapter (`isoformat(" ")`; deprecated
        // since 3.12, still registered): only a live write's caller can
        // pass one; a row's JSON never holds one.
        PyValue::DateTime(dt) => Value::Text(dt.isoformat_sep(' ')),
        PyValue::Str(s) => match unicode::to_string(s) {
            Some(t) => Value::Text(t),
            None => {
                return Err(Fail::Caught(
                    "UnicodeEncodeError",
                    encode_error(s).unwrap_or_default(),
                ))
            }
        },
        _ => return Err(Fail::Raised("ProgrammingError")),
    })
}

fn bind_all(values: &[PyValue]) -> Result<Vec<Value>, Fail> {
    values.iter().map(bind).collect()
}

fn record_fail(e: RecordError) -> Fail {
    match e {
        RecordError::Validation { title, errors } => Fail::Caught(
            "ValidationError",
            format!(
                "{} validation error{} for {title}",
                errors.len(),
                if errors.len() == 1 { "" } else { "s" }
            ),
        ),
        RecordError::Raised(class) => Fail::Raised(class),
    }
}

fn json_text(v: &PyValue) -> Result<PyValue, Fail> {
    records::json_text(v).map(PyValue::Str).map_err(py_fail)
}

/// `int(value)`, with the reference's messages.
fn py_int(v: &PyValue) -> Result<PyValue, Fail> {
    match v {
        PyValue::Float(f) if f.is_nan() => Err(Fail::Caught(
            "ValueError",
            "cannot convert float NaN to integer".into(),
        )),
        PyValue::Float(f) if f.is_infinite() => Err(Fail::Raised("OverflowError")),
        PyValue::Bool(_) | PyValue::Int(_) | PyValue::Float(_) => {
            pyvalue::py_int(v).map_err(py_fail)
        }
        PyValue::Str(s) => pyvalue::int_from_str(s).map_err(|_| {
            Fail::Caught(
                "ValueError",
                match too_many_digits(s) {
                    Some(n) => format!(
                        "Exceeds the limit (4300 digits) for integer string conversion: value has \
                     {n} digits; use sys.set_int_max_str_digits() to increase the limit"
                    ),
                    // CPython's `%.200R`: the repr cut to its first 200
                    // code points (measured against 3.13 for str and
                    // bytes, escapes included).
                    None => format!(
                        "invalid literal for int() with base 10: {}",
                        text(repr_str(s).into_iter().take(200).collect())
                    ),
                },
            )
        }),
        other => Err(Fail::Caught(
            "TypeError",
            format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                type_name(other)
            ),
        )),
    }
}

/// The digit count of a well-formed integer literal past 4300 digits.
fn too_many_digits(s: &[u32]) -> Option<usize> {
    let space = |c: u32| matches!(c, 0x09..=0x0D | 0x20) || (c >= 0x80 && unicode::is_space(c));
    let start = s.iter().position(|&c| !space(c))?;
    let end = s.iter().rposition(|&c| !space(c))? + 1;
    let mut body = &s[start..end];
    if matches!(body.first(), Some(&0x2B | &0x2D)) {
        body = &body[1..];
    }
    let mut digits = 0;
    let mut underscore = true;
    for &c in body {
        if c == 0x5F {
            if underscore {
                return None;
            }
            underscore = true;
        } else if unicode::is_decimal(c) {
            digits += 1;
            underscore = false;
        } else {
            return None;
        }
    }
    (!underscore && digits > 4300).then_some(digits)
}

/// `list(value)`.
fn py_list(v: &PyValue) -> Result<Vec<PyValue>, Fail> {
    match v {
        PyValue::List(items) | PyValue::Tuple(items) => Ok(items.clone()),
        PyValue::Dict(items) => Ok(items.iter().map(|(k, _)| k.clone()).collect()),
        PyValue::Str(s) => Ok(s.iter().map(|&c| PyValue::Str(vec![c])).collect()),
        other => Err(Fail::Caught(
            "TypeError",
            format!("'{}' object is not iterable", type_name(other)),
        )),
    }
}

/// `models.validate_path_targets`, with its messages.
fn check_targets(fields: &RecordFields) -> Applied {
    for (name, targets, inputs) in [
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
        if let Some(targets) = targets {
            if targets.len() > 64 {
                return Err(Fail::Caught(
                    "ValueError",
                    format!("{name} capped at 64 entries (got {})", targets.len()),
                ));
            }
            if !inputs.iter().all(|i| targets.iter().any(|(k, _)| k == i)) {
                return Err(Fail::Caught(
                    "ValueError",
                    format!("{name} must cover every legacy attestation"),
                ));
            }
        }
    }
    Ok(())
}

fn provenance(payload: &PyValue) -> Result<PyValue, Fail> {
    let value = py_str(item(payload, "provenance")?)?;
    let PyValue::Str(s) = &value else {
        unreachable!("str() is a str")
    };
    if !["local", "imported", "synced"].contains(&text(s.clone()).as_str())
        || unicode::to_string(s).is_none()
    {
        return Err(Fail::Caught(
            "ValueError",
            format!("unknown provenance {}", text(repr_str(s))),
        ));
    }
    Ok(value)
}

fn named(names: &[&str], values: Vec<PyValue>) -> HashMap<String, PyValue> {
    names.iter().map(|n| (*n).to_owned()).zip(values).collect()
}

fn ordered(columns: &[&str], mut map: HashMap<String, PyValue>) -> Vec<PyValue> {
    columns
        .iter()
        .map(|c| map.remove(*c).unwrap_or(PyValue::None))
        .collect()
}

/// `" ".join(scopes)` from a memory's `scopes_text`: the scopes less its
/// outer spaces (a memory has at least one scope).
fn scopes_inner(scopes_text: &str) -> &str {
    scopes_text
        .strip_prefix(' ')
        .and_then(|s| s.strip_suffix(' '))
        .unwrap_or("")
}

/// The fields `models.Memory` defines (`Memory.model_fields`); the
/// write fixture records the reference's list and the golden test holds
/// this one to it.
pub const MEMORY_FIELDS: [&str; 22] = [
    "id",
    "created",
    "updated",
    "scopes",
    "confidence",
    "source",
    "body",
    "origin",
    "actor",
    "last_verified_at",
    "category",
    "verified_paths",
    "verified_commits",
    "verified_versions",
    "verified_absent_paths",
    "verified_path_targets",
    "verified_absent_path_targets",
    "claims",
    "verified_head",
    "links",
    "corroborations",
    "last_corroborated",
];

/// The fields `models.TombstonedMemory` defines
/// (`TombstonedMemory.model_fields`): [`MEMORY_FIELDS`] less the links and
/// the corroborations, which a tombstone's row keeps beside the record,
/// and with the removal's three.
pub const TOMBSTONE_FIELDS: [&str; 22] = [
    "id",
    "created",
    "updated",
    "scopes",
    "confidence",
    "source",
    "body",
    "origin",
    "actor",
    "last_verified_at",
    "category",
    "verified_paths",
    "verified_commits",
    "verified_versions",
    "verified_absent_paths",
    "verified_path_targets",
    "verified_absent_path_targets",
    "claims",
    "verified_head",
    "removed",
    "removed_reason",
    "removed_session",
];

/// `store_rows._unknown_record_fields`: sorted, without repeats.
fn unknown_record_fields(conn: &Connection, id: &PyValue) -> rusqlite::Result<Vec<PyStr>> {
    let id = bind(id).map_err(|_| rusqlite::Error::InvalidQuery)?;
    let mut statement = conn.prepare(
        "SELECT kind, payload FROM log WHERE mac IN (\
         SELECT log_mac FROM memories WHERE id = ?1 \
         UNION ALL SELECT log_mac FROM tombstones WHERE id = ?1)",
    )?;
    let mut rows = statement.query([id])?;
    let mut unknown: Vec<PyStr> = Vec::new();
    while let Some(row) = rows.next()? {
        let kind: Option<String> = row.get(0).ok().flatten();
        let (key, tombstone) = match kind.as_deref() {
            Some("memory_put") => ("memory", false),
            Some("tombstone_put") => ("tombstone", true),
            _ => continue,
        };
        let Ok(Some(payload)) = row.get::<usize, Option<String>>(1) else {
            continue;
        };
        // `json.loads(payload).get(key)`: a payload that does not parse, or
        // is not an object, is passed over (ValueError, AttributeError).
        let Ok(value @ PyValue::Dict(_)) = json::loads(&unicode::cps(&payload)) else {
            continue;
        };
        let Some(PyValue::Dict(record)) = value.get(key) else {
            continue;
        };
        for (name, _) in record {
            let PyValue::Str(name) = name else { continue };
            let fields: &[&str] = if tombstone {
                &TOMBSTONE_FIELDS
            } else {
                &MEMORY_FIELDS
            };
            let known = unicode::to_string(name).is_some_and(|n| fields.contains(&n.as_str()));
            if !known && !unknown.contains(name) {
                unknown.push(name.clone());
            }
        }
    }
    unknown.sort();
    Ok(unknown)
}

/// `UnknownFieldsError`'s text.
fn unknown_fields_text(record_id: &str, kind: &str, fields: &[PyStr]) -> String {
    let shown: Vec<String> = fields
        .iter()
        .take(8)
        .map(|f| text(repr_str(f)).chars().take(66).collect())
        .collect();
    let more = if fields.len() > 8 {
        format!(" and {} more", fields.len() - 8)
    } else {
        String::new()
    };
    let refused = match kind {
        "memory_delete" => "the removal",
        "tombstone_put" => "the change to the removed record",
        "tombstone_delete" => "the restore, purge or prune",
        _ => "the change",
    };
    format!(
        "record {record_id} was last written by a build that defines fields this one does not \
         ({}{more}); this build would drop them, so it refuses {refused}: upgrade bettermemory to \
         change the record",
        shown.join(", ")
    )
}

/// `body_fts` and `scopes_fts` of every memory the replay left:
/// `fts_index_text(body)` and `fts_index_text(" ".join(scopes))`, the
/// scopes being `scopes_text` less its outer spaces (a memory has at least
/// one scope). Spelled on every core.
fn fill_index_text(conn: &Connection) -> Result<(), FoldError> {
    let mut rows: Vec<(i64, String, String)> = Vec::new();
    {
        let mut statement = conn
            .prepare("SELECT rowid, body, scopes_text FROM memories")
            .map_err(StoreError::from)?;
        let mut query = statement.query([]).map_err(StoreError::from)?;
        while let Some(row) = query.next().map_err(StoreError::from)? {
            rows.push((
                row.get(0).map_err(StoreError::from)?,
                row.get(1).map_err(StoreError::from)?,
                row.get(2).map_err(StoreError::from)?,
            ));
        }
    }
    let spell = |(rowid, body, scopes): &(i64, String, String)| {
        (
            *rowid,
            lexicon::fts_index_text(&unicode::cps(body)),
            lexicon::fts_index_text(&unicode::cps(scopes_inner(scopes))),
        )
    };
    let threads = std::thread::available_parallelism()
        .map_or(1, |n| n.get())
        .min(8);
    let chunk = rows.len().div_ceil(threads).max(1);
    let spelled: Vec<(i64, String, String)> = std::thread::scope(|scope| {
        let handles: Vec<_> = rows
            .chunks(chunk)
            .map(|part| scope.spawn(move || part.iter().map(spell).collect::<Vec<_>>()))
            .collect();
        handles
            .into_iter()
            .flat_map(|h| h.join().expect("a spelling thread"))
            .collect()
    });
    conn.execute_batch("BEGIN").map_err(StoreError::from)?;
    {
        let mut update = conn
            .prepare("UPDATE memories SET body_fts = ?, scopes_fts = ? WHERE rowid = ?")
            .map_err(StoreError::from)?;
        for (rowid, body, scopes) in spelled {
            update
                .execute(rusqlite::params![body, scopes, rowid])
                .map_err(StoreError::from)?;
        }
    }
    conn.execute_batch("COMMIT").map_err(StoreError::from)?;
    Ok(())
}

struct Scratch {
    conn: Connection,
    memory_upsert: String,
    tombstone_upsert: String,
}

/// `store_rows._apply_mutation` on one connection, with its statements
/// prepared once each: the fold's scratch database, or (`live`) the store
/// itself inside a write's transaction (`Store._mutate`).
///
/// The two differ only where the reference's one function reads what
/// differs between the two connections: on the live store
/// `_refuse_unknown_fields` reads the log the scratch database lacks, and
/// the index text (`body_fts`, `scopes_fts`) is spelled per put, where the
/// fold spells it once per row the replay leaves (`fill_index_text`).
pub(crate) struct Applier<'c> {
    conn: &'c Connection,
    memory_upsert: String,
    tombstone_upsert: String,
    live: bool,
    statements: std::cell::RefCell<HashMap<String, rusqlite::Statement<'c>>>,
}

impl<'c> Applier<'c> {
    fn new(scratch: &'c Scratch) -> Self {
        Applier {
            conn: &scratch.conn,
            memory_upsert: scratch.memory_upsert.clone(),
            tombstone_upsert: scratch.tombstone_upsert.clone(),
            live: false,
            statements: std::cell::RefCell::new(HashMap::new()),
        }
    }

    /// The applier of a write on the live store `conn`.
    pub(crate) fn live(conn: &'c Connection) -> Self {
        Applier {
            conn,
            memory_upsert: upsert("memories", &MEMORY_COLUMNS, "id"),
            tombstone_upsert: upsert("tombstones", &TOMBSTONE_COLUMNS, "id"),
            live: true,
            statements: std::cell::RefCell::new(HashMap::new()),
        }
    }

    /// `apply` for the write path: the exception the reference raises, as
    /// its class name and `str(exc)` where the fold would catch it.
    pub(crate) fn apply_live(
        &self,
        kind: &str,
        payload: &PyValue,
        mac: &str,
    ) -> Result<(), (&'static str, Option<String>)> {
        self.apply(kind, payload, mac).map_err(|f| match f {
            Fail::Caught(class, text) => (class, Some(text)),
            Fail::Raised(class) => (class, None),
        })
    }

    /// `_refuse_unknown_fields` on the live store: the names of the fields
    /// the payloads of the rows `id`'s memory and tombstone point at
    /// carry that this build's models do not define. The scratch log is
    /// empty, so the fold finds none.
    fn refuse_unknown(&self, id: &PyValue, kind: &str) -> Applied {
        bind(id)?;
        if !self.live {
            return Ok(());
        }
        let unknown =
            unknown_record_fields(self.conn, id).map_err(|e| Fail::Raised(sqlite_class(&e)))?;
        if unknown.is_empty() {
            return Ok(());
        }
        let PyValue::Str(id) = id else {
            return Err(Fail::Raised("TypeError"));
        };
        Err(Fail::Caught(
            "UnknownFieldsError",
            unknown_fields_text(&text(id.clone()), kind, &unknown),
        ))
    }

    /// Bind `values` in order, as Python's sqlite3 does, and run `sql`.
    fn exec(&self, sql: &str, values: &[PyValue]) -> Applied {
        let params = bind_all(values)?;
        let mut statements = self.statements.borrow_mut();
        if !statements.contains_key(sql) {
            let statement = self
                .conn
                .prepare(sql)
                .map_err(|e| Fail::Raised(sqlite_class(&e)))?;
            statements.insert(sql.to_owned(), statement);
        }
        let statement = statements.get_mut(sql).expect("prepared");
        statement
            .execute(rusqlite::params_from_iter(params))
            .map_err(|e| Fail::Raised(sqlite_class(&e)))?;
        Ok(())
    }

    fn apply(&self, kind: &str, payload: &PyValue, mac: &str) -> Applied {
        let mac = PyValue::str(mac);
        match kind {
            "memory_put" => {
                let memory = Memory::from_payload(item(payload, "memory")?).map_err(record_fail)?;
                self.refuse_unknown(&PyValue::Str(memory.fields.id.clone()), kind)?;
                let provenance = provenance(payload)?;
                check_targets(&memory.fields)?;
                let row = memory.row().map_err(py_fail)?;
                let mut names: Vec<&str> = records::RECORD_COLUMNS.to_vec();
                names.extend(records::MEMORY_ROW_COLUMNS);
                let mut map = named(&names, row);
                // In the fold the index text is spelled once per row the
                // replay leaves, after it (`fill_index_text`): a later put
                // of the same id overwrites it, and nothing else reads it.
                // A live write spells it here, as `_write_memory_row` does.
                let (body_fts, scopes_fts) = if self.live {
                    let body = match map.get("body") {
                        Some(PyValue::Str(b)) => b.clone(),
                        _ => return Err(Fail::Raised("TypeError")),
                    };
                    let scopes = match map.get("scopes_text") {
                        Some(PyValue::Str(s)) => text(s.clone()),
                        _ => return Err(Fail::Raised("TypeError")),
                    };
                    (
                        PyValue::str(&lexicon::fts_index_text(&body)),
                        PyValue::str(&lexicon::fts_index_text(&unicode::cps(scopes_inner(
                            &scopes,
                        )))),
                    )
                } else {
                    (PyValue::str(""), PyValue::str(""))
                };
                map.insert("body_fts".into(), body_fts);
                map.insert("scopes_fts".into(), scopes_fts);
                map.insert("filename".into(), get(payload, "filename").clone());
                map.insert("provenance".into(), provenance);
                map.insert("log_mac".into(), mac);
                self.exec(&self.memory_upsert, &ordered(&MEMORY_COLUMNS, map))?;
                let id = PyValue::Str(memory.fields.id.clone());
                self.exec(
                    "DELETE FROM memory_links WHERE source_id = ?",
                    std::slice::from_ref(&id),
                )?;
                let mut seen: Vec<[PyValue; 4]> = Vec::new();
                for link in &memory.links {
                    let key = [
                        id.clone(),
                        PyValue::str(link.link_type.value()),
                        PyValue::Str(link.target_id.clone()),
                        link.note.clone().map_or(PyValue::None, PyValue::Str),
                    ];
                    if seen.contains(&key) {
                        continue;
                    }
                    seen.push(key);
                }
                for key in &seen {
                    self.exec(
                        "INSERT OR IGNORE INTO memory_links(source_id, type, target_id, note) \
                         VALUES (?, ?, ?, ?)",
                        key,
                    )?;
                }
                Ok(())
            }
            "memory_delete" | "tombstone_delete" | "episode_delete" | "conflict_delete" => {
                let id = py_str(item(payload, "id")?)?;
                let table = match kind {
                    "memory_delete" => "memories",
                    "tombstone_delete" => "tombstones",
                    "episode_delete" => "episodes",
                    _ => "conflicts",
                };
                if kind != "episode_delete" && kind != "conflict_delete" {
                    // `_refuse_unknown_fields` binds the id to read the
                    // log (empty in the fold's scratch database).
                    self.refuse_unknown(&id, kind)?;
                }
                self.exec(&format!("DELETE FROM {table} WHERE id = ?"), &[id])
            }
            "tombstone_put" => {
                let dead =
                    Tombstone::from_payload(item(payload, "tombstone")?).map_err(record_fail)?;
                self.refuse_unknown(&PyValue::Str(dead.fields.id.clone()), kind)?;
                let provenance = provenance(payload)?;
                let links = get(payload, "links");
                let links = if truthy(links) {
                    links.clone()
                } else {
                    PyValue::List(Vec::new())
                };
                if !matches!(links, PyValue::List(_)) {
                    return Err(Fail::Caught("TypeError", "links must be a list".into()));
                }
                let filename = get(payload, "filename").clone();
                let given = get(payload, "corroborations");
                let corroborations = if truthy(given) {
                    py_int(given)?
                } else {
                    PyValue::int(0)
                };
                let last_corroborated = get(payload, "last_corroborated").clone();
                check_targets(&dead.fields)?;
                let row = dead.row().map_err(py_fail)?;
                let mut names: Vec<&str> = records::RECORD_COLUMNS.to_vec();
                names.extend(records::TOMBSTONE_ROW_COLUMNS);
                let mut map = named(&names, row);
                map.insert("filename".into(), filename);
                map.insert("provenance".into(), provenance);
                map.insert("links_json".into(), json_text(&links)?);
                map.insert("corroborations".into(), corroborations);
                map.insert("last_corroborated".into(), last_corroborated);
                map.insert("log_mac".into(), mac);
                self.exec(&self.tombstone_upsert, &ordered(&TOMBSTONE_COLUMNS, map))
            }
            "episode_put" => {
                let episode =
                    Episode::from_payload(item(payload, "episode")?).map_err(record_fail)?;
                let mut row = episode.row().map_err(py_fail)?;
                row.push(mac);
                self.exec(EPISODE_UPSERT, &row)
            }
            "verification_put" => {
                let memory_id = py_str(item(payload, "memory_id")?)?;
                let machine_id = get(payload, "machine_id").clone();
                let mut lists = Vec::new();
                for name in VERIFICATION_LISTS {
                    let v = get(payload, name);
                    lists.push(if truthy(v) { py_list(v)? } else { Vec::new() });
                }
                self.exec(
                    "DELETE FROM verifications WHERE memory_id = ? \
                     AND COALESCE(machine_id, '') = COALESCE(?, '')",
                    &[memory_id.clone(), machine_id.clone()],
                )?;
                let mut values = vec![
                    memory_id,
                    machine_id,
                    py_str(item(payload, "verified_at")?)?,
                    get(payload, "verified_head").clone(),
                ];
                for list in lists {
                    values.push(json_text(&PyValue::List(list))?);
                }
                self.exec(
                    "INSERT INTO verifications(memory_id, machine_id, verified_at, \
                     verified_head, verified_paths_json, verified_commits_json, \
                     verified_versions_json, verified_absent_paths_json) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    &values,
                )
            }
            "verification_delete" => {
                let memory_id = py_str(item(payload, "memory_id")?)?;
                self.exec(
                    "DELETE FROM verifications WHERE memory_id = ? \
                     AND COALESCE(machine_id, '') = COALESCE(?, '')",
                    &[memory_id, get(payload, "machine_id").clone()],
                )
            }
            "conflict_put" => {
                let record = item(payload, "conflict")?;
                if !matches!(record, PyValue::Dict(_)) {
                    return Err(Fail::Raised("AttributeError"));
                }
                let values: Vec<PyValue> = CONFLICT_COLUMNS
                    .iter()
                    .map(|c| get(record, c).clone())
                    .collect();
                self.exec(
                    &format!(
                        "INSERT OR REPLACE INTO conflicts({}) VALUES ({})",
                        CONFLICT_COLUMNS.join(", "),
                        vec!["?"; CONFLICT_COLUMNS.len()].join(", ")
                    ),
                    &values,
                )
            }
            "import_put" => {
                let values = vec![
                    py_str(item(payload, "source")?)?,
                    py_str(item(payload, "content_hash")?)?,
                    py_str(item(payload, "imported_at")?)?,
                    get(payload, "memory_id").clone(),
                ];
                self.exec(
                    "INSERT OR REPLACE INTO imports(source, content_hash, imported_at, memory_id) \
                     VALUES (?, ?, ?, ?)",
                    &values,
                )
            }
            "import_delete" => {
                let source = py_str(item(payload, "source")?)?;
                self.exec("DELETE FROM imports WHERE source = ?", &[source])
            }
            "quarantine_put" => {
                let name = py_str(item(payload, "name")?)?;
                let reason = py_str(item(payload, "reason")?)?;
                let detail = get(payload, "detail").clone();
                let remote = get(payload, "remote").clone();
                let at = py_str(item(payload, "at")?)?;
                let values = vec![
                    name,
                    reason,
                    detail,
                    remote,
                    at,
                    get(payload, "size").clone(),
                    get(payload, "sha256").clone(),
                ];
                self.exec(
                    "INSERT OR REPLACE INTO quarantine(name, reason, detail, remote, at, size, \
                     sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    &values,
                )
            }
            "quarantine_delete" => {
                let name = py_str(item(payload, "name")?)?;
                self.exec("DELETE FROM quarantine WHERE name = ?", &[name])
            }
            _ => {
                if let Some(spec) = kind
                    .strip_suffix("_put")
                    .and_then(|k| SPECS.iter().find(|s| s.kind == k))
                {
                    let values = membership_values(spec, item(payload, spec.kind)?)?;
                    return self.exec(
                        &format!(
                            "INSERT OR REPLACE INTO {}({}) VALUES ({})",
                            spec.table,
                            spec.columns.join(", "),
                            vec!["?"; spec.columns.len()].join(", ")
                        ),
                        &values,
                    );
                }
                if let Some(spec) = kind
                    .strip_suffix("_delete")
                    .and_then(|k| SPECS.iter().find(|s| s.kind == k))
                {
                    let key = membership_key(spec, payload)?;
                    let clause: Vec<String> = spec.key.iter().map(|c| format!("{c} = ?")).collect();
                    return self.exec(
                        &format!("DELETE FROM {} WHERE {}", spec.table, clause.join(" AND ")),
                        &key,
                    );
                }
                Err(Fail::Caught(
                    "ValueError",
                    format!(
                        "unknown mutation kind {}",
                        text(repr_str(&unicode::cps(kind)))
                    ),
                ))
            }
        }
    }
}

/// `_membership_values`.
fn membership_values(spec: &Spec, row: &PyValue) -> Result<Vec<PyValue>, Fail> {
    let PyValue::Dict(items) = row else {
        return Err(Fail::Caught(
            "TypeError",
            format!("a {} row must be an object", spec.kind),
        ));
    };
    let mut unknown: Vec<PyStr> = Vec::new();
    for (k, _) in items {
        let PyValue::Str(k) = k else {
            return Err(Fail::Raised("TypeError"));
        };
        let known = unicode::to_string(k).is_some_and(|k| spec.columns.contains(&k.as_str()));
        if !known && !unknown.contains(k) {
            unknown.push(k.clone());
        }
    }
    if !unknown.is_empty() {
        unknown.sort();
        let shown: Vec<String> = unknown.iter().map(|k| text(repr_str(k))).collect();
        return Err(Fail::Caught(
            "ValueError",
            format!("unknown {} columns [{}]", spec.kind, shown.join(", ")),
        ));
    }
    let mut values = Vec::with_capacity(spec.columns.len());
    for column in spec.columns {
        let value = if spec.optional.contains(column) {
            get(row, column)
        } else {
            item(row, column)?
        };
        values.push(if column.ends_with("_json") {
            match value {
                PyValue::None => PyValue::None,
                other => json_text(other)?,
            }
        } else {
            match value {
                PyValue::Bool(b) => PyValue::int(i128::from(*b)),
                PyValue::None | PyValue::Str(_) | PyValue::Int(_) => value.clone(),
                PyValue::Float(f) if spec.floats.contains(column) && f.is_finite() => value.clone(),
                _ => {
                    return Err(Fail::Caught(
                        "TypeError",
                        format!("{}.{column} must be text, an integer or null", spec.kind),
                    ))
                }
            }
        });
    }
    membership_key(spec, row)?;
    Ok(values)
}

/// `_membership_key`.
fn membership_key(spec: &Spec, payload: &PyValue) -> Result<Vec<PyValue>, Fail> {
    let mut key = Vec::new();
    for column in spec.key {
        match item(payload, column)? {
            PyValue::Str(s) if !s.is_empty() => key.push(PyValue::Str(s.clone())),
            _ => {
                return Err(Fail::Caught(
                    "ValueError",
                    format!("{}.{column} must be non-empty text", spec.kind),
                ))
            }
        }
    }
    Ok(key)
}

/// The first line of `text` as `str.splitlines` cuts it, at most 200
/// characters.
fn first_line(text: &str) -> String {
    let breaks = |c: char| {
        matches!(
            c,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        )
    };
    text.split(breaks)
        .next()
        .unwrap_or("")
        .chars()
        .take(200)
        .collect()
}

// ---------------------------------------------------------------------------
// The scratch database
// ---------------------------------------------------------------------------

fn schema_texts() -> Vec<String> {
    let parsed = json::loads(&unicode::cps(super::admission::reference_schema_json()))
        .expect("the pinned schema JSON");
    let Some(PyValue::List(texts)) = parsed.get("texts") else {
        panic!("schema texts")
    };
    texts
        .iter()
        .take(2)
        .map(|t| match t {
            PyValue::Str(s) => unicode::to_string(s).expect("schema text"),
            _ => panic!("schema text"),
        })
        .collect()
}

fn scratch() -> Result<Scratch, FoldError> {
    let conn = Connection::open_in_memory().map_err(StoreError::from)?;
    for text in schema_texts() {
        conn.execute_batch(&text).map_err(StoreError::from)?;
    }
    conn.execute_batch(
        "DROP TRIGGER memories_ai; DROP TRIGGER memories_ad; DROP TRIGGER memories_au; \
         DROP TABLE memories_fts;",
    )
    .map_err(StoreError::from)?;
    Ok(Scratch {
        conn,
        memory_upsert: upsert("memories", &MEMORY_COLUMNS, "id"),
        tombstone_upsert: upsert("tombstones", &TOMBSTONE_COLUMNS, "id"),
    })
}

fn columns_of(conn: &Connection, table: &str) -> Result<Vec<String>, FoldError> {
    let mut statement = conn
        .prepare(&format!("PRAGMA table_info({table})"))
        .map_err(StoreError::from)?;
    let names = statement
        .query_map([], |r| r.get::<_, String>(1))
        .map_err(StoreError::from)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(StoreError::from)?;
    Ok(names)
}

fn cell(v: ValueRef<'_>) -> Result<Cell, FoldError> {
    Ok(match v {
        ValueRef::Null => Cell::Null,
        ValueRef::Integer(i) => Cell::Int(i),
        ValueRef::Real(f) => Cell::Real(f),
        ValueRef::Text(t) => Cell::Text(
            std::str::from_utf8(t)
                .map_err(|_| FoldError::Raised("OperationalError"))?
                .to_owned(),
        ),
        ValueRef::Blob(b) => Cell::Blob(b.to_vec()),
    })
}

/// `_table_rows`: insertion-ordered, keyed as a Python dict. A row's
/// values are held as the SHA-256 of their encoding ([`row_digest`]),
/// equal exactly when Python's tuples compare equal, so a table costs its
/// keys and 32 bytes a row, not its text.
#[derive(Default)]
struct Rows {
    entries: Vec<(Vec<Cell>, [u8; 32])>,
    index: HashMap<Vec<KeyCell>, usize>,
}

/// The values of a row as a digest: the column count, then each value as
/// its dict-key form ([`key_cell`]: an integral float within reach is the
/// int it equals), so two rows digest alike exactly when every value
/// compares equal in Python.
fn row_digest(row: &rusqlite::Row<'_>, count: usize) -> Result<[u8; 32], FoldError> {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update((count as u64).to_le_bytes());
    for i in 0..count {
        let v = row.get_ref(i).map_err(StoreError::from)?;
        let c = match v {
            ValueRef::Text(t) => {
                let t =
                    std::str::from_utf8(t).map_err(|_| FoldError::Raised("OperationalError"))?;
                h.update([3]);
                h.update((t.len() as u64).to_le_bytes());
                h.update(t.as_bytes());
                continue;
            }
            ValueRef::Blob(b) => {
                h.update([4]);
                h.update((b.len() as u64).to_le_bytes());
                h.update(b);
                continue;
            }
            ValueRef::Null => Cell::Null,
            ValueRef::Integer(i) => Cell::Int(i),
            ValueRef::Real(f) => Cell::Real(f),
        };
        match key_cell(&c) {
            KeyCell::Null => h.update([0]),
            KeyCell::Int(i) => {
                h.update([1]);
                h.update(i.to_le_bytes());
            }
            KeyCell::Float(bits) => {
                h.update([2]);
                h.update(bits.to_le_bytes());
            }
            KeyCell::Text(_) | KeyCell::Blob(_) => unreachable!("handled above"),
        }
    }
    Ok(h.finalize().into())
}

impl Rows {
    fn get(&self, key: &[Cell]) -> Option<&[u8; 32]> {
        let k: Vec<KeyCell> = key.iter().map(key_cell).collect();
        self.index.get(&k).map(|&i| &self.entries[i].1)
    }
}

fn table_rows(
    conn: &Connection,
    table: &str,
    known: &[String],
    have: &[String],
    skip: &[&str],
) -> Result<Rows, FoldError> {
    let columns: Vec<&String> = known
        .iter()
        .filter(|c| have.contains(c) && c.as_str() != "rowid" && !skip.contains(&c.as_str()))
        .collect();
    if columns.is_empty() {
        return Err(FoldError::Raised("OperationalError"));
    }
    let keys: Vec<usize> = table_keys(table)
        .iter()
        .map(|k| {
            columns
                .iter()
                .position(|c| c == k)
                .ok_or(FoldError::Raised("IndexError"))
        })
        .collect::<Result<_, _>>()?;
    let list: Vec<&str> = columns.iter().map(|c| c.as_str()).collect();
    let mut statement = conn
        .prepare(&format!("SELECT {} FROM {table}", list.join(", ")))
        .map_err(StoreError::from)?;
    let mut rows = statement.query([]).map_err(StoreError::from)?;
    let mut out = Rows::default();
    while let Some(row) = rows.next().map_err(StoreError::from)? {
        let values = row_digest(row, columns.len())?;
        let key: Vec<Cell> = keys
            .iter()
            .map(|&i| cell(row.get_ref(i).map_err(StoreError::from)?))
            .collect::<Result<_, _>>()?;
        let hashed: Vec<KeyCell> = key.iter().map(key_cell).collect();
        match out.index.get(&hashed) {
            Some(&i) => out.entries[i].1 = values,
            None => {
                out.index.insert(hashed, out.entries.len());
                out.entries.push((key, values));
            }
        }
    }
    Ok(out)
}

fn sorted(mut labels: Vec<Label>) -> Vec<Label> {
    labels.sort_by_cached_key(Label::py_str);
    labels
}

/// `store_index.stored_fingerprint`.
fn stored_fingerprint(conn: &Connection) -> Result<Option<PyStr>, FoldError> {
    let mut named: Option<(i64, PyStr)> = None;
    for (kind, key) in [
        ("index_rebuilt", "new_fingerprint"),
        ("store_created", "tokenizer_fingerprint"),
    ] {
        let mut statement = conn
            .prepare("SELECT seq, payload FROM log WHERE kind = ? ORDER BY seq DESC LIMIT 1")
            .map_err(StoreError::from)?;
        let mut rows = statement.query([kind]).map_err(StoreError::from)?;
        let Some(row) = rows.next().map_err(StoreError::from)? else {
            continue;
        };
        let seq = match cell(row.get_ref(0).map_err(StoreError::from)?)? {
            Cell::Int(i) => i,
            _ => return Err(FoldError::Raised("TypeError")),
        };
        let payload = match cell(row.get_ref(1).map_err(StoreError::from)?)? {
            Cell::Null => None,
            Cell::Text(t) => match json::loads_detailed(&unicode::cps(&t)) {
                Ok(v) => Some(v),
                Err(LoadError::Recursion) => return Err(FoldError::Raised("RecursionError")),
                Err(_) => None,
            },
            _ => return Err(FoldError::Raised("TypeError")),
        };
        if let Some(PyValue::Str(value)) = payload.as_ref().and_then(|p| p.get(key)) {
            if named.as_ref().is_none_or(|(s, _)| seq > *s) {
                named = Some((seq, value.clone()));
            }
        }
    }
    if let Some((_, value)) = named {
        return Ok(Some(value));
    }
    let mut statement = conn
        .prepare("SELECT value FROM meta WHERE key = ?")
        .map_err(StoreError::from)?;
    let mut rows = statement
        .query(["tokenizer_fingerprint"])
        .map_err(StoreError::from)?;
    match rows.next().map_err(StoreError::from)? {
        Some(row) => Ok(Some(unicode::cps(
            &cell(row.get_ref(0).map_err(StoreError::from)?)?.py_str(),
        ))),
        None => Ok(None),
    }
}

/// Log rows a fold reads, parses and applies at a time: the memory a
/// fold holds is a batch's rows and their parsed payloads, whatever the
/// log's length.
pub const FOLD_BATCH: usize = 4096;

/// How many times the batched phase of `log verify` reads the log's end
/// again and follows a writer outside the final snapshot, so the rows read
/// under its lock are those appended since the last follow (fewer than a
/// batch, unless the writer outruns the reader).
pub const FOLLOW_ROUNDS: usize = 4;

/// The mutation rows of one batch: seq, kind, payload, MAC.
struct Batch {
    rows: Vec<(i64, String, String, String)>,
    /// Rows read, of every kind.
    read: usize,
    last_seq: i64,
}

/// What the final snapshot of a pass reads besides the log, each as the
/// reference would meet it after the replay (a raise there comes after
/// any raise of the replay).
struct Live {
    present: Result<Vec<String>, FoldError>,
    fingerprint: Result<Option<PyStr>, FoldError>,
    tables: Vec<Result<Rows, FoldError>>,
}

fn text_ok(v: ValueRef<'_>) -> Result<(), FoldError> {
    if let ValueRef::Text(t) = v {
        std::str::from_utf8(t).map_err(|_| FoldError::Raised("OperationalError"))?;
    }
    Ok(())
}

/// The live store's shape, read before a pass's final snapshot so the
/// snapshot holds the read lock for rows only: the tables present, each
/// compared table's columns, and the schema version they were read at
/// (read again in the snapshot; when it moved, the shape is read there).
struct Shape {
    version: i64,
    present: Result<Vec<String>, FoldError>,
    columns: Vec<Result<Vec<String>, FoldError>>,
}

fn schema_version(conn: &Connection) -> Result<i64, FoldError> {
    Ok(conn
        .query_row("PRAGMA schema_version", [], |r| r.get(0))
        .map_err(StoreError::from)?)
}

fn read_shape(conn: &Connection) -> Result<Shape, FoldError> {
    let version = schema_version(conn)?;
    let present = (|| {
        let mut statement = conn
            .prepare("SELECT name FROM sqlite_master WHERE type = 'table'")
            .map_err(StoreError::from)?;
        let mut rows = statement.query([]).map_err(StoreError::from)?;
        let mut out = Vec::new();
        while let Some(row) = rows.next().map_err(StoreError::from)? {
            if let Cell::Text(t) = cell(row.get_ref(0).map_err(StoreError::from)?)? {
                out.push(t);
            }
        }
        Ok(out)
    })();
    let columns = FOLDED_TABLES
        .iter()
        .chain(MEMBERSHIP_TABLES.iter())
        .map(|t| columns_of(conn, t))
        .collect();
    Ok(Shape {
        version,
        present,
        columns,
    })
}

/// The scratch database a fold replays into, begun, and its tables'
/// columns.
fn begun_scratch() -> Result<(Scratch, Vec<Vec<String>>), FoldError> {
    let scratch = scratch()?;
    scratch
        .conn
        .execute_batch("BEGIN")
        .map_err(StoreError::from)?;
    let known = FOLDED_TABLES
        .iter()
        .chain(MEMBERSHIP_TABLES.iter())
        .map(|t| columns_of(&scratch.conn, t))
        .collect::<Result<_, _>>()?;
    Ok((scratch, known))
}

impl StoreFile {
    /// The rows after `after`, at most [`FOLD_BATCH`], in seq order, in
    /// one statement; the mutation rows kept. As Python's sqlite3 fetches
    /// every column of every row, text that is not UTF-8 in any of them is
    /// OperationalError.
    fn read_log_batch(&self, after: i64) -> Result<Batch, FoldError> {
        let mut statement = self
            .conn()
            .prepare(
                "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log \
                 WHERE seq > ?1 ORDER BY seq LIMIT ?2",
            )
            .map_err(StoreError::from)?;
        let mut query = statement
            .query(rusqlite::params![after, FOLD_BATCH as i64])
            .map_err(StoreError::from)?;
        let mut batch = Batch {
            rows: Vec::new(),
            read: 0,
            last_seq: after,
        };
        while let Some(row) = query.next().map_err(StoreError::from)? {
            let get = |i| {
                row.get_ref(i)
                    .map_err(|e| FoldError::from(StoreError::from(e)))
            };
            for i in 1..7 {
                text_ok(get(i)?)?;
            }
            let seq = match get(0)? {
                ValueRef::Integer(i) => i,
                _ => return Err(FoldError::Raised("TypeError")),
            };
            batch.read += 1;
            batch.last_seq = seq;
            let kind = match cell(get(3)?)? {
                Cell::Text(t) => t,
                _ => continue,
            };
            if !is_mutation(&kind) {
                continue;
            }
            let payload = match cell(get(4)?)? {
                Cell::Text(t) => t,
                _ => return Err(FoldError::Raised("TypeError")),
            };
            let mac = cell(get(6)?)?.py_str();
            batch.rows.push((seq, kind, payload, mac));
        }
        Ok(batch)
    }

    /// What a pass's final snapshot reads besides the log: the tables
    /// present, the tokenizer the log names, every compared table's rows,
    /// `shape` was read before the snapshot; it is read
    /// again here only when the schema moved since.
    fn read_live(&self, shape: Shape, known: &[Vec<String>]) -> Result<Live, FoldError> {
        let conn = self.conn();
        let shape = if schema_version(conn)? == shape.version {
            shape
        } else {
            read_shape(conn)?
        };
        let fingerprint = stored_fingerprint(conn);
        let drift = match &fingerprint {
            Ok(f) => *f != Some(unicode::cps(&lexicon::python_tokenizer_fingerprint())),
            Err(_) => false,
        };
        let tables = FOLDED_TABLES
            .iter()
            .chain(MEMBERSHIP_TABLES.iter())
            .zip(known)
            .zip(&shape.columns)
            .map(|((table, known), have)| {
                let here = match &shape.present {
                    Ok(p) => p.iter().any(|n| n == table),
                    Err(_) => return Ok(Rows::default()),
                };
                if !here {
                    return Ok(Rows::default());
                }
                let skip: &[&str] = if drift && *table == "memories" {
                    &INDEX_TEXT_COLUMNS
                } else {
                    &[]
                };
                let have = match have {
                    Ok(h) => h,
                    Err(_) => return Err(FoldError::Raised("OperationalError")),
                };
                table_rows(conn, table, known, have, skip)
            })
            .collect();
        Ok(Live {
            present: shape.present,
            fingerprint,
            tables,
        })
    }

    /// One pass of the fold: the full batches of the log read one
    /// statement at a time (outside any transaction the caller holds, each
    /// statement its own short read) and applied; then, in one read
    /// transaction (the caller's, or one opened here), the rest of the log,
    /// the tables and the tokenizer the log names. Under writers that only
    /// append, what the replay has read is a prefix of the log in that
    /// snapshot, so the tables are compared with the fold of the very log
    /// they were read with.
    fn fold_pass(&self) -> Result<FoldReport, FoldError> {
        let (scratch, known) = begun_scratch()?;
        let mut problems = Vec::new();
        let applier = Applier::new(&scratch);
        let mut after = 0i64;
        let outside = self.conn().is_autocommit();
        if outside {
            loop {
                let batch = self.read_log_batch(after)?;
                if batch.read < FOLD_BATCH {
                    break;
                }
                after = batch.last_seq;
                apply_batch(&applier, batch, &mut problems)?;
            }
        }
        let shape = read_shape(self.conn())?;
        let mut tail: Vec<Batch> = Vec::new();
        let live = self.read_transaction(|s| -> Result<Live, FoldError> {
            loop {
                let batch = s.read_log_batch(after)?;
                let short = batch.read < FOLD_BATCH;
                after = batch.last_seq;
                if outside {
                    // Applied after the snapshot ends: the lock is held
                    // for reads only.
                    tail.push(batch);
                } else {
                    apply_batch(&applier, batch, &mut problems)?;
                }
                if short {
                    break;
                }
            }
            s.read_live(shape, &known)
        })??;
        for batch in tail {
            apply_batch(&applier, batch, &mut problems)?;
        }
        drop(applier);
        compare(&scratch, &known, live, problems)
    }

    /// `Store.refold()`: the fold report, or what the reference raises.
    /// The tables are compared with the fold of the log they were read
    /// with (`fold_pass`), so a writer that appends and applies in one
    /// transaction never makes it diverge.
    pub fn refold(&self) -> Result<FoldReport, FoldError> {
        self.fold_pass()
    }

    /// One pass of `log verify`: every log row read once, its bytes fed to
    /// the chain verifier and to the fold's replay ([`OneRead`]); the full
    /// batches outside any transaction (on a rollback-journal file read in
    /// place), then one read transaction for the rows after them, the head
    /// file and the live tables. Both reports describe the log as it
    /// stands in that snapshot.
    fn one_read_pass(
        &self,
        ring: &KeyRing,
        fingerprint: &str,
        between: &mut dyn FnMut(),
    ) -> Result<Pass, FoldError> {
        let (scratch, known) = begun_scratch()?;
        let applier = Applier::new(&scratch);
        let source = OneRead {
            between: std::cell::RefCell::new(between),
            visited: std::cell::Cell::new(0),
            first_seq: std::cell::Cell::new(None),
            snapshot_began: std::cell::Cell::new(None),
            store: self,
            applier: &applier,
            batch: VERIFY_BATCH,
            cap: std::cell::Cell::new(0),
            rekeys: std::cell::RefCell::new(None),
            feed: std::cell::RefCell::new(Feed::default()),
            opened: std::cell::Cell::new(false),
            shape: std::cell::RefCell::new(None),
            longest_hold: std::cell::Cell::new(std::time::Duration::ZERO),
        };
        let chain = crate::log::verify_chain_from(&source, ring, fingerprint);
        // Still in the final snapshot (when the rows were read in one).
        // The rows the pass visited must be the log's rows now: the batches
        // assume the log only grows, and a row removed or inserted behind
        // the read position shows here as another count or first seq.
        let moved = chain.is_ok() && {
            let now: Result<(i64, Option<i64>), _> = self.conn().query_row(
                "SELECT count(*), min(seq) FROM log WHERE seq > 0",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            );
            match now {
                Ok((count, first)) => {
                    count != source.visited.get() as i64 || first != source.first_seq.get()
                }
                Err(_) => true,
            }
        };
        // The tables.
        let shape = match source.shape.borrow_mut().take() {
            Some(shape) => shape,
            None => read_shape(self.conn()),
        };
        let live = shape.and_then(|shape| self.read_live(shape, &known));
        let mut hold = source.longest_hold.get();
        if source.opened.get() {
            // A read transaction: ending it changes nothing.
            let _ = self.conn().execute_batch("COMMIT");
            if let Some(began) = source.snapshot_began.get() {
                hold = hold.max(began.elapsed());
            }
        }
        self.longest_hold.set(hold);
        let feed = source.feed.into_inner();
        let mut problems = feed.problems;
        let mut raised = feed.raised;
        if raised.is_none() {
            for rows in feed.tail {
                let batch = Batch {
                    rows,
                    read: 0,
                    last_seq: 0,
                };
                if let Err(e) = apply_batch(&applier, batch, &mut problems) {
                    raised = Some(e);
                    break;
                }
            }
        }
        drop(applier);
        let fold = match raised {
            Some(e) => Err(e),
            None => live.and_then(|live| compare(&scratch, &known, live, problems)),
        };
        Ok(Pass { chain, fold, moved })
    }

    /// `Store.log_verify()`, as a command runs it: `load` reads the key
    /// ring and meta's fingerprint (as for [`StoreFile::verify_loading`]),
    /// then one pass reads every log row once for both the chain and the
    /// fold (`one_read_pass`), and the two are merged.
    ///
    /// A WAL file, a private copy, or a caller's open transaction: the
    /// load and the pass in one read transaction (a WAL reader holds no
    /// writer off); the head file is outside SQLite, so a head a writer
    /// moved past the snapshot is told apart from a deleted tail and the
    /// pass run again. A rollback-journal file read in place: the full
    /// batches each their own statement, then one short read transaction
    /// for the rest of the log, the head file, the tables and the log's
    /// end. A row a writer appends during the pass needs no second pass.
    /// A report that is not ok, or a raise, from a pass during which the
    /// store changed (`PRAGMA data_version`) or whose rows are not the
    /// log's rows in the final snapshot (another count or first seq), is
    /// not returned: the whole pass runs once more in one read transaction
    /// and that is returned, so every not-ok report is one snapshot's. A key rotation met during the pass
    /// (a rekey row past the pass's rekey read) loads again, at most
    /// [`LOAD_TRIES`] times; past them [`StoreError::KeptChanging`].
    pub fn log_verify_loading(&self, load: &mut KeyLoader<'_>) -> Result<LogVerify, FoldError> {
        self.log_verify_loading_with(load, &mut || {})
    }

    /// [`StoreFile::log_verify_loading`] with `between` run after each
    /// batch read outside the final snapshot (a test edits the store
    /// there, as a writer or an attacker would).
    #[doc(hidden)]
    pub fn log_verify_loading_with(
        &self,
        load: &mut KeyLoader<'_>,
        between: &mut dyn FnMut(),
    ) -> Result<LogVerify, FoldError> {
        let before = data_version(self)?;
        let mut moved = false;
        let first = self.verify_passes(load, between, &mut moved);
        if matches!(first, Err(FoldError::Store(StoreError::KeptChanging))) {
            return first;
        }
        // Rows removed or inserted behind the read position: whatever the
        // pass found, report what one snapshot shows instead. Not ok, or a
        // raise, while the store changed during the pass: the same.
        let clean = matches!(&first, Ok(r) if r.status == "ok");
        let changed = data_version(self).map_or(true, |now| now != before);
        if !moved && (clean || !changed) {
            return first;
        }
        let store_id = |s: &StoreFile| -> Result<String, FoldError> {
            Ok(s.meta()?
                .into_iter()
                .find(|(k, _)| k == "store_id")
                .map(|(_, v)| v)
                .unwrap_or_default())
        };
        self.read_transaction(|s| -> Result<LogVerify, FoldError> {
            let (ring, fp) = load(s)?;
            let id = store_id(s)?;
            let pass = s.one_read_pass(&ring, &fp, &mut || {})?;
            merge(pass.chain.map_err(chain_error)?, pass.fold?, &id, &fp)
        })?
    }

    /// The passes of [`StoreFile::log_verify_loading_with`] before any
    /// confirming one: loaded again on a key rotation met mid-pass, and
    /// (one snapshot) on a head file a writer moved past it.
    fn verify_passes(
        &self,
        load: &mut KeyLoader<'_>,
        between: &mut dyn FnMut(),
        moved: &mut bool,
    ) -> Result<LogVerify, FoldError> {
        let store_id = |s: &StoreFile| -> Result<String, FoldError> {
            Ok(s.meta()?
                .into_iter()
                .find(|(k, _)| k == "store_id")
                .map(|(_, v)| v)
                .unwrap_or_default())
        };
        let mut changed = false;
        for _ in 0..LOAD_TRIES {
            let batched = self.reads_in_batches()?;
            let mut run = |s: &StoreFile| -> Result<Option<Loaded>, FoldError> {
                let (ring, fp) = load(s)?;
                if changed {
                    // A rotation renames its pending key in after its
                    // commit (`key_lifecycle._rotate` step 5).
                    let current = ring
                        .current
                        .as_ref()
                        .map(|k| crate::log::fingerprint(k.bytes()));
                    if current.as_deref() != Some(fp.as_str()) {
                        return Ok(None);
                    }
                }
                let id = store_id(s)?;
                let pass = s.one_read_pass(&ring, &fp, &mut *between)?;
                Ok(Some(Loaded { ring, fp, id, pass }))
            };
            let loaded = if batched {
                run(self)?
            } else {
                self.read_transaction(&mut run)??
            };
            let Some(loaded) = loaded else {
                std::thread::sleep(std::time::Duration::from_millis(50));
                continue;
            };
            *moved = loaded.pass.moved;
            let chain = match loaded.pass.chain {
                Err(VerifyError::Source(StoreError::Changed)) => {
                    changed = true;
                    continue;
                }
                Err(e) => Err(chain_error(e)),
                Ok(chain) => Ok(chain),
            };
            if !batched {
                if let (Ok(c), Ok(f)) = (&chain, &loaded.pass.fold) {
                    if f.status == "ok"
                        && self.head_moved_on(c, &loaded.ring).map_err(chain_error)?
                    {
                        continue;
                    }
                }
            }
            if let (Ok(c), Ok(f)) = (&chain, &loaded.pass.fold) {
                // A private copy cannot be read forward: a head that moved
                // with a writer after the copy was taken is no tampering.
                if f.status == "ok" && self.copy_is_behind(c) {
                    return Err(FoldError::Store(StoreError::KeptChanging));
                }
            }
            return chain.and_then(|c| merge(c, loaded.pass.fold?, &loaded.id, &loaded.fp));
        }
        Err(FoldError::Store(StoreError::KeptChanging))
    }
}

fn chain_error(e: VerifyError<StoreError>) -> FoldError {
    match e {
        VerifyError::Py(e) => FoldError::Raised(e.name()),
        VerifyError::Source(e) => FoldError::from(e),
    }
}

/// `PRAGMA data_version`: it moves when another connection commits.
fn data_version(store: &StoreFile) -> Result<i64, FoldError> {
    Ok(store
        .conn()
        .query_row("PRAGMA data_version", [], |r| r.get(0))
        .map_err(StoreError::from)?)
}

/// A pass's two reports, before the merge.
struct Pass {
    chain: Result<ChainReport, VerifyError<StoreError>>,
    fold: Result<FoldReport, FoldError>,
    /// The log's row count or first seq in the final snapshot is not what
    /// the pass visited: rows were removed or inserted behind it.
    moved: bool,
}

struct Loaded {
    ring: KeyRing,
    fp: String,
    id: String,
    pass: Pass,
}

/// The fold's share of the rows [`OneRead`] reads: the mutation rows not
/// yet applied, those read in the final snapshot (applied after it ends),
/// the payload problems so far, and the class the replay raised (the
/// rows after it are still fed to the chain, as the reference verifies
/// the chain before it folds).
#[derive(Default)]
struct Feed {
    pending: Vec<(i64, String, String, String)>,
    tail: Vec<Vec<(i64, String, String, String)>>,
    problems: Vec<PayloadProblem>,
    raised: Option<FoldError>,
}

/// The log as `log verify` reads it: each row once, the chain verifier's
/// `visit` and the fold's replay fed the same row. As the batched verify
/// (`store::Batched`): `first`, the rekey rows and the log's end then
/// (`cap`) in one short read; then the rows by seq in batches of `batch`,
/// each its own statement, to `cap`, then on to the log's end as it
/// stands while more than a batch was appended (at most [`FOLLOW_ROUNDS`]
/// times); a rekey row past `cap` stops the pass with
/// [`StoreError::Changed`]. Then (when no transaction was open) one read
/// transaction is begun and the rest of the log read in it; it stays open
/// for the head file, the row count and the live tables, and
/// `one_read_pass` ends it. In a caller's transaction (WAL, a copy, the
/// one-snapshot pass after a change) every row is read in that one.
struct OneRead<'a, 'c> {
    store: &'a StoreFile,
    between: std::cell::RefCell<&'a mut dyn FnMut()>,
    /// Rows visited, and the first one's seq.
    visited: std::cell::Cell<usize>,
    first_seq: std::cell::Cell<Option<i64>>,
    /// When the final snapshot began (its hold is part of the longest).
    snapshot_began: std::cell::Cell<Option<std::time::Instant>>,
    applier: &'a Applier<'c>,
    batch: i64,
    cap: std::cell::Cell<i64>,
    rekeys: std::cell::RefCell<Option<Vec<LogRow>>>,
    feed: std::cell::RefCell<Feed>,
    opened: std::cell::Cell<bool>,
    /// The live store's shape, read just before the final snapshot.
    shape: std::cell::RefCell<Option<Result<Shape, FoldError>>>,
    longest_hold: std::cell::Cell<std::time::Duration>,
}

impl OneRead<'_, '_> {
    fn timed<T>(&self, body: impl FnOnce() -> T) -> T {
        let started = std::time::Instant::now();
        let out = body();
        let took = started.elapsed();
        if took > self.longest_hold.get() {
            self.longest_hold.set(took);
        }
        out
    }

    fn snapshot(&self) -> Result<Option<LogRow>, StoreError> {
        self.timed(|| {
            self.store.read_transaction(|store| {
                let cap: Option<i64> =
                    store
                        .conn()
                        .query_row("SELECT max(seq) FROM log", [], |r| r.get(0))?;
                self.cap.set(cap.unwrap_or(0));
                let first = LogSource::first(store)?;
                let mut out = Vec::new();
                store
                    .log_query(
                        "WHERE seq > ? AND seq <= ? AND kind = 'rekey' ORDER BY seq",
                        &[&0i64, &self.cap.get()],
                        &mut |row| {
                            if row.kind == crate::log::REKEY {
                                out.push(row);
                            }
                            Ok(true)
                        },
                    )?
                    .map_err(StoreError::Py)?;
                *self.rekeys.borrow_mut() = Some(out);
                Ok(first)
            })?
        })
    }

    /// The rows after `after`, at most `batch`, each with whether its
    /// payload is text.
    fn read(&self, after: i64) -> Result<Vec<(LogRow, bool)>, StoreError> {
        self.timed(|| {
            let mut statement = self
                .store
                .conn()
                .prepare(&format!("{LOG_SELECT} WHERE seq > ? ORDER BY seq LIMIT ?"))?;
            let mut rows = statement.query(rusqlite::params![after, self.batch])?;
            let mut out = Vec::new();
            while let Some(row) = rows.next()? {
                let text = matches!(row.get_ref(4)?, ValueRef::Text(_));
                out.push((log_row(row)?, text));
            }
            Ok(out)
        })
    }

    /// Hand a row to the fold: a mutation row is kept, and applied with
    /// its batch when the batch is full, unless it was read in the final
    /// snapshot (`defer`: applied after the snapshot ends).
    fn take(&self, row: &LogRow, text: bool, defer: bool) {
        let mut feed = self.feed.borrow_mut();
        if feed.raised.is_some() || !is_mutation(&row.kind) {
            return;
        }
        if !text {
            // A mutation kind whose payload is not text: `json.loads`
            // of it raises.
            feed.raised = Some(FoldError::Raised("TypeError"));
            return;
        }
        feed.pending.push((
            row.seq,
            row.kind.clone(),
            row.payload.clone(),
            row.mac.clone(),
        ));
        if feed.pending.len() >= FOLD_BATCH {
            let rows = std::mem::take(&mut feed.pending);
            if defer {
                feed.tail.push(rows);
            } else {
                let Feed {
                    problems, raised, ..
                } = &mut *feed;
                let batch = Batch {
                    rows,
                    read: 0,
                    last_seq: 0,
                };
                if let Err(e) = apply_batch(self.applier, batch, problems) {
                    *raised = Some(e);
                }
            }
        }
    }
}

impl LogSource for OneRead<'_, '_> {
    type Error = StoreError;

    fn first(&self) -> Result<Option<LogRow>, StoreError> {
        self.snapshot()
    }

    fn rekeys(&self) -> Result<Vec<LogRow>, StoreError> {
        if self.rekeys.borrow().is_none() {
            self.snapshot()?;
        }
        Ok(self.rekeys.borrow_mut().take().unwrap_or_default())
    }

    fn at_seq(&self, seq: i64) -> Result<Option<LogRow>, StoreError> {
        self.timed(|| self.store.at_seq(seq))
    }

    fn each(
        &self,
        visit: &mut dyn FnMut(&LogRow) -> Result<(), PyError>,
    ) -> Result<Result<(), PyError>, StoreError> {
        let conn = self.store.conn();
        let mut after = 0i64;
        let mut in_snapshot = !conn.is_autocommit();
        // Where the batched phase reads to: the log's end when the pass
        // began, then (at most FOLLOW_ROUNDS times) its end as it stands,
        // while more than a batch was appended meanwhile.
        let mut target = self.cap.get();
        let mut rounds = 0usize;
        loop {
            if !in_snapshot && after >= target {
                let end: Option<i64> = if rounds < FOLLOW_ROUNDS {
                    rounds += 1;
                    self.timed(|| conn.query_row("SELECT max(seq) FROM log", [], |r| r.get(0)))?
                } else {
                    None
                };
                match end {
                    Some(end) if end - after > self.batch => target = end,
                    _ => in_snapshot = true,
                }
            }
            if in_snapshot && conn.is_autocommit() {
                // The final snapshot: the shape first (outside it), then
                // the rest of the log in it.
                *self.shape.borrow_mut() = Some(read_shape(conn));
                conn.execute_batch("BEGIN")?;
                self.opened.set(true);
                self.snapshot_began.set(Some(std::time::Instant::now()));
            }
            let rows = self.read(after)?;
            let short = (rows.len() as i64) < self.batch;
            let defer = self.opened.get();
            if !self.opened.get() {
                // Outside a snapshot this pass holds: a test's edit.
                (self.between.borrow_mut())();
            }
            for (row, text) in &rows {
                if row.seq > self.cap.get() && row.kind == crate::log::REKEY {
                    return Err(StoreError::Changed);
                }
                if self.first_seq.get().is_none() {
                    self.first_seq.set(Some(row.seq));
                }
                self.visited.set(self.visited.get() + 1);
                if let Err(e) = visit(row) {
                    return Ok(Err(e));
                }
                self.take(row, *text, defer);
            }
            if let Some((row, _)) = rows.last() {
                after = row.seq;
            }
            if short {
                if in_snapshot {
                    break;
                }
                in_snapshot = true;
            }
        }
        // The last partial batch.
        let mut feed = self.feed.borrow_mut();
        let rows = std::mem::take(&mut feed.pending);
        if !rows.is_empty() {
            if self.opened.get() {
                feed.tail.push(rows);
            } else {
                let Feed {
                    problems, raised, ..
                } = &mut *feed;
                if raised.is_none() {
                    let batch = Batch {
                        rows,
                        read: 0,
                        last_seq: 0,
                    };
                    if let Err(e) = apply_batch(self.applier, batch, problems) {
                        *raised = Some(e);
                    }
                }
            }
        }
        Ok(Ok(()))
    }
}

/// Compare the replay with the live tables: the fold report.
fn compare(
    scratch: &Scratch,
    known: &[Vec<String>],
    live: Live,
    problems: Vec<PayloadProblem>,
) -> Result<FoldReport, FoldError> {
    scratch
        .conn
        .execute_batch("COMMIT")
        .map_err(StoreError::from)?;
    live.present?;
    let drift = live.fingerprint? != Some(unicode::cps(&lexicon::python_tokenizer_fingerprint()));
    if !drift {
        fill_index_text(&scratch.conn)?;
    }
    let mut diverged = !problems.is_empty();
    let mut tables = Vec::new();
    let mut membership = Vec::new();
    for ((table, known), live_rows) in FOLDED_TABLES
        .iter()
        .chain(MEMBERSHIP_TABLES.iter())
        .zip(known)
        .zip(live.tables)
    {
        let live_rows = live_rows?;
        let skip: &[&str] = if drift && *table == "memories" {
            &INDEX_TEXT_COLUMNS
        } else {
            &[]
        };
        let folded = table_rows(&scratch.conn, table, known, known, skip)?;
        let unaccounted = sorted(
            live_rows
                .entries
                .iter()
                .filter(|(k, v)| folded.get(k).is_none_or(|f| f != v))
                .map(|(k, _)| Label::of(k))
                .collect(),
        );
        let missing = sorted(
            folded
                .entries
                .iter()
                .filter(|(k, _)| live_rows.get(k).is_none())
                .map(|(k, _)| Label::of(k))
                .collect(),
        );
        if !unaccounted.is_empty() || !missing.is_empty() {
            diverged = true;
        }
        let report = TableFold {
            table,
            unaccounted,
            missing,
            rows: live_rows.entries.len(),
        };
        if FOLDED_TABLES.contains(table) {
            tables.push(report);
        } else {
            membership.push(report);
        }
    }
    Ok(FoldReport {
        status: if diverged { "diverged" } else { "ok" },
        tables,
        membership,
        problems,
    })
}

/// Parse a batch's payloads on every core (up to 8), then apply them in
/// seq order. The payload text is not kept beside its value: a refused
/// one is read again for its message.
fn apply_batch(
    applier: &Applier<'_>,
    batch: Batch,
    problems: &mut Vec<PayloadProblem>,
) -> Result<(), FoldError> {
    let rows = batch.rows;
    let threads = std::thread::available_parallelism()
        .map_or(1, |n| n.get())
        .min(8);
    let chunk = rows.len().div_ceil(threads).max(1);
    let parsed: Vec<Result<PyValue, LoadError>> = std::thread::scope(|scope| {
        let handles: Vec<_> = rows
            .chunks(chunk)
            .map(|part| {
                scope.spawn(move || {
                    part.iter()
                        .map(|(_, _, payload, _)| json::loads_detailed(&unicode::cps(payload)))
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        handles
            .into_iter()
            .flat_map(|h| h.join().expect("a parsing thread"))
            .collect()
    });
    for ((seq, kind, payload, mac), value) in rows.iter().zip(parsed) {
        let outcome = match value {
            Ok(value @ PyValue::Dict(_)) => applier.apply(kind, &value, mac),
            Ok(_) => Err(Fail::Caught(
                "ValueError",
                "payload is not an object".into(),
            )),
            Err(LoadError::Recursion) => Err(Fail::Raised("RecursionError")),
            Err(_) => {
                let text = unicode::cps(payload);
                Err(match json::decode_failure(&text) {
                    Some(json::DecodeFailure::Recursion) => Fail::Raised("RecursionError"),
                    Some(f) => Fail::Caught("JSONDecodeError", f.message(&text)),
                    None => Fail::Raised("JSONDecodeError"),
                })
            }
        };
        match outcome {
            Ok(()) => {}
            Err(Fail::Caught(_, message)) => problems.push(PayloadProblem {
                seq: *seq,
                kind: kind.clone(),
                detail: first_line(&message),
            }),
            Err(Fail::Raised(class)) => return Err(FoldError::Raised(class)),
        }
    }
    Ok(())
}

/// Whether `kind` is in `MUTATION_KINDS`.
pub fn is_mutation(kind: &str) -> bool {
    const RECORD: [&str; 7] = [
        "memory",
        "tombstone",
        "episode",
        "verification",
        "conflict",
        "import",
        "quarantine",
    ];
    let Some(table) = kind
        .strip_suffix("_put")
        .or_else(|| kind.strip_suffix("_delete"))
    else {
        return false;
    };
    RECORD.contains(&table) || SPECS.iter().any(|s| s.kind == table)
}

/// One problem of the merged report.
#[derive(Clone, Debug, PartialEq)]
pub enum Problem {
    Chain(ReportDict),
    Payload(PayloadProblem),
}

/// `log_verify()`'s report, `path` aside.
#[derive(Clone, Debug, PartialEq)]
pub struct LogVerify {
    pub status: &'static str,
    pub store_id: String,
    pub key_fingerprint: String,
    pub chain: ChainReport,
    /// Sorted by `(seq, problem)`, stable: the chain's first.
    pub problems: Vec<Problem>,
    pub fold: FoldReport,
}

impl LogVerify {
    pub fn unaccounted_ids(&self) -> &[Label] {
        self.fold.unaccounted_ids()
    }
}

/// The merge `log_verify` makes of the chain report and the fold.
pub fn merge(
    chain: ChainReport,
    fold: FoldReport,
    store_id: &str,
    key_fingerprint: &str,
) -> Result<LogVerify, FoldError> {
    let mut problems: Vec<(i128, String, Problem)> = Vec::new();
    for p in &chain.problems {
        let seq = match p.iter().find(|(k, _)| *k == "seq").map(|(_, v)| v) {
            Some(ReportValue::Int(i)) => i128::from(*i),
            Some(ReportValue::BigInt(t)) => {
                t.parse().map_err(|_| FoldError::Raised("ValueError"))?
            }
            Some(ReportValue::Str(s)) => {
                let parsed =
                    pyvalue::int_from_str(s).map_err(|_| FoldError::Raised("ValueError"))?;
                let PyValue::Int(t) = &parsed else {
                    return Err(FoldError::Raised("ValueError"));
                };
                t.parse().map_err(|_| FoldError::Raised("ValueError"))?
            }
            Some(ReportValue::Null) => return Err(FoldError::Raised("TypeError")),
            None => return Err(FoldError::Raised("KeyError")),
        };
        let problem = match p.iter().find(|(k, _)| *k == "problem").map(|(_, v)| v) {
            Some(ReportValue::Str(s)) => text(s.clone()),
            Some(ReportValue::Int(i)) => i.to_string(),
            Some(ReportValue::BigInt(t)) => t.clone(),
            Some(ReportValue::Null) => "None".into(),
            None => return Err(FoldError::Raised("KeyError")),
        };
        problems.push((seq, problem, Problem::Chain(p.clone())));
    }
    for p in &fold.problems {
        problems.push((
            i128::from(p.seq),
            "payload_invalid".into(),
            Problem::Payload(p.clone()),
        ));
    }
    problems.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
    let status = if fold.status != "ok" {
        "tampered"
    } else {
        chain.status
    };
    Ok(LogVerify {
        status,
        store_id: store_id.to_owned(),
        key_fingerprint: key_fingerprint.to_owned(),
        chain,
        problems: problems.into_iter().map(|(_, _, p)| p).collect(),
        fold,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mutation_kinds() {
        assert!(is_mutation("memory_put"));
        assert!(is_mutation("native_member_delete"));
        assert!(!is_mutation("index_rebuilt"));
        assert!(!is_mutation("rekey"));
    }

    #[test]
    fn first_lines() {
        assert_eq!(first_line("a\nb"), "a");
        assert_eq!(first_line(&"x".repeat(300)).len(), 200);
    }

    #[test]
    fn encode_errors() {
        assert_eq!(
            encode_error(&[0x61, 0xDC00, 0xD800]).unwrap(),
            "'utf-8' codec can't encode characters in position 1-2: surrogates not allowed"
        );
    }
}
