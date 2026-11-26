//! The `Store` methods of the membership tables (`store_membership.py`):
//! a put and a delete per table, the native row clear, the control rows of
//! design 8.10 and their log, the batched reads, the id counters,
//! `committed_seq`, `in_batch`, `wrote_record`, `membership_store_id` and
//! its reissue, with `store_native`'s `legacy_row` and (private here)
//! `row_key`, `native_rows` and `matches`. README, "The membership tables'
//! store methods".
//!
//! A write runs inside a [`Transaction`] (a nested entry, as `_transaction`
//! joins a batch) or on a [`WritableStore`] in a transaction of its own,
//! checked before it begins as the reference checks before `BEGIN`. A row
//! or key holding a lone surrogate is written as `store_native` writes it:
//! the legacy projection (`legacy_row`) where its key allows one, then a
//! `native_member` row. Reads take the connection; `membership_log` and
//! `reissue_membership_store_id` are here too.

use std::ops::ControlFlow;

use rusqlite::types::Value as Bound;
use rusqlite::{Connection, OptionalExtension};

use super::create::{Provider, MEMBERSHIP_STORE_ID};
use super::open::WritableStore;
use super::provenance::BATCH;
use super::rows::{
    membership_column, membership_key, membership_spec, membership_spec_by_table,
    membership_values, sort_rows, Fail, Spec, MEMBERSHIP_EVENT_KINDS, MUTATION_TABLES,
};
use super::tx::{Committed, Transaction};
use super::write::{has_surrogates, WriteError, WriteNotPorted};
use super::{sql_value, SqlValue};
use crate::json::{self, DumpOptions};
use crate::log::LogRow;
use crate::native_text::{encoded_json, utf8_text};
use crate::pyvalue::{int_from_str, repr_str, PyStr, PyValue};
use crate::records::rows::{load_json, Row};
use crate::unicode;

/// A Python mapping's items, in its order.
pub type Items = [(PyValue, PyValue)];

fn refused(class: &'static str, text: impl Into<String>) -> WriteError {
    WriteError::Refused {
        class,
        text: Some(text.into()),
    }
}

fn failed(f: Fail) -> WriteError {
    match f {
        Fail::Caught(class, text) => refused(class, text),
        Fail::Raised(class) => WriteError::Refused { class, text: None },
    }
}

/// sqlite3 raises its own class, whose text the fixtures do not compare.
fn sqlite_failed(class: &'static str) -> WriteError {
    WriteError::Refused { class, text: None }
}

fn shown(text: &[u32]) -> String {
    unicode::to_string(&repr_str(text)).unwrap_or_default()
}

fn column_text(column: &[u32]) -> String {
    // Checked against the spec first: ASCII.
    unicode::to_string(column).unwrap_or_default()
}

/// `store_native.require_native_writer`: meta's `min_writer_revision`
/// read as `int()`, at least 5, else StoreVersionError.
pub(crate) fn require_native_writer(conn: &Connection) -> Result<(), WriteError> {
    let value = conn
        .query_row(
            "SELECT value FROM meta WHERE key = 'min_writer_revision'",
            [],
            |r| Ok(sql_value(r.get_ref(0)?)),
        )
        .optional()?
        .transpose()
        .map_err(|_| sqlite_failed("OperationalError"))?;
    let low = match value {
        None => true,
        Some(v) => py_int(&sql_py(v)?)? < 5,
    };
    if low {
        return Err(refused(
            "StoreVersionError",
            "open this store as its owner to preserve native paths",
        ));
    }
    Ok(())
}

/// A value as `sqlite3` returns it.
fn sql_py(v: SqlValue) -> Result<PyValue, WriteError> {
    Ok(match v {
        SqlValue::Null => PyValue::None,
        SqlValue::Integer(i) => PyValue::int(i128::from(i)),
        SqlValue::Real(f) => PyValue::Float(f),
        SqlValue::Text(t) => PyValue::str(&t),
        SqlValue::Blob(_) => return Err(WriteError::NotPorted(WriteNotPorted::Value("bytes"))),
    })
}

/// `int(value)` within 128 bits.
fn py_int(v: &PyValue) -> Result<i128, WriteError> {
    let big = || WriteError::NotPorted(WriteNotPorted::Value("an int past 128 bits"));
    match v {
        PyValue::Bool(b) => Ok(i128::from(*b)),
        PyValue::Int(t) => t.parse().map_err(|_| big()),
        PyValue::Float(f) if f.is_nan() => {
            Err(refused("ValueError", "cannot convert float NaN to integer"))
        }
        PyValue::Float(f) if f.is_infinite() => Err(refused(
            "OverflowError",
            "cannot convert float infinity to integer",
        )),
        // Whole floats past 2**127 are beyond what this build carries.
        #[allow(clippy::cast_possible_truncation)]
        PyValue::Float(f) if f.abs() < 1.7e38 => Ok(f.trunc() as i128),
        PyValue::Str(s) => match int_from_str(s) {
            Ok(PyValue::Int(ref t)) => t.parse().map_err(|_| big()),
            _ => Err(refused(
                "ValueError",
                // `%.200R`: the repr's first 200 code points.
                format!(
                    "invalid literal for int() with base 10: {}",
                    unicode::to_string(&repr_str(s).into_iter().take(200).collect::<Vec<_>>())
                        .unwrap_or_default()
                ),
            )),
        },
        PyValue::Float(_) => Err(big()),
        other => Err(refused(
            "TypeError",
            format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                super::home::type_name(other)
            ),
        )),
    }
}

/// `store_native.row_key`: the key columns' values as `encoded_json`.
fn row_key(spec: &Spec, row: &PyValue) -> Result<String, WriteError> {
    let mut key = Vec::with_capacity(spec.key.len());
    for column in spec.key {
        key.push(
            row.get(column)
                .cloned()
                .ok_or_else(|| failed(rows_key_error(column)))?,
        );
    }
    let text =
        json::dumps(&PyValue::List(key), DumpOptions::SORTED_ASCII_COMPACT).map_err(|e| {
            WriteError::Refused {
                class: e.name(),
                text: None,
            }
        })?;
    Ok(unicode::to_string(&text).unwrap_or_default())
}

/// The Python frames `native_text.has_surrogates` may use when
/// `put_membership` or `delete_membership` is called from a module's top
/// level under CPython 3.13.13: the recursion limit (1000) less the
/// module's frame and the method's own. A deeper caller has fewer.
pub const SURROGATE_FRAMES: usize = 998;

/// `native_text.has_surrogates(value)` entered at Python frame `frame`,
/// counted from `put_membership`'s `has_surrogates(row)` as frame 1. Each
/// value takes its frame and the next (the generator over a str's
/// characters, `Mapping.__instancecheck__` for anything else, the
/// generator over a container's items); a container's keys and values are
/// two frames further. The walk is the reference's: a dict's key before
/// its value, the first surrogate ending it; RecursionError where it would
/// enter a frame past [`SURROGATE_FRAMES`].
fn surrogates_within(value: &PyValue, frame: usize) -> Result<bool, WriteError> {
    if frame + 1 > SURROGATE_FRAMES {
        return Err(refused(
            "RecursionError",
            "maximum recursion depth exceeded",
        ));
    }
    let inner = frame + 2;
    match value {
        PyValue::Str(s) => Ok(s.iter().any(|c| (0xD800..=0xDFFF).contains(c))),
        PyValue::Dict(items) => {
            for (k, v) in items {
                if surrogates_within(k, inner)? || surrogates_within(v, inner)? {
                    return Ok(true);
                }
            }
            Ok(false)
        }
        PyValue::List(items) | PyValue::Tuple(items) => {
            for v in items {
                if surrogates_within(v, inner)? {
                    return Ok(true);
                }
            }
            Ok(false)
        }
        _ => Ok(false),
    }
}

/// `store_native.legacy_row`, as `put_membership` calls it: None when a
/// key column's value holds a surrogate; else the row in its order with
/// each value that holds one replaced: a `_json` column's by `[]` for a
/// list or tuple and `{}` for anything else, a `name`, `line` or `reason`
/// str by its `utf8_text`, any other by None. Its walk counts frames as
/// [`SURROGATE_FRAMES`] does, `legacy_row` beside `has_surrogates(row)`
/// at frame 1 (each value at 2, a key column's at 3 under its generator),
/// so a value the put's own walk never reached (a surrogate in an earlier
/// column ends that walk) can raise RecursionError here.
///
/// Its domain is a row `put_membership`'s checks passed
/// (`_membership_values`): a dict of the table's columns named by str,
/// each key column non-empty text. Outside it the answer is not the
/// reference's (which raises KeyError for a missing key column and
/// TypeError for a row that is not a mapping).
pub fn legacy_row(spec: &Spec, row: &PyValue) -> Result<Option<PyValue>, WriteError> {
    let PyValue::Dict(items) = row else {
        return Ok(None);
    };
    for column in spec.key {
        if let Some(value) = row.get(column) {
            if surrogates_within(value, 3)? {
                return Ok(None);
            }
        }
    }
    let mut out = items.clone();
    for (column, value) in &mut out {
        if !surrogates_within(value, 2)? {
            continue;
        }
        let PyValue::Str(name) = &*column else {
            *value = PyValue::None;
            continue;
        };
        let name = unicode::to_string(name).unwrap_or_default();
        *value = match &*value {
            PyValue::List(_) | PyValue::Tuple(_) if name.ends_with("_json") => {
                PyValue::List(Vec::new())
            }
            _ if name.ends_with("_json") => PyValue::Dict(Vec::new()),
            PyValue::Str(text) if ["name", "line", "reason"].contains(&name.as_str()) => {
                PyValue::str(&utf8_text(text))
            }
            _ => PyValue::None,
        };
    }
    Ok(Some(PyValue::Dict(out)))
}

fn rows_key_error(column: &str) -> Fail {
    Fail::Caught("KeyError", shown(&unicode::cps(column)))
}

/// `store_native.native_rows`: each `native_membership_rows` row of the
/// spec's kind by its `key_text` (as stored), its row when it parses to an
/// object that passes the table's checks under its own key, else None. No
/// table: none.
fn native_rows(
    conn: &Connection,
    spec: &Spec,
) -> Result<Vec<(SqlValue, Option<PyValue>)>, WriteError> {
    if spec.kind == "native_member" {
        return Ok(Vec::new());
    }
    let statement =
        conn.prepare("SELECT key_text, row_text FROM native_membership_rows WHERE member_kind = ?");
    let mut statement = match statement {
        Err(e) if e.to_string() == "no such table: native_membership_rows" => return Ok(Vec::new()),
        other => other?,
    };
    let mut found = Vec::new();
    let mut rows = statement.query([spec.kind])?;
    while let Some(r) = rows.next()? {
        let read = |i| sql_value(r.get_ref(i)?).map_err(|_| sqlite_failed("OperationalError"));
        let (key, text) = (read(0)?, read(1)?);
        let row = match &text {
            SqlValue::Null => None,
            other => load_json(other, PyValue::None).ok(),
        }
        .filter(|row| matches!(row, PyValue::Dict(_)))
        .filter(|row| membership_values(spec, row).is_ok())
        .filter(|row| row_key(spec, row).is_ok_and(|k| SqlValue::Text(k) == key));
        found.push((key, row));
    }
    Ok(found)
}

/// Python's `==` over values JSON and SQLite give.
fn py_eq(a: &PyValue, b: &PyValue) -> bool {
    use PyValue::{Dict, List, Tuple};
    match (a, b) {
        (List(x), List(y)) | (Tuple(x), Tuple(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_eq(p, q))
        }
        (Dict(x), Dict(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(k, v)| y.iter().any(|(j, w)| py_eq(k, j) && py_eq(v, w)))
        }
        _ => match (number(a), number(b)) {
            (Some(x), Some(y)) => x == y,
            _ => a == b,
        },
    }
}

/// A number as exact decimal text (an integral float spelled as its int),
/// or a float that is not integral.
#[derive(PartialEq)]
enum Num {
    Whole(String),
    Frac(f64),
}

fn number(v: &PyValue) -> Option<Num> {
    Some(match v {
        PyValue::Bool(b) => Num::Whole(u8::from(*b).to_string()),
        PyValue::Int(t) => Num::Whole(t.clone()),
        PyValue::Float(f) if f.is_finite() && f.fract() == 0.0 => {
            let text = format!("{f:.0}");
            Num::Whole(if text == "-0" { "0".to_owned() } else { text })
        }
        PyValue::Float(f) => Num::Frac(*f),
        _ => return None,
    })
}

/// `store_native.matches`.
fn matches(
    row: &PyValue,
    where_: &[(PyStr, PyValue)],
    column_in: Option<(&[u32], &[PyValue])>,
) -> bool {
    let get = |c: &[u32]| {
        let c = PyValue::Str(c.to_vec());
        match row {
            PyValue::Dict(items) => items.iter().rev().find(|(k, _)| *k == c).map(|(_, v)| v),
            _ => None,
        }
        .cloned()
        .unwrap_or(PyValue::None)
    };
    where_.iter().all(|(c, v)| py_eq(&get(c), v))
        && column_in.is_none_or(|(c, values)| {
            let value = get(c);
            values.iter().any(|v| py_eq(&value, v))
        })
}

/// A value bound as a statement parameter, as `sqlite3` binds it.
fn bound(v: &PyValue) -> Result<Bound, WriteError> {
    Ok(match v {
        PyValue::None => Bound::Null,
        PyValue::Bool(b) => Bound::Integer(i64::from(*b)),
        PyValue::Int(t) => Bound::Integer(t.parse().map_err(|_| {
            refused(
                "OverflowError",
                "Python int too large to convert to SQLite INTEGER",
            )
        })?),
        PyValue::Float(f) => Bound::Real(*f),
        PyValue::Str(s) => Bound::Text(unicode::to_string(s).unwrap_or_default()),
        PyValue::DateTime(_) => {
            return Err(WriteError::NotPorted(WriteNotPorted::Value("datetime")))
        }
        _ => return Err(sqlite_failed("ProgrammingError")),
    })
}

/// A text two values share exactly when `py_eq` holds between them, or
/// None for a value that is not a str, int, bool, None, float that is not
/// NaN, or a tuple of those.
fn eq_key(v: &PyValue) -> Option<String> {
    Some(match v {
        PyValue::None => "N".to_owned(),
        PyValue::Str(s) => {
            let mut k = String::from("s");
            for cp in s {
                k += &format!("{cp:x},");
            }
            k
        }
        PyValue::Tuple(items) => {
            let mut k = String::from("t(");
            for item in items {
                let inner = eq_key(item)?;
                k += &format!("{}:{inner}", inner.len());
            }
            k + ")"
        }
        _ => match number(v)? {
            Num::Whole(t) => format!("w{t}"),
            Num::Frac(f) if f.is_nan() => return None,
            Num::Frac(f) => format!("f{:x}", f.to_bits()),
        },
    })
}

fn hashable(v: &PyValue) -> Result<(), WriteError> {
    match v {
        PyValue::List(_) | PyValue::Dict(_) => Err(refused(
            "TypeError",
            format!("unhashable type: '{}'", super::home::type_name(v)),
        )),
        PyValue::Tuple(items) => items.iter().try_for_each(hashable),
        _ => Ok(()),
    }
}

/// `Store.membership_rows(table, where=, column_in=, order_by=)`. Each row
/// is a dict, a `_json` column parsed back to its value.
pub fn membership_rows(
    conn: &Connection,
    table: &str,
    where_: Option<&[(PyStr, PyValue)]>,
    column_in: Option<(&[u32], &[PyValue])>,
    order_by: &[PyStr],
) -> Result<Vec<PyValue>, WriteError> {
    let spec = membership_spec_by_table(table).ok_or_else(|| {
        refused(
            "ValueError",
            format!("{} is not a membership table", shown(&unicode::cps(table))),
        )
    })?;
    let native = native_rows(conn, spec)?;
    let where_ = where_.unwrap_or(&[]);
    let legacy_possible = !where_
        .iter()
        .any(|(k, v)| has_surrogates(&PyValue::Str(k.clone())) || has_surrogates(v));
    let mut clauses = Vec::new();
    let mut params = Vec::new();
    for (column, value) in where_ {
        membership_column(spec, column).map_err(failed)?;
        let column = column_text(column);
        if matches!(value, PyValue::None) {
            clauses.push(format!("{column} IS NULL"));
        } else {
            clauses.push(format!("{column} = ?"));
            params.push(value);
        }
    }
    let names: Vec<PyStr> = if order_by.is_empty() {
        spec.key.iter().map(|k| unicode::cps(k)).collect()
    } else {
        order_by.to_vec()
    };
    let mut order = Vec::new();
    for name in &names {
        let column = &name[name.iter().take_while(|&&c| c == u32::from(b'-')).count()..];
        membership_column(spec, column).map_err(failed)?;
        let descending = name.first() == Some(&u32::from(b'-'));
        order.push(format!(
            "{}{}",
            column_text(column),
            if descending { " DESC" } else { "" }
        ));
    }
    let select = format!("SELECT {} FROM {table}", spec.columns.join(", "));
    let mut batches: Vec<Vec<PyValue>> = vec![Vec::new()];
    if let Some((column, values)) = column_in {
        membership_column(spec, column).map_err(failed)?;
        // `dict.fromkeys`: a value equal to one before it is dropped. A
        // value with an equality key is looked up by it; any other is
        // compared with the others that have none.
        let mut wanted: Vec<PyValue> = Vec::new();
        let mut seen = std::collections::HashSet::new();
        let mut keyless: Vec<usize> = Vec::new();
        for v in values {
            hashable(v)?;
            let fresh = match eq_key(v) {
                Some(key) => seen.insert(key),
                None => !keyless.iter().any(|&i| py_eq(&wanted[i], v)),
            };
            if fresh {
                if eq_key(v).is_none() {
                    keyless.push(wanted.len());
                }
                wanted.push(v.clone());
            }
        }
        if wanted.is_empty() {
            return Ok(Vec::new());
        }
        let legacy: Vec<PyValue> = wanted.into_iter().filter(|v| !has_surrogates(v)).collect();
        batches = legacy.chunks(BATCH).map(<[PyValue]>::to_vec).collect();
    }
    let mut out = Vec::new();
    for batch in batches.iter().filter(|_| legacy_possible) {
        let mut batch_clauses = clauses.clone();
        if let Some((column, _)) = column_in {
            let marks = vec!["?"; batch.len()].join(",");
            batch_clauses.push(format!("{} IN ({marks})", column_text(column)));
        }
        let mut sql = select.clone();
        if !batch_clauses.is_empty() {
            sql += &format!(" WHERE {}", batch_clauses.join(" AND "));
        }
        sql += &format!(" ORDER BY {}", order.join(", "));
        let values = params
            .iter()
            .copied()
            .chain(batch)
            .map(bound)
            .collect::<Result<Vec<_>, _>>()?;
        let mut statement = conn.prepare(&sql)?;
        let mut rows = statement.query(rusqlite::params_from_iter(values))?;
        while let Some(r) = rows.next()? {
            let mut items = Vec::with_capacity(spec.columns.len());
            for (i, column) in spec.columns.iter().enumerate() {
                let raw =
                    sql_value(r.get_ref(i)?).map_err(|_| sqlite_failed("OperationalError"))?;
                let value = if column.ends_with("_json") {
                    load_json(&raw, PyValue::None).map_err(|e| WriteError::Refused {
                        class: e.class(),
                        text: None,
                    })?
                } else {
                    sql_py(raw)?
                };
                items.push((PyValue::str(column), value));
            }
            out.push(PyValue::Dict(items));
        }
    }
    if !native.is_empty() {
        let mut kept = Vec::with_capacity(out.len());
        for row in out {
            let key = SqlValue::Text(row_key(spec, &row)?);
            if !native.iter().any(|(k, _)| *k == key) {
                kept.push(row);
            }
        }
        out = kept;
        out.extend(
            native
                .into_iter()
                .filter_map(|(_, row)| row)
                .filter(|row| matches(row, where_, column_in)),
        );
        sort(&mut out, &names)?;
    } else if batches.len() > 1 {
        sort(&mut out, &names)?;
    }
    Ok(out)
}

/// `_sort_rows` over dict rows: each order column carried as the value
/// SQLite would hold, the row's place as a column no table has.
fn sort(out: &mut Vec<PyValue>, names: &[PyStr]) -> Result<(), WriteError> {
    let names: Vec<String> = names.iter().map(|n| column_text(n)).collect();
    let mut keyed = Vec::with_capacity(out.len());
    for (i, row) in out.iter().enumerate() {
        let mut columns = vec![(
            String::new(),
            SqlValue::Integer(i64::try_from(i).unwrap_or(0)),
        )];
        for name in &names {
            let column = name.trim_start_matches('-');
            let Some(value) = row.get(column) else {
                continue;
            };
            let v = match value {
                PyValue::None => SqlValue::Null,
                PyValue::Int(t) => t
                    .parse()
                    .map(SqlValue::Integer)
                    .map_err(|_| WriteError::NotPorted(WriteNotPorted::Value("a sort value")))?,
                PyValue::Float(f) => SqlValue::Real(*f),
                PyValue::Str(s) => SqlValue::Text(ordered_text(s)),
                _ => return Err(WriteError::NotPorted(WriteNotPorted::Value("a sort value"))),
            };
            columns.push((column.to_owned(), v));
        }
        keyed.push(Row { columns });
    }
    let order: Vec<&str> = names.iter().map(String::as_str).collect();
    sort_rows(&mut keyed, &order).map_err(failed)?;
    let mut old: Vec<Option<PyValue>> = std::mem::take(out).into_iter().map(Some).collect();
    for row in keyed {
        if let Some(SqlValue::Integer(i)) = row.get("") {
            let i = usize::try_from(*i).unwrap_or(0);
            out.extend(old[i].take());
        }
    }
    Ok(())
}

/// Text that sorts as `s`'s code points do, lone surrogates included: a
/// code point below U+D800 as itself, any other as two characters, the
/// first from U+E000 on (after every lower one) and the second its low ten
/// bits, so byte order is code point order.
fn ordered_text(s: &[u32]) -> String {
    let mut out = String::with_capacity(s.len());
    for &cp in s {
        if cp < 0xD800 {
            out.extend(char::from_u32(cp));
        } else {
            out.extend(char::from_u32(0xE000 + ((cp - 0xD800) >> 10)));
            out.extend(char::from_u32(0x100 + ((cp - 0xD800) & 0x3FF)));
        }
    }
    out
}

/// `Store.next_membership_id(table, prefix)`.
pub fn next_membership_id(
    conn: &Connection,
    table: &str,
    prefix: &str,
) -> Result<String, WriteError> {
    let spec = membership_spec_by_table(table)
        .filter(|s| s.key == ["id"])
        .ok_or_else(|| {
            refused(
                "ValueError",
                format!("{} has no minted id", shown(&unicode::cps(table))),
            )
        })?;
    let high: Option<SqlValue> = conn.query_row(
        &format!(
            "SELECT MAX(CAST(SUBSTR(id, ?) AS INTEGER)) FROM {} WHERE id LIKE ?",
            spec.table
        ),
        rusqlite::params![
            i64::try_from(prefix.chars().count()).unwrap_or(i64::MAX) + 1,
            format!("{prefix}%")
        ],
        |r| Ok(sql_value(r.get_ref(0)?).ok()),
    )?;
    let number = or_zero(sql_py(high.unwrap_or(SqlValue::Null))?)? + 1;
    Ok(format!("{prefix}{number:03}"))
}

/// `int(value or 0)`.
fn or_zero(v: PyValue) -> Result<i128, WriteError> {
    let falsy = match &v {
        PyValue::None => true,
        PyValue::Str(s) => s.is_empty(),
        PyValue::Float(f) => *f == 0.0,
        PyValue::Int(t) => t == "0",
        _ => false,
    };
    if falsy {
        return Ok(0);
    }
    py_int(&v)
}

/// `Store.next_membership_number(table, column)`.
pub fn next_membership_number(
    conn: &Connection,
    table: &str,
    column: &[u32],
) -> Result<i128, WriteError> {
    let spec = membership_spec_by_table(table).ok_or_else(|| {
        refused(
            "ValueError",
            format!("{} is not a membership table", shown(&unicode::cps(table))),
        )
    })?;
    membership_column(spec, column).map_err(failed)?;
    let column = column_text(column);
    let high: SqlValue = conn
        .query_row(&format!("SELECT MAX({column}) FROM {table}"), [], |r| {
            Ok(sql_value(r.get_ref(0)?))
        })?
        .map_err(|_| sqlite_failed("OperationalError"))?;
    let mut native_high = 0;
    let mut first = true;
    for (_, row) in native_rows(conn, spec)? {
        let Some(row) = row else { continue };
        let value = row
            .get(&column)
            .ok_or_else(|| failed(rows_key_error(&column)))?;
        let n = py_int(value)?;
        native_high = if first { n } else { native_high.max(n) };
        first = false;
    }
    or_zero(sql_py(high)?)?
        .max(native_high)
        .checked_add(1)
        .ok_or(WriteError::NotPorted(WriteNotPorted::Value(
            "an int past 128 bits",
        )))
}

/// `Store.wrote_record(record_id)`: a home names it, or a memory,
/// tombstone or episode row of the log holds its `"id"` pair.
pub fn wrote_record(conn: &Connection, record_id: &str) -> Result<bool, WriteError> {
    if conn
        .prepare("SELECT 1 FROM homes WHERE record_id = ?")?
        .exists([record_id])?
    {
        return Ok(true);
    }
    let payload = PyValue::Dict(vec![(PyValue::str("id"), PyValue::str(record_id))]);
    let text = json::canonical_payload(&payload).map_err(|e| WriteError::Refused {
        class: e.name(),
        text: None,
    })?;
    let needle = &text[1..text.len() - 1];
    Ok(conn
        .prepare(
            "SELECT 1 FROM log WHERE kind IN ('memory_put', 'tombstone_put', 'episode_put') \
             AND instr(payload, ?) > 0 LIMIT 1",
        )?
        .exists([needle])?)
}

/// `Store.membership_store_id`: meta's value, or RuntimeError.
pub fn membership_store_id(
    conn: &Connection,
    path: &std::path::Path,
) -> Result<String, WriteError> {
    let value = conn
        .query_row(
            "SELECT value FROM meta WHERE key = ?",
            [MEMBERSHIP_STORE_ID],
            |r| Ok(sql_value(r.get_ref(0)?)),
        )
        .optional()?
        .transpose()
        .map_err(|_| sqlite_failed("OperationalError"))?;
    match value {
        Some(v) => Ok(v.py_str()),
        None => Err(refused(
            "RuntimeError",
            format!("store {} carries no membership store id", path.display()),
        )),
    }
}

/// `str.encode("utf-8")`'s refusal of a text holding a lone surrogate,
/// as `sqlite3` raises it binding the text: the first run of surrogates
/// named, one by its escape, several by their positions.
fn unencodable(s: &[u32]) -> Option<WriteError> {
    let surrogate = |c: &u32| (0xD800..=0xDFFF).contains(c);
    let start = s.iter().position(surrogate)?;
    let run = s[start..].iter().take_while(|c| surrogate(c)).count();
    let what = if run == 1 {
        format!("character '\\u{:04x}' in position {start}", s[start])
    } else {
        format!("characters in position {start}-{}", start + run - 1)
    };
    Some(refused(
        "UnicodeEncodeError",
        format!("'utf-8' codec can't encode {what}: surrogates not allowed"),
    ))
}

/// `Store.membership_log(kinds, since_seq=, checkout=)` read as its
/// generator is: the rows of the membership kinds
/// (`MEMBERSHIP_MUTATION_KINDS | MEMBERSHIP_EVENT_KINDS`), or of those
/// `kinds` names (a kind not a str, or no membership kind, is dropped; an
/// unhashable one is TypeError), after `since_seq`, in log order; with
/// `checkout`, only those whose payload is JSON and names it as its
/// `checkout`, compared by SQLite (`kinds` is the collection's items, in
/// its order). Each row is read and handed to `each` only after `each`
/// answered `Continue` for the one before, so a row past the one where
/// `each` answers `Break` is never read: one there that cannot be read (a
/// TEXT column that is not UTF-8) fails nothing, as it fails nothing for
/// the reference's caller that leaves its `for` loop first. The checks of
/// `kinds` and `checkout` come before any row, as the generator's first
/// `next` makes them. Answers the `Break`, or `Continue` past the last
/// row.
pub fn membership_log_each<B>(
    conn: &Connection,
    kinds: Option<&[PyValue]>,
    since_seq: i64,
    checkout: Option<&PyValue>,
    mut each: impl FnMut(LogRow) -> ControlFlow<B>,
) -> Result<ControlFlow<B>, WriteError> {
    let allowed: std::collections::BTreeSet<String> = MUTATION_TABLES[7..]
        .iter()
        .flat_map(|t| [format!("{t}_put"), format!("{t}_delete")])
        .chain(MEMBERSHIP_EVENT_KINDS.iter().map(|k| (*k).to_owned()))
        .collect();
    let names = match kinds {
        None => allowed,
        Some(kinds) => {
            kinds.iter().try_for_each(hashable)?;
            kinds
                .iter()
                .filter_map(|k| match k {
                    PyValue::Str(k) => unicode::to_string(k),
                    _ => None,
                })
                .filter(|k| allowed.contains(k))
                .collect()
        }
    };
    if names.is_empty() {
        return Ok(ControlFlow::Continue(()));
    }
    let marks = vec!["?"; names.len()].join(",");
    let mut params: Vec<Bound> = names.into_iter().map(Bound::Text).collect();
    params.push(Bound::Integer(since_seq));
    let mut naming = "";
    if let Some(checkout) = checkout {
        naming = " AND CASE WHEN json_valid(payload) \
                  THEN json_extract(payload, '$.checkout') END = ?";
        if let Some(e) = match checkout {
            PyValue::Str(s) => unencodable(s),
            _ => None,
        } {
            return Err(e);
        }
        params.push(bound(checkout)?);
    }
    let sql = format!(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log \
         WHERE kind IN ({marks}) AND seq > ?{naming} ORDER BY seq"
    );
    let mut statement = conn.prepare(&sql)?;
    let mut rows = statement.query(rusqlite::params_from_iter(params))?;
    while let Some(r) = rows.next()? {
        let read = |i| sql_value(r.get_ref(i)?).map_err(|_| sqlite_failed("OperationalError"));
        let text = |i| read(i).map(|v| v.py_str());
        let seq = match read(0)? {
            SqlValue::Integer(i) => i,
            other => i64::try_from(py_int(&sql_py(other)?)?)
                .map_err(|_| WriteError::NotPorted(WriteNotPorted::Value("an int past 64 bits")))?,
        };
        let session = match read(2)? {
            SqlValue::Null => None,
            other => Some(other.py_str()),
        };
        let row = LogRow {
            seq,
            ts: text(1)?,
            session,
            kind: text(3)?,
            payload: text(4)?,
            prev_mac: text(5)?,
            mac: text(6)?,
        };
        if let ControlFlow::Break(b) = each(row) {
            return Ok(ControlFlow::Break(b));
        }
    }
    Ok(ControlFlow::Continue(()))
}

/// `list(Store.membership_log(kinds, since_seq=, checkout=))`: every row
/// [`membership_log_each`] reads, the first that cannot be read failing
/// the whole read as `list` does. A caller that stops at a row it finds
/// takes [`membership_log_each`].
pub fn membership_log(
    conn: &Connection,
    kinds: Option<&[PyValue]>,
    since_seq: i64,
    checkout: Option<&PyValue>,
) -> Result<Vec<LogRow>, WriteError> {
    let mut out = Vec::new();
    let _: ControlFlow<()> = membership_log_each(conn, kinds, since_seq, checkout, |row| {
        out.push(row);
        ControlFlow::Continue(())
    })?;
    Ok(out)
}

/// `Store.reissue_membership_store_id(record_id=, source=, session=)`
/// with the new id `new` already drawn (the reference draws it before
/// entering its transaction), inside `tx`: the old id read under the write
/// lock (`path` names the store in its RuntimeError), meta's value
/// replaced and a `store_forked` row appended under one savepoint, so a
/// failure leaves both as they were. Answers `(old, new)`.
fn reissue(
    tx: &mut Transaction<'_>,
    path: &std::path::Path,
    new: &str,
    record_id: &str,
    source: &str,
    session: Option<&str>,
) -> Result<(String, String), WriteError> {
    tx.scope(|tx| {
        let old = membership_store_id(tx.connection(), path)?;
        tx.connection().execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            [new, MEMBERSHIP_STORE_ID],
        )?;
        let payload = PyValue::Dict(vec![
            (PyValue::str("old_store"), PyValue::str(&old)),
            (PyValue::str("new_store"), PyValue::str(new)),
            (PyValue::str("record"), PyValue::str(record_id)),
            (PyValue::str("source"), PyValue::str(source)),
        ]);
        tx.append_control("store_forked", &payload, session)?;
        Ok((old, new.to_owned()))
    })
}

/// The payload `put_membership` logs, checked: `{kind: dict(row)}`.
fn put_payload(kind: &[u32], row: &Items) -> Result<(&'static Spec, PyValue), WriteError> {
    let spec = membership_spec(kind).map_err(failed)?;
    let row = PyValue::Dict(row.to_vec());
    membership_values(spec, &row).map_err(failed)?;
    Ok((spec, row))
}

/// The key `delete_membership` logs, checked: its key columns.
fn delete_payload(kind: &[u32], key: &Items) -> Result<(&'static Spec, PyValue), WriteError> {
    let spec = membership_spec(kind).map_err(failed)?;
    let given = PyValue::Dict(key.to_vec());
    let mut items = Vec::new();
    for column in spec.key {
        let value = given
            .get(column)
            .ok_or_else(|| failed(rows_key_error(column)))?;
        items.push((PyValue::str(column), value.clone()));
    }
    let payload = PyValue::Dict(items);
    membership_key(spec, &payload).map_err(failed)?;
    Ok((spec, payload))
}

fn record_payload(kind: &[u32], payload: &Items) -> Result<(String, PyValue), WriteError> {
    match unicode::to_string(kind).filter(|k| MEMBERSHIP_EVENT_KINDS.contains(&k.as_str())) {
        Some(kind) => Ok((kind, PyValue::Dict(payload.to_vec()))),
        None => Err(refused(
            "ValueError",
            format!("{} is not a membership row kind", shown(kind)),
        )),
    }
}

impl Transaction<'_> {
    /// `_clear_native`: a `native_member_delete` of the row's native twin,
    /// when there is one.
    fn clear_native(
        &mut self,
        spec: &Spec,
        row: &PyValue,
        session: Option<&str>,
    ) -> Result<(), WriteError> {
        if spec.kind == "native_member" {
            return Ok(());
        }
        let key = row_key(spec, row)?;
        let found = self
            .connection()
            .prepare("SELECT 1 FROM native_membership_rows WHERE member_kind = ? AND key_text = ?")?
            .exists([spec.kind, key.as_str()])?;
        if found {
            let payload = PyValue::Dict(vec![
                (PyValue::str("member_kind"), PyValue::str(spec.kind)),
                (PyValue::str("key_text"), PyValue::str(&key)),
            ]);
            self.mutate_membership("native_member_delete", &payload, session)?;
        }
        Ok(())
    }

    /// The surrogate branch of `put_membership` and `delete_membership`,
    /// after `require_native_writer`: a put logs `<kind>_put` of
    /// `legacy_row` when it answers one, then `native_member_put` of the
    /// row's `encoded_json` under its key; a delete logs
    /// `native_member_delete` of the key alone. No `_clear_native` follows.
    fn native_write(
        &mut self,
        spec: &Spec,
        row: &PyValue,
        op: &str,
        session: Option<&str>,
    ) -> Result<LogRow, WriteError> {
        let member = |key: String| {
            vec![
                (PyValue::str("member_kind"), PyValue::str(spec.kind)),
                (PyValue::str("key_text"), PyValue::str(&key)),
            ]
        };
        if op != "put" {
            let payload = PyValue::Dict(member(row_key(spec, row)?));
            return self.mutate_membership("native_member_delete", &payload, session);
        }
        if let Some(legacy) = legacy_row(spec, row)? {
            let payload = PyValue::Dict(vec![(PyValue::str(spec.kind), legacy)]);
            self.mutate_membership(&format!("{}_put", spec.kind), &payload, session)?;
        }
        let mut native = member(row_key(spec, row)?);
        let text = encoded_json(row).map_err(|e| WriteError::Refused {
            class: e.name(),
            text: None,
        })?;
        native.push((PyValue::str("row_text"), PyValue::Str(text)));
        let payload = PyValue::Dict(vec![(PyValue::str("native_member"), PyValue::Dict(native))]);
        self.mutate_membership("native_member_put", &payload, session)
    }

    fn put_or_delete(
        &mut self,
        spec: &'static Spec,
        payload: PyValue,
        op: &str,
        session: Option<&str>,
    ) -> Result<LogRow, WriteError> {
        self.scope(|tx| {
            if surrogates_within(&payload, 1)? {
                require_native_writer(tx.connection())?;
                return tx.native_write(spec, &payload, op, session);
            }
            let kind = format!("{}_{op}", spec.kind);
            let logged = if op == "put" {
                PyValue::Dict(vec![(PyValue::str(spec.kind), payload.clone())])
            } else {
                payload.clone()
            };
            let written = tx.mutate_membership(&kind, &logged, session)?;
            tx.clear_native(spec, &payload, session)?;
            Ok(written)
        })
    }

    /// `Store.put_membership(kind, row, session=)` inside this transaction.
    pub fn put_membership(
        &mut self,
        kind: &[u32],
        row: &Items,
        session: Option<&str>,
    ) -> Result<LogRow, WriteError> {
        let (spec, row) = put_payload(kind, row)?;
        self.put_or_delete(spec, row, "put", session)
    }

    /// `Store.delete_membership(kind, key, session=)` inside this transaction.
    pub fn delete_membership(
        &mut self,
        kind: &[u32],
        key: &Items,
        session: Option<&str>,
    ) -> Result<LogRow, WriteError> {
        let (spec, key) = delete_payload(kind, key)?;
        self.put_or_delete(spec, key, "delete", session)
    }

    /// `Store.reissue_membership_store_id` inside this transaction, with
    /// the new id `new` the caller drew before entering it, as the
    /// reference draws it before its nested `_transaction`; `path` is the
    /// store's, for the RuntimeError of a meta without the id.
    pub fn reissue_membership_store_id(
        &mut self,
        path: &std::path::Path,
        new: &str,
        record_id: &str,
        source: &str,
        session: Option<&str>,
    ) -> Result<(String, String), WriteError> {
        reissue(self, path, new, record_id, source, session)
    }

    /// `Store.record_membership(kind, payload, session=)` inside this
    /// transaction.
    pub fn record_membership(
        &mut self,
        kind: &[u32],
        payload: &Items,
        session: Option<&str>,
    ) -> Result<LogRow, WriteError> {
        let (kind, payload) = record_payload(kind, payload)?;
        self.scope(|tx| tx.append_control(&kind, &payload, session))
    }
}

impl WritableStore {
    /// `Store.put_membership` in a transaction of its own.
    pub fn put_membership(
        &mut self,
        provider: &mut dyn Provider,
        kind: &[u32],
        row: &Items,
        session: Option<&str>,
    ) -> Result<(LogRow, Committed), WriteError> {
        put_payload(kind, row)?;
        let mut tx = self.begin(provider)?;
        let written = tx.put_membership(kind, row, session)?;
        Ok((written, tx.commit()?))
    }

    /// `Store.delete_membership` in a transaction of its own.
    pub fn delete_membership(
        &mut self,
        provider: &mut dyn Provider,
        kind: &[u32],
        key: &Items,
        session: Option<&str>,
    ) -> Result<(LogRow, Committed), WriteError> {
        delete_payload(kind, key)?;
        let mut tx = self.begin(provider)?;
        let written = tx.delete_membership(kind, key, session)?;
        Ok((written, tx.commit()?))
    }

    /// `Store.record_membership` in a transaction of its own.
    pub fn record_membership(
        &mut self,
        provider: &mut dyn Provider,
        kind: &[u32],
        payload: &Items,
        session: Option<&str>,
    ) -> Result<(LogRow, Committed), WriteError> {
        record_payload(kind, payload)?;
        let mut tx = self.begin(provider)?;
        let written = tx.record_membership(kind, payload, session)?;
        Ok((written, tx.commit()?))
    }

    /// `Store.reissue_membership_store_id` in a transaction of its own: the
    /// new id drawn (`Provider::membership_store_id`) before `BEGIN`.
    pub fn reissue_membership_store_id(
        &mut self,
        provider: &mut dyn Provider,
        record_id: &str,
        source: &str,
        session: Option<&str>,
    ) -> Result<((String, String), Committed), WriteError> {
        let new = provider.membership_store_id().map_err(WriteError::Clock)?;
        let path = self.path().to_owned();
        let mut tx = self.begin(provider)?;
        let ids = reissue(&mut tx, &path, &new, record_id, source, session)?;
        Ok((ids, tx.commit()?))
    }

    /// `Store.committed_seq()` with no transaction of this store's open: 0
    /// when the connection is still in one (a residue), else None.
    pub fn committed_seq(&self) -> Option<i64> {
        (!self.conn.is_autocommit()).then_some(0)
    }

    /// `Store.in_batch` with no transaction of this store's open.
    pub fn in_batch(&self) -> bool {
        false
    }
}
