//! The stores and calls of `store_reads_seams.jsonl`: the header's tables
//! replayed into an in-memory SQLite file holding the reference's schema,
//! the bulk store rebuilt by `gen_store_reads_seams`' rule, and each case's
//! call answered as the generator spells it.

use std::collections::HashMap;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::lexicon::fts_index_text;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Memory;
use bettermemory_core::store::admission::reference_schema_json;
use bettermemory_core::store::index_reads::{inbound_links, rows_updated_since, RankedQuery};
use bettermemory_core::store::record_reads::{self as rr, ReadError};
use bettermemory_core::unicode;
use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// `gen_store_write.ulid`.
pub fn ulid(n: u64) -> String {
    let digits = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    let mut n = n;
    let mut tail = Vec::new();
    for _ in 0..23 {
        tail.push(digits[(n % 32) as usize]);
        n /= 32;
    }
    tail.reverse();
    format!("01J{}", String::from_utf8(tail).unwrap())
}

/// An empty store: the reference's `SCHEMA`.
pub fn empty() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    let schema: Value = serde_json::from_str(reference_schema_json()).unwrap();
    conn.execute_batch(schema["texts"][0].as_str().unwrap())
        .unwrap();
    conn
}

fn sql_of(v: &Value) -> Sql {
    match v {
        Value::Null => Sql::Null,
        Value::Number(n) => Sql::Integer(n.as_i64().expect("an int")),
        Value::String(s) => Sql::Text(s.clone()),
        other => panic!("no column value {other}"),
    }
}

fn insert(conn: &Connection, table: &str, columns: &[String], row: &[Sql]) {
    let sql = format!(
        "INSERT INTO {table} ({}) VALUES ({})",
        columns.join(","),
        vec!["?"; columns.len()].join(",")
    );
    conn.execute(&sql, rusqlite::params_from_iter(row.iter()))
        .unwrap();
}

/// The header's tables, each row inserted in the recorded order.
pub fn full(header: &Value) -> Connection {
    let conn = empty();
    for table in ["memories", "memory_links", "tombstones", "log"] {
        let t = &header["tables"][table];
        let columns: Vec<String> = t["columns"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c.as_str().unwrap().to_owned())
            .collect();
        for row in t["rows"].as_array().unwrap() {
            let values: Vec<Sql> = row.as_array().unwrap().iter().map(sql_of).collect();
            insert(&conn, table, &columns, &values);
        }
    }
    conn
}

/// `gen_store_reads_seams._bulk_body`.
pub fn bulk_body(i: u64) -> String {
    let mut words = vec!["alpha"; (1 + i % 4) as usize];
    words.extend(vec!["filler"; (i % 7) as usize]);
    words.join(" ")
}

/// The bulk store by its rule, row 1 checked against the header's.
pub fn bulk(header: &Value) -> Connection {
    let conn = empty();
    let first = header["bulk"]["first"].as_object().unwrap();
    let n = header["bulk"]["n"].as_u64().unwrap();
    assert_eq!(
        fts_index_text(&unicode::cps(&bulk_body(7))),
        header["bulk"]["body_fts_7"].as_str().unwrap()
    );
    let columns: Vec<String> = first.keys().cloned().collect();
    let updated = first["updated"].as_str().unwrap().to_owned();
    let links = first["links_json"].as_str().unwrap().to_owned();
    for i in 1..=n {
        let body = bulk_body(i);
        let row: Vec<Sql> = columns
            .iter()
            .map(|c| match c.as_str() {
                "rowid" => Sql::Integer(i as i64),
                "id" => Sql::Text(ulid(i)),
                "body" => Sql::Text(body.clone()),
                "body_fts" => Sql::Text(fts_index_text(&unicode::cps(&body))),
                "updated" => {
                    Sql::Text(format!("{}{:02}{}", &updated[..17], i % 60, &updated[19..]))
                }
                "links_json" => Sql::Text(links.replace(&ulid(2), &ulid(i + 1))),
                other => sql_of(&first[other]),
            })
            .collect();
        if i == 1 {
            let again: Vec<Sql> = columns.iter().map(|c| sql_of(&first[c])).collect();
            assert_eq!(row, again, "the bulk rule's first row");
        }
        insert(&conn, "memories", &columns, &row);
        conn.execute(
            "INSERT INTO memory_links (source_id, type, target_id, note) VALUES (?, 'supersedes', ?, NULL)",
            [ulid(i), ulid(i + 1)],
        )
        .unwrap();
    }
    conn
}

/// A bulk case's argument by its rule (`gen_store_reads_seams._bulk_arg`).
pub fn bulk_arg(arg: &Value, n: u64) -> Value {
    match arg.as_str() {
        Some("@rowids") => json!((1..=n + 2).rev().collect::<Vec<_>>()),
        Some("@ids") => json!((1..=n + 2).map(ulid).collect::<Vec<_>>()),
        _ => arg.clone(),
    }
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap().to_owned())
        .collect()
}

fn payload(m: &Memory) -> Value {
    json!(m.payload_text().expect("a payload"))
}

fn memories(ms: Vec<Memory>) -> Value {
    Value::Array(ms.iter().map(payload).collect())
}

fn raised(e: &ReadError) -> Value {
    let class = e.class();
    if class == "ValidationError" {
        return json!({"raised": class});
    }
    let text = e.text().map(|t| json::utf8(&t));
    json!({"raised": class, "text": text})
}

mod json {
    pub fn utf8(t: &[u32]) -> String {
        bettermemory_core::json::utf8_text(t)
    }
}

fn inbound(map: HashMap<String, Vec<(String, String, String)>>) -> Value {
    let mut out = serde_json::Map::new();
    for (k, v) in map {
        out.insert(
            k,
            Value::Array(v.into_iter().map(|(a, b, c)| json!([a, b, c])).collect()),
        );
    }
    Value::Object(out)
}

/// The case's answer: `{"ok": value}` or the exception.
pub fn call(conn: &Connection, func: &str, args: &[Value]) -> Value {
    let s = |i: usize| args[i].as_str().unwrap().to_owned();
    let result: Result<Value, ReadError> = match func {
        "ranked" => (|| {
            let text = unicode::cps(&s(0));
            let mut q = RankedQuery::new(conn, &text)?;
            let batch = args[1].as_i64().unwrap();
            let all: Result<Vec<Vec<i64>>, ReadError> = q.batches(batch).collect();
            Ok(json!(all?))
        })(),
        "since" => {
            let dt = DateTime::from_isoformat(&unicode::cps(&s(0))).unwrap();
            rows_updated_since(conn, &dt).map(|v| json!(v))
        }
        "inbound" => inbound_links(conn, &strings(&args[0]), &strings(&args[1])).map(inbound),
        "body_of" => rr::body_of(conn, &s(0)).map(|v| json!(v)),
        "get_memory" => rr::get_memory(conn, &s(0)).map(|m| payload(&m)),
        "has_memory" => rr::has_memory(conn, &s(0)).map(|v| json!(v)),
        "filename_for" => rr::filename_for(conn, &s(0)).map(|v| json!(v)),
        "get_many" => rr::get_many(conn, &strings(&args[0])).map(memories),
        "light_many" => {
            let rowids: Vec<i64> = args[0]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_i64().unwrap())
                .collect();
            rr::light_many(conn, &rowids).map(memories)
        }
        "light_by_ids" => rr::light_by_ids(conn, &strings(&args[0])).map(memories),
        "memory_ids" => rr::memory_ids(conn).map(|v| json!(v)),
        "count_memories" => rr::count_memories(conn).map(|v| json!(v)),
        "has_memories" => rr::has_memories(conn).map(|v| json!(v)),
        "load_one" => rr::load_one(conn, &unicode::cps(&s(0))).map(|m| payload(&m)),
        other => panic!("no call {other}"),
    };
    match result {
        Ok(v) => json!({ "ok": v }),
        Err(e) => raised(&e),
    }
}

fn py(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => PyValue::int_from_decimal(&n.to_string()).unwrap(),
        Value::String(s) => PyValue::str(s),
        Value::Array(items) => PyValue::List(items.iter().map(py).collect()),
        Value::Object(map) => {
            PyValue::Dict(map.iter().map(|(k, v)| (PyValue::str(k), py(v))).collect())
        }
    }
}

/// `{"count", "sha256"}` of an answer, as the generator digests it
/// (`json.dumps(value, sort_keys=True)`).
pub fn digest(outcome: &Value) -> Value {
    let Some(value) = outcome.get("ok") else {
        return outcome.clone();
    };
    let options = DumpOptions {
        sort_keys: true,
        ..DumpOptions::DEFAULT
    };
    let text = json::utf8(&dumps(&py(value), options).unwrap());
    let hash = Sha256::digest(text.as_bytes());
    let hex: String = hash.iter().map(|b| format!("{b:02x}")).collect();
    let count = match value {
        Value::Array(a) => a.len(),
        Value::Object(o) => o.len(),
        _ => panic!("a digest of a scalar"),
    };
    json!({"count": count, "sha256": hex})
}
