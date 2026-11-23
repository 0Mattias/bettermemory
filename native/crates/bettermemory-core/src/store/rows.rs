//! `store_rows`'s shared leaves: the membership table specs and their row
//! checks (`_MEMBERSHIP_SPECS`, `_membership_values`, `_membership_key`,
//! `_membership_spec`, `_membership_column`), the upsert statement
//! (`_upsert_sql`), the link rows (`_sync_links`), the batch row sort
//! (`_sort_rows`), the schema's columns (`_known_columns`), a
//! verification row read back (`_verification_row`), a log row as a
//! telemetry event (`_event_from_columns`, `Store._event_from_raw`) and
//! the kind sets of `store_schema` and `log` that decide which rows are
//! telemetry.

use std::cmp::Ordering;
use std::sync::OnceLock;

use rusqlite::Connection;

use super::fold::{json_text, FoldError};
use super::{SqlValue, StoreError};
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::records::rows::{load_json, Row};
use crate::records::RecordError;
use crate::unicode;

/// One `_MembershipTable`.
pub struct Spec {
    pub table: &'static str,
    pub kind: &'static str,
    pub key: &'static [&'static str],
    pub columns: &'static [&'static str],
    pub optional: &'static [&'static str],
    pub floats: &'static [&'static str],
}

pub const SPECS: [Spec; 14] = [
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

pub(crate) const VERIFICATION_LISTS: [&str; 4] = [
    "verified_paths",
    "verified_commits",
    "verified_versions",
    "verified_absent_paths",
];

pub fn upsert(table: &str, columns: &[&str], key: &str) -> String {
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

/// What applying a row raised: an exception `refold` catches, with
/// `str(exc)`, or one it lets out.
#[derive(Clone, Debug, PartialEq)]
pub enum Fail {
    Caught(&'static str, String),
    Raised(&'static str),
}

pub(crate) fn key_error(name: &str) -> Fail {
    Fail::Caught(
        "KeyError",
        unicode::to_string(&repr_str(&unicode::cps(name))).unwrap_or_default(),
    )
}

pub(crate) fn text(s: PyStr) -> String {
    unicode::to_string(&s).unwrap_or_default()
}

pub(crate) fn item<'a>(payload: &'a PyValue, name: &str) -> Result<&'a PyValue, Fail> {
    payload.get(name).ok_or_else(|| key_error(name))
}

pub(crate) fn get<'a>(payload: &'a PyValue, name: &str) -> &'a PyValue {
    static NONE: PyValue = PyValue::None;
    payload.get(name).unwrap_or(&NONE)
}

/// `_membership_values`.
pub fn membership_values(spec: &Spec, row: &PyValue) -> Result<Vec<PyValue>, Fail> {
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
pub(crate) fn membership_key(spec: &Spec, payload: &PyValue) -> Result<Vec<PyValue>, Fail> {
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

pub(crate) fn columns_of(conn: &Connection, table: &str) -> Result<Vec<String>, FoldError> {
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

/// `_sync_links`: replaces the link rows of `source_id` with `links`
/// (type, target, note), exact duplicates collapsed in first-seen order.
/// `exec` runs one statement with its bound values.
pub fn sync_links<E>(
    source_id: &PyValue,
    links: impl IntoIterator<Item = [PyValue; 3]>,
    mut exec: impl FnMut(&str, &[PyValue]) -> Result<(), E>,
) -> Result<(), E> {
    exec(
        "DELETE FROM memory_links WHERE source_id = ?",
        std::slice::from_ref(source_id),
    )?;
    let mut seen: Vec<[PyValue; 4]> = Vec::new();
    for [kind, target, note] in links {
        let key = [source_id.clone(), kind, target, note];
        if !seen.contains(&key) {
            seen.push(key);
        }
    }
    for key in &seen {
        exec(
            "INSERT OR IGNORE INTO memory_links(source_id, type, target_id, note) \
             VALUES (?, ?, ?, ?)",
            key,
        )?;
    }
    Ok(())
}

/// `_membership_spec`.
pub fn membership_spec(kind: &[u32]) -> Result<&'static Spec, Fail> {
    unicode::to_string(kind)
        .and_then(|k| SPECS.iter().find(|s| s.kind == k))
        .ok_or_else(|| {
            Fail::Caught(
                "ValueError",
                format!("{} is not a membership row kind", text(repr_str(kind))),
            )
        })
}

/// `_MEMBERSHIP_BY_TABLE`.
pub fn membership_spec_by_table(table: &str) -> Option<&'static Spec> {
    SPECS.iter().find(|s| s.table == table)
}

/// `_membership_column`.
pub fn membership_column(spec: &Spec, column: &[u32]) -> Result<(), Fail> {
    if unicode::to_string(column).is_some_and(|c| spec.columns.contains(&c.as_str())) {
        return Ok(());
    }
    Err(Fail::Caught(
        "ValueError",
        format!("{} has no column {}", spec.table, text(repr_str(column))),
    ))
}

/// `_iso_opt`: `dt.isoformat()`, or None for None.
pub fn iso_opt(dt: Option<&crate::datetime::DateTime>) -> Option<String> {
    dt.map(crate::datetime::DateTime::isoformat)
}

/// A sort key: `(value is not None, value or 0)`.
type Key = (bool, SqlValue);

fn type_name(v: &SqlValue) -> &'static str {
    match v {
        SqlValue::Null => "NoneType",
        SqlValue::Integer(_) => "int",
        SqlValue::Real(_) => "float",
        SqlValue::Text(_) => "str",
        SqlValue::Blob(_) => "bytes",
    }
}

/// An int against a float, exactly, as Python compares them.
fn int_float(i: i64, f: f64) -> Option<Ordering> {
    if f.is_nan() {
        return None;
    }
    if f >= 9_223_372_036_854_775_808.0 {
        return Some(Ordering::Less);
    }
    if f < -9_223_372_036_854_775_808.0 {
        return Some(Ordering::Greater);
    }
    let whole = f.trunc();
    // In range: `whole` is an integer that fits i64.
    #[allow(clippy::cast_possible_truncation)]
    let t = whole as i64;
    Some(
        i.cmp(&t)
            .then_with(|| 0.0f64.partial_cmp(&(f - whole)).unwrap_or(Ordering::Equal)),
    )
}

/// Python's comparison of two values of comparable types (None when it
/// is unordered: a NaN), or the TypeError of `<` across types.
fn compare(a: &SqlValue, b: &SqlValue) -> Result<Option<Ordering>, Fail> {
    use SqlValue::{Blob, Integer, Real, Text};
    Ok(match (a, b) {
        (Integer(x), Integer(y)) => Some(x.cmp(y)),
        (Real(x), Real(y)) => x.partial_cmp(y),
        (Integer(x), Real(y)) => int_float(*x, *y),
        (Real(x), Integer(y)) => int_float(*y, *x).map(Ordering::reverse),
        (Text(x), Text(y)) => Some(x.as_bytes().cmp(y.as_bytes())),
        (Blob(x), Blob(y)) => Some(x.cmp(y)),
        _ => {
            return Err(Fail::Caught(
                "TypeError",
                format!(
                    "'<' not supported between instances of '{}' and '{}'",
                    type_name(a),
                    type_name(b)
                ),
            ))
        }
    })
}

/// `x < y` over two keys, as Python compares tuples: the first items
/// that differ (by `==`, which never raises) decide, by `<`.
fn key_lt(x: &Key, y: &Key) -> Result<bool, Fail> {
    if x.0 != y.0 {
        return Ok(!x.0 & y.0);
    }
    // Across types `==` is False and `<` raises.
    Ok(compare(&x.1, &y.1)? == Some(Ordering::Less))
}

/// `_sort_rows`: one stable sort per name, the last name first, by
/// `(value is not None, value or 0)`, descending for a name led by `-`
/// (all its leading `-` stripped). Descending is `reverse=True`: the
/// list reversed, sorted ascending and reversed back, so equal rows keep
/// their order. A row without the column is KeyError, raised while the
/// keys are made, before any comparison; values Python cannot order
/// against each other are its TypeError. On a refusal every row is still
/// there, in the order the names before the refusing one left them (the
/// reference leaves its list part sorted: not compared).
pub fn sort_rows(rows: &mut Vec<Row>, order_by: &[&str]) -> Result<(), Fail> {
    for name in order_by.iter().rev() {
        let column = name.trim_start_matches('-');
        let mut keyed = Vec::with_capacity(rows.len());
        for (i, row) in rows.iter().enumerate() {
            let value = row.get(column).ok_or_else(|| key_error(column))?;
            let key = match value {
                SqlValue::Null => (false, SqlValue::Integer(0)),
                v => (true, v.clone()),
            };
            keyed.push((key, i));
        }
        let reverse = name.starts_with('-');
        if reverse {
            keyed.reverse();
        }
        // On a refusal the rows stay as this name's sort found them.
        crate::pyvalue::list_sort::sort_by(&mut keyed, |a, b| key_lt(&a.0, &b.0))?;
        if reverse {
            keyed.reverse();
        }
        let mut old: Vec<Option<Row>> = std::mem::take(rows).into_iter().map(Some).collect();
        *rows = keyed
            .into_iter()
            .map(|(_, i)| old[i].take().expect("each row once"))
            .collect();
    }
    Ok(())
}

/// `_known_columns`: every table of an in-memory database built from
/// `SCHEMA` and then `MEMBERSHIP_SCHEMA` (the first two of the pinned
/// schema texts), in `sqlite_master`'s order, with its columns in the
/// schema's order.
pub fn known_columns() -> &'static [(String, Vec<String>)] {
    static KNOWN: OnceLock<Vec<(String, Vec<String>)>> = OnceLock::new();
    KNOWN.get_or_init(|| read_known().expect("the pinned schema builds"))
}

fn read_known() -> Result<Vec<(String, Vec<String>)>, FoldError> {
    let conn = Connection::open_in_memory().map_err(StoreError::from)?;
    for text in super::admission::schema_texts().iter().take(2) {
        conn.execute_batch(text).map_err(StoreError::from)?;
    }
    let tables: Vec<String> = {
        let mut statement = conn
            .prepare("SELECT name FROM sqlite_master WHERE type = 'table'")
            .map_err(StoreError::from)?;
        let names = statement
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(StoreError::from)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(StoreError::from)?;
        names
    };
    tables
        .into_iter()
        .map(|t| columns_of(&conn, &t).map(|c| (t, c)))
        .collect()
}

/// A value `_verification_row` puts in its dict: a column as sqlite3
/// gives it, or a value it decoded.
#[derive(Clone, Debug, PartialEq)]
pub enum Field {
    Column(SqlValue),
    Py(PyValue),
}

/// `_verification_row`: the four key columns as read, the four lists
/// (`_load_json` of each `*_json` column, `[]` for null), then the path
/// targets the row's attestation carries. A column the row lacks is
/// IndexError.
pub fn verification_row(row: &Row) -> Result<Vec<(&'static str, Field)>, RecordError> {
    let at = |name: &str| row.item(name).ok_or(RecordError::Raised("IndexError"));
    let mut out = Vec::new();
    for name in ["memory_id", "machine_id", "verified_at", "verified_head"] {
        out.push((name, Field::Column(at(name)?.clone())));
    }
    for name in VERIFICATION_LISTS {
        let value = at(&format!("{name}_json"))?;
        out.push((
            name,
            Field::Py(load_json(value, PyValue::List(Vec::new()))?),
        ));
    }
    for (name, value) in path_targets(row)? {
        out.push((name, Field::Py(value)));
    }
    Ok(out)
}

/// `store_rows._path_targets`, whole: the target fields of the record the
/// row's `attestation_payload` carries, as parsed. The id check is made
/// only when the row names an `id`. `records::rows`' copy serves the
/// record readers, whose rows always name one, and keeps only the first
/// level of each map (all the model reads), so it is not reused here.
fn path_targets(row: &Row) -> Result<Vec<(&'static str, PyValue)>, RecordError> {
    // `in row.keys()` is exact; `row[name]` is sqlite3.Row's lookup.
    let payload = match row.item("attestation_payload") {
        _ if !row.has_key("attestation_payload") => return Ok(Vec::new()),
        None | Some(SqlValue::Null) => return Ok(Vec::new()),
        Some(v) => match load_json(v, PyValue::None) {
            Ok(p) => p,
            Err(RecordError::Raised(
                "JSONDecodeError" | "UnicodeDecodeError" | "ValueError" | "RecursionError",
            )) => return Ok(Vec::new()),
            Err(e) => return Err(e),
        },
    };
    let field = |v: &PyValue, name: &str| -> Option<PyValue> {
        let PyValue::Dict(items) = v else { return None };
        let name = unicode::cps(name);
        items
            .iter()
            .find(|(k, _)| matches!(k, PyValue::Str(s) if *s == name))
            .map(|(_, v)| v.clone())
    };
    if !matches!(payload, PyValue::Dict(_)) {
        return Ok(Vec::new());
    }
    let record = field(&payload, "memory")
        .or_else(|| field(&payload, "tombstone"))
        .unwrap_or_else(|| payload.clone());
    if !matches!(record, PyValue::Dict(_)) {
        return Ok(Vec::new());
    }
    if let Some(id) = row.item("id").filter(|_| row.has_key("id")) {
        if !json_eq_column(&field(&record, "id").unwrap_or(PyValue::None), id) {
            return Ok(Vec::new());
        }
    }
    Ok(["verified_path_targets", "verified_absent_path_targets"]
        .into_iter()
        .filter_map(|name| field(&record, name).map(|v| (name, v)))
        .collect())
}

/// Python's `==` between a parsed JSON value and a column value.
fn json_eq_column(a: &PyValue, b: &SqlValue) -> bool {
    let number = match a {
        PyValue::Bool(x) => Some(SqlValue::Integer(i64::from(*x))),
        PyValue::Int(digits) => match (digits.parse::<i64>(), b) {
            (Ok(i), _) => Some(SqlValue::Integer(i)),
            // Past i64: equal only to a float of exactly that value.
            (Err(_), SqlValue::Real(f)) => {
                return f.is_finite() && f.fract() == 0.0 && format!("{f:.0}") == *digits
            }
            (Err(_), _) => return false,
        },
        PyValue::Float(f) => Some(SqlValue::Real(*f)),
        _ => None,
    };
    match (a, b) {
        (PyValue::None, SqlValue::Null) => true,
        (PyValue::Str(s), SqlValue::Text(t)) => *s == unicode::cps(t),
        _ => match (number, b) {
            (Some(x), SqlValue::Integer(_) | SqlValue::Real(_)) => {
                matches!(compare(&x, b), Ok(Some(Ordering::Equal)))
            }
            _ => false,
        },
    }
}

/// `store_schema.MUTATION_KINDS`: `<table>_put` and `<table>_delete` of
/// the record tables and the membership tables.
pub const MUTATION_TABLES: [&str; 21] = [
    "memory",
    "tombstone",
    "episode",
    "verification",
    "conflict",
    "import",
    "quarantine",
    "machine",
    "project",
    "project_witness",
    "project_blob",
    "project_remote",
    "project_file",
    "declared_remote",
    "checkout",
    "checkout_view",
    "home",
    "proposal",
    "standing",
    "returned",
    "native_member",
];

/// Whether `kind` is in `MUTATION_KINDS`.
pub fn is_mutation(kind: &str) -> bool {
    kind.strip_suffix("_put")
        .or_else(|| kind.strip_suffix("_delete"))
        .is_some_and(|table| MUTATION_TABLES.contains(&table))
}

/// Whether `kind` is in `MEMBERSHIP_MUTATION_KINDS` (the membership
/// tables' `_put` and `_delete`).
pub fn is_membership_mutation(kind: &str) -> bool {
    kind.strip_suffix("_put")
        .or_else(|| kind.strip_suffix("_delete"))
        .is_some_and(|table| MUTATION_TABLES[7..].contains(&table))
}

/// `log.CONTROL_KINDS`, all four of its assignments together.
pub const CONTROL_KINDS: [&str; 13] = [
    "store_created",
    "rekey",
    "migrate_v8",
    "capture_open",
    "capture_member",
    "capture_close",
    "capture_undo",
    "capture_relations",
    "index_rebuilt",
    "store_restored",
    "history_accepted",
    "key_imported",
    "key_adopted",
];

/// `store_schema.MEMBERSHIP_EVENT_KINDS`.
pub const MEMBERSHIP_EVENT_KINDS: [&str; 30] = [
    "resolution_indeterminate",
    "resolution_deferred",
    "resolution_session",
    "resolution_joined",
    "resolution_new",
    "proposal_opened",
    "proposal_answered",
    "checkout_registered",
    "checkout_moved",
    "checkout_view_added",
    "checkout_rekeyed",
    "checkout_object_refreshed",
    "checkout_retired",
    "token_superseded",
    "token_reminted",
    "remote_declared",
    "project_minted",
    "project_merged",
    "project_absorbed",
    "project_unmerged",
    "project_file_bound",
    "project_reparented",
    "machine_cloned",
    "machine_facts_updated",
    "witness_put",
    "witness_released",
    "import_homed",
    "recall_suppressed",
    "membership_migrated",
    "store_forked",
];

/// Whether `kind` is in `store_schema._NOT_TELEMETRY`
/// (`MUTATION_KINDS | CONTROL_KINDS | MEMBERSHIP_EVENT_KINDS`).
pub fn not_telemetry(kind: &str) -> bool {
    is_mutation(kind) || CONTROL_KINDS.contains(&kind) || MEMBERSHIP_EVENT_KINDS.contains(&kind)
}

/// `_event_from_columns`: a log row's columns as the v8 event (`ts`,
/// `session` when not None, `kind`, then the payload's other keys in its
/// order), or None for a kind that is not telemetry and for a payload
/// that is not a JSON object. `json.loads` refusing the payload with a
/// ValueError (a JSONDecodeError, a UnicodeDecodeError of bytes, too
/// many digits) is None; TypeError (null, a number) and RecursionError
/// propagate.
pub fn event_from_columns(
    ts: &[u32],
    session: Option<&[u32]>,
    kind: &[u32],
    payload: &SqlValue,
) -> Result<Option<PyValue>, RecordError> {
    if unicode::to_string(kind).is_some_and(|k| not_telemetry(&k)) {
        return Ok(None);
    }
    if matches!(payload, SqlValue::Null) {
        return Err(RecordError::Raised("TypeError"));
    }
    let fields = match load_json(payload, PyValue::None) {
        Ok(v) => v,
        Err(RecordError::Raised("JSONDecodeError" | "UnicodeDecodeError" | "ValueError")) => {
            return Ok(None)
        }
        Err(e) => return Err(e),
    };
    let PyValue::Dict(items) = &fields else {
        return Ok(None);
    };
    let mut event = vec![(PyValue::str("ts"), PyValue::Str(ts.to_vec()))];
    if let Some(session) = session {
        event.push((PyValue::str("session"), PyValue::Str(session.to_vec())));
    }
    event.push((PyValue::str("kind"), PyValue::Str(kind.to_vec())));
    for (key, value) in items {
        let reserved = matches!(key, PyValue::Str(k)
            if ["ts", "session", "kind"].iter().any(|r| unicode::cps(r) == *k));
        if !reserved {
            event.push((key.clone(), value.clone()));
        }
    }
    Ok(Some(PyValue::Dict(event)))
}

/// `store_schema.IMPORTED_FROM_V8`.
pub const IMPORTED_FROM_V8: &str = "v8";

/// `store_rows._ROW_COLUMNS`: the fields an event keeps in the row's own
/// columns.
pub const ROW_COLUMNS: [&str; 3] = ["ts", "session", "kind"];

/// What `event_import_payload` answers: `(ts, session, kind, payload)`.
pub type ImportedEvent = (Option<PyStr>, Option<PyStr>, PyStr, Vec<(PyValue, PyValue)>);

/// `event_import_payload(event, imported_from=...)`, with
/// `credentials.secret_spans` passed as `spans`: the row a v8 event
/// becomes. `Err` is the `ValueError`'s text: a kind that is not a
/// non-empty `str`, or one in `_NOT_TELEMETRY` (by its `repr`). A `ts` or
/// `session` that is not a non-empty `str` is None; every field but the
/// three columns goes through `_redact_event_fields`, then
/// `imported_from` is set (in place when the event held that key).
pub fn event_import_payload(
    event: &[(PyValue, PyValue)],
    imported_from: &[u32],
    spans: crate::event_redact::Spans,
) -> Result<ImportedEvent, PyStr> {
    let get = |name: &str| {
        let name = unicode::cps(name);
        event.iter().find_map(|(k, v)| match k {
            PyValue::Str(s) if *s == name => Some(v),
            _ => None,
        })
    };
    let kind = match get("kind") {
        Some(PyValue::Str(k)) if !k.is_empty() => k.clone(),
        _ => return Err(unicode::cps("an event needs a string kind")),
    };
    if unicode::to_string(&kind).is_some_and(|k| not_telemetry(&k)) {
        let mut text = repr_str(&kind);
        text.extend(unicode::cps(" is a mutation or control kind, not an event"));
        return Err(text);
    }
    let text_of = |v: Option<&PyValue>| match v {
        Some(PyValue::Str(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    };
    let rest: Vec<(PyValue, PyValue)> = event
        .iter()
        .filter(|(k, _)| !matches!(k, PyValue::Str(s) if ROW_COLUMNS.iter().any(|c| unicode::cps(c) == *s)))
        .cloned()
        .collect();
    let mut payload = crate::event_redact::redact_event_fields(&rest, spans);
    let from = PyValue::Str(imported_from.to_vec());
    match payload
        .iter_mut()
        .find(|(k, _)| matches!(k, PyValue::Str(s) if *s == unicode::cps("imported_from")))
    {
        Some(slot) => slot.1 = from,
        None => payload.push((PyValue::str("imported_from"), from)),
    }
    Ok((text_of(get("ts")), text_of(get("session")), kind, payload))
}

/// `Store._event_from_raw`: `str()` of the row's `ts` and `kind`, of
/// `session` when not null, and the payload as read.
pub fn event_from_raw(raw: &Row) -> Result<Option<PyValue>, RecordError> {
    let at = |name: &str| raw.item(name).ok_or(RecordError::Raised("IndexError"));
    let ts = unicode::cps(&at("ts")?.py_str());
    let session = match at("session")? {
        SqlValue::Null => None,
        v => Some(unicode::cps(&v.py_str())),
    };
    let kind = unicode::cps(&at("kind")?.py_str());
    let payload = at("payload")?;
    event_from_columns(&ts, session.as_deref(), &kind, payload)
}
