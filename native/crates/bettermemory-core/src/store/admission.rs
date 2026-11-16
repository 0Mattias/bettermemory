//! `store_admission`: the check every opener runs on a store file before it
//! reads a value from it (`admit_store`), the deadline its reads run under
//! (`_deadline`), the refusal (`StoreRefusedError`, `LaterStoreError`) and
//! an admitted file's meta read (`read_store_meta`).
//!
//! What is admitted is the reference's own schema, built from the texts it
//! creates a store from (`reference_schema.json`, a copy of
//! `store_schema.SCHEMA`, `MEMBERSHIP_SCHEMA`, `ADDITIVE_SCHEMA`,
//! `PROPOSALS_INDEX_SCHEMA` and `SESSION_INDEX_SCHEMA` and of
//! `_OPTIONAL_COLUMNS`, pinned to `store_admission.json` by a test), run in
//! an in-memory database as the reference runs them, and what a later
//! build of the major may add: plain tables and indexes on columns.
//!
//! The reference's regular expressions are written out by hand over
//! Python's own character classes (`\w`, `\s`, `\d` as `re` reads them
//! for a `str` pattern; `re.I`'s case equivalents, where `i` also matches
//! U+0130 and U+0131, `k` U+212A and `s` U+017F), so they match exactly
//! what the reference's match.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering::Relaxed;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use rusqlite::types::ValueRef;
use rusqlite::{Connection, ErrorCode};

use crate::json;
use crate::pyvalue::{repr_str, PyValue};
use crate::unicode;

/// `ADMISSION_DEADLINE_SECONDS`.
pub const ADMISSION_DEADLINE: Duration = Duration::from_secs(2);
/// `_PROGRESS_STEPS`.
pub const PROGRESS_STEPS: i32 = 100;
/// `_ADMISSION_MAX_LENGTH`.
pub const ADMISSION_MAX_LENGTH: i32 = 1 << 14;

const REFERENCE_SCHEMA: &str = include_str!("reference_schema.json");

/// `StoreRefusedError` (and `LaterStoreError`): the file is not opened.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    /// `why`: the text after "is not opened: ".
    pub why: String,
    pub timed_out: bool,
    pub holds_meta: Option<bool>,
    /// A `LaterStoreError`: meta says a later build wrote the file.
    pub later: Option<Box<Later>>,
    /// The reference does not refuse this file but raises out of
    /// `connect_read_only` with this message (`sqlite3.OperationalError`).
    pub reference_raises: Option<String>,
}

/// What `_refuse_later` read: the revision needed, and the parts of the
/// message that do not name the path or this build's version.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Later {
    pub key: &'static str,
    pub needed: i64,
    /// The inner reason (`why` before it is wrapped).
    pub inner: String,
    revision: i64,
    reader_above: bool,
    newest: String,
}

impl Refusal {
    /// `str(exc)`: `"{path} is not opened: {why}"`, or a later store's
    /// message, with `version` as the reference's `__version__`.
    pub fn message(&self, path: &str, version: &str) -> String {
        match &self.later {
            None => format!("{path} is not opened: {}", self.why),
            Some(l) => {
                let ours = format!(
                    "this build, bettermemory {version}, reads revision {}",
                    super::SCHEMA_REVISION
                );
                let said = if l.reader_above {
                    format!(
                        "needs a reader at schema revision {} or later (meta's {}){}; {ours}",
                        l.needed, l.key, l.newest
                    )
                } else {
                    format!(
                        "is at schema revision {} (meta's {}){}, and {ours}",
                        l.revision, l.key, l.newest
                    )
                };
                format!(
                    "{path} {said} and does not admit what it holds ({}): upgrade bettermemory \
                     to open it",
                    l.inner
                )
            }
        }
    }
}

/// What admitting a file can end in besides a refusal: an SQLite error
/// the reference lets through.
#[derive(Debug)]
pub enum AdmitError {
    Refused(Refusal),
    Sqlite(rusqlite::Error),
    /// A value the caller's reader refused.
    Store(Box<super::StoreError>),
}

impl AdmitError {
    pub fn from_store(e: super::StoreError) -> Self {
        AdmitError::Store(Box::new(e))
    }
}

impl From<rusqlite::Error> for AdmitError {
    fn from(e: rusqlite::Error) -> Self {
        AdmitError::Sqlite(e)
    }
}

fn repr(text: &str) -> String {
    json::utf8_text(&repr_str(&unicode::cps(text)))
}

fn lower(text: &str) -> String {
    json::utf8_text(&unicode::lower(&unicode::cps(text)))
}

fn upper(text: &str) -> String {
    json::utf8_text(&unicode::upper(&unicode::cps(text)))
}

// ---------------------------------------------------------------------------
// Python's `re` classes, by hand
// ---------------------------------------------------------------------------

fn is_s(c: char) -> bool {
    unicode::is_space(c as u32)
}

fn is_w(c: char) -> bool {
    unicode::is_word(c as u32)
}

fn is_d(c: char) -> bool {
    unicode::is_re_digit(c as u32)
}

/// `re.I`'s equivalence of `c` with the ASCII letter or digit `want`.
fn ci(c: char, want: char) -> bool {
    let w = want.to_ascii_lowercase();
    c.to_ascii_lowercase() == w
        || match w {
            'i' => c == '\u{130}' || c == '\u{131}',
            'k' => c == '\u{212a}',
            's' => c == '\u{17f}',
            _ => false,
        }
}

fn word_at(s: &[char], i: usize) -> bool {
    s.get(i).is_some_and(|&c| is_w(c))
}

/// `\b` at `i`.
fn boundary(s: &[char], i: usize) -> bool {
    let before = i > 0 && word_at(s, i - 1);
    before != word_at(s, i)
}

/// A keyword, case-insensitively, at `i`: its end.
fn keyword(s: &[char], i: usize, word: &str) -> Option<usize> {
    let n = word.chars().count();
    if i + n > s.len() {
        return None;
    }
    word.chars()
        .zip(&s[i..i + n])
        .all(|(w, &c)| ci(c, w))
        .then_some(i + n)
}

fn spaces(s: &[char], mut i: usize) -> usize {
    while i < s.len() && is_s(s[i]) {
        i += 1;
    }
    i
}

fn spaces1(s: &[char], i: usize) -> Option<usize> {
    let j = spaces(s, i);
    (j > i).then_some(j)
}

fn name_chars(s: &[char], mut i: usize, dollar: bool) -> Option<usize> {
    let start = i;
    while i < s.len() && (is_w(s[i]) || (dollar && s[i] == '$')) {
        i += 1;
    }
    (i > start).then_some(i)
}

/// `_QUOTED`'s match at `i`, if any: its end.
fn quoted_at(s: &[char], i: usize) -> Option<usize> {
    match s[i] {
        q @ ('\'' | '"' | '`') => {
            // `q(?:[^q]|qq)*q`: escapes taken first, backing off to the
            // last escape's first quote when no closing quote follows.
            let mut j = i + 1;
            let mut last_pair = None;
            while j < s.len() {
                if s[j] == q {
                    if s.get(j + 1) == Some(&q) {
                        last_pair = Some(j);
                        j += 2;
                        continue;
                    }
                    return Some(j + 1);
                }
                j += 1;
            }
            last_pair.map(|p| p + 1)
        }
        '[' => s[i + 1..]
            .iter()
            .position(|&c| c == ']')
            .map(|p| i + 1 + p + 1),
        '-' if s.get(i + 1) == Some(&'-') => {
            let rest = s[i..].iter().position(|&c| c == '\n');
            Some(rest.map_or(s.len(), |p| i + p))
        }
        '/' if s.get(i + 1) == Some(&'*') => {
            let mut j = i + 2;
            while j + 1 < s.len() {
                if s[j] == '*' && s[j + 1] == '/' {
                    return Some(j + 2);
                }
                j += 1;
            }
            None
        }
        _ => None,
    }
}

/// `_QUOTED.sub(replace, text)`.
fn sub_quoted(text: &str, replace: impl Fn(char) -> &'static str) -> Vec<char> {
    let s: Vec<char> = text.chars().collect();
    let mut out = Vec::with_capacity(s.len());
    let mut i = 0;
    while i < s.len() {
        match quoted_at(&s, i) {
            Some(end) => {
                out.extend(replace(s[i]).chars());
                i = end;
            }
            None => {
                out.push(s[i]);
                i += 1;
            }
        }
    }
    out
}

/// `_blanked(sql)`.
fn blanked(sql: Option<&str>) -> Vec<char> {
    sub_quoted(sql.unwrap_or(""), |first| match first {
        '\'' => "'",
        '-' | '/' => " ",
        _ => " q ",
    })
}

/// `\s*CREATE\s+<word>\b` at the start (`_VIRTUAL`, `_UNIQUE`).
fn create_then(s: &[char], word: &str) -> bool {
    let i = spaces(s, 0);
    let Some(i) = keyword(s, i, "CREATE") else {
        return false;
    };
    let Some(i) = spaces1(s, i) else {
        return false;
    };
    keyword(s, i, word).is_some_and(|j| boundary(s, j))
}

fn is_virtual(s: &[char]) -> bool {
    create_then(s, "VIRTUAL")
}

/// `_COLUMN` at `i`: its end.
fn index_column(s: &[char], i: usize) -> Option<usize> {
    let mut i = name_chars(s, i, true)?;
    // (?:\s+COLLATE\s+\w+)?
    let collate = spaces1(s, i)
        .and_then(|j| keyword(s, j, "COLLATE"))
        .and_then(|j| spaces1(s, j))
        .and_then(|j| name_chars(s, j, false));
    if let Some(j) = collate {
        i = j;
    }
    // (?:\s+(?:ASC|DESC))?
    let order = spaces1(s, i).and_then(|j| keyword(s, j, "ASC").or_else(|| keyword(s, j, "DESC")));
    if let Some(j) = order {
        // Taken only when what follows can still match; otherwise the
        // regex backs off, and fails there too (see `plain_index`).
        i = j;
    }
    Some(i)
}

/// `_PLAIN_INDEX.fullmatch(s)`.
fn plain_index(s: &[char]) -> bool {
    let go = || -> Option<usize> {
        let mut i = spaces(s, 0);
        i = keyword(s, i, "CREATE")?;
        i = spaces1(s, i)?;
        if let Some(j) = keyword(s, i, "UNIQUE").and_then(|j| spaces1(s, j)) {
            if keyword(s, j, "INDEX").is_some() {
                i = j;
            }
        }
        i = keyword(s, i, "INDEX")?;
        i = spaces1(s, i)?;
        i = name_chars(s, i, true)?;
        i = spaces1(s, i)?;
        i = keyword(s, i, "ON")?;
        i = spaces1(s, i)?;
        i = name_chars(s, i, true)?;
        i = spaces(s, i);
        (s.get(i) == Some(&'(')).then_some(())?;
        i = spaces(s, i + 1);
        i = index_column(s, i)?;
        loop {
            let j = spaces(s, i);
            if s.get(j) == Some(&',') {
                let k = spaces(s, j + 1);
                match index_column(s, k) {
                    Some(end) => {
                        i = end;
                        continue;
                    }
                    None => break,
                }
            }
            break;
        }
        i = spaces(s, i);
        (s.get(i) == Some(&')')).then_some(())?;
        i = spaces(s, i + 1);
        if s.get(i) == Some(&';') {
            i += 1;
        }
        Some(spaces(s, i))
    };
    go() == Some(s.len())
}

/// `re.search(r"\b<words>\b", s, re.I)` for `CHECK` and `WITHOUT\s+ROWID`.
fn search_words(s: &[char], words: &[&str]) -> bool {
    (0..s.len()).any(|start| {
        if !boundary(s, start) {
            return false;
        }
        let mut i = start;
        for (n, w) in words.iter().enumerate() {
            if n > 0 {
                match spaces1(s, i) {
                    Some(j) => i = j,
                    None => return false,
                }
            }
            match keyword(s, i, w) {
                Some(j) => i = j,
                None => return false,
            }
        }
        boundary(s, i)
    })
}

/// The ends the string alternative `'(?:[^']|'')*'` (or `X'[0-9A-F]*'`)
/// can match at `i`.
fn literal_ends(s: &[char], i: usize) -> Vec<usize> {
    let mut ends = Vec::new();
    let at = |j: usize| s.get(j).copied();
    // NULL | TRUE | FALSE
    for w in ["NULL", "TRUE", "FALSE"] {
        if let Some(j) = keyword(s, i, w) {
            ends.push(j);
        }
    }
    // [-+]?(0X[0-9A-F]+|(\d+(\.\d*)?|\.\d+)(E[-+]?\d+)?)
    let mut j = i;
    if matches!(at(j), Some('-' | '+')) {
        j += 1;
    }
    let hex = |c: char| c.is_ascii_hexdigit();
    if at(j) == Some('0') && at(j + 1).is_some_and(|c| ci(c, 'X')) {
        let mut k = j + 2;
        while at(k).is_some_and(hex) {
            k += 1;
            ends.push(k);
        }
    }
    let mut mantissas = Vec::new();
    let mut k = j;
    while at(k).is_some_and(is_d) {
        k += 1;
        mantissas.push(k);
    }
    if k > j && at(k) == Some('.') {
        let mut m = k + 1;
        mantissas.push(m);
        while at(m).is_some_and(is_d) {
            m += 1;
            mantissas.push(m);
        }
    }
    if at(j) == Some('.') {
        let mut m = j + 1;
        while at(m).is_some_and(is_d) {
            m += 1;
            mantissas.push(m);
        }
    }
    for &m in &mantissas {
        ends.push(m);
        if at(m).is_some_and(|c| ci(c, 'E')) {
            let mut e = m + 1;
            if matches!(at(e), Some('-' | '+')) {
                e += 1;
            }
            while at(e).is_some_and(is_d) {
                e += 1;
                ends.push(e);
            }
        }
    }
    // '(?:[^']|'')*'
    if at(i) == Some('\'') {
        let mut j = i + 1;
        while j < s.len() {
            if s[j] == '\'' {
                ends.push(j + 1);
                if at(j + 1) == Some('\'') {
                    j += 2;
                    continue;
                }
                break;
            }
            j += 1;
        }
    }
    // X'[0-9A-F]*'
    if at(i).is_some_and(|c| ci(c, 'X')) && at(i + 1) == Some('\'') {
        let mut j = i + 2;
        while at(j).is_some_and(hex) {
            j += 1;
        }
        if at(j) == Some('\'') {
            ends.push(j + 1);
        }
    }
    ends
}

/// `_LITERAL.fullmatch(text)`.
fn is_literal(text: &str) -> bool {
    let s: Vec<char> = text.chars().collect();
    let mut i = 0;
    while s.get(i) == Some(&'(') {
        i += 1;
    }
    let i = spaces(&s, i);
    literal_ends(&s, i).into_iter().any(|end| {
        let mut j = spaces(&s, end);
        while s.get(j) == Some(&')') {
            j += 1;
        }
        j == s.len()
    })
}

// ---------------------------------------------------------------------------
// The reference's schema
// ---------------------------------------------------------------------------

/// `_SQLITE_STATISTICS`.
fn statistics_text(name: &str) -> Option<&'static str> {
    match name {
        "sqlite_stat1" => Some("CREATE TABLE sqlite_stat1(tbl,idx,stat)"),
        "sqlite_stat4" => Some("CREATE TABLE sqlite_stat4(tbl,idx,neq,nlt,ndlt,sample)"),
        _ => None,
    }
}

type Built = BTreeMap<(String, String), (String, Option<String>, bool)>;

type Column = (&'static str, bool, Option<String>, i64);

#[derive(Clone, Debug, PartialEq, Eq)]
struct Shape {
    /// In SQLite's column order; a later column of the same lowered name
    /// replaces an earlier one, as the reference's dict does.
    columns: Vec<(String, Column)>,
    keys: BTreeSet<Vec<(String, String)>>,
    without_rowid: bool,
}

struct Reference {
    built: Built,
    shapes: BTreeMap<String, Shape>,
    optional: BTreeMap<String, Vec<String>>,
    texts: Vec<String>,
}

/// A cell as `str()` would give it. Text that is not UTF-8 is an error,
/// never a lossy string: a name looked up or compared must be the bytes the
/// file holds (the reference's fetch raises on it too).
fn text_of(v: ValueRef<'_>) -> rusqlite::Result<Option<String>> {
    Ok(match v {
        ValueRef::Null => None,
        ValueRef::Integer(i) => Some(i.to_string()),
        ValueRef::Real(f) => Some(crate::float_repr::repr(f)),
        ValueRef::Text(t) | ValueRef::Blob(t) => Some(
            std::str::from_utf8(t)
                .map_err(|e| rusqlite::Error::Utf8Error(0, e))?
                .to_owned(),
        ),
    })
}

/// `str(value)` of a pragma's cell (None as "None").
fn str_of(v: ValueRef<'_>) -> rusqlite::Result<String> {
    Ok(text_of(v)?.unwrap_or_else(|| "None".to_owned()))
}

fn int_of(v: ValueRef<'_>) -> i64 {
    match v {
        ValueRef::Integer(i) => i,
        _ => 0,
    }
}

fn quote_name(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

type MasterRow = (String, String, String, Option<String>);

const MASTER_COLUMNS: [&str; 4] = ["type", "name", "tbl_name", "sql"];

/// What makes a schema row unreadable as text: the column, and either the
/// error Python's sqlite3 raises fetching it (text that is not UTF-8) or
/// None (a value that is not TEXT, which the reference fails on later).
pub(crate) type Undecodable = (&'static str, Option<String>, String);

fn master_rows(conn: &Connection) -> rusqlite::Result<Result<Vec<MasterRow>, Undecodable>> {
    let mut statement = conn.prepare("SELECT type, name, tbl_name, sql FROM sqlite_master")?;
    let mut rows = statement.query([])?;
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        let mut cells: Vec<Option<String>> = Vec::with_capacity(4);
        for (i, column) in MASTER_COLUMNS.iter().enumerate() {
            match row.get_ref(i)? {
                ValueRef::Text(t) => match std::str::from_utf8(t) {
                    Ok(text) => cells.push(Some(text.to_owned())),
                    Err(_) => {
                        // pysqlite's message; the text shown is for the
                        // message only, never looked up.
                        let shown = String::from_utf8_lossy(t);
                        let raised = format!(
                            "Could not decode to UTF-8 column '{column}' with text '{shown}'"
                        );
                        return Ok(Err((column, Some(raised), shown.into_owned())));
                    }
                },
                ValueRef::Null if i == 3 => cells.push(None),
                other => {
                    let shown = format!("{:?}", other.data_type());
                    return Ok(Err((column, None, shown)));
                }
            }
        }
        let mut cells = cells.into_iter();
        let mut next = || cells.next().flatten();
        let (t, n, b) = (next(), next(), next());
        out.push((
            t.unwrap_or_default(),
            n.unwrap_or_default(),
            b.unwrap_or_default(),
            next(),
        ));
    }
    Ok(Ok(out))
}

fn reference() -> &'static Reference {
    static REFERENCE: OnceLock<Reference> = OnceLock::new();
    REFERENCE.get_or_init(|| {
        let parsed = json::loads(&unicode::cps(REFERENCE_SCHEMA)).expect("reference schema JSON");
        let texts: Vec<String> = match parsed.get("texts") {
            Some(PyValue::List(items)) => items
                .iter()
                .map(|t| match t {
                    PyValue::Str(s) => unicode::to_string(s).expect("text"),
                    _ => panic!("a schema text"),
                })
                .collect(),
            _ => panic!("texts"),
        };
        let mut optional = BTreeMap::new();
        if let Some(PyValue::Dict(items)) = parsed.get("optional_columns") {
            for (k, v) in items {
                let (PyValue::Str(k), PyValue::List(v)) = (k, v) else {
                    panic!("optional columns")
                };
                optional.insert(
                    unicode::to_string(k).expect("name"),
                    v.iter()
                        .map(|c| match c {
                            PyValue::Str(c) => unicode::to_string(c).expect("column"),
                            _ => panic!("column"),
                        })
                        .collect(),
                );
            }
        }
        let conn = Connection::open_in_memory().expect("memory");
        for text in &texts {
            conn.execute_batch(text).expect("the reference's schema");
        }
        let master = master_rows(&conn).expect("master").expect("utf-8");
        let mut built = Built::new();
        for (kind, name, table, sql) in &master {
            let exact = sql.is_some()
                && (kind != "table"
                    || is_virtual(&sql.as_deref().unwrap_or("").chars().collect::<Vec<_>>()));
            let text = if exact {
                sql.clone()
            } else {
                let pragma = if kind == "table" {
                    "table_xinfo"
                } else {
                    "index_info"
                };
                let mut statement = conn
                    .prepare(&format!("PRAGMA {pragma}(\"{name}\")"))
                    .expect("pragma");
                let mut rows = statement.query([]).expect("pragma");
                let mut parts = Vec::new();
                while let Some(row) = rows.next().expect("row") {
                    if kind == "table" {
                        let notnull = int_of(row.get_ref(3).expect("notnull")) != 0;
                        let no_default = matches!(row.get_ref(4).expect("dflt"), ValueRef::Null);
                        if notnull && no_default {
                            parts.push(lower(
                                &str_of(row.get_ref(1).expect("name")).expect("utf-8"),
                            ));
                        }
                    } else {
                        parts.push(lower(
                            &str_of(row.get_ref(2).expect("name")).expect("utf-8"),
                        ));
                    }
                }
                Some(parts.join(","))
            };
            built.insert((kind.clone(), lower(name)), (lower(table), text, exact));
        }
        let constraints = constraint_indexes(&master);
        let virtual_prefixes: Vec<String> = master
            .iter()
            .filter(|(k, _, _, sql)| {
                k == "table"
                    && is_virtual(&sql.as_deref().unwrap_or("").chars().collect::<Vec<_>>())
            })
            .map(|(_, n, _, _)| format!("{}_", lower(n)))
            .collect();
        let mut shapes = BTreeMap::new();
        for (kind, name, _, sql) in &master {
            if kind != "table" {
                continue;
            }
            let low = lower(name);
            let chars: Vec<char> = sql.as_deref().unwrap_or("").chars().collect();
            if is_virtual(&chars)
                || low.starts_with("sqlite_")
                || virtual_prefixes.iter().any(|p| low.starts_with(p))
            {
                continue;
            }
            let own = constraints.get(&low).cloned().unwrap_or_default();
            shapes.insert(
                low,
                table_shape(&conn, name, sql.as_deref(), &own).expect("shape"),
            );
        }
        Reference {
            built,
            shapes,
            optional,
            texts,
        }
    })
}

fn constraint_indexes(rows: &[MasterRow]) -> BTreeMap<String, Vec<String>> {
    let mut found: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (kind, name, table, sql) in rows {
        if kind == "index" && sql.is_none() && lower(name).starts_with("sqlite_autoindex_") {
            found.entry(lower(table)).or_default().push(name.clone());
        }
    }
    found
}

/// `_affinity`.
fn affinity(declared: &str) -> &'static str {
    let upper = upper(declared);
    if upper.contains("INT") {
        "INTEGER"
    } else if ["CHAR", "CLOB", "TEXT"].iter().any(|w| upper.contains(w)) {
        "TEXT"
    } else if upper.contains("BLOB") || upper.chars().all(is_s) {
        "BLOB"
    } else if ["REAL", "FLOA", "DOUB"].iter().any(|w| upper.contains(w)) {
        "REAL"
    } else {
        "NUMERIC"
    }
}

/// `_default`.
fn default_of(raw: Option<String>) -> Option<String> {
    let raw = raw?;
    let strip = |t: &str| t.trim_matches(is_s).to_owned();
    let mut text = strip(&raw);
    while text.starts_with('(') && text.ends_with(')') && !text.is_empty() {
        let inner: String = text.chars().skip(1).collect();
        let inner: String = inner
            .chars()
            .take(inner.chars().count().saturating_sub(1))
            .collect();
        text = strip(&inner);
        if text.is_empty() {
            break;
        }
    }
    Some(if text.starts_with('\'') {
        text
    } else {
        upper(&text)
    })
}

fn table_shape(
    conn: &Connection,
    name: &str,
    sql: Option<&str>,
    constraints: &[String],
) -> rusqlite::Result<Shape> {
    let mut columns: Vec<(String, Column)> = Vec::new();
    {
        let mut statement = conn.prepare(&format!("PRAGMA table_xinfo({})", quote_name(name)))?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            let name = lower(&str_of(row.get_ref(1)?)?);
            let column: Column = (
                affinity(&text_of(row.get_ref(2)?)?.unwrap_or_default()),
                int_of(row.get_ref(3)?) != 0,
                default_of(text_of(row.get_ref(4)?)?),
                int_of(row.get_ref(5)?),
            );
            match columns.iter_mut().find(|(n, _)| *n == name) {
                Some(slot) => slot.1 = column,
                None => columns.push((name, column)),
            }
        }
    }
    let without_rowid = search_words(&blanked(sql), &["WITHOUT", "ROWID"]);
    let mut keys = BTreeSet::new();
    let own = without_rowid.then(|| name.to_owned());
    for index in constraints.iter().chain(own.iter()) {
        let mut statement = conn.prepare(&format!("PRAGMA index_xinfo({})", quote_name(index)))?;
        let mut rows = statement.query([])?;
        let mut key = Vec::new();
        while let Some(row) = rows.next()? {
            if int_of(row.get_ref(5)?) != 0 {
                key.push((
                    lower(&str_of(row.get_ref(2)?)?),
                    upper(&str_of(row.get_ref(4)?)?),
                ));
            }
        }
        keys.insert(key);
    }
    Ok(Shape {
        columns,
        keys,
        without_rowid,
    })
}

// ---------------------------------------------------------------------------
// The checks
// ---------------------------------------------------------------------------

/// `schema_refusal`.
fn schema_refusal(rows: &[MasterRow]) -> Option<String> {
    let reference = reference();
    let built = &reference.built;
    let names: BTreeSet<&str> = built.keys().map(|(_, n)| n.as_str()).collect();
    let tables: BTreeSet<&str> = built
        .keys()
        .filter(|(k, _)| k == "table")
        .map(|(_, n)| n.as_str())
        .collect();
    for (kind, name, table, sql) in rows {
        let low = lower(name);
        if kind == "table" {
            if let Some(text) = statistics_text(&low) {
                if sql.as_deref() != Some(text) {
                    return Some(format!(
                        "its table {} is not the one SQLite writes",
                        repr(name)
                    ));
                }
                continue;
            }
        }
        let b = blanked(sql.as_deref());
        let virtual_table = is_virtual(&b);
        match built.get(&(kind.clone(), low.clone())) {
            None => {
                let never = format!("named {}, which this build never creates", repr(name));
                if kind == "table" && virtual_table {
                    return Some(format!("it holds a virtual table {never}"));
                }
                if kind == "index"
                    && !(plain_index(&b) || (sql.is_none() && low.starts_with("sqlite_autoindex_")))
                {
                    return Some(format!("it holds an index {never}, on more than columns"));
                }
                if kind != "table" && kind != "index" {
                    return Some(format!("it holds a {kind} {never}"));
                }
                if names.contains(low.as_str()) {
                    return Some(format!(
                        "its {kind} {} has a name this build gives another object",
                        repr(name)
                    ));
                }
                let unique = sql.is_none() || create_then(&b, "UNIQUE");
                if kind == "index" && unique && tables.contains(lower(table).as_str()) {
                    return Some(format!(
                        "its unique index {} is on {}, which this build writes",
                        repr(name),
                        repr(table)
                    ));
                }
            }
            Some((on, text, exact)) => {
                if lower(table) != *on {
                    return Some(format!(
                        "its {kind} {} is on {}, not on {}",
                        repr(name),
                        repr(table),
                        repr(on)
                    ));
                }
                if (*exact && sql != text) || (kind == "index" && !exact && sql.is_some()) {
                    return Some(format!(
                        "its {kind} {} is not the one this build creates",
                        repr(name)
                    ));
                }
                if !exact && virtual_table {
                    return Some(format!(
                        "its table {} is a virtual table, which this build never makes it",
                        repr(name)
                    ));
                }
            }
        }
    }
    None
}

/// `table_refusal`.
fn table_refusal(
    conn: &Connection,
    name: &str,
    sql: Option<&str>,
    filled: Option<&str>,
) -> rusqlite::Result<Option<String>> {
    let quoted = quote_name(name);
    let mut statement = conn.prepare(&format!("PRAGMA table_xinfo({quoted})"))?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let column = str_of(row.get_ref(1)?)?;
        let notnull = int_of(row.get_ref(3)?) != 0;
        let default = text_of(row.get_ref(4)?)?;
        let hidden = int_of(row.get_ref(6)?) != 0;
        if notnull && default.is_none() {
            if let Some(filled) = filled {
                if !filled.split(',').any(|c| c == lower(&column)) {
                    return Ok(Some(format!(
                        "its table {} needs {}, which this build leaves out",
                        repr(name),
                        repr(&column)
                    )));
                }
            }
        }
        if hidden {
            return Ok(Some(format!(
                "its table {} has a generated column {}",
                repr(name),
                repr(&column)
            )));
        }
        if let Some(d) = &default {
            if !is_literal(d) {
                return Ok(Some(format!(
                    "its table {} gives {} a default that is not a literal",
                    repr(name),
                    repr(&column)
                )));
            }
        }
    }
    drop(rows);
    let mut fk = conn.prepare(&format!("PRAGMA foreign_key_list({quoted})"))?;
    if fk.query([])?.next()?.is_some() {
        return Ok(Some(format!("its table {} has a foreign key", repr(name))));
    }
    let plain = sub_quoted(sql.unwrap_or(""), |_| " ");
    if search_words(&plain, &["CHECK"]) {
        return Ok(Some(format!("its table {} has a check", repr(name))));
    }
    Ok(None)
}

/// `index_refusal`.
fn index_refusal(
    conn: &Connection,
    name: &str,
    columns: Option<&str>,
) -> rusqlite::Result<Option<String>> {
    let mut statement = conn.prepare(&format!("PRAGMA index_xinfo({})", quote_name(name)))?;
    let mut rows = statement.query([])?;
    let mut keys = Vec::new();
    while let Some(row) = rows.next()? {
        if int_of(row.get_ref(1)?) == -2 {
            return Ok(Some(format!(
                "its index {} is on an expression",
                repr(name)
            )));
        }
        let collation = upper(&str_of(row.get_ref(4)?)?);
        if !["BINARY", "NOCASE", "RTRIM"].contains(&collation.as_str()) {
            return Ok(Some(format!(
                "its index {} sorts by a collation SQLite does not build in",
                repr(name)
            )));
        }
        if int_of(row.get_ref(5)?) != 0 {
            keys.push(lower(&str_of(row.get_ref(2)?)?));
        }
    }
    if let Some(columns) = columns {
        if keys.join(",") != columns {
            return Ok(Some(format!(
                "its index {} is not on the columns this build gives it",
                repr(name)
            )));
        }
    }
    Ok(None)
}

/// `shape_refusal`.
fn shape_refusal(
    conn: &Connection,
    name: &str,
    sql: Option<&str>,
    constraints: &[String],
) -> rusqlite::Result<Option<String>> {
    let reference = reference();
    let Some(want) = reference.shapes.get(&lower(name)) else {
        return Ok(None);
    };
    let have = table_shape(conn, name, sql, constraints)?;
    let optional = reference.optional.get(&lower(name));
    for (column, shape) in &want.columns {
        match have
            .columns
            .iter()
            .find(|(n, _)| n == column)
            .map(|(_, c)| c)
        {
            None if !optional.is_some_and(|o| o.iter().any(|c| c == column)) => {
                return Ok(Some(format!(
                    "its table {} lacks {}, which this build writes",
                    repr(name),
                    repr(column)
                )));
            }
            Some(found) if found != shape => {
                return Ok(Some(format!(
                    "its table {} declares {} otherwise than this build",
                    repr(name),
                    repr(column)
                )));
            }
            _ => {}
        }
    }
    if have.keys != want.keys {
        return Ok(Some(format!(
            "its table {} is not keyed as this build keys it",
            repr(name)
        )));
    }
    if have.without_rowid != want.without_rowid {
        let layout = if have.without_rowid {
            "WITHOUT ROWID"
        } else {
            "a rowid table"
        };
        return Ok(Some(format!(
            "its table {} is {layout}, unlike this build's",
            repr(name)
        )));
    }
    Ok(None)
}

// ---------------------------------------------------------------------------
// The deadline and the admission
// ---------------------------------------------------------------------------

/// SQLite's primary result code of an error, if it is one.
fn code_of(e: &rusqlite::Error) -> Option<(ErrorCode, i32, String)> {
    match e {
        rusqlite::Error::SqliteFailure(f, msg) => Some((
            f.code,
            f.extended_code & 0xff,
            msg.clone().unwrap_or_else(|| f.to_string()),
        )),
        rusqlite::Error::SqlInputError { error, msg, .. } => {
            Some((error.code, error.extended_code & 0xff, msg.clone()))
        }
        _ => None,
    }
}

/// The deadline's state, shared with the progress handler [`install`]
/// puts on a connection for its life: idle until [`with_deadline`] arms
/// it, as the reference sets its handler for a block and removes it after.
#[derive(Debug, Default)]
pub struct Deadline {
    armed: AtomicBool,
    fired: AtomicBool,
    started: Mutex<Option<Instant>>,
}

/// Put the deadline's progress handler on `conn` (every
/// [`PROGRESS_STEPS`] virtual-machine steps; it interrupts only while
/// armed).
pub fn install(conn: &mut Connection) -> rusqlite::Result<Arc<Deadline>> {
    let deadline = Arc::new(Deadline::default());
    let state = deadline.clone();
    conn.progress_handler(
        PROGRESS_STEPS,
        Some(move || {
            if !state.armed.load(Relaxed) {
                return false;
            }
            let now = Instant::now();
            let mut started = state.started.lock().unwrap_or_else(|p| p.into_inner());
            match *started {
                None => {
                    *started = Some(now);
                    false
                }
                Some(s) if now.duration_since(s) > ADMISSION_DEADLINE => {
                    state.fired.store(true, Relaxed);
                    true
                }
                Some(_) => false,
            }
        }),
    )?;
    Ok(deadline)
}

/// `_deadline`: run `body` with the deadline armed (it interrupts the
/// running statement [`ADMISSION_DEADLINE`] after its first progress call)
/// and with `length` as SQLITE_LIMIT_LENGTH; both put back after.
/// `Err(Refusal)` for a value too big ("DataError") or an interruption the
/// deadline made.
pub fn with_deadline<T>(
    conn: &Connection,
    deadline: &Deadline,
    what: &str,
    length: Option<i32>,
    body: impl FnOnce(&Connection) -> Result<T, AdmitError>,
) -> Result<T, AdmitError> {
    *deadline.started.lock().unwrap_or_else(|p| p.into_inner()) = None;
    deadline.fired.store(false, Relaxed);
    deadline.armed.store(true, Relaxed);
    let previous = length.map(|l| conn.set_limit(rusqlite::limits::Limit::SQLITE_LIMIT_LENGTH, l));
    let result = body(conn);
    deadline.armed.store(false, Relaxed);
    if let Some(Ok(previous)) = previous {
        let _ = conn.set_limit(rusqlite::limits::Limit::SQLITE_LIMIT_LENGTH, previous);
    }
    match result {
        Err(AdmitError::Sqlite(e)) => match code_of(&e) {
            Some((ErrorCode::TooBig, _, msg)) => Err(AdmitError::Refused(Refusal {
                why: format!("it is not a store this build wrote ({msg} in its {what})"),
                timed_out: false,
                holds_meta: None,
                later: None,
                reference_raises: None,
            })),
            Some(_) if deadline.fired.load(Relaxed) => Err(AdmitError::Refused(Refusal {
                why: format!(
                    "reading its {what} ran past the 2 s a store file is given before it is \
                     admitted"
                ),
                timed_out: true,
                holds_meta: None,
                later: None,
                reference_raises: None,
            })),
            _ => Err(AdmitError::Sqlite(e)),
        },
        other => other,
    }
}

/// `admit_store`.
pub fn admit(conn: &Connection, deadline: &Deadline) -> Result<(), AdmitError> {
    let (rows, why) = with_deadline(
        conn,
        deadline,
        "schema",
        Some(ADMISSION_MAX_LENGTH),
        |conn| {
            let read = conn
                .execute_batch("PRAGMA trusted_schema = OFF")
                .and_then(|()| master_rows(conn));
            let rows = match read {
                Ok(Ok(rows)) => rows,
                Ok(Err((column, raised, shown))) => {
                    return Err(AdmitError::Refused(match raised {
                        Some(raised) => Refusal {
                            why: format!(
                                "it is not a store this build wrote, or it is damaged: its \
                                 schema cannot be read ({raised})"
                            ),
                            timed_out: false,
                            holds_meta: None,
                            later: None,
                            reference_raises: Some(raised),
                        },
                        None => Refusal {
                            why: format!(
                                "it is not a store this build wrote: its schema's {column} \
                                 holds a {shown}, not text"
                            ),
                            timed_out: false,
                            holds_meta: None,
                            later: None,
                            reference_raises: None,
                        },
                    }));
                }
                Err(e) => {
                    if let Some((_, primary, msg)) = code_of(&e) {
                        if primary == 11 || primary == 26 {
                            return Err(AdmitError::Refused(Refusal {
                                why: format!(
                                    "it is not a store this build wrote, or it is damaged: its \
                                 schema cannot be read ({msg})"
                                ),
                                timed_out: false,
                                holds_meta: None,
                                later: None,
                                reference_raises: None,
                            }));
                        }
                    }
                    return Err(e.into());
                }
            };
            let mut why = schema_refusal(&rows);
            let constraints = constraint_indexes(&rows);
            let reference = reference();
            if why.is_none() {
                for (kind, name, _table, sql) in &rows {
                    let expected = reference.built.get(&(kind.clone(), lower(name)));
                    let known = expected.and_then(|e| e.1.as_deref());
                    if kind == "table" && statistics_text(&lower(name)).is_none() {
                        if expected.is_none_or(|e| !e.2) {
                            why = table_refusal(conn, name, sql.as_deref(), known)?;
                        }
                        if why.is_none() && expected.is_some_and(|e| !e.2) {
                            let own = constraints.get(&lower(name)).cloned().unwrap_or_default();
                            why = shape_refusal(conn, name, sql.as_deref(), &own)?;
                        }
                    } else if kind == "index" && expected.is_none_or(|e| !e.2) {
                        why = index_refusal(conn, name, known)?;
                    }
                    if why.is_some() {
                        break;
                    }
                }
            }
            Ok((rows, why))
        },
    )?;
    let Some(why) = why else {
        return Ok(());
    };
    let meta_sql = rows
        .iter()
        .find(|(k, n, _, _)| k == "table" && lower(n) == "meta")
        .map(|(_, _, _, s)| s.clone());
    let holds_meta = meta_sql.is_some();
    if let Some(sql) = meta_sql {
        if let Some(later) = refuse_later(conn, deadline, sql.as_deref(), &why) {
            return Err(AdmitError::Refused(Refusal {
                why: why.clone(),
                timed_out: false,
                holds_meta: Some(true),
                later: Some(Box::new(later)),
                reference_raises: None,
            }));
        }
    }
    Err(AdmitError::Refused(Refusal {
        why: format!("it is not a store this build wrote ({why})"),
        timed_out: false,
        holds_meta: Some(holds_meta),
        later: None,
        reference_raises: None,
    }))
}

fn whole(text: &str) -> Option<i64> {
    ((1..=9).contains(&text.len()) && text.bytes().all(|b| b.is_ascii_digit()))
        .then(|| text.parse().ok())
        .flatten()
}

/// `_refuse_later`.
fn refuse_later(
    conn: &Connection,
    deadline: &Deadline,
    sql: Option<&str>,
    why: &str,
) -> Option<Later> {
    let keys = ["schema_revision", "min_reader_revision", "engine_version"];
    let found = with_deadline(
        conn,
        deadline,
        "meta table",
        Some(ADMISSION_MAX_LENGTH),
        |conn| {
            if is_virtual(&blanked(sql)) || table_refusal(conn, "meta", sql, None)?.is_some() {
                return Ok(None);
            }
            let mut statement =
                conn.prepare("SELECT key, value FROM meta WHERE key IN (?, ?, ?)")?;
            let mut rows = statement.query(keys)?;
            let mut found: BTreeMap<String, String> = BTreeMap::new();
            while let Some(row) = rows.next()? {
                // `dict(rows)`: a later row under the same key wins.
                found.insert(str_of(row.get_ref(0)?)?, str_of(row.get_ref(1)?)?);
            }
            Ok(Some(found))
        },
    )
    .ok()??;
    let get = |k: &str| found.get(k).cloned().unwrap_or_default();
    let (revision, reader, engine) = (get(keys[0]), get(keys[1]), get(keys[2]));
    let revision = whole(&revision).filter(|r| *r > super::SCHEMA_REVISION)?;
    let newest = if engine.is_empty() {
        String::new()
    } else {
        format!(
            " (its newest writer was bettermemory {})",
            super::shown(&engine)
        )
    };
    let reader_above = whole(&reader).filter(|r| *r > super::SCHEMA_REVISION);
    Some(Later {
        key: if reader_above.is_some() {
            "min_reader_revision"
        } else {
            "schema_revision"
        },
        needed: reader_above.unwrap_or(revision),
        inner: why.to_owned(),
        revision,
        reader_above: reader_above.is_some(),
        newest,
    })
}

/// The reference's five schema texts, in `Store.create`'s order
/// (`SCHEMA`, `MEMBERSHIP_SCHEMA`, `ADDITIVE_SCHEMA`,
/// `PROPOSALS_INDEX_SCHEMA`, `SESSION_INDEX_SCHEMA`): what a native create
/// runs.
pub(crate) fn schema_texts() -> &'static [String] {
    &reference().texts
}

/// The pinned copy of the reference's schema texts, for a test.
pub fn reference_schema_json() -> &'static str {
    REFERENCE_SCHEMA
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literals() {
        for yes in [
            "0", "''", "'[]'", "'it''s'", "(0)", "((NULL))", "-1.5e3", "X'00'", "0x1F", ".5",
            " 'a' ",
        ] {
            assert!(is_literal(yes), "{yes}");
        }
        for no in ["abs(1)", "'a", "0x", "1e", "CURRENT_TIME", "'a' || 'b'"] {
            assert!(!is_literal(no), "{no}");
        }
    }

    #[test]
    fn plain_indexes() {
        let ok = |t: &str| plain_index(&t.chars().collect::<Vec<_>>());
        assert!(ok("CREATE INDEX a ON t(x)"));
        assert!(ok("CREATE UNIQUE INDEX a ON t (x COLLATE NOCASE DESC, y);"));
        assert!(!ok("CREATE INDEX a ON t(x) WHERE x > 1"));
        assert!(!ok("CREATE INDEX a ON t(lower(x))"));
    }

    #[test]
    fn quoting() {
        let b: String = blanked(Some(
            "CREATE TABLE \"x\" (a CHECK, b -- c\n, 'it''s' /* d */)",
        ))
        .into_iter()
        .collect();
        assert_eq!(b, "CREATE TABLE  q  (a CHECK, b  \n, '  )");
        assert!(is_virtual(
            &"  create\tvirtual table".chars().collect::<Vec<_>>()
        ));
        assert!(search_words(
            &"x CHEC\u{212a} y".chars().collect::<Vec<_>>(),
            &["CHECK"]
        ));
    }
}
