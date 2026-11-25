//! What the restore-row tests share: a case of `restore_rows.jsonl` set
//! up as `gen_restore_rows.run_case` sets it up (the created store opened
//! for writing, then its rows inserted and its record written), a step
//! called as the generator calls it, and what it did read back as the
//! generator reads it.

#![allow(dead_code)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use bettermemory_core::json;
use bettermemory_core::log::replaced::Appended;
use bettermemory_core::pyvalue::{py_str, PyValue};
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::restore_rows::{
    append_for, append_missing, restore_record_path, settle, Arg, RestoreError, Warning,
};
use bettermemory_core::unicode;
use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::{json, Value};

use crate::common::opened;
use crate::common::stores::{scratch, Scratch};
use crate::common::txs::Replay;
use crate::common::written;

pub const STORE_ID: &str = "0123456789abcdef0123456789abcdef";

/// A case's store, open for writing, with its rows and record in place.
pub struct Opened {
    pub dir: Scratch,
    pub file: PathBuf,
    pub keys: PathBuf,
    pub record: PathBuf,
    pub store: WritableStore,
}

/// A text a case spells (`gen_restore_rows._text`): itself, its parts
/// repeated, or `{"blob": hex}`.
pub enum Spelled {
    Text(String),
    Blob(Vec<u8>),
}

pub fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).expect("hex"))
        .collect()
}

pub fn spelled(spec: &Value) -> Spelled {
    if let Some(text) = spec.as_str() {
        return Spelled::Text(text.to_owned());
    }
    if let Some(blob) = spec.get("blob") {
        return Spelled::Blob(unhex(blob.as_str().expect("hex")));
    }
    if let Some(text) = spec.get("text") {
        return Spelled::Text(text.as_str().expect("text").to_owned());
    }
    let mut out = String::new();
    for part in spec["parts"].as_array().expect("parts") {
        let text = part[0].as_str().expect("part");
        out.push_str(&text.repeat(part[1].as_u64().expect("count") as usize));
    }
    Spelled::Text(out)
}

fn sql(spec: &Value) -> Sql {
    match spelled(spec) {
        Spelled::Text(t) => Sql::Text(t),
        Spelled::Blob(b) => Sql::Blob(b),
    }
}

/// `gen_restore_rows._insert`.
pub fn insert(conn: &Connection, row: &Value) {
    conn.execute(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) \
         VALUES (?, ?, NULL, ?, ?, 'p', ?)",
        rusqlite::params![
            row["seq"].as_i64().expect("seq"),
            row["ts"].as_str().expect("ts"),
            sql(&row["kind"]),
            sql(&row["payload"]),
            sql(&row["mac"]),
        ],
    )
    .expect("insert");
}

/// `gen_restore_rows._put_record`.
pub fn put_record(path: &Path, spec: &Value) {
    if spec.is_null() {
        return;
    }
    if spec.get("dir").is_some() {
        fs::create_dir(path).expect("dir");
    } else if let Some(hex) = spec.get("hex") {
        fs::write(path, unhex(hex.as_str().expect("hex"))).expect("record");
    } else if let Spelled::Text(text) = spelled(spec) {
        fs::write(path, text).expect("record");
    }
}

/// The record's text as the generator reads it (Latin-1), or None.
pub fn read_record(path: &Path) -> Option<String> {
    if !path.is_file() {
        return None;
    }
    Some(
        fs::read(path)
            .expect("read")
            .iter()
            .map(|&b| char::from(b))
            .collect(),
    )
}

/// The created store opened for writing, `rows` inserted, the record
/// written and its lock file made where `lock` says.
pub fn open_case(name: &str, rows: &[Value], record: &Value, lock: bool) -> Opened {
    let dir = scratch(name);
    let c = dir.0.join("c");
    opened::made("created", &c);
    let file = c.join("memory.sqlite");
    let keys = c.join("keys");
    let store = open_for_writing(&file, &keys, &opened::engine_provider()).expect("open");
    for row in rows {
        insert(store.connection(), row);
    }
    let record_path = restore_record_path(&keys, STORE_ID);
    put_record(&record_path, record);
    if lock {
        let lock_path = keys.join(format!("{STORE_ID}.backup_restore.json.lock"));
        fs::write(&lock_path, b"").expect("lock");
        fs::set_permissions(&lock_path, fs::Permissions::from_mode(0o600)).expect("mode");
    }
    Opened {
        dir,
        file,
        keys,
        record: record_path,
        store,
    }
}

pub fn row_json(row: &Appended) -> Value {
    json!({"seq": row.seq, "mac": row.mac})
}

/// The class a call raised, as `type(exc).__name__`.
pub fn raised(error: &RestoreError) -> String {
    if let Some(cause) = error.cause() {
        return cause.class;
    }
    match error {
        RestoreError::Py(e) => e.name().to_owned(),
        other => format!("{other:?}"),
    }
}

/// `gen_restore_rows._call_it`, with the step's answer or the class it
/// raised.
pub fn call(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    step: &Value,
    log: &mut Vec<Warning>,
) -> Value {
    let loads =
        |text: &Value| json::loads(&unicode::cps(text.as_str().expect("JSON text"))).expect("JSON");
    let out = match step["call"].as_str().expect("call") {
        "settle" => settle(store, provider, log).map(|()| Value::Null),
        "append_for" => append_for(store, provider, &loads(&step["entry"]), log)
            .map(|row| row.as_ref().map_or(Value::Null, row_json)),
        _ => {
            let entries = step.get("entries").map(|e| match &loads(e) {
                PyValue::List(items) => items.clone(),
                other => panic!("entries {other:?}"),
            });
            append_missing(store, provider, entries, log).map(|settled| {
                Value::Array(
                    settled
                        .iter()
                        .map(|(entry, row)| {
                            let text = json::dumps(entry, json::DumpOptions::SORTED_ASCII_SPACED)
                                .expect("dumps");
                            json!([unicode::to_string(&text).expect("text"), row_json(row)])
                        })
                        .collect(),
                )
            })
        }
    };
    match out {
        Ok(answer) => json!({ "answer": answer }),
        Err(e) => json!({ "raised": raised(&e) }),
    }
}

/// The step's recorded answer or class, in [`call`]'s shape.
pub fn expected_call(step: &Value) -> Value {
    match step.get("raised") {
        Some(class) => json!({ "raised": class }),
        None => json!({ "answer": step["answer"] }),
    }
}

/// `gen_restore_rows._arg` of a text.
fn text_arg(points: &[u32]) -> Value {
    match unicode::to_string(points) {
        Some(text) => json!({ "text": text }),
        None => json!({ "text_cps": points }),
    }
}

/// The warnings as `gen_restore_rows._warnings` records them.
pub fn warnings(log: &[Warning], store: &Path, root: &Path) -> Vec<Value> {
    let path = |p: &Path| opened::scrub(&p.display().to_string(), root);
    log.iter()
        .map(|w| {
            let args: Vec<Value> = w
                .args()
                .into_iter()
                .map(|a| match a {
                    Arg::Store => json!({ "path": path(store) }),
                    Arg::Path(p) => json!({ "path": path(p) }),
                    Arg::Value(v) => text_arg(&py_str(v).expect("str()")),
                    Arg::Text(t) => json!({ "text": t }),
                    Arg::Int(n) => json!({ "text": n.to_string() }),
                    Arg::Cause(c) => json!({ "exc": c.class, "sqlite": c.sqlite }),
                })
                .collect();
            json!({ "level": "WARNING", "msg": w.format(), "args": args })
        })
        .collect()
}

/// The hook around a step's call: set up, then undone by the closure
/// returned.
pub fn hooked(hook: Option<&str>, opened: &Opened) -> Box<dyn FnOnce(&Opened)> {
    match hook {
        Some("query_only") => {
            opened
                .store
                .connection()
                .execute_batch("PRAGMA query_only = ON")
                .expect("pragma");
            Box::new(|o: &Opened| {
                o.store
                    .connection()
                    .execute_batch("PRAGMA query_only = OFF")
                    .expect("pragma")
            })
        }
        Some("busy") => {
            let holder = Connection::open(&opened.file).expect("holder");
            holder.execute_batch("BEGIN IMMEDIATE").expect("hold");
            opened
                .store
                .connection()
                .execute_batch("PRAGMA busy_timeout = 100")
                .expect("timeout");
            Box::new(move |o: &Opened| {
                o.store
                    .connection()
                    .execute_batch("PRAGMA busy_timeout = 5000")
                    .expect("timeout");
                holder.execute_batch("ROLLBACK").expect("release");
            })
        }
        Some("readonly_keys") => {
            fs::set_permissions(&opened.keys, fs::Permissions::from_mode(0o500)).expect("mode");
            Box::new(|o: &Opened| {
                fs::set_permissions(&o.keys, fs::Permissions::from_mode(0o700)).expect("mode")
            })
        }
        None => Box::new(|_: &Opened| {}),
        Some(other) => panic!("hook {other}"),
    }
}

/// One step run as `gen_restore_rows.run_case` runs it, read back in the
/// shape the generator records; the differences from `step` are named.
pub fn run_step(opened: &mut Opened, step: &Value, label: &str) -> Vec<String> {
    for op in step
        .get("before")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        opened
            .store
            .connection()
            .execute_batch(op["sql"].as_str().expect("sql"))
            .expect("before");
    }
    let before = written::last_seq(&opened.file);
    let text_before = read_record(&opened.record);
    let mut clocks = Replay::of(step);
    let undo = hooked(step["hook"].as_str(), opened);
    let mut log = Vec::new();
    let got = call(&mut opened.store, &mut clocks, step, &mut log);
    undo(opened);
    let mut failures = Vec::new();
    let mut check = |what: &str, got: Value, want: &Value| {
        if got != *want {
            failures.push(format!("{label}: {what} {got}, the reference {want}"));
        }
    };
    check("call", got, &expected_call(step));
    let root = opened.dir.0.clone();
    check(
        "warnings",
        Value::Array(warnings(&log, opened.store.path(), &root)),
        &step["warnings"],
    );
    check(
        "rows",
        written::log_rows(&opened.file, before),
        &step["rows"],
    );
    let after = read_record(&opened.record);
    let after = if after == text_before {
        json!("unchanged")
    } else {
        json!(after)
    };
    check("record", after, &step["record_after"]);
    let head = fs::read_to_string(opened.keys.join(format!("{STORE_ID}.head"))).ok();
    check("head", json!(head), &step["head"]);
    check(
        "in_transaction",
        json!(!opened.store.connection().is_autocommit()),
        &step["in_transaction"],
    );
    if !clocks.drained() {
        failures.push(format!(
            "{label}: clocks read less often than the reference"
        ));
    }
    failures
}

/// The MAC of the created store's first row.
pub fn mac1(opened: &Opened) -> String {
    opened
        .store
        .connection()
        .query_row("SELECT mac FROM log WHERE seq = 1", [], |r| r.get(0))
        .expect("seq 1")
}

/// `gen_restore_rows._entry`: a restore's entry with `fields` over it
/// (a null field in `drop` removed).
pub fn entry(mac1: &str, fields: Value, drop: &[&str]) -> Value {
    let mut out = json!({
        "kind": "store_restored",
        "row": null,
        "restored_at": "2002-01-01T00:00:00.000000Z",
        "bundle": "/b/1",
        "store_id": STORE_ID,
        "head_before": null,
        "head_after": {"seq": 1, "mac": mac1},
        "log_end_before": null,
    });
    for (k, v) in fields.as_object().expect("fields") {
        out[k] = v.clone();
    }
    for k in drop {
        out.as_object_mut().expect("entry").remove(*k);
    }
    out
}

/// A record holding `entries`, as a case spells it.
pub fn record_of(entries: &[Value]) -> Value {
    let text = serde_json::to_string(&json!({
        "format": "bettermemory.backup_restore",
        "restores": entries,
    }))
    .expect("JSON");
    json!({ "text": text })
}

/// A log row as a case spells it.
pub fn row(seq: i64, kind: &str, ts: &str) -> Value {
    json!({"seq": seq, "ts": ts, "kind": kind, "payload": "{}", "mac": format!("m{seq}")})
}

pub const LATE: &str = "2002-06-01T00:00:00.000000Z";
pub const EARLY: &str = "2001-06-01T00:00:00.000000Z";

/// A provider whose log clock reads one instant, again and again.
pub fn clock() -> bettermemory_core::store::create::Fixed {
    bettermemory_core::store::create::Fixed {
        clock: vec![written::instant("2003-04-05T06:07:08.123456Z")],
        ..opened::engine_provider()
    }
}

/// The record's entries, read back.
pub fn record_entries(path: &Path) -> Vec<Value> {
    let text = fs::read_to_string(path).expect("record");
    let doc: Value = serde_json::from_str(&text).expect("JSON");
    doc["restores"].as_array().expect("restores").clone()
}

/// The seqs of the log's rows past `after`, with their kinds.
pub fn rows_after(file: &Path, after: i64) -> Vec<(i64, String)> {
    written::log_rows(file, after)
        .as_array()
        .expect("rows")
        .iter()
        .map(|r| {
            (
                r[0].as_i64().expect("seq"),
                r[3].as_str().expect("kind").to_owned(),
            )
        })
        .collect()
}
