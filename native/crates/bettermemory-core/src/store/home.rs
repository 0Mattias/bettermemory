//! A record's home and its standing answer: `Store._home_row`
//! (`store.py` 1459-1497), `store_records._standing_row` (202-222) and
//! `put_standing` (225-239). README, "The store's row writers".
//!
//! [`home_row`] and [`standing_row`] decide a row inside a caller's open
//! [`Transaction`], reading through [`Transaction::connection`];
//! [`put_home`] and [`put_standing_row`] append it (`home_put`,
//! `standing_put`, which a direct [`Transaction::mutate`] still refuses).
//! A refusal is a [`WriteError::Refused`] with the reference's class and
//! text, so a caller that runs them in a [`Transaction::scope`] rolls back
//! only that scope. Nothing here writes before it has decided.

use rusqlite::OptionalExtension;
use sha2::{Digest, Sha256};

use super::create::Provider;
use super::tx::{Committed, Transaction};
use super::write::WriteError;
use super::{sql_value, SqlValue};
use crate::log::LogRow;
use crate::pyvalue::{repr, PyValue};
use crate::records::rows::{self, Row};
use crate::records::{Memory, RecordError};
use crate::unicode;

/// `store_schema.RecordHome`: the project (`global` outside any), the
/// checkout, and the rule that decided it, as the caller passes them.
#[derive(Clone, Debug, PartialEq)]
pub struct Home {
    pub project: PyValue,
    pub checkout: PyValue,
    pub rule: PyValue,
}

/// `store_schema.StandingAnswer`: the probability from 0 to 1, its source
/// and who gave it, as the caller passes them.
#[derive(Clone, Debug, PartialEq)]
pub struct Answer {
    pub probability: PyValue,
    pub source: PyValue,
    pub answered_by: PyValue,
}

pub(crate) fn refused(class: &'static str, text: impl Into<String>) -> WriteError {
    WriteError::Refused {
        class,
        text: Some(text.into()),
    }
}

/// `NotFoundError(key)`: a KeyError, whose `str` is the key's `repr`.
pub(crate) fn not_found(key: &PyValue) -> WriteError {
    match repr(key) {
        Ok(text) => WriteError::Refused {
            class: "NotFoundError",
            text: unicode::to_string(&text),
        },
        Err(e) => WriteError::Refused {
            class: e.name(),
            text: None,
        },
    }
}

/// The type's name as CPython's error messages spell it (`tp_name`:
/// `'datetime.datetime' object has no attribute 'strip'`, where
/// `type(x).__name__` is `datetime`), checked against the reference for
/// every variant.
pub(crate) fn type_name(v: &PyValue) -> &'static str {
    match v {
        PyValue::None => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::Str(_) => "str",
        PyValue::DateTime(_) => "datetime.datetime",
        PyValue::Path(_) => "PurePosixPath",
        PyValue::Tuple(_) => "tuple",
        PyValue::List(_) => "list",
        PyValue::Dict(_) => "dict",
    }
}

/// `bool(value)`.
pub(crate) fn truthy(v: &PyValue) -> bool {
    match v {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(t) => t != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) => !s.is_empty(),
        PyValue::DateTime(_) | PyValue::Path(_) => true,
        PyValue::Tuple(i) | PyValue::List(i) => !i.is_empty(),
        PyValue::Dict(i) => !i.is_empty(),
    }
}

/// A number as Python compares it (`bool` is an `int`).
fn number(v: &PyValue) -> Option<f64> {
    match v {
        PyValue::Bool(b) => Some(f64::from(u8::from(*b))),
        PyValue::Int(t) => t.parse::<f64>().ok(),
        PyValue::Float(f) => Some(*f),
        _ => None,
    }
}

/// `stored == given` for a column value and a caller's value. The
/// `Integer` and `Real` arms are unreachable on a store this build wrote
/// (every `homes` column has TEXT affinity, so a number is stored as
/// text); they matter only when a `homes` table altered by hand gives a
/// column INTEGER, REAL or NUMERIC affinity, where `sqlite3` reads a number
/// and Python's `==` compares it with the caller's.
fn py_eq(stored: &SqlValue, given: &PyValue) -> bool {
    match (stored, given) {
        (SqlValue::Null, PyValue::None) => true,
        (SqlValue::Text(t), PyValue::Str(s)) => unicode::cps(t) == *s,
        (SqlValue::Integer(i), PyValue::Int(t)) => t.parse::<i128>().ok() == Some(i128::from(*i)),
        (SqlValue::Integer(i), PyValue::Bool(b)) => *i == i64::from(*b),
        (SqlValue::Integer(i), PyValue::Float(f)) => (*i as f64) == *f && f.fract() == 0.0,
        (SqlValue::Real(r), g) => number(g) == Some(*r),
        _ => false,
    }
}

/// `Store._home_row(record_id, kind, home)` inside `tx`: None when no home
/// is given or the record already has this very home; the row otherwise,
/// its `decided` the wall clock's reading (one read, only then). Refused,
/// with nothing read or written, for a project or rule that is not a
/// non-empty `str`, and for another home of a homed record.
pub fn home_row(
    tx: &mut Transaction<'_>,
    record_id: &str,
    kind: &str,
    home: Option<&Home>,
) -> Result<Option<PyValue>, WriteError> {
    let Some(home) = home else {
        return Ok(None);
    };
    if !matches!(&home.project, PyValue::Str(s) if !s.is_empty()) {
        return Err(refused("ValueError", "a home names a project (or global)"));
    }
    if !matches!(&home.rule, PyValue::Str(s) if !s.is_empty()) {
        return Err(refused(
            "ValueError",
            "a home names the rule that decided it",
        ));
    }
    let existing = tx
        .connection()
        .query_row(
            "SELECT project, checkout, rule FROM homes WHERE record_id = ?",
            [record_id],
            |r| {
                Ok([
                    sql_value(r.get_ref(0)?),
                    sql_value(r.get_ref(1)?),
                    sql_value(r.get_ref(2)?),
                ])
            },
        )
        .optional()?;
    if let Some(existing) = existing {
        let mut stored = Vec::with_capacity(3);
        for v in existing {
            stored.push(v.map_err(|_| WriteError::Refused {
                class: "UnicodeDecodeError",
                text: None,
            })?);
        }
        if py_eq(&stored[0], &home.project)
            && py_eq(&stored[1], &home.checkout)
            && py_eq(&stored[2], &home.rule)
        {
            return Ok(None);
        }
        return Err(refused(
            "ValueError",
            format!("{record_id} already has a home; a home is written once"),
        ));
    }
    let decided = tx.wall_now()?.isoformat_utc();
    Ok(Some(dict(vec![
        ("record_id", PyValue::str(record_id)),
        ("kind", PyValue::str(kind)),
        ("project", home.project.clone()),
        ("checkout", home.checkout.clone()),
        ("rule", home.rule.clone()),
        ("decided", PyValue::str(&decided)),
    ])))
}

/// `_mutate(tx, "home_put", {"home": row}, session=)`.
pub fn put_home(
    tx: &mut Transaction<'_>,
    row: &PyValue,
    session: Option<&str>,
) -> Result<LogRow, WriteError> {
    let payload = dict(vec![("home", row.clone())]);
    tx.mutate_home_or_standing("home_put", &payload, session)
}

/// `store_records._standing_row(memory, answer)` inside `tx`, for the
/// record `record_id` whose stored body is `body`: the row, its
/// `answered` the wall clock's reading (one read, after the checks).
/// Refused, with nothing written: a probability that is a `bool`, not a
/// number, not finite or outside 0 to 1 (`ValueError`; an `int` too large
/// for a float is `OverflowError`), a falsy source or giver.
pub fn standing_row(
    tx: &mut Transaction<'_>,
    record_id: &[u32],
    body: &[u32],
    answer: &Answer,
) -> Result<PyValue, WriteError> {
    let bad = || {
        refused(
            "ValueError",
            "a standing answer is a probability from 0 to 1",
        )
    };
    let probability = match &answer.probability {
        PyValue::Int(t) => {
            let f = t.parse::<f64>().map_err(|_| bad())?;
            if f.is_infinite() {
                return Err(refused(
                    "OverflowError",
                    "int too large to convert to float",
                ));
            }
            f
        }
        PyValue::Float(f) => *f,
        _ => return Err(bad()),
    };
    if !probability.is_finite() || !(0.0..=1.0).contains(&probability) {
        return Err(bad());
    }
    if !truthy(&answer.source) || !truthy(&answer.answered_by) {
        return Err(refused(
            "ValueError",
            "a standing answer names its source and who gave it",
        ));
    }
    let Some(text) = unicode::to_string(body) else {
        return Err(WriteError::Refused {
            class: "UnicodeEncodeError",
            text: None,
        });
    };
    let sha: String = Sha256::digest(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let answered = tx.wall_now()?.isoformat_utc();
    Ok(dict(vec![
        ("record_id", PyValue::Str(record_id.to_vec())),
        ("probability", PyValue::Float(probability)),
        ("body_sha256", PyValue::str(&sha)),
        ("source", answer.source.clone()),
        ("answered_by", answer.answered_by.clone()),
        ("answered", PyValue::str(&answered)),
    ]))
}

/// `_mutate(tx, "standing_put", {"standing": row}, session=)`.
pub fn put_standing_row(
    tx: &mut Transaction<'_>,
    row: &PyValue,
    session: Option<&str>,
) -> Result<LogRow, WriteError> {
    let payload = dict(vec![("standing", row.clone())]);
    tx.mutate_home_or_standing("standing_put", &payload, session)
}

/// `Store.get_memory(memory_id)` inside `tx`: the active record, or
/// NotFoundError; a row the model refuses raises as the reference's read.
pub fn get_memory(tx: &Transaction<'_>, memory_id: &str) -> Result<Memory, WriteError> {
    let mut statement = tx.connection().prepare(
        "SELECT memories.*, (SELECT payload FROM log WHERE mac = memories.log_mac \
         AND kind = 'memory_put' LIMIT 1) AS attestation_payload FROM memories WHERE id = ?",
    )?;
    let names: Vec<String> = statement.column_names().map(str::to_owned).collect();
    let mut found = statement.query([memory_id])?;
    let Some(r) = found.next()? else {
        return Err(not_found(&PyValue::str(memory_id)));
    };
    let mut columns = Vec::with_capacity(names.len());
    for (i, name) in names.iter().enumerate() {
        let v = sql_value(r.get_ref(i)?).map_err(|_| WriteError::Refused {
            class: "UnicodeDecodeError",
            text: None,
        })?;
        columns.push((name.clone(), v));
    }
    rows::memory(&Row { columns }).map_err(record_refusal)
}

/// A model's refusal as `_refusal` records it: a ValidationError's first
/// line, another exception by class.
pub(crate) fn record_refusal(e: RecordError) -> WriteError {
    match e {
        RecordError::Validation { title, errors } => refused(
            "ValidationError",
            format!(
                "{} validation error{} for {title}",
                errors.len(),
                if errors.len() == 1 { "" } else { "s" }
            ),
        ),
        RecordError::Raised(class) => WriteError::Refused { class, text: None },
    }
}

pub(crate) fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

impl super::open::WritableStore {
    /// `Store.put_standing(record_id, answer, session=)`: one transaction;
    /// the active record read under the lock (NotFoundError when it is not
    /// active), the row decided for its stored body, `standing_put`. Returns
    /// the row.
    pub fn put_standing(
        &mut self,
        record_id: &str,
        answer: &Answer,
        session: Option<&str>,
        provider: &mut dyn Provider,
    ) -> Result<(PyValue, Committed), WriteError> {
        let mut tx = self.begin(provider)?;
        let memory = get_memory(&tx, record_id)?;
        let row = standing_row(&mut tx, &memory.fields.id, &memory.fields.body, answer)?;
        put_standing_row(&mut tx, &row, session)?;
        Ok((row, tx.commit()?))
    }
}
