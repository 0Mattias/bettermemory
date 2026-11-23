//! The store's digest (`store_digest`): its tables' schema text, the log
//! kinds it follows, what it keeps of a record (`_features`: the raw
//! content tokens, the claim size, the provenance stamp, the BM25 length),
//! one record's entry read from the store (`_index_record`), and the two
//! ways a writer brings it current (`digest_mark`): applying the record
//! mutations after its mark (`_ingest`), and building it from the records
//! (`_build`).
//!
//! One implementation serves both callers: the reader's search pool
//! ([`super::pool`]) brings a digest forward in memory from the same
//! [`mutated_ids`] and [`read_entry`], and the writing open
//! ([`super::open`]) writes what they give with [`ingest`] and [`build`].

use std::collections::BTreeSet;

use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};

use super::{sql_value, SqlValue};
use crate::json;
use crate::lexicon;
use crate::pyvalue::{self, PyStr, PyValue};
use crate::records::rows::{self, Row};
use crate::unicode;

/// `store_digest._KINDS`: the log kinds whose last row the digest follows.
pub const DIGEST_KINDS: [&str; 4] = [
    "memory_put",
    "memory_delete",
    "tombstone_put",
    "tombstone_delete",
];

/// `store_digest._BUILD_BATCH`.
const BUILD_BATCH: i64 = 2000;

/// `store_digest._SCHEMA`, byte for byte (pinned to `store_create.json`).
pub const DIGEST_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS digest_records (
    doc INTEGER PRIMARY KEY,
    removed INTEGER NOT NULL DEFAULT 0,
    id TEXT NOT NULL DEFAULT '',
    rid INTEGER NOT NULL DEFAULT 0,
    n INTEGER NOT NULL DEFAULT 0,
    claim INTEGER NOT NULL DEFAULT 0,
    stamped INTEGER NOT NULL DEFAULT 0,
    dl INTEGER NOT NULL DEFAULT 0,
    client TEXT,
    model TEXT,
    category TEXT,
    sha TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '[]',
    cwd TEXT NOT NULL DEFAULT '',
    tokens TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS digest_records_by_id ON digest_records(removed, id);
CREATE INDEX IF NOT EXISTS digest_records_by_category
    ON digest_records(removed, category);
CREATE TABLE IF NOT EXISTS digest_tokens (
    token TEXT NOT NULL DEFAULT '',
    doc INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token, doc)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS digest_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
";

/// `store_digest.fingerprint()` under the reference this crate reproduces
/// (Unicode 15.1.0's tokenizer, the supersession rules of this revision),
/// as `store_create.json`'s `digest_fingerprint` pins it: the tests'
/// expectation only. The code reads and writes [`fingerprint`], computed
/// from this module's [`features`] once per process, so a change to the
/// rules that moves a probe's result moves the fingerprint, and a digest
/// built under the old rules is rebuilt, as the reference does.
pub const DIGEST_FINGERPRINT: &str =
    "59e8e47cd83bbb95543f0def4de2f4eee3f2c4abaa5cabe2c93b68c5b747687f";

/// `supersession.MAX_CLAIM_TOKENS`, `MAX_CLAIM_SENTENCES`.
const MAX_CLAIM_TOKENS: usize = 80;
const MAX_CLAIM_SENTENCES: usize = 5;
/// The patterns `_SENTENCE_RE` and `_PROVENANCE_RE` are compiled from, as
/// the fingerprint names them; [`sentences`] and [`strip_provenance`] are
/// their matchers.
const SENTENCE_PATTERN: &str = r"(?:[!?\n]|(?<!\d)\.|\.(?!\d))+";
const PROVENANCE_PATTERN: &str = r"\n\n_\(consolidate --llm --from-transcript: .*\)_\s*$";
/// `store_digest._SHAPE`.
const SHAPE: &str = "records: dl, client, model, category, sha; index: category";
/// The stamp `_PROVENANCE_RE` opens with.
const STAMP: &str = "\n\n_(consolidate --llm --from-transcript: ";

/// `_features(body)`: what the digest keeps of a body.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Features {
    /// `raw_content_tokens(body)`, in code point (= UTF-8 byte) order.
    pub tokens: BTreeSet<String>,
    pub claim: usize,
    pub stamped: bool,
    pub dl: usize,
}

/// `_features(body).dl`: the length of the token stream BM25 scores
/// (compounds beside their parts, stopwords out, repeats counted).
pub fn bm25_length(body: &[u32]) -> usize {
    let stream = lexicon::tokenize(body);
    lexicon::strip_stopwords(&lexicon::expand_kebab(&stream)).len()
}

/// `search_dedup.raw_content_tokens`: the stopword-stripped tokens.
fn raw_content_tokens(text: &[u32]) -> BTreeSet<String> {
    lexicon::strip_stopwords(&lexicon::tokenize(text))
        .into_iter()
        .collect()
}

/// `supersession._strip_provenance`: `_PROVENANCE_RE.sub("", body)`. The
/// pattern (`re.S`) matches from the leftmost stamp opening that is
/// followed, anywhere after it, by `)_` and then only whitespace to the
/// end; the match runs to the end, so the body is cut at that opening. No
/// second match can follow one.
pub fn strip_provenance(body: &[u32]) -> PyStr {
    let stamp = unicode::cps(STAMP);
    // Where the trailing whitespace begins: `)_` must end exactly there,
    // since neither of its characters is whitespace.
    let mut end = body.len();
    while end > 0 && unicode::is_space(body[end - 1]) {
        end -= 1;
    }
    if end < 2 || body[end - 2] != u32::from(b')') || body[end - 1] != u32::from(b'_') {
        return body.to_vec();
    }
    let close = end - 2;
    let mut i = 0;
    while i + stamp.len() <= close {
        if body[i..i + stamp.len()] == stamp[..] {
            return body[..i].to_vec();
        }
        i += 1;
    }
    body.to_vec()
}

/// How many pieces of `_SENTENCE_RE.split(text)` hold something besides
/// whitespace. A run of `!`, `?`, newlines and full stops that do not sit
/// between two digits splits; each character qualifies by its own
/// neighbours, so the maximal run is the regex's greedy match.
fn sentences(text: &[u32]) -> usize {
    let separates = |k: usize| -> bool {
        let c = text[k];
        if c == u32::from(b'!') || c == u32::from(b'?') || c == u32::from(b'\n') {
            return true;
        }
        if c != u32::from(b'.') {
            return false;
        }
        let digit_before = k > 0 && unicode::is_re_digit(text[k - 1]);
        let digit_after = k + 1 < text.len() && unicode::is_re_digit(text[k + 1]);
        !(digit_before && digit_after)
    };
    let mut count = 0;
    let mut filled = false;
    for (k, &c) in text.iter().enumerate() {
        if separates(k) {
            if filled {
                count += 1;
            }
            filled = false;
        } else if !unicode::is_space(c) {
            filled = true;
        }
    }
    if filled {
        count += 1;
    }
    count
}

/// `supersession.claim_sized(body)`: the port of that function, which
/// `review::related_entry` calls and the supersession unit re-exports.
pub fn claim_sized(body: &[u32]) -> bool {
    let stripped = strip_provenance(body);
    let tokens = raw_content_tokens(&stripped).len();
    0 < tokens && tokens <= MAX_CLAIM_TOKENS && sentences(&stripped) <= MAX_CLAIM_SENTENCES
}

/// `store_digest._features(body)`.
pub fn features(body: &[u32]) -> Features {
    let stream = lexicon::tokenize(body);
    let tokens: BTreeSet<String> = lexicon::strip_stopwords(&stream).into_iter().collect();
    let dl = lexicon::strip_stopwords(&lexicon::expand_kebab(&stream)).len();
    let stripped = strip_provenance(body);
    let stamped = stripped != body;
    let mut claim = 0;
    if claim_sized(&stripped) {
        claim = if stamped {
            raw_content_tokens(&stripped).len()
        } else {
            tokens.len()
        };
    }
    Features {
        tokens,
        claim,
        stamped,
        dl,
    }
}

/// `store_digest.expanded(tokens)`: each compound's parts beside it,
/// stopword parts left out.
pub fn expanded<'a>(tokens: impl IntoIterator<Item = &'a String>) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for token in tokens {
        out.insert(token.clone());
        for part in lexicon::kebab_parts(token) {
            if !lexicon::is_stopword(&part) {
                out.insert(part);
            }
        }
    }
    out
}

/// `store_digest.fingerprint()` computed from this module's rules: the
/// sha256 of the JSON the reference hashes. Held equal to
/// [`DIGEST_FINGERPRINT`] by a test.
pub fn computed_fingerprint() -> String {
    computed()
}

/// [`computed_fingerprint`], computed once.
pub fn fingerprint() -> &'static str {
    static ONCE: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    ONCE.get_or_init(computed)
}

fn computed() -> String {
    let probes = [
        "Claude-Code reads feature_flags from the daemon's config on 3.12.1.".to_owned(),
        "The and of to a an.".to_owned(),
        "Port moved to 9443.\n\n_(consolidate --llm --from-transcript: x)_".to_owned(),
        (0..120)
            .map(|i| format!("word{i}."))
            .collect::<Vec<_>>()
            .join(" "),
    ];
    let strs =
        |set: &BTreeSet<String>| PyValue::List(set.iter().map(|t| PyValue::str(t)).collect());
    let mut parts = vec![
        PyValue::str(SHAPE),
        PyValue::str(&lexicon::python_tokenizer_fingerprint()),
        PyValue::int(MAX_CLAIM_TOKENS as i128),
        PyValue::int(MAX_CLAIM_SENTENCES as i128),
        PyValue::str(SENTENCE_PATTERN),
        PyValue::str(PROVENANCE_PATTERN),
    ];
    for probe in &probes {
        let found = features(&unicode::cps(probe));
        parts.push(PyValue::List(vec![
            strs(&found.tokens),
            strs(&expanded(&found.tokens)),
            PyValue::int(found.claim as i128),
            PyValue::Bool(found.stamped),
            PyValue::int(found.dl as i128),
        ]));
    }
    let options = json::DumpOptions {
        sort_keys: true,
        ensure_ascii: true,
        ..json::DumpOptions::DEFAULT
    };
    let text = json::dumps(&PyValue::List(parts), options).expect("probe JSON");
    let bytes: Vec<u8> = text.iter().map(|&c| c as u8).collect();
    Sha256::digest(&bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Why the digest could not be brought current: what `digest_mark`
/// catches (`sqlite3.Error`, `ValueError`, `KeyError`, `TypeError`), after
/// which the reference leaves the digest as its last committed transaction
/// left it.
/// CPython's `sqlite3` text for an int past an SQLite INTEGER.
pub const OVERFLOW_TEXT: &str = "Python int too large to convert to SQLite INTEGER";

#[derive(Debug)]
pub enum DigestError {
    /// A transaction refused its writer before the digest body ran.
    Writer(super::write::WriteError),
    Sqlite(rusqlite::Error),
    /// A payload, record or value the reference would raise on.
    Unreadable(&'static str),
    /// `digest_state.seq` past an SQLite INTEGER: the reference's
    /// `OverflowError`, which `digest_mark` does not catch and which leaves
    /// `Store.open`.
    Overflow,
}

impl From<rusqlite::Error> for DigestError {
    fn from(e: rusqlite::Error) -> Self {
        DigestError::Sqlite(e)
    }
}

impl std::fmt::Display for DigestError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DigestError::Writer(e) => write!(f, "{e}"),
            DigestError::Sqlite(e) => write!(f, "{e}"),
            DigestError::Unreadable(what) => write!(f, "{what} does not read"),
            DigestError::Overflow => write!(f, "{OVERFLOW_TEXT}"),
        }
    }
}

type Res<T> = Result<T, DigestError>;

fn value(v: rusqlite::types::ValueRef<'_>) -> Res<SqlValue> {
    sql_value(v).map_err(|_| DigestError::Unreadable("a text that is not UTF-8"))
}

fn to_value(v: &SqlValue) -> rusqlite::types::Value {
    use rusqlite::types::Value;
    match v {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(i) => Value::Integer(*i),
        SqlValue::Real(f) => Value::Real(*f),
        SqlValue::Text(t) => Value::Text(t.clone()),
        SqlValue::Blob(b) => Value::Blob(b.clone()),
    }
}

// -- the state ---------------------------------------------------------------

/// `_state`: `digest_state` as `(str(key), str(value))` pairs, None for a
/// store with no digest table.
pub fn state(conn: &Connection) -> Res<Option<Vec<(String, String)>>> {
    let mut statement = match conn.prepare("SELECT key, value FROM digest_state") {
        Ok(s) => s,
        // `_state` catches OperationalError: no such table.
        Err(rusqlite::Error::SqliteFailure(_, Some(m))) if m.starts_with("no such table") => {
            return Ok(None)
        }
        Err(e) => return Err(e.into()),
    };
    let mut rows = statement.query([])?;
    let mut out: Vec<(String, String)> = Vec::new();
    while let Some(row) = rows.next()? {
        let k = value(row.get_ref(0)?)?.py_str();
        let v = value(row.get_ref(1)?)?.py_str();
        match out.iter_mut().find(|(have, _)| *have == k) {
            Some(slot) => slot.1 = v,
            None => out.push((k, v)),
        }
    }
    Ok(Some(out))
}

fn get<'a>(state: &'a [(String, String)], key: &str) -> Option<&'a str> {
    state
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.as_str())
}

fn set_state(conn: &Connection, values: &[(&str, &str)]) -> Res<()> {
    let mut insert =
        conn.prepare("INSERT OR REPLACE INTO digest_state(key, value) VALUES (?, ?)")?;
    for (k, v) in values {
        insert.execute([k, v])?;
    }
    Ok(())
}

/// `_head`: the seq of the log's last record mutation, 0 with none.
pub fn head(conn: &Connection) -> Res<i64> {
    let mut head = 0;
    let mut statement =
        conn.prepare("SELECT seq FROM log WHERE kind = ? ORDER BY seq DESC LIMIT 1")?;
    for kind in DIGEST_KINDS {
        let seq = statement
            .query_row([kind], |row| Ok(value(row.get_ref(0)?)))
            .optional()?;
        match seq.transpose()? {
            None => {}
            Some(SqlValue::Integer(s)) => head = head.max(s),
            Some(_) => return Err(DigestError::Unreadable("a log seq")),
        }
    }
    Ok(head)
}

/// `_mac_at`: the MAC of the log row at `seq`, empty for none.
pub fn mac_at(conn: &Connection, seq: i64) -> Res<String> {
    if seq <= 0 {
        return Ok(String::new());
    }
    let mac = conn
        .prepare("SELECT mac FROM log WHERE seq = ?")?
        .query_row([seq], |row| Ok(value(row.get_ref(0)?)))
        .optional()?;
    Ok(match mac.transpose()? {
        None | Some(SqlValue::Null) => String::new(),
        Some(v) => v.py_str(),
    })
}

/// `_built`: the seq the digest has seen, or None when it has to be
/// rebuilt (no digest, another fingerprint, a build that did not finish,
/// a log whose row at that seq is not the one it saw).
pub fn built(conn: &Connection, state: Option<&[(String, String)]>) -> Res<Option<i64>> {
    let Some(state) = state else {
        return Ok(None);
    };
    if get(state, "fingerprint") != Some(fingerprint()) {
        return Ok(None);
    }
    let Some(seen) = get(state, "seq") else {
        return Ok(None);
    };
    if seen.is_empty() || !seen.chars().all(|c| unicode::is_digit(c as u32)) {
        return Ok(None);
    }
    // `str.isdigit` passed; `int()` reads Unicode decimal digits (a
    // superscript is a digit and not a decimal: ValueError, which
    // `digest_mark` catches).
    let parsed = pyvalue::int_from_str(&unicode::cps(seen));
    let Ok(PyValue::Int(text)) = &parsed else {
        return Err(DigestError::Unreadable("the digest's seq"));
    };
    // `_mac_at` binds it: past an SQLite INTEGER, sqlite3 raises
    // OverflowError, which nothing catches.
    let Ok(seen) = text.parse::<i64>() else {
        return Err(DigestError::Overflow);
    };
    if mac_at(conn, seen)? != get(state, "mac").unwrap_or("") {
        return Ok(None);
    }
    Ok(Some(seen))
}

/// `_mark`'s question: whether the digest has seen the log's last record
/// mutation.
pub fn current(conn: &Connection) -> Res<bool> {
    let state = state(conn)?;
    let seen = built(conn, state.as_deref())?;
    Ok(seen.is_some() && seen == Some(head(conn)?))
}

// -- the log -----------------------------------------------------------------

/// `json.loads` of bytes as far as a payload needs it: UTF-8, with a
/// leading BOM dropped (`json.detect_encoding`'s ``utf-8-sig``); None for
/// bytes that are not UTF-8 and for the UTF-16 and UTF-32 spellings
/// `detect_encoding` would also read (not ported: a store never writes
/// them).
fn json_bytes(b: &[u8]) -> Option<Vec<u32>> {
    let utf16_or_32 = b.starts_with(&[0xff, 0xfe])
        || b.starts_with(&[0xfe, 0xff])
        || (b.len() >= 4 && (b[0] == 0 || b[1] == 0))
        || (b.len() == 2 && (b[0] == 0 || b[1] == 0));
    if utf16_or_32 {
        return None;
    }
    let b = b.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(b);
    std::str::from_utf8(b).ok().map(unicode::cps)
}

/// `str()` of a payload's id where it is a scalar. None for a container
/// (whose `str()` is not ported) and for text holding a lone surrogate.
fn py_str_scalar(v: &PyValue) -> Option<String> {
    match v {
        PyValue::Str(s) => unicode::to_string(s),
        PyValue::Int(decimal) => Some(decimal.clone()),
        PyValue::None => Some("None".to_owned()),
        PyValue::Bool(b) => Some(if *b { "True" } else { "False" }.to_owned()),
        PyValue::Float(f) => Some(crate::float_repr::repr(*f)),
        _ => None,
    }
}

fn dict_get<'v>(value: &'v PyValue, key: &str) -> Option<&'v PyValue> {
    match value {
        PyValue::Dict(items) => items
            .iter()
            .find(|(k, _)| matches!(k, PyValue::Str(k) if unicode::to_string(k).as_deref() == Some(key)))
            .map(|(_, v)| v),
        _ => None,
    }
}

/// `_mutated_ids(after)`: the ids the record mutations after `after`
/// name, in first-seen order, and the last such row's seq (`after` with
/// none). Err where a payload does not read (the reference's KeyError,
/// TypeError or ValueError, which `digest_mark` catches).
pub fn mutated_ids(conn: &Connection, after: i64) -> Res<(Vec<String>, i64)> {
    let mut statement = conn.prepare(
        "SELECT seq, kind, payload FROM log WHERE kind IN (?, ?, ?, ?) AND seq > ? ORDER BY seq",
    )?;
    let mut rows = statement.query(rusqlite::params![
        DIGEST_KINDS[0],
        DIGEST_KINDS[1],
        DIGEST_KINDS[2],
        DIGEST_KINDS[3],
        after
    ])?;
    let bad = DigestError::Unreadable;
    // First-seen order, with a set beside it: searching the list made a
    // catch-up quadratic in the unseen rows (`golden_search_store`).
    let mut ids: Vec<String> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut last = after;
    while let Some(row) = rows.next()? {
        let seq = match value(row.get_ref(0)?)? {
            SqlValue::Integer(s) => s,
            _ => return Err(bad("a log seq")),
        };
        let kind = value(row.get_ref(1)?)?.py_str();
        let text = match value(row.get_ref(2)?)? {
            // `json.loads` of a str refuses a leading BOM.
            SqlValue::Text(t) if t.starts_with('\u{feff}') => return Err(bad("a payload")),
            SqlValue::Text(t) => unicode::cps(&t),
            SqlValue::Blob(b) => json_bytes(&b).ok_or(bad("a payload"))?,
            _ => return Err(bad("a payload")),
        };
        let payload = json::loads(&text).map_err(|_| bad("a payload"))?;
        let holder = match kind.as_str() {
            "memory_put" => dict_get(&payload, "memory").ok_or(bad("a payload"))?,
            "tombstone_put" => dict_get(&payload, "tombstone").ok_or(bad("a payload"))?,
            _ => &payload,
        };
        let id = dict_get(holder, "id")
            .and_then(py_str_scalar)
            .ok_or(bad("a payload's id"))?;
        if seen.insert(id.clone()) {
            ids.push(id);
        }
        last = seq;
    }
    Ok((ids, last))
}

// -- one record's entry ------------------------------------------------------

/// One record as `_index_record` reads it: its row number, the record's
/// body and working directory as the model reads them, and the row's own
/// `scopes_json`, `actor_client`, `actor_model` and `category` columns.
#[derive(Clone, Debug)]
pub struct Entry {
    pub id: String,
    pub rid: i64,
    pub body: PyStr,
    pub scopes: String,
    pub cwd: String,
    pub client: SqlValue,
    pub model: SqlValue,
    pub category: SqlValue,
}

const MEMORY_ROW: &str =
    "SELECT memories.*, (SELECT payload FROM log WHERE mac = memories.log_mac \
     AND kind = 'memory_put' LIMIT 1) AS attestation_payload FROM memories WHERE id = ?";
const TOMBSTONE_ROW: &str = "SELECT tombstones.*, (SELECT payload FROM log WHERE mac = \
     tombstones.log_mac AND kind = 'tombstone_put' LIMIT 1) AS attestation_payload \
     FROM tombstones WHERE id = ?";

fn named_row(conn: &Connection, sql: &str, id: &str) -> Res<Option<Row>> {
    let mut statement = conn.prepare(sql)?;
    let names: Vec<String> = statement.column_names().map(str::to_owned).collect();
    let mut rows = statement.query([id])?;
    let Some(row) = rows.next()? else {
        return Ok(None);
    };
    let mut columns = Vec::with_capacity(names.len());
    for (i, name) in names.into_iter().enumerate() {
        columns.push((name, value(row.get_ref(i)?)?));
    }
    Ok(Some(Row { columns }))
}

/// The entry `_index_record(conn, removed, id)` writes, or None when the
/// table holds no row with that id (the entry is dropped). Err where the
/// record does not read through the model (the reference raises, and
/// `digest_mark` catches it).
pub fn read_entry(conn: &Connection, removed: bool, id: &str) -> Res<Option<Entry>> {
    let key_sql = if removed {
        "SELECT rowid FROM tombstones WHERE id = ?"
    } else {
        "SELECT rowid FROM memories WHERE id = ?"
    };
    let key = conn
        .prepare(key_sql)?
        .query_row([id], |row| Ok(value(row.get_ref(0)?)))
        .optional()?
        .transpose()?;
    let rid = match key {
        None => return Ok(None),
        Some(SqlValue::Integer(rid)) => rid,
        Some(_) => return Err(DigestError::Unreadable("a rowid")),
    };
    let Some(row) = named_row(conn, if removed { TOMBSTONE_ROW } else { MEMORY_ROW }, id)? else {
        return Ok(None);
    };
    let (fields, what) = if removed {
        (rows::tombstone(&row).map(|t| t.fields), "a tombstone")
    } else {
        (rows::memory(&row).map(|m| m.fields), "a memory")
    };
    let fields = fields.map_err(|_| DigestError::Unreadable(what))?;
    let column = |name: &str| -> Res<SqlValue> {
        row.get(name)
            .cloned()
            .ok_or(DigestError::Unreadable("a record column"))
    };
    let cwd = match fields.origin.as_ref().and_then(|o| o.cwd.as_ref()) {
        Some(cwd) => unicode::to_string(cwd).ok_or(DigestError::Unreadable("a cwd"))?,
        None => String::new(),
    };
    Ok(Some(Entry {
        id: unicode::to_string(&fields.id).ok_or(DigestError::Unreadable("an id"))?,
        rid,
        body: fields.body,
        scopes: column("scopes_json")?.py_str(),
        cwd,
        client: column("actor_client")?,
        model: column("actor_model")?,
        category: column("category")?,
    }))
}

// -- writing entries ---------------------------------------------------------

/// `_drop_entry`: the entry at `doc` and the postings its tokens give.
fn drop_entry(conn: &Connection, doc: i64, tokens: &str) -> Res<()> {
    let held: Vec<String> = tokens
        .split(|c: char| unicode::is_space(c as u32))
        .filter(|t| !t.is_empty())
        .map(str::to_owned)
        .collect();
    let mut delete = conn.prepare("DELETE FROM digest_tokens WHERE token = ? AND doc = ?")?;
    for token in expanded(&held) {
        delete.execute(rusqlite::params![token, doc])?;
    }
    conn.prepare("DELETE FROM digest_records WHERE doc = ?")?
        .execute([doc])?;
    Ok(())
}

/// The existing entry of `(removed, id)`, dropped.
fn drop_old(conn: &Connection, removed: bool, id: &str) -> Res<()> {
    let old = conn
        .prepare("SELECT doc, tokens FROM digest_records WHERE removed = ? AND id = ?")?
        .query_row(rusqlite::params![i64::from(removed), id], |row| {
            Ok((value(row.get_ref(0)?), value(row.get_ref(1)?)))
        })
        .optional()?;
    if let Some((doc, tokens)) = old {
        let doc = match doc? {
            SqlValue::Integer(d) => d,
            _ => return Err(DigestError::Unreadable("a digest doc")),
        };
        drop_entry(conn, doc, &tokens?.py_str())?;
    }
    Ok(())
}

/// `_put_entry`: one record's entry and postings, replacing any it had.
fn put_entry(conn: &Connection, removed: bool, entry: &Entry) -> Res<()> {
    let found = features(&entry.body);
    let body = unicode::to_string(&entry.body).ok_or(DigestError::Unreadable("a body"))?;
    let sha: String = Sha256::digest(body.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    drop_old(conn, removed, &entry.id)?;
    let tokens: Vec<&str> = found.tokens.iter().map(String::as_str).collect();
    conn.prepare(
        "INSERT INTO digest_records(removed, id, rid, n, claim, stamped, dl, client, model, \
         category, sha, scopes, cwd, tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    )?
    .execute(rusqlite::params![
        i64::from(removed),
        entry.id,
        entry.rid,
        found.tokens.len() as i64,
        found.claim as i64,
        i64::from(found.stamped),
        found.dl as i64,
        to_value(&entry.client),
        to_value(&entry.model),
        to_value(&entry.category),
        sha,
        entry.scopes,
        entry.cwd,
        tokens.join(" "),
    ])?;
    let doc = conn.last_insert_rowid();
    let mut post = conn.prepare("INSERT OR IGNORE INTO digest_tokens(token, doc) VALUES (?, ?)")?;
    for token in expanded(&found.tokens) {
        post.execute(rusqlite::params![token, doc])?;
    }
    Ok(())
}

/// `_index_record(conn, removed, id)`: bring one id's entry to what its
/// table holds now.
fn index_record(conn: &Connection, removed: bool, id: &str) -> Res<()> {
    match read_entry(conn, removed, id)? {
        None => drop_old(conn, removed, id),
        Some(entry) => put_entry(conn, removed, &entry),
    }
}

/// `_ingest(conn, after)`, inside the caller's transaction: apply the
/// record mutations after `after` and move the state to the last of them.
pub fn ingest(conn: &Connection, after: i64) -> Res<()> {
    let (ids, last) = mutated_ids(conn, after)?;
    for id in &ids {
        index_record(conn, false, id)?;
        index_record(conn, true, id)?;
    }
    if last != after {
        let mac = mac_at(conn, last)?;
        set_state(conn, &[("seq", &last.to_string()), ("mac", &mac)])?;
    }
    Ok(())
}

/// The first transaction of `_build`: the three tables dropped and made
/// again, the state naming the rules and this build's `token`. Returns
/// the log's last record mutation, which the build ends at.
pub fn build_begin(conn: &Connection, token: &str) -> Res<i64> {
    for table in ["digest_records", "digest_tokens", "digest_state"] {
        conn.execute(&format!("DROP TABLE IF EXISTS {table}"), [])?;
    }
    for statement in DIGEST_SCHEMA.split(';') {
        if !statement.trim().is_empty() {
            conn.execute(statement, [])?;
        }
    }
    let start = head(conn)?;
    set_state(conn, &[("fingerprint", fingerprint()), ("build", token)])?;
    Ok(start)
}

/// Whether the state still names this build (`_build` stops otherwise).
pub fn build_owns(conn: &Connection, token: &str) -> Res<bool> {
    Ok(state(conn)?
        .as_deref()
        .and_then(|s| get(s, "build").map(|b| b == token))
        .unwrap_or(false))
}

/// One batch of `_build`: the entries of up to `_BUILD_BATCH` rows of
/// `memories` (or `tombstones`) after rowid `after`. Returns the last
/// rowid and whether the batch was full.
pub fn build_batch(conn: &Connection, removed: bool, after: i64) -> Res<(i64, bool)> {
    let sql = if removed {
        "SELECT rowid, id FROM tombstones WHERE rowid > ? ORDER BY rowid LIMIT ?"
    } else {
        "SELECT rowid, id FROM memories WHERE rowid > ? ORDER BY rowid LIMIT ?"
    };
    let keys: Vec<(SqlValue, SqlValue)> = {
        let mut statement = conn.prepare(sql)?;
        let mut rows = statement.query(rusqlite::params![after, BUILD_BATCH])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            out.push((value(row.get_ref(0)?)?, value(row.get_ref(1)?)?));
        }
        out
    };
    let mut last = after;
    for (rowid, id) in &keys {
        index_record(conn, removed, &id.py_str())?;
        if let SqlValue::Integer(r) = rowid {
            last = *r;
        }
    }
    Ok((last, keys.len() as i64 >= BUILD_BATCH))
}

/// The last transaction of `_build`: the token removed, the state at
/// `start`.
pub fn build_end(conn: &Connection, start: i64) -> Res<()> {
    conn.execute("DELETE FROM digest_state WHERE key = 'build'", [])?;
    let mac = mac_at(conn, start)?;
    set_state(conn, &[("seq", &start.to_string()), ("mac", &mac)])
}

/// `_build` and the `digest_mark` that follows it, on a store with no
/// record, inside the caller's transaction (what a new store is born
/// with): the three tables dropped and made again, then the state
/// `fingerprint`, `seq` and `mac` of the log's last record mutation (none:
/// `0` and the empty MAC). The rows left are the reference's.
pub fn build_empty(conn: &Connection) -> rusqlite::Result<()> {
    for table in ["digest_records", "digest_tokens", "digest_state"] {
        conn.execute(&format!("DROP TABLE IF EXISTS {table}"), [])?;
    }
    for statement in DIGEST_SCHEMA.split(';') {
        if !statement.trim().is_empty() {
            conn.execute(statement, [])?;
        }
    }
    let mut insert =
        conn.prepare("INSERT OR REPLACE INTO digest_state(key, value) VALUES (?, ?)")?;
    insert.execute(["fingerprint", fingerprint()])?;
    insert.execute(["seq", "0"])?;
    insert.execute(["mac", ""])?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(t: &str) -> PyStr {
        unicode::cps(t)
    }

    #[test]
    fn the_fingerprint_is_computed_from_the_ported_rules() {
        assert_eq!(computed_fingerprint(), DIGEST_FINGERPRINT);
    }

    #[test]
    fn provenance_is_cut_at_the_leftmost_stamp() {
        let stamp = "\n\n_(consolidate --llm --from-transcript: x)_";
        assert_eq!(strip_provenance(&s(&format!("a{stamp}"))), s("a"));
        assert_eq!(strip_provenance(&s(&format!("a{stamp} \n "))), s("a"));
        assert_eq!(strip_provenance(&s(&format!("a{stamp}{stamp}"))), s("a"));
        assert_eq!(
            strip_provenance(&s(&format!("a{stamp}b"))),
            s(&format!("a{stamp}b"))
        );
        let open = "\n\n_(consolidate --llm --from-transcript: )_";
        assert_eq!(strip_provenance(&s(&format!("a{open}"))), s("a"));
        let short = "\n\n_(consolidate --llm --from-transcript: _";
        assert_eq!(strip_provenance(&s(short)), s(short));
    }

    #[test]
    fn sentences_split_as_the_pattern() {
        assert_eq!(sentences(&s("One. Two! Three?")), 3);
        assert_eq!(sentences(&s("Version 3.12 is out.")), 1);
        assert_eq!(sentences(&s("a.b")), 2);
        assert_eq!(sentences(&s("1.b")), 2);
        assert_eq!(sentences(&s("...")), 0);
        assert_eq!(sentences(&s(" \n \n x")), 1);
    }
}
