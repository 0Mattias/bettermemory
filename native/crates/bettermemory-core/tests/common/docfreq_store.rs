//! What the tests of the store's document frequency source share:
//! `store_docfreq_store.jsonl`'s cases, a case's store opened natively and
//! brought to a step's tables, the fixture's admit predicates, and the
//! native answers, memo, digest and warnings in the fixture's spelling.

use std::path::{Path, PathBuf};

use bettermemory_core::float_repr;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::docfreq::{DocActor, DocFreqError, DocFreqMemo, DocFreqSource};
use bettermemory_core::store::docfreq_store::{
    self, DigestWarning, Home, StoreDocFreq, WarningKind,
};
use bettermemory_core::store::versions::path_text;
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode;
use rusqlite::types::{Value as Sql, ValueRef};
use rusqlite::Connection;
use serde_json::{json, Value};

use super::{hex, jsonl, unhex};

/// The header and the cases.
pub fn cases() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_docfreq_store.jsonl");
    let header = lines.remove(0);
    (header, lines)
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

/// Lay a step's tables into `file` on a connection of its own: a table
/// whole (its rows replaced), or the log's rows appended.
pub fn lay(file: &Path, tables: &Value) {
    let conn = Connection::open(file).expect("open");
    conn.execute_batch("BEGIN IMMEDIATE").expect("begin");
    for (table, content) in tables.as_object().expect("tables") {
        let columns: Vec<&str> = content["columns"]
            .as_array()
            .expect("columns")
            .iter()
            .map(|c| c.as_str().expect("column"))
            .collect();
        let rows = match content.get("append") {
            Some(rows) => rows,
            None => {
                conn.execute(&format!("DELETE FROM {table}"), [])
                    .expect("delete");
                &content["rows"]
            }
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
/// carry), then its tables.
pub fn lay_step(file: &Path, step: &Value) {
    if let Some(sql) = step["sql"].as_array() {
        let conn = Connection::open(file).expect("open");
        for statement in sql {
            conn.execute_batch(statement.as_str().expect("sql"))
                .expect("sql");
        }
    }
    lay(file, &step["tables"]);
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
                            rid,
                            scopes,
                            sql_cell(home),
                            sql_cell(client),
                            sql_cell(model),
                            length
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
