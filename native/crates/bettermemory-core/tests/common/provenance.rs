//! What the provenance tests share: `store_provenance.jsonl`'s cases, a
//! case's store rebuilt from its recorded rows and key ring, the native
//! answers to its queries in the fixture's spelling, and scratch
//! directories named by a counter.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use bettermemory_core::log::KeyRing;
use bettermemory_core::store::create;
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::provenance::{Link, Provenance, ProvenanceError, TrustRow};
use bettermemory_core::store::{SqlValue, StoreFile};
use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::{json, Value};

use super::{created, hex, jsonl, unhex};

/// The header and the cases.
pub fn cases() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_provenance.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// A fresh directory under the target dir, removed on drop.
pub struct Scratch(pub PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// `prefix` names the test binary; the counter names the directory.
pub fn scratch(prefix: &str) -> Scratch {
    let base = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join("provenance");
    fs::create_dir_all(&base).expect("base");
    loop {
        let dir = base.join(format!(
            "{prefix}-{}",
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        match fs::create_dir(&dir) {
            Ok(()) => return Scratch(fs::canonicalize(&dir).expect("dir")),
            // Left by an earlier run that did not finish: take the next.
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => panic!("scratch: {e}"),
        }
    }
}

/// The store `store_create.json` pins, made natively at `dir`: the file
/// and its keys directory.
pub fn created_store(dir: &Path) -> (PathBuf, PathBuf) {
    let doc = created::fixture();
    let keys = dir.join("keys");
    create::create(dir, &keys, &mut created::fixed(&doc)).expect("create");
    (dir.join("memory.sqlite"), keys)
}

fn sql(cell: &Value) -> Sql {
    match cell {
        Value::Null => Sql::Null,
        Value::Number(n) => Sql::Integer(n.as_i64().expect("an i64")),
        Value::String(s) => Sql::Text(s.clone()),
        Value::Object(o) if o.contains_key("$bytes") => {
            Sql::Blob(unhex(o["$bytes"].as_str().expect("hex")))
        }
        Value::Object(o) if o.contains_key("$float") => Sql::Real(
            o["$float"]
                .as_str()
                .expect("float")
                .parse()
                .expect("a float"),
        ),
        other => panic!("a cell {other}"),
    }
}

/// Replace the six tables' rows with the case's.
pub fn put_rows(file: &Path, tables: &Value) {
    let conn = Connection::open(file).expect("open");
    conn.execute_batch("BEGIN IMMEDIATE").expect("begin");
    for (table, content) in tables.as_object().expect("tables") {
        conn.execute(&format!("DELETE FROM {table}"), [])
            .expect("delete");
        let columns: Vec<&str> = content["columns"]
            .as_array()
            .expect("columns")
            .iter()
            .map(|c| c.as_str().expect("column"))
            .collect();
        let statement = format!(
            "INSERT INTO {table} ({}) VALUES ({})",
            columns.join(", "),
            vec!["?"; columns.len()].join(", ")
        );
        for row in content["rows"].as_array().expect("rows") {
            let values: Vec<Sql> = row.as_array().expect("row").iter().map(sql).collect();
            conn.execute(&statement, rusqlite::params_from_iter(values))
                .expect("insert");
        }
    }
    conn.execute_batch("COMMIT").expect("commit");
}

/// Replace the ring's key and origin files with the case's.
pub fn put_keys(keys: &Path, store_id: &str, recorded: &Value) {
    for entry in fs::read_dir(keys).expect("keys") {
        let path = entry.expect("entry").path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        if name.ends_with(".key") || name.ends_with(".origin") {
            fs::remove_file(&path).expect("remove");
        }
    }
    for (name, content) in recorded.as_object().expect("keys") {
        let path = keys.join(name.replace("<id>", store_id));
        let content = content.as_str().expect("text");
        if name.ends_with(".key") {
            fs::write(&path, unhex(content)).expect("key");
        } else {
            fs::write(&path, content).expect("origin");
        }
    }
}

/// A case's store under `dir`: the file and its keys directory.
pub fn build(case: &Value, store_id: &str, dir: &Path) -> (PathBuf, PathBuf) {
    let (file, keys) = created_store(dir);
    put_rows(&file, &case["tables"]);
    put_keys(&keys, store_id, &case["keys"]);
    (file, keys)
}

pub fn ring(keys: &Path, store_id: &str) -> KeyRing {
    load_key_ring(keys, store_id).expect("ring")
}

fn cell(v: &SqlValue) -> Value {
    match v {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(i) => json!(i),
        SqlValue::Real(f) => json!({ "$float": bettermemory_core::float_repr::repr(*f) }),
        SqlValue::Text(t) => json!(t),
        SqlValue::Blob(b) => json!({ "$bytes": hex(b) }),
    }
}

fn links(links: &[Link]) -> Value {
    Value::Array(
        links
            .iter()
            .map(|(a, b, c)| json!([cell(a), cell(b), cell(c)]))
            .collect(),
    )
}

fn trust(row: &Option<TrustRow>) -> Value {
    match row {
        None => Value::Null,
        Some(r) => json!([r.label, r.verified_at]),
    }
}

fn pairs<T: Into<Value> + Clone>(v: &[(String, T)]) -> Value {
    Value::Array(
        v.iter()
            .map(|(k, x)| Value::Array(vec![json!(k), x.clone().into()]))
            .collect(),
    )
}

/// One query's answer as the fixture spells it: `{"returned": ...}` or
/// `{"raised": [class, text]}`; Err for a refusal as not ported.
pub fn ask(reader: &Provenance<'_>, query: &Value) -> Result<Value, String> {
    let ids: Vec<&str> = query
        .get("ids")
        .map(|v| {
            v.as_array()
                .expect("ids")
                .iter()
                .map(|i| i.as_str().expect("id"))
                .collect()
        })
        .unwrap_or_default();
    let got: Result<Value, ProvenanceError> = match query["call"].as_str().expect("call") {
        "provenance_for" => reader.provenance_for(&ids).map(|v| pairs(&v)),
        "trust_rows" => reader.trust_rows(&ids).map(|v| match v {
            None => Value::Null,
            Some(rows) => Value::Array(
                rows.iter()
                    .map(|(id, r)| json!([id, r.label, r.verified_at]))
                    .collect(),
            ),
        }),
        "unaccounted_ids" => reader.unaccounted_ids().map(|v| json!(v)),
        "provenance_counts" => reader.provenance_counts().map(|v| pairs(&v)),
        "links_with_status" => reader
            .links_with_status(query["id"].as_str().expect("id"))
            .map(|(o, i, t)| json!({"out": links(&o), "in": links(&i), "trust": trust(&t)})),
        "episode_provenance" => reader.episode_provenance(&ids).map(|v| pairs(&v)),
        other => panic!("a call {other}"),
    };
    match got {
        Ok(v) => Ok(json!({ "returned": v })),
        Err(ProvenanceError::Raised { class, text }) => Ok(json!({ "raised": [class, text] })),
        Err(e @ ProvenanceError::NotPorted(_)) => Err(e.to_string()),
    }
}

/// Whether a native answer is the reference's: equal, or a raise of the
/// same class where native carries no text.
pub fn same(want: &Value, got: &Value) -> bool {
    if want == got {
        return true;
    }
    match (want.get("raised"), got.get("raised")) {
        (Some(w), Some(g)) => w[0] == g[0] && g[1].is_null(),
        _ => false,
    }
}

/// Every query of a case on one native reader of `file`.
pub fn answers(
    file: &Path,
    keys: &Path,
    store_id: &str,
    verifies: bool,
    strict: bool,
    queries: &[Value],
) -> Result<Vec<Value>, String> {
    let store = StoreFile::open(file).map_err(|e| format!("open: {e:?}"))?;
    let ring = ring(keys, store_id);
    let reader = Provenance::new(&store, &ring, verifies, strict).map_err(|e| e.to_string())?;
    queries.iter().map(|q| ask(&reader, q)).collect()
}
