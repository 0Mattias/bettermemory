//! The digest's questions (`store_digest.py` 466-473, 549-740): the mark
//! a digest stands at (`_mark`), the shared counts one body's tokens find
//! (`_share`), the records that can score against it (`digest_candidates`,
//! `digest_shares`), and the per-store memo of answers (`digest_memo`).
//!
//! The mark is an argument: the caller brings the digest current
//! (`digest_mark`, the writing open's) and hands its answer in. A query
//! answers in the reference's order: no tokens, then no mark, answer
//! None; then the floor becomes thousandths (`int(floor * 1000)`, whose
//! NaN and infinity raise); then the reads, where any SQLite error answers
//! None with the warning the reference logs.

use std::collections::{BTreeSet, VecDeque};
use std::sync::Arc;

use rusqlite::types::{Type, Value, ValueRef};
use rusqlite::{params, Connection};
use sha2::{Digest, Sha256};

use super::digest::{self, DigestError};
use crate::pyvalue::{self, PyStr, PyValue};
use crate::search_dedup::{DigestDoc, CONTAINMENT_MIN_TOKENS, MEDIUM_SIMILARITY};
use crate::unicode;

/// `_mark(conn)`: `"{seen}:{mac}"` when the digest has seen the log's last
/// record mutation, else None. Errors leave as the reference's do (it
/// catches none; `digest_mark` does).
pub fn mark(conn: &Connection) -> Result<Option<String>, DigestError> {
    let state = digest::state(conn)?;
    let seen = digest::built(conn, state.as_deref())?;
    let (Some(seen), Some(state)) = (seen, state) else {
        return Ok(None);
    };
    if seen != digest::head(conn)? {
        return Ok(None);
    }
    let mac = state
        .iter()
        .find(|(k, _)| k == "mac")
        .map(|(_, v)| v.as_str())
        .unwrap_or("");
    Ok(Some(format!("{seen}:{mac}")))
}

/// What a query raises before its reads, or, for a floor past an SQLite
/// INTEGER, as it binds it (after `_share` has run).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum QueryError {
    /// `int(nan)`, or `int()` of a text in `n` or `claim` that is not a
    /// numeral: ValueError.
    Value(String),
    /// `int(None)` of a NULL in `n` or `claim`: TypeError.
    Type(String),
    /// `int(inf)`, or the bound floor past an SQLite INTEGER: OverflowError.
    Overflow(&'static str),
}

/// `int(float('nan'))`'s message.
pub const NAN_TEXT: &str = "cannot convert float NaN to integer";
/// `int(float('inf'))`'s message.
pub const INF_TEXT: &str = "cannot convert float infinity to integer";

/// One query's answer: the rows (None when the digest is not available)
/// and the warning the reference logs when a read failed.
#[derive(Clone, Debug, PartialEq)]
pub struct Answer<T> {
    pub rows: Option<Vec<T>>,
    pub warning: Option<String>,
}

impl<T> Answer<T> {
    fn none() -> Self {
        Answer {
            rows: None,
            warning: None,
        }
    }
}

/// One row of `digest_candidates`: the record as `DigestDoc` holds it.
#[derive(Clone, Debug)]
pub struct Candidate {
    pub doc: DigestDoc,
    /// `int(n)`: a `PyValue::Int`, as large as the column's value makes it.
    pub n: PyValue,
    pub claim: PyValue,
    pub stamped: bool,
}

/// `DigestShare`: a candidate's sizes and its shared count.
#[derive(Clone, Debug, PartialEq)]
pub struct DigestShare {
    pub id: String,
    pub n: PyValue,
    pub claim: PyValue,
    pub stamped: bool,
    pub shared: i64,
}

/// The hex sha256 `_share` names a query by: the mark, a newline, and the
/// expanded tokens sorted and joined by newlines.
pub fn asked(mark: &str, tokens: &BTreeSet<String>) -> String {
    let query: Vec<String> = digest::expanded(tokens).into_iter().collect();
    let text = format!("{mark}\n{}", query.join("\n"));
    hex(&Sha256::digest(text.as_bytes()))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Whether the reference's sqlite3 raises this error as OperationalError
/// (`_pysqlite_seterror`'s mapping of the primary result code).
fn is_operational(e: &rusqlite::Error) -> bool {
    match e {
        // ERROR, PERM, ABORT, BUSY, LOCKED, READONLY, INTERRUPT, IOERR,
        // FULL, CANTOPEN, PROTOCOL, EMPTY, SCHEMA.
        rusqlite::Error::SqliteFailure(f, _) => matches!(
            f.extended_code & 0xff,
            1 | 3 | 4 | 5 | 6 | 8 | 9 | 10 | 13 | 14 | 15 | 16 | 17
        ),
        _ => false,
    }
}

/// `_share(conn, mark, tokens)`: fill `temp.digest_shared` for the asked
/// query unless `temp.digest_asked` already names it.
pub fn share(conn: &Connection, mark: &str, tokens: &BTreeSet<String>) -> rusqlite::Result<()> {
    let query = digest::expanded(tokens);
    let asked = asked(mark, tokens);
    let held = (|| -> rusqlite::Result<bool> {
        let mut statement = conn.prepare("SELECT asked FROM temp.digest_asked")?;
        let mut rows = statement.query([])?;
        Ok(match rows.next()? {
            Some(row) => matches!(row.get_ref(0)?, ValueRef::Text(t) if t == asked.as_bytes()),
            None => false,
        })
    })();
    match held {
        Ok(true) => return Ok(()),
        Ok(false) => {}
        Err(e) if is_operational(&e) => {
            conn.execute_batch(
                "CREATE TEMP TABLE IF NOT EXISTS digest_query(token TEXT PRIMARY KEY) WITHOUT ROWID;\
                 CREATE TEMP TABLE IF NOT EXISTS digest_shared(\
                 doc INTEGER PRIMARY KEY, shared INTEGER, removed INTEGER, n INTEGER, claim INTEGER);\
                 CREATE TEMP TABLE IF NOT EXISTS digest_asked(asked TEXT)",
            )?;
        }
        Err(e) => return Err(e),
    }
    conn.execute("DELETE FROM temp.digest_asked", [])?;
    conn.execute("DELETE FROM temp.digest_query", [])?;
    conn.execute("DELETE FROM temp.digest_shared", [])?;
    let mut insert = conn.prepare("INSERT INTO temp.digest_query(token) VALUES (?)")?;
    for token in &query {
        insert.execute([token])?;
    }
    conn.execute(
        "INSERT INTO temp.digest_shared(doc, shared, removed, n, claim) \
         SELECT s.doc, s.shared, d.removed, d.n, d.claim FROM (\
           SELECT p.doc AS doc, COUNT(*) AS shared \
           FROM temp.digest_query q JOIN digest_tokens p ON p.token = q.token \
           GROUP BY p.doc) s JOIN digest_records d ON d.doc = s.doc",
        [],
    )?;
    conn.execute("INSERT INTO temp.digest_asked(asked) VALUES (?)", [&asked])?;
    Ok(())
}

/// `int(floor * 1000)`: truncated toward zero; NaN and infinity raise.
/// The answer may lie past an SQLite INTEGER (a float, exact).
fn floor_milli(floor: f64) -> Result<f64, QueryError> {
    let v = floor * 1000.0;
    if v.is_nan() {
        return Err(QueryError::Value(NAN_TEXT.into()));
    }
    if v.is_infinite() {
        return Err(QueryError::Overflow(INF_TEXT));
    }
    Ok(v.trunc())
}

const TWO_63: f64 = 9_223_372_036_854_775_808.0;

// The two public queries' arguments, plus the column and reader that
// tell them apart.
#[allow(clippy::too_many_arguments)]
fn query<T>(
    conn: &Connection,
    path: &str,
    mark: Option<&str>,
    tokens: &BTreeSet<String>,
    removed: bool,
    floor: f64,
    claims: bool,
    last: &str,
    read: impl Fn(Vec<Value>) -> Result<T, QueryError>,
) -> Result<Answer<T>, QueryError> {
    if tokens.is_empty() {
        return Ok(Answer::none());
    }
    let Some(mark) = mark else {
        return Ok(Answer::none());
    };
    let size = if claims { "s.claim" } else { "s.n" };
    let n = tokens.len() as i64;
    let milli = floor_milli(floor)?;
    let medium_milli = (MEDIUM_SIMILARITY * 1000.0) as i64;
    let contained = -((-medium_milli * CONTAINMENT_MIN_TOKENS as i64).div_euclid(1000));
    let warn = |e: rusqlite::Error| Answer {
        rows: None,
        warning: Some(format!(
            "store {path}: the digest could not be read ({})",
            sqlite_message(&e)
        )),
    };
    if let Err(e) = share(conn, mark, tokens) {
        return Ok(warn(e));
    }
    // Binding a whole number past an SQLite INTEGER raises OverflowError,
    // which `except sqlite3.Error` does not catch.
    if !(-TWO_63..TWO_63).contains(&milli) {
        return Err(QueryError::Overflow(digest::OVERFLOW_TEXT));
    }
    let sql = format!(
        "SELECT d.id, d.n, d.claim, d.stamped, {last} \
         FROM temp.digest_shared s JOIN digest_records d ON d.doc = s.doc \
         WHERE s.removed = ? AND {size} > 0 AND (\
           s.shared * 1000 >= ? * MAX(?, {size}) \
           OR (s.shared >= ? AND s.shared * 1000 >= ? * MIN(?, {size})))"
    );
    // `fetchall()` inside the `try`: a text not UTF-8 in any column is
    // OperationalError there. The conversions run after it, outside the
    // `try`, row by row and column by column, so what they raise leaves.
    let run = || -> rusqlite::Result<Vec<Vec<Value>>> {
        let mut statement = conn.prepare(&sql)?;
        let rows = statement.query_map(
            params![removed as i64, milli as i64, n, contained, medium_milli, n],
            |row| (0..5).map(|i| fetched(row, i)).collect(),
        )?;
        rows.collect()
    };
    match run() {
        Ok(rows) => Ok(Answer {
            rows: Some(rows.into_iter().map(read).collect::<Result<_, _>>()?),
            warning: None,
        }),
        Err(e) => Ok(warn(e)),
    }
}

/// `str(exc)` of an sqlite3 error: SQLite's own message.
fn sqlite_message(e: &rusqlite::Error) -> String {
    match e {
        rusqlite::Error::SqliteFailure(_, Some(m)) => m.clone(),
        rusqlite::Error::SqliteFailure(f, None) => {
            rusqlite::ffi::code_to_str(f.extended_code).to_owned()
        }
        other => other.to_string(),
    }
}

fn unreadable(i: usize, t: Type) -> rusqlite::Error {
    rusqlite::Error::InvalidColumnType(i, "a value the reference does not read".into(), t)
}

/// A column as `fetchall` hands it over: a text not UTF-8 is the
/// reference's OperationalError at fetch.
fn fetched(row: &rusqlite::Row<'_>, i: usize) -> rusqlite::Result<Value> {
    Ok(match row.get_ref(i)? {
        ValueRef::Text(t) => match std::str::from_utf8(t) {
            Ok(s) => Value::Text(s.to_owned()),
            Err(_) => return Err(unreadable(i, Type::Text)),
        },
        ValueRef::Integer(v) => Value::Integer(v),
        ValueRef::Real(v) => Value::Real(v),
        ValueRef::Null => Value::Null,
        ValueRef::Blob(b) => Value::Blob(b.to_vec()),
    })
}

/// `str(value)` of a fetched column; a blob is `repr(bytes)`.
fn text(value: &Value) -> String {
    match value {
        Value::Text(t) => t.clone(),
        Value::Integer(v) => v.to_string(),
        Value::Real(v) => crate::float_repr::repr(*v),
        Value::Null => "None".into(),
        Value::Blob(b) => bytes_repr(b),
    }
}

/// `repr(bytes)`.
fn bytes_repr(b: &[u8]) -> String {
    let quote = if b.contains(&b'\'') && !b.contains(&b'"') {
        '"'
    } else {
        '\''
    };
    let mut out = format!("b{quote}");
    for &c in b {
        match c {
            b'\\' => out.push_str("\\\\"),
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            _ if c as char == quote => {
                out.push('\\');
                out.push(quote);
            }
            0x20..=0x7e => out.push(c as char),
            _ => out.push_str(&format!("\\x{c:02x}")),
        }
    }
    out.push(quote);
    out
}

/// `int(value)` of a fetched column, as Python answers it: an integer;
/// a real truncated (NaN ValueError, infinity OverflowError); a text or
/// a blob read as a base-10 numeral (`str` takes Unicode digits and
/// spaces, `bytes` only ASCII ones), else ValueError; None TypeError.
/// The ValueError's text is `invalid literal for int() with base 10:`
/// and the value's repr; the fixture pins only the exception's type.
fn int(value: &Value) -> Result<PyValue, QueryError> {
    let literal = |cps: &[u32], shown: String| {
        pyvalue::int_from_str(cps).map_err(|_| {
            QueryError::Value(format!("invalid literal for int() with base 10: {shown}"))
        })
    };
    match value {
        Value::Integer(v) => Ok(PyValue::int(i128::from(*v))),
        Value::Real(v) if v.is_nan() => Err(QueryError::Value(NAN_TEXT.into())),
        Value::Real(v) if v.is_infinite() => Err(QueryError::Overflow(INF_TEXT)),
        Value::Real(v) => {
            pyvalue::py_int(&PyValue::Float(*v)).map_err(|_| QueryError::Value(NAN_TEXT.into()))
        }
        Value::Text(t) => {
            let cps: Vec<u32> = t.chars().map(u32::from).collect();
            let shown = pyvalue::repr_str(&cps)
                .into_iter()
                .filter_map(char::from_u32)
                .collect();
            literal(&cps, shown)
        }
        Value::Blob(b) if b.is_ascii() => {
            let cps: Vec<u32> = b.iter().map(|&c| u32::from(c)).collect();
            literal(&cps, bytes_repr(b))
        }
        Value::Blob(b) => Err(QueryError::Value(format!(
            "invalid literal for int() with base 10: {}",
            bytes_repr(b)
        ))),
        Value::Null => Err(QueryError::Type(
            "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"
                .into(),
        )),
    }
}

/// `bool(value)` of a fetched column.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Integer(v) => *v != 0,
        Value::Real(v) => *v != 0.0,
        Value::Text(t) => !t.is_empty(),
        Value::Blob(t) => !t.is_empty(),
        Value::Null => false,
    }
}

/// `str.split()`: runs of `str.isspace` separate the words.
fn split(text: &str) -> BTreeSet<String> {
    text.split(|c: char| unicode::is_space(c as u32))
        .filter(|w| !w.is_empty())
        .map(str::to_owned)
        .collect()
}

/// `str(row[0])`, `int(row[1])`, `int(row[2])`, `bool(row[3])`, in order.
fn sizes(row: &[Value]) -> Result<(String, PyValue, PyValue, bool), QueryError> {
    Ok((text(&row[0]), int(&row[1])?, int(&row[2])?, truthy(&row[3])))
}

/// `digest_candidates(tokens, removed=, floor=, claims=)` over `conn`, the
/// store at `path`, with the digest's `mark`.
pub fn digest_candidates(
    conn: &Connection,
    path: &str,
    mark: Option<&str>,
    tokens: &BTreeSet<String>,
    removed: bool,
    floor: f64,
    claims: bool,
) -> Result<Answer<Candidate>, QueryError> {
    query(
        conn,
        path,
        mark,
        tokens,
        removed,
        floor,
        claims,
        "d.tokens",
        |row| {
            let (id, n, claim, stamped) = sizes(&row)?;
            Ok(Candidate {
                doc: DigestDoc {
                    id: id.chars().map(u32::from).collect::<PyStr>(),
                    tokens: Arc::new(split(&text(&row[4]))),
                },
                n,
                claim,
                stamped,
            })
        },
    )
}

/// `digest_shares(tokens, removed=, floor=, claims=)`.
pub fn digest_shares(
    conn: &Connection,
    path: &str,
    mark: Option<&str>,
    tokens: &BTreeSet<String>,
    removed: bool,
    floor: f64,
    claims: bool,
) -> Result<Answer<DigestShare>, QueryError> {
    query(
        conn,
        path,
        mark,
        tokens,
        removed,
        floor,
        claims,
        "s.shared",
        |row| {
            let (id, n, claim, stamped) = sizes(&row)?;
            // `int(row[4])`: a COUNT(*) the port's own temp table holds.
            let shared = match &row[4] {
                Value::Integer(v) => *v,
                other => match &int(other)? {
                    PyValue::Int(t) => t.parse().unwrap_or(i64::MAX),
                    _ => i64::MAX,
                },
            };
            Ok(DigestShare {
                id,
                n,
                claim,
                stamped,
                shared,
            })
        },
    )
}

/// `_MEMO_ENTRIES`.
pub const MEMO_ENTRIES: usize = 16;

/// `digest_memo`'s held answers: the mark they were computed under and at
/// most [`MEMO_ENTRIES`] answers, least recently used first.
#[derive(Clone, Debug)]
pub struct DigestMemo<K, A> {
    mark: Option<String>,
    answers: VecDeque<(K, A)>,
}

impl<K, A> Default for DigestMemo<K, A> {
    fn default() -> Self {
        DigestMemo {
            mark: None,
            answers: VecDeque::new(),
        }
    }
}

impl<K: PartialEq, A: Clone> DigestMemo<K, A> {
    pub fn new() -> Self {
        Self::default()
    }

    /// `digest_memo(key, compute)` with the digest's `mark`: no mark
    /// computes and keeps nothing; a new mark drops what is held; a held
    /// key moves to the end; an answer of None is not kept.
    pub fn get_or(
        &mut self,
        mark: Option<&str>,
        key: K,
        compute: impl FnOnce() -> Option<A>,
    ) -> Option<A> {
        let Some(mark) = mark else {
            return compute();
        };
        if self.mark.as_deref() != Some(mark) {
            self.answers.clear();
            self.mark = Some(mark.to_owned());
        }
        if let Some(at) = self.answers.iter().position(|(k, _)| *k == key) {
            let entry = self.answers.remove(at).expect("a held entry");
            let answer = entry.1.clone();
            self.answers.push_back(entry);
            return Some(answer);
        }
        let answer = compute()?;
        self.answers.push_back((key, answer.clone()));
        while self.answers.len() > MEMO_ENTRIES {
            self.answers.pop_front();
        }
        Some(answer)
    }

    /// The held keys, least recently used first.
    pub fn keys(&self) -> impl Iterator<Item = &K> {
        self.answers.iter().map(|(k, _)| k)
    }
}

#[cfg(test)]
mod tests {
    use super::is_operational;

    fn failure(code: i32) -> rusqlite::Error {
        rusqlite::Error::SqliteFailure(rusqlite::ffi::Error::new(code), None)
    }

    #[test]
    fn operational_codes_are_the_references_mapping() {
        let operational = [1, 3, 4, 5, 6, 8, 9, 10, 13, 14, 15, 16, 17];
        for code in 0..=28 {
            assert_eq!(
                is_operational(&failure(code)),
                operational.contains(&code),
                "code {code}"
            );
        }
        // The primary code decides: an extended CANTOPEN is still one.
        assert!(is_operational(&failure(14 | (1 << 8))));
        assert!(!is_operational(&rusqlite::Error::QueryReturnedNoRows));
    }
}
