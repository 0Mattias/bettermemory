//! What the tests of the store's document frequency source share:
//! `store_docfreq_store.jsonl`'s cases, a case's store opened natively and
//! brought to a step's tables, the fixture's admit predicates, and the
//! native answers, memo, digest and warnings in the fixture's spelling.

use std::path::{Path, PathBuf};

use bettermemory_core::float_repr;
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::docfreq::{
    DocActor, DocFreqError, DocFreqInputs, DocFreqMemo, DocFreqSource, Mask, PyInt,
};
use bettermemory_core::store::docfreq_store::{
    self, DigestWarning, Home, StoreDocFreq, WarningKind,
};
use bettermemory_core::store::versions::path_text;
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode;
use rusqlite::types::{Value as Sql, ValueRef};
use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::{hex, jsonl, unhex};

/// The fixture's files (`gen_store_docfreq_store.FILES`).
pub const FILES: [&str; 3] = [
    "store_docfreq_store.jsonl",
    "store_docfreq_store_b.jsonl",
    "store_docfreq_advance.jsonl",
];

/// A file's header and its cases.
pub fn file_cases(name: &str) -> (Value, Vec<Value>) {
    let mut lines = jsonl(name);
    let header = lines.remove(0);
    (header, lines)
}

/// The first file's header and every file's cases (each file's store is
/// opened alike: `columns_checked` holds each header to it).
pub fn cases() -> (Value, Vec<Value>) {
    let (header, mut all) = file_cases(FILES[0]);
    for name in &FILES[1..] {
        all.extend(file_cases(name).1);
    }
    (header, all)
}

/// Each table's columns in `file` against the header's (the reference's
/// opened store): a step's rows are laid by position, so a column order
/// that drifted would lay values in the wrong columns. Err names the
/// tables that differ.
pub fn columns_checked(file: &Path, header: &Value) -> Result<(), String> {
    let conn = Connection::open(file).expect("open");
    let want = header["columns"].as_object().expect("the header's columns");
    let mut differs = Vec::new();
    for (table, columns) in want {
        let got: Vec<String> = conn
            .prepare(reading(table))
            .expect("columns")
            .column_names()
            .map(str::to_owned)
            .collect();
        if json!(got) != *columns {
            differs.push(format!("{table}: {got:?} want {columns}"));
        }
    }
    if want.len() != 7 {
        differs.push(format!("{} tables in the header, want 7", want.len()));
    }
    if differs.is_empty() {
        Ok(())
    } else {
        Err(differs.join("; "))
    }
}

fn sql(cell: &Value) -> Sql {
    match cell {
        Value::Null => Sql::Null,
        Value::Number(n) => Sql::Integer(n.as_i64().expect("an i64")),
        Value::String(s) => Sql::Text(s.clone()),
        Value::Object(o) if o.contains_key("$bytes") => {
            Sql::Blob(unhex(o["$bytes"].as_str().expect("hex")))
        }
        Value::Object(o) if o.contains_key("$float") => {
            Sql::Real(o["$float"].as_str().expect("f").parse().expect("a float"))
        }
        other => panic!("a cell {other}"),
    }
}

/// A cell as the fixture spells it.
pub fn cell(v: ValueRef<'_>) -> Value {
    match v {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(i) => json!(i),
        ValueRef::Real(f) => json!({ "$float": float_repr::repr(f) }),
        ValueRef::Text(t) => json!(String::from_utf8_lossy(t)),
        ValueRef::Blob(b) => json!({ "$bytes": hex(b) }),
    }
}

/// The statement the fixture reads a table with
/// (`gen_store_docfreq_store.TABLES`): its columns are the ones a step's
/// rows hold.
fn reading(table: &str) -> &'static str {
    match table {
        "memories" => "SELECT rowid AS rowid, * FROM memories",
        "homes" => "SELECT * FROM homes",
        "log" => "SELECT * FROM log",
        "tombstones" => "SELECT * FROM tombstones",
        "digest_records" => "SELECT * FROM digest_records",
        "digest_tokens" => "SELECT * FROM digest_tokens",
        "digest_state" => "SELECT * FROM digest_state",
        other => panic!("a table {other}"),
    }
}

/// Lay a step's tables into `file` on a connection of its own: a table
/// whole (its rows replaced), the log's rows appended, or a keyed table's
/// rows changed (`upsert`, by its first column) and gone (`delete`). The
/// columns are the table's own in `file`, whose schema is the reference's
/// at that step.
pub fn lay(file: &Path, tables: &Value) {
    let conn = Connection::open(file).expect("open");
    conn.execute_batch("BEGIN IMMEDIATE").expect("begin");
    for (table, content) in tables.as_object().expect("tables") {
        let columns: Vec<String> = conn
            .prepare(reading(table))
            .expect("columns")
            .column_names()
            .map(str::to_owned)
            .collect();
        let rows = if let Some(rows) = content.get("append") {
            rows
        } else if let Some(rows) = content.get("upsert") {
            let gone = content["delete"].as_array().expect("delete");
            let changed = rows.as_array().expect("upsert").iter().map(|r| &r[0]);
            let statement = format!("DELETE FROM {table} WHERE {} = ?", columns[0]);
            for key in gone.iter().chain(changed) {
                conn.execute(&statement, [sql(key)]).expect("delete");
            }
            rows
        } else {
            conn.execute(&format!("DELETE FROM {table}"), [])
                .expect("delete");
            &content["rows"]
        };
        let statement = format!(
            "INSERT INTO {table} ({}) VALUES ({})",
            columns.join(", "),
            vec!["?"; columns.len()].join(", ")
        );
        for row in rows.as_array().expect("rows") {
            let values: Vec<Sql> = row.as_array().expect("row").iter().map(sql).collect();
            conn.execute(&statement, rusqlite::params_from_iter(values))
                .expect("insert");
        }
    }
    conn.execute_batch("COMMIT").expect("commit");
}

/// Lay a step into `file`: its `sql` first (what the tables do not
/// carry), then its tables, then the log rows it planted after them.
pub fn lay_step(file: &Path, step: &Value) {
    let run = |field: &str| {
        if let Some(sql) = step[field].as_array() {
            let conn = Connection::open(file).expect("open");
            for statement in sql {
                conn.execute_batch(statement.as_str().expect("sql"))
                    .expect("sql");
            }
        }
    };
    run("sql");
    lay(file, &step["tables"]);
    run("plant");
}

/// The fixture's predicate by name (`gen_store_docfreq_store.admit_of`).
pub fn admit_of(name: &str) -> impl FnMut(&[PyValue], Option<Home<'_>>, Option<&DocActor>) -> bool {
    let (kind, arg) = name.split_once(':').unwrap_or((name, ""));
    let (kind, arg) = (kind.to_owned(), arg.to_owned());
    move |scopes, project, actor| match kind.as_str() {
        "all" => true,
        "none" => false,
        "scope" => scopes.iter().any(|s| *s == PyValue::str(&arg)),
        "project" => project == Some(Home::Text(arg.as_str())),
        "homebytes" => project == Some(Home::Bytes(&unhex(&arg))),
        "nohome" => project.is_none(),
        "client" => actor.is_some_and(|a| a.client.as_deref() == Some(arg.as_str())),
        "noactor" => actor.is_none(),
        other => panic!("a predicate {other}"),
    }
}

/// The class a failed call raises, as the fixture names it.
pub fn class_of(e: &DocFreqError) -> String {
    match e {
        DocFreqError::Py(p) => p.name().to_owned(),
        DocFreqError::IndexError => "IndexError".to_owned(),
        DocFreqError::Source(class) => class.clone(),
    }
}

fn pairs(v: &[(String, usize)]) -> Value {
    let mut v = v.to_vec();
    v.sort();
    Value::Array(v.iter().map(|(t, n)| json!([t, n])).collect())
}

/// A column's value as the fixture spells it.
fn sql_cell(v: &SqlValue) -> Value {
    match v {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(i) => json!(i),
        SqlValue::Real(f) => json!({ "$float": float_repr::repr(*f) }),
        SqlValue::Text(t) => json!(t),
        SqlValue::Blob(b) => json!({ "$bytes": hex(b) }),
    }
}

/// An int as the fixture spells it: one past 64 bits as its decimal text
/// under `$int` (`gen_store_docfreq_store._cell`).
fn int_cell(n: &PyInt) -> Value {
    match n {
        PyInt::Small(i) => json!(i),
        PyInt::Big(text) => json!({ "$int": text }),
    }
}

/// One question: `rank` (`ranking_statistics`), `memo` or `plain`
/// (`document_frequencies` with `memoised` True or False), or the
/// digest's own: `mark` (`digest_mark(build=False)`), `rows`
/// (`digest_admission_rows`), `head` (`_memory_head`).
pub fn answer(
    call: &str,
    src: &StoreDocFreq<'_>,
    memo: &DocFreqMemo,
    terms: &[String],
    admit: &str,
) -> Value {
    let mut admit = admit_of(admit);
    let got = match call {
        "mark" => src.digest_mark().map(|m| m.map(|m| json!(m))),
        "head" => src.memory_head().map(|(seq, mac)| Some(json!([seq, mac]))),
        "rows" => src.digest_admission_rows().map(|rows| {
            rows.map(|rows| {
                rows.iter()
                    .map(|(rid, scopes, home, client, model, length)| {
                        json!([
                            int_cell(rid),
                            scopes,
                            sql_cell(home),
                            sql_cell(client),
                            sql_cell(model),
                            int_cell(length)
                        ])
                    })
                    .collect::<Value>()
            })
        }),
        "rank" => docfreq_store::ranking_statistics(src, memo, terms, &mut admit).map(|found| {
            found.map(|(size, body, scope, average)| {
                json!([
                    size,
                    pairs(&body),
                    pairs(&scope),
                    average.map(float_repr::repr)
                ])
            })
        }),
        _ => docfreq_store::document_frequencies(src, memo, terms, &mut admit, call == "memo").map(
            |found| found.map(|(size, body, scope)| json!([size, pairs(&body), pairs(&scope)])),
        ),
    };
    match got {
        Ok(v) => json!({ "returned": v }),
        Err(e) => json!({ "raised": class_of(&e) }),
    }
}

/// Every question of a step on one source.
pub fn answers(src: &StoreDocFreq<'_>, memo: &DocFreqMemo, asks: &Value) -> Value {
    Value::Array(
        asks.as_array()
            .expect("asks")
            .iter()
            .map(|a| {
                let terms: Vec<String> = a[1]
                    .as_array()
                    .expect("terms")
                    .iter()
                    .map(|t| t.as_str().expect("term").to_owned())
                    .collect();
                let call = a[0].as_str().expect("call");
                let got = answer(call, src, memo, &terms, a[2].as_str().expect("admit"));
                json!([a[0], a[1], a[2], got])
            })
            .collect(),
    )
}

/// The path text a store at `path` is keyed by, the root spelled `<root>`.
pub fn spelled(path: &str, root: &Path) -> String {
    let root = unicode::to_string(&path_text(root)).expect("utf-8");
    path.replacen(&root, "<root>", 1)
}

/// The memo's keys, least recently read first, each with its terms kept.
pub fn memo_view(memo: &DocFreqMemo, root: &Path) -> Value {
    Value::Array(
        memo.keys()
            .into_iter()
            .map(|key| {
                let terms = memo.entry(&key).map(|e| e.terms()).unwrap_or_default();
                json!([spelled(&key.0, root), key.1, terms])
            })
            .collect(),
    )
}

/// The longest canonical entry text the fixture writes out in full
/// (`gen_store_docfreq_store.ENTRY_INLINE`).
const ENTRY_INLINE: usize = 500;

fn home_cell(key: Option<&str>) -> Value {
    docfreq_store::with_home(key, |home| match home {
        None => Value::Null,
        Some(Home::Text(t)) => json!(t),
        Some(Home::Bytes(b)) => json!({ "$bytes": hex(b) }),
    })
}

fn positions(mask: &Mask) -> Vec<usize> {
    let (n, mut out, mut p) = (mask.count(), Vec::new(), 0);
    while out.len() < n {
        if mask.get(p) {
            out.push(p);
        }
        p += 1;
    }
    out
}

/// `json.dumps(value, ensure_ascii=True, separators=(",", ":"))`.
fn dumps(value: &PyValue) -> String {
    let options = DumpOptions {
        ensure_ascii: true,
        item_separator: ",",
        key_separator: ":",
        ..DumpOptions::DEFAULT
    };
    unicode::to_string(&json::dumps(value, options).expect("dumps")).expect("ascii")
}

/// A JSON text as `json.dumps(..., ensure_ascii=True)` spells it: every
/// character past ASCII (only strings hold one) as `\uXXXX`, a pair past
/// U+FFFF.
fn ascii_text(value: &Value) -> String {
    let mut out = String::new();
    for c in serde_json::to_string(value).expect("text").chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut units = [0u16; 2];
            for unit in c.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

/// A memo entry's inputs in the fixture's canonical form
/// (`gen_store_docfreq_store._entry`), or the SHA-256 of its JSON text
/// past `ENTRY_INLINE` bytes.
pub fn entry_view(inputs: &DocFreqInputs) -> Value {
    let groups: Vec<Value> = inputs
        .groups
        .iter()
        .map(|g| {
            json!([
                dumps(&PyValue::List(g.scopes.clone())),
                home_cell(g.home_project.as_deref()),
                g.actor.as_ref().map(|a| json!([a.client, a.model])),
                positions(&g.mask),
            ])
        })
        .collect();
    let mut index: Vec<(&PyInt, usize)> = inputs.index.iter().map(|(r, p)| (r, *p)).collect();
    index.sort_unstable();
    let lengths = inputs.lengths.as_ref().map(|l| {
        l.iter()
            .map(|n| match i64::try_from(*n) {
                Ok(n) => json!(n),
                // A total past 64 bits, as the fixture spells an int past them.
                Err(_) => json!({ "$int": n.to_string() }),
            })
            .collect::<Vec<_>>()
    });
    let keys: Vec<Value> = inputs
        .keys
        .iter()
        .map(|(scopes, home, client, model)| {
            json!([scopes, home_cell(home.as_deref()), client, model])
        })
        .collect();
    let form = json!([
        [inputs.head.0, inputs.head.1],
        groups,
        lengths,
        index
            .iter()
            .map(|(r, p)| json!([int_cell(r), p]))
            .collect::<Vec<_>>(),
        keys,
        inputs.group_of,
        inputs.length_of,
        inputs.version,
        inputs
            .changed
            .iter()
            .map(|(v, r, p)| json!([v, int_cell(r), p]))
            .collect::<Vec<_>>(),
    ]);
    let text = ascii_text(&form);
    if text.len() <= ENTRY_INLINE {
        return form;
    }
    let digest: String = Sha256::digest(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    json!({ "sha256": digest })
}

/// Every memo entry's inputs, in the memo's order.
pub fn entries_view(memo: &DocFreqMemo) -> Value {
    Value::Array(
        memo.keys()
            .into_iter()
            .map(|key| entry_view(&memo.entry(&key).expect("an entry").inputs))
            .collect(),
    )
}

/// The fixture's entries with each group key's client and model as the
/// port holds them: the reference keys a client or model column that holds
/// bytes as the bytes, the port as their UTF-8 text (a bytes client and the
/// same text are one group key here; README, store-2). Nothing else is
/// changed, and a column that does not decode is left as it is.
pub fn entries_as_keyed_here(want: &Value) -> Value {
    let mut want = want.clone();
    for entry in want.as_array_mut().expect("entries") {
        let Some(keys) = entry.get_mut(4).and_then(Value::as_array_mut) else {
            continue;
        };
        for key in keys {
            for column in [2, 3] {
                let text = key[column]
                    .get("$bytes")
                    .and_then(Value::as_str)
                    .and_then(|h| String::from_utf8(unhex(h)).ok());
                if let Some(text) = text {
                    key[column] = json!(text);
                }
            }
        }
    }
    want
}

/// The digest's state, its rows' admission columns and its token count.
pub fn digest_after(file: &Path) -> Value {
    let conn = Connection::open(file).expect("open");
    let rows = |sql: &str, n: usize| -> Vec<Value> {
        let mut statement = conn.prepare(sql).expect("prepare");
        let mut rows = statement.query([]).expect("query");
        let mut out = Vec::new();
        while let Some(row) = rows.next().expect("row") {
            out.push(Value::Array(
                (0..n)
                    .map(|i| cell(row.get_ref(i).expect("cell")))
                    .collect(),
            ));
        }
        out
    };
    let tokens: i64 = conn
        .query_row("SELECT COUNT(*) FROM digest_tokens", [], |r| r.get(0))
        .expect("count");
    json!({
        "state": rows("SELECT key, value FROM digest_state ORDER BY key", 2),
        "records": rows(
            "SELECT rid, removed, dl, scopes, client, model FROM digest_records ORDER BY doc",
            6
        ),
        "tokens": tokens,
    })
}

/// Whether an exception's text is the store's own wording, which the
/// fixture keeps (`gen_store_docfreq_store._own_wording`).
fn own_wording(w: &DigestWarning) -> bool {
    w.class == "StoreVersionError" || w.text.starts_with("the write transaction on store ")
}

/// The warnings as the fixture's handler takes them, the last item the
/// message (`DigestWarning::message`), the root spelled `<root>` and the
/// exception's text `<exc>` where the fixture spells it so.
pub fn warnings_view(warnings: &[DigestWarning], root: &Path) -> Value {
    Value::Array(
        warnings
            .iter()
            .map(|w| {
                let format = match w.kind {
                    WarningKind::NotCurrent => {
                        "store %s: the digest could not be brought current (%s)"
                    }
                    WarningKind::NotRead => "store %s: the digest could not be read (%s)",
                    WarningKind::Residue => "%s was left inside a transaction; %s",
                    WarningKind::NotTakenBack => {
                        "store %s: a nested write could not be taken back (%s); \
                         the owner's transaction is rolled back with it"
                    }
                };
                let mut shown = w.clone();
                if w.kind != WarningKind::Residue && !own_wording(w) {
                    shown.text = "<exc>".to_owned();
                }
                let class = (w.kind != WarningKind::Residue).then_some(w.class);
                let root = unicode::to_string(&path_text(root)).expect("utf-8");
                let message = shown.message().replace(&root, "<root>");
                json!(["WARNING", format, class, w.code, message])
            })
            .collect(),
    )
}

/// The fixture's warnings with each message made from its format and
/// arguments, as `record.getMessage()` makes it.
pub fn want_warnings(want: &Value) -> Value {
    Value::Array(
        want.as_array()
            .expect("warnings")
            .iter()
            .map(|w| {
                let format = w[1].as_str().expect("format");
                let mut args = w[4].as_array().expect("args").iter();
                let mut message = String::new();
                for (i, piece) in format.split("%s").enumerate() {
                    if i > 0 {
                        message.push_str(args.next().and_then(Value::as_str).expect("an arg"));
                    }
                    message.push_str(piece);
                }
                json!([w[0], w[1], w[2], w[3], message])
            })
            .collect(),
    )
}

/// The head checkpoint file's bytes, None where there is none.
pub fn head_bytes(keys: &Path, store_id: &str) -> Option<Vec<u8>> {
    std::fs::read(keys.join(format!("{store_id}.head"))).ok()
}

/// The store file and keys directory of a case under `root`.
pub fn files(root: &Path) -> (PathBuf, PathBuf) {
    let c = root.join("c");
    (c.join("memory.sqlite"), c.join("keys"))
}

/// A store of the tables `_advanced_inputs` and the digest's reads use,
/// made by hand: the log, `homes`, the memory rows and their FTS table,
/// and the digest, kept current as rows are added (its state at the log's
/// last record mutation, under this build's fingerprint).
pub struct HandStore {
    pub conn: Connection,
    pub seq: i64,
}

const HAND_SCHEMA: &str = "
CREATE TABLE log (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
    mac TEXT NOT NULL);
CREATE TABLE homes (record_id TEXT PRIMARY KEY, project TEXT NOT NULL);
CREATE TABLE memories (id TEXT PRIMARY KEY, scopes_json TEXT NOT NULL,
    actor_client TEXT, actor_model TEXT);
CREATE VIRTUAL TABLE memories_fts USING fts5(body_fts, scopes_fts);
";

impl HandStore {
    /// Over `conn`, which holds no table yet.
    pub fn new(conn: Connection) -> HandStore {
        conn.execute_batch(HAND_SCHEMA).expect("schema");
        conn.execute_batch(bettermemory_core::store::digest::DIGEST_SCHEMA)
            .expect("digest");
        conn.execute(
            "INSERT INTO digest_state (key, value) VALUES ('fingerprint', ?), \
             ('seq', '0'), ('mac', '')",
            [bettermemory_core::store::digest::fingerprint()],
        )
        .expect("state");
        HandStore { conn, seq: 0 }
    }

    /// A log row; a record mutation moves the digest's state to it, so
    /// the digest stays current, unless `behind`.
    pub fn log_row(&mut self, kind: &str, payload: &str, behind: bool) -> (i64, String) {
        self.seq += 1;
        let mac = format!("m{}", self.seq);
        self.conn
            .execute(
                "INSERT INTO log (seq, kind, payload, mac) VALUES (?, ?, ?, ?)",
                rusqlite::params![self.seq, kind, payload, mac],
            )
            .expect("log");
        let digest = bettermemory_core::store::digest::DIGEST_KINDS.contains(&kind);
        if digest && !behind {
            self.conn
                .execute(
                    "UPDATE digest_state SET value = CASE key WHEN 'seq' THEN ?1 ELSE ?2 END \
                     WHERE key IN ('seq', 'mac')",
                    rusqlite::params![self.seq.to_string(), mac],
                )
                .expect("state");
        }
        (self.seq, mac)
    }

    /// The record `id` written: its digest row (row number, scopes text,
    /// client, length) and its memory row, then its `memory_put` row.
    pub fn put(&mut self, id: &str, rid: i64, scopes: &str, client: Option<&str>, dl: i64) {
        self.conn
            .execute(
                "INSERT OR REPLACE INTO digest_records (id, rid, scopes, client, dl) \
                 VALUES (?, ?, ?, ?, ?)",
                rusqlite::params![id, rid, scopes, client, dl],
            )
            .expect("digest row");
        self.conn
            .execute(
                "INSERT OR REPLACE INTO memories (rowid, id, scopes_json, actor_client) \
                 VALUES (?, ?, ?, ?)",
                rusqlite::params![rid, id, scopes, client],
            )
            .expect("memory row");
        let payload = json!({ "memory": { "id": id } }).to_string();
        self.log_row("memory_put", &payload, false);
    }

    /// `home_put` of `id` under `project` (`None`: `home_delete`).
    pub fn home(&mut self, id: &str, project: Option<&str>) {
        match project {
            Some(p) => {
                self.conn
                    .execute(
                        "INSERT OR REPLACE INTO homes (record_id, project) VALUES (?, ?)",
                        [id, p],
                    )
                    .expect("home");
                let payload = json!({ "home": { "record_id": id } }).to_string();
                self.log_row("home_put", &payload, false);
            }
            None => {
                self.conn
                    .execute("DELETE FROM homes WHERE record_id = ?", [id])
                    .expect("home");
                let payload = json!({ "record_id": id }).to_string();
                self.log_row("home_delete", &payload, false);
            }
        }
    }

    /// `count` `home_delete` rows naming `name(i)` for `i` from 1.
    pub fn named(&mut self, count: usize, name: impl Fn(usize) -> String) {
        self.conn.execute_batch("BEGIN").expect("begin");
        for i in 1..=count {
            let payload = json!({ "record_id": name(i) }).to_string();
            self.log_row("home_delete", &payload, false);
        }
        self.conn.execute_batch("COMMIT").expect("commit");
    }
}

/// The positions a group's mask holds, in order.
pub fn mask_positions(mask: &Mask) -> Vec<usize> {
    positions(mask)
}
