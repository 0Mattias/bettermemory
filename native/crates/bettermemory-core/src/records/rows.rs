//! `store_rows._row_to_memory`, `_row_to_tombstone` and `_row_to_episode`:
//! a row's SQLite values through the readers' own steps (`_dt`,
//! `_load_json`, `_row_origin`, `_path_targets`, `_anchor_or_none`,
//! `int()`, `bool()`), in the order Python evaluates the keyword arguments,
//! then the model. A step that raises ends the read with that exception's
//! class ([`RecordError::Raised`]).

use super::lax::Arg;
use super::{commit_anchor, Episode, Fields, Memory, Origin, RecordError, Tombstone};
use crate::datetime::{self, DateTime};
use crate::json::{self, LoadError};
use crate::pyvalue::{self, PyError, PyValue};
use crate::store::SqlValue;
use crate::unicode;

/// A row: each column's value by name, as `sqlite3.Row` gives it. A
/// column the select did not name is absent (only `attestation_payload`
/// may be: the readers ask `row.keys()` for it).
#[derive(Clone, Debug, Default)]
pub struct Row {
    pub columns: Vec<(String, SqlValue)>,
}

impl Row {
    pub fn get(&self, name: &str) -> Option<&SqlValue> {
        self.columns
            .iter()
            .rev()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v)
    }

    /// `row[name]`: a column the row lacks is IndexError, as
    /// `sqlite3.Row` raises it (a table missing a column).
    fn at(&self, name: &str) -> Result<&SqlValue, RecordError> {
        self.get(name).ok_or(RecordError::Raised("IndexError"))
    }
}

/// A value the reader hands the model.
#[derive(Clone, Debug)]
enum Owned {
    Py(PyValue),
    Bytes(Vec<u8>),
}

impl Owned {
    fn arg(&self) -> Arg<'_> {
        match self {
            Owned::Py(v) => Arg::Py(v),
            Owned::Bytes(b) => Arg::Bytes(b),
        }
    }
}

/// The Python value `sqlite3` gives for a column.
fn raw(v: &SqlValue) -> Owned {
    match v {
        SqlValue::Null => Owned::Py(PyValue::None),
        SqlValue::Integer(i) => Owned::Py(PyValue::int(i128::from(*i))),
        SqlValue::Real(f) => Owned::Py(PyValue::Float(*f)),
        SqlValue::Text(t) => Owned::Py(PyValue::str(t)),
        SqlValue::Blob(b) => Owned::Bytes(b.clone()),
    }
}

/// `store_rows._dt`: `text.replace("Z", "+00:00")` then `fromisoformat`,
/// naive read as UTC.
fn dt(v: &SqlValue) -> Result<DateTime, RecordError> {
    match v {
        SqlValue::Text(t) => {
            datetime::parse_record_dt(&unicode::cps(t)).map_err(|e| RecordError::Raised(e.name()))
        }
        SqlValue::Blob(_) => Err(RecordError::Raised("TypeError")),
        _ => Err(RecordError::Raised("AttributeError")),
    }
}

fn dt_opt(v: &SqlValue) -> Result<Option<DateTime>, RecordError> {
    match v {
        SqlValue::Null => Ok(None),
        other => dt(other).map(Some),
    }
}

fn load_error(e: LoadError) -> RecordError {
    RecordError::Raised(e.name())
}

/// `json.detect_encoding` and the `surrogatepass` decode `json.loads`
/// gives bytes.
fn decode_json_bytes(b: &[u8]) -> Result<Vec<u32>, RecordError> {
    let fail = RecordError::Raised("UnicodeDecodeError");
    if b.starts_with(&[0, 0, 0xFE, 0xFF]) {
        return utf32(&b[4..], true).ok_or(fail);
    }
    if b.starts_with(&[0xFF, 0xFE, 0, 0]) {
        return utf32(&b[4..], false).ok_or(fail);
    }
    if b.starts_with(&[0xFE, 0xFF]) {
        return utf16(&b[2..], true).ok_or(fail);
    }
    if b.starts_with(&[0xFF, 0xFE]) {
        return utf16(&b[2..], false).ok_or(fail);
    }
    if b.starts_with(&[0xEF, 0xBB, 0xBF]) {
        return utf8_surrogatepass(&b[3..]).ok_or(fail);
    }
    let decoded = if b.len() >= 4 {
        if b[0] == 0 {
            if b[1] != 0 {
                utf16(b, true)
            } else {
                utf32(b, true)
            }
        } else if b[1] == 0 {
            if b[2] != 0 || b[3] != 0 {
                utf16(b, false)
            } else {
                utf32(b, false)
            }
        } else {
            utf8_surrogatepass(b)
        }
    } else if b.len() == 2 && b[0] == 0 {
        utf16(b, true)
    } else if b.len() == 2 && b[1] == 0 {
        utf16(b, false)
    } else {
        utf8_surrogatepass(b)
    };
    decoded.ok_or(fail)
}

/// UTF-8 with `surrogatepass`: an encoded surrogate decodes to itself.
fn utf8_surrogatepass(b: &[u8]) -> Option<Vec<u32>> {
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        let (len, min, init) = match c {
            0x00..=0x7F => (1, 0, u32::from(c)),
            0xC2..=0xDF => (2, 0x80, u32::from(c & 0x1F)),
            0xE0..=0xEF => (3, 0x800, u32::from(c & 0x0F)),
            0xF0..=0xF4 => (4, 0x10000, u32::from(c & 0x07)),
            _ => return None,
        };
        if i + len > b.len() {
            return None;
        }
        let mut cp = init;
        for &cont in &b[i + 1..i + len] {
            if cont & 0xC0 != 0x80 {
                return None;
            }
            cp = (cp << 6) | u32::from(cont & 0x3F);
        }
        if cp < min || cp > 0x10FFFF {
            return None;
        }
        out.push(cp);
        i += len;
    }
    Some(out)
}

fn utf16(b: &[u8], big: bool) -> Option<Vec<u32>> {
    if !b.len().is_multiple_of(2) {
        return None;
    }
    let units: Vec<u32> = b
        .chunks(2)
        .map(|p| {
            if big {
                u32::from(p[0]) << 8 | u32::from(p[1])
            } else {
                u32::from(p[1]) << 8 | u32::from(p[0])
            }
        })
        .collect();
    let mut out = Vec::with_capacity(units.len());
    let mut i = 0;
    while i < units.len() {
        let u = units[i];
        if (0xD800..0xDC00).contains(&u)
            && i + 1 < units.len()
            && (0xDC00..0xE000).contains(&units[i + 1])
        {
            out.push(0x10000 + ((u - 0xD800) << 10) + (units[i + 1] - 0xDC00));
            i += 2;
        } else {
            out.push(u);
            i += 1;
        }
    }
    Some(out)
}

fn utf32(b: &[u8], big: bool) -> Option<Vec<u32>> {
    if !b.len().is_multiple_of(4) {
        return None;
    }
    b.chunks(4)
        .map(|q| {
            let cp = if big {
                u32::from_be_bytes([q[0], q[1], q[2], q[3]])
            } else {
                u32::from_le_bytes([q[0], q[1], q[2], q[3]])
            };
            (cp <= 0x10FFFF).then_some(cp)
        })
        .collect()
}

/// `json.loads(value)` of a column value: TypeError for a number.
fn json_loads(v: &SqlValue) -> Result<PyValue, RecordError> {
    match v {
        SqlValue::Text(t) => json::loads_detailed(&unicode::cps(t)).map_err(load_error),
        SqlValue::Blob(b) => json::loads_detailed(&decode_json_bytes(b)?).map_err(load_error),
        _ => Err(RecordError::Raised("TypeError")),
    }
}

/// `store_rows._load_json(value, default)`.
fn load_json(v: &SqlValue, default: PyValue) -> Result<PyValue, RecordError> {
    match v {
        SqlValue::Null => Ok(default),
        SqlValue::Text(t) if t == "[]" => Ok(PyValue::List(Vec::new())),
        other => json_loads(other),
    }
}

/// Python's `==` between a JSON value and a column value.
fn py_eq(a: &PyValue, b: &SqlValue) -> bool {
    let number = |v: &PyValue| -> Option<Num> {
        match v {
            PyValue::Bool(x) => Some(Num::Int(i128::from(*x))),
            PyValue::Int(t) => Some(t.parse::<i128>().map_or(Num::Big, Num::Int)),
            PyValue::Float(f) => Some(Num::Float(*f)),
            _ => None,
        }
    };
    match (a, b) {
        (PyValue::None, SqlValue::Null) => true,
        (PyValue::Str(s), SqlValue::Text(t)) => *s == unicode::cps(t),
        (_, SqlValue::Integer(i)) => number(a).is_some_and(|n| n.eq(&Num::Int(i128::from(*i)))),
        (_, SqlValue::Real(f)) => number(a).is_some_and(|n| n.eq(&Num::Float(*f))),
        _ => false,
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Num {
    Int(i128),
    /// An int past i128: equal to no column value.
    Big,
    Float(f64),
}

impl Num {
    fn eq(&self, other: &Num) -> bool {
        match (*self, *other) {
            (Num::Int(a), Num::Int(b)) => a == b,
            (Num::Int(a), Num::Float(f)) | (Num::Float(f), Num::Int(a)) => {
                f.is_finite() && f.fract() == 0.0 && f.abs() < 1e38 && f as i128 == a
            }
            (Num::Float(a), Num::Float(b)) => a == b,
            _ => false,
        }
    }
}

fn dict_get<'v>(value: &'v PyValue, name: &str) -> Option<&'v PyValue> {
    let PyValue::Dict(items) = value else {
        return None;
    };
    let name = PyValue::str(name);
    items.iter().rev().find(|(k, _)| *k == name).map(|(_, v)| v)
}

/// `store_rows._row_origin`.
fn row_origin(row: &Row) -> Result<Option<Origin>, RecordError> {
    let legacy = load_json(row.at("origin_json")?, PyValue::None)?;
    if let Some(attestation) = row.get("attestation_payload").filter(|v| truthy(v)) {
        if let Some(origin) = native_origin(row, attestation)? {
            return Ok(Some(origin));
        }
    }
    match legacy {
        PyValue::None => Ok(None),
        value => Origin::from_value(&value).map(Some),
    }
}

/// The signed `native_origin` an attestation payload carries for this
/// row's record; None wherever the reader falls back to the legacy column
/// (every ValueError, TypeError and RecursionError on the way is caught).
fn native_origin(row: &Row, attestation: &SqlValue) -> Result<Option<Origin>, RecordError> {
    let Ok(signed) = json_loads(attestation) else {
        return Ok(None);
    };
    let record = ["memory", "tombstone", "episode"]
        .iter()
        .find_map(|k| dict_get(&signed, k));
    let Some(record @ PyValue::Dict(_)) = record else {
        return Ok(None);
    };
    let none = PyValue::None;
    let id = dict_get(record, "id").unwrap_or(&none);
    if !py_eq(id, row.at("id")?) {
        return Ok(None);
    }
    let Some(PyValue::Str(native)) = dict_get(&signed, "native_origin") else {
        return Ok(None);
    };
    match json::loads_detailed(native) {
        Ok(value) => Ok(Origin::from_value(&value).ok()),
        Err(_) => Ok(None),
    }
}

/// `store_rows._path_targets`: the target maps the signed payload
/// carries for this row's record.
fn path_targets(row: &Row) -> Result<Vec<(&'static str, PyValue)>, RecordError> {
    let Some(attestation) = row.get("attestation_payload") else {
        return Ok(Vec::new());
    };
    let payload = match attestation {
        SqlValue::Null => return Ok(Vec::new()),
        SqlValue::Integer(_) | SqlValue::Real(_) => return Err(RecordError::Raised("TypeError")),
        other => match json_loads(other) {
            Ok(p) => p,
            Err(_) => return Ok(Vec::new()),
        },
    };
    if !matches!(payload, PyValue::Dict(_)) {
        return Ok(Vec::new());
    }
    let record = dict_get(&payload, "memory")
        .or_else(|| dict_get(&payload, "tombstone"))
        .unwrap_or(&payload);
    if !matches!(record, PyValue::Dict(_)) {
        return Ok(Vec::new());
    }
    let none = PyValue::None;
    let id = dict_get(record, "id").unwrap_or(&none);
    if !py_eq(id, row.at("id")?) {
        return Ok(Vec::new());
    }
    Ok(["verified_path_targets", "verified_absent_path_targets"]
        .into_iter()
        .filter_map(|name| dict_get(record, name).map(|v| (name, shallow(v))))
        .collect())
}

/// A target map with every container below its values replaced by an
/// empty list, and a container that is not a dict by an empty list: the
/// model reads no deeper (a value must be text or null, the map a dict),
/// and the errors are the same, so a deep value is never cloned.
fn shallow(v: &PyValue) -> PyValue {
    let scalar = |v: &PyValue| match v {
        PyValue::List(_) | PyValue::Dict(_) | PyValue::Tuple(_) => PyValue::List(Vec::new()),
        other => other.clone(),
    };
    match v {
        PyValue::Dict(items) => {
            PyValue::Dict(items.iter().map(|(k, v)| (k.clone(), scalar(v))).collect())
        }
        other => scalar(other),
    }
}

/// Python truthiness of a column value.
fn truthy(v: &SqlValue) -> bool {
    match v {
        SqlValue::Null => false,
        SqlValue::Integer(i) => *i != 0,
        SqlValue::Real(f) => *f != 0.0,
        SqlValue::Text(t) => !t.is_empty(),
        SqlValue::Blob(b) => !b.is_empty(),
    }
}

/// `int(value)` of a column value.
fn py_int(v: &SqlValue) -> Result<PyValue, RecordError> {
    let raised = |e: PyError| RecordError::Raised(e.name());
    match v {
        SqlValue::Null => Err(RecordError::Raised("TypeError")),
        SqlValue::Integer(i) => Ok(PyValue::int(i128::from(*i))),
        SqlValue::Real(f) => pyvalue::py_int(&PyValue::Float(*f)).map_err(raised),
        SqlValue::Text(t) => pyvalue::int_from_str(&unicode::cps(t)).map_err(raised),
        SqlValue::Blob(b) => {
            if !b.is_ascii() || b.iter().any(|c| (0x1C..=0x1F).contains(c)) {
                return Err(RecordError::Raised("ValueError"));
            }
            let text: Vec<u32> = b.iter().map(|&c| u32::from(c)).collect();
            pyvalue::int_from_str(&text).map_err(raised)
        }
    }
}

/// `_anchor_or_none`: `str(value)` of a non-null column, kept only when it
/// is a full commit hash (only text can be one).
fn anchor_or_none(v: &SqlValue) -> PyValue {
    match v {
        SqlValue::Text(t) => commit_anchor(&unicode::cps(t)).map_or(PyValue::None, PyValue::Str),
        _ => PyValue::None,
    }
}

/// The actor column: `Actor.model_validate(_load_json(...))` when set.
fn actor(row: &Row) -> Result<Owned, RecordError> {
    match row.at("actor_json")? {
        SqlValue::Null => Ok(Owned::Py(PyValue::None)),
        other => {
            let value = load_json(other, PyValue::Dict(Vec::new()))?;
            super::Actor::from_value(&value)?;
            Ok(Owned::Py(value))
        }
    }
}

/// The links column: each entry through `MemoryLink.model_validate`, the
/// first that fails raising its ValidationError; iterating a value that
/// is not a container is TypeError.
fn links(row: &Row) -> Result<Owned, RecordError> {
    let mut value = load_json(row.at("links_json")?, PyValue::List(Vec::new()))?;
    // Taken, not cloned: a deep value is never walked recursively.
    let entries: Vec<PyValue> = match &mut value {
        PyValue::List(items) => std::mem::take(items),
        PyValue::Dict(items) => std::mem::take(items).into_iter().map(|(k, _)| k).collect(),
        PyValue::Str(s) => s.iter().map(|&c| PyValue::Str(vec![c])).collect(),
        _ => return Err(RecordError::Raised("TypeError")),
    };
    for entry in &entries {
        super::MemoryLink::from_value(entry)?;
    }
    Ok(Owned::Py(PyValue::List(entries)))
}

fn origin_value(origin: Option<Origin>) -> PyValue {
    origin.map_or(PyValue::None, |o| o.dump(false))
}

/// The keyword arguments of `Memory(...)` / `TombstonedMemory(...)`, in
/// evaluation order.
#[allow(clippy::vec_init_then_push)]
fn record_kwargs(row: &Row, tombstone: bool) -> Result<Vec<(&'static str, Owned)>, RecordError> {
    let mut kw: Vec<(&'static str, Owned)> = Vec::new();
    kw.push(("id", raw(row.at("id")?)));
    kw.push((
        "created",
        Owned::Py(PyValue::DateTime(dt(row.at("created")?)?)),
    ));
    kw.push((
        "updated",
        Owned::Py(PyValue::DateTime(dt(row.at("updated")?)?)),
    ));
    kw.push((
        "scopes",
        Owned::Py(load_json(
            row.at("scopes_json")?,
            PyValue::List(Vec::new()),
        )?),
    ));
    kw.push(("confidence", raw(row.at("confidence")?)));
    kw.push(("source", raw(row.at("source")?)));
    kw.push(("body", raw(row.at("body")?)));
    kw.push(("origin", Owned::Py(origin_value(row_origin(row)?))));
    kw.push(("actor", actor(row)?));
    let last = dt_opt(row.at("last_verified_at")?)?;
    kw.push((
        "last_verified_at",
        Owned::Py(last.map_or(PyValue::None, PyValue::DateTime)),
    ));
    kw.push(("category", raw(row.at("category")?)));
    for (field, column) in [
        ("verified_paths", "verified_paths_json"),
        ("verified_commits", "verified_commits_json"),
        ("verified_versions", "verified_versions_json"),
        ("verified_absent_paths", "verified_absent_paths_json"),
    ] {
        kw.push((
            field,
            Owned::Py(load_json(row.at(column)?, PyValue::List(Vec::new()))?),
        ));
    }
    for (name, value) in path_targets(row)? {
        kw.push((name, Owned::Py(value)));
    }
    kw.push((
        "claims",
        Owned::Py(load_json(
            row.at("claims_json")?,
            PyValue::List(Vec::new()),
        )?),
    ));
    kw.push((
        "verified_head",
        Owned::Py(anchor_or_none(row.at("verified_head")?)),
    ));
    if tombstone {
        kw.push((
            "removed",
            Owned::Py(PyValue::DateTime(dt(row.at("removed")?)?)),
        ));
        kw.push(("removed_reason", raw(row.at("removed_reason")?)));
        kw.push(("removed_session", raw(row.at("removed_session")?)));
    } else {
        kw.push(("links", links(row)?));
        kw.push((
            "corroborations",
            Owned::Py(py_int(row.at("corroborations")?)?),
        ));
        let last = dt_opt(row.at("last_corroborated")?)?;
        kw.push((
            "last_corroborated",
            Owned::Py(last.map_or(PyValue::None, PyValue::DateTime)),
        ));
    }
    Ok(kw)
}

fn fields_of<'a>(kw: &'a [(&'static str, Owned)]) -> Fields<'a> {
    let mut fields = Fields::default();
    for (name, value) in kw {
        fields.push(name, value.arg());
    }
    fields
}

/// `store_rows._row_to_memory`.
pub fn memory(row: &Row) -> Result<Memory, RecordError> {
    let kw = record_kwargs(row, false)?;
    Memory::validate(&fields_of(&kw))
}

/// `store_rows._row_to_tombstone`.
pub fn tombstone(row: &Row) -> Result<Tombstone, RecordError> {
    let kw = record_kwargs(row, true)?;
    Tombstone::validate(&fields_of(&kw))
}

/// `store_rows._row_to_episode`.
#[allow(clippy::vec_init_then_push)]
pub fn episode(row: &Row) -> Result<Episode, RecordError> {
    let mut kw: Vec<(&'static str, Owned)> = Vec::new();
    kw.push(("id", raw(row.at("id")?)));
    kw.push(("session_id", raw(row.at("session_id")?)));
    kw.push((
        "created",
        Owned::Py(PyValue::DateTime(dt(row.at("created")?)?)),
    ));
    kw.push(("body", raw(row.at("body")?)));
    kw.push((
        "scopes",
        Owned::Py(load_json(
            row.at("scopes_json")?,
            PyValue::List(Vec::new()),
        )?),
    ));
    kw.push(("takeaway", raw(row.at("takeaway")?)));
    kw.push(("origin", Owned::Py(origin_value(row_origin(row)?))));
    kw.push((
        "is_floor",
        Owned::Py(PyValue::Bool(truthy(row.at("is_floor")?))),
    ));
    kw.push(("swarm_id", raw(row.at("swarm_id")?)));
    Episode::validate(&fields_of(&kw))
}
