//! What the write tests share: `store_write.jsonl`'s sequences replayed
//! as `gen_store_write.run_sequence` runs them, and a store read as it
//! reads one after each step (the rows a step appended, the state's
//! digest, the FTS5 index as queries see it).

use std::fs;
use std::path::Path;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json;
use bettermemory_core::log::Key;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::create::Fixed;
use bettermemory_core::store::open::WritableStore;
use bettermemory_core::unicode;
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::{hex, jsonl, opened};

/// The header and the sequences of `store_write.jsonl`.
pub fn sequences() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_write.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

/// `gen_store_write.LONG_PAYLOAD`.
pub const LONG_PAYLOAD: usize = 2048;

pub fn instant(text: &str) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(&text.replace('Z', "+00:00"))).expect("a time")
}

/// A provider whose clock reads `stamps` in order (the last repeats).
pub fn clock(stamps: &[DateTime]) -> Fixed {
    let mut p = opened::engine_provider();
    p.key = Key::new(vec![0; 32]);
    p.clock = stamps.to_vec();
    p
}

/// A payload as the fixture spells it (JSON, ASCII), as the reference's
/// `json.loads` reads it, lone surrogates included.
pub fn payload(text: &str) -> PyValue {
    decode(json::loads(&unicode::cps(text)).expect("a payload"))
}

/// `gen_store_write.decode`: `{"$tuple": [...]}`, `{"$datetime": iso}` and
/// `{"$path": text}` made the values they stand for, and `{"$dict":
/// [[k, v], ...]}` a dict in that order (its keys distinct, as the
/// fixtures write them; JSON's sorted object keys would lose the order),
/// `{"$int": digits}` and `{"$float": repr}` numbers exactly (serde_json
/// reads an int past 64 bits as a float and may round a float's text).
pub fn decode(value: PyValue) -> PyValue {
    match &value {
        PyValue::Dict(items) => {
            if let [(PyValue::Str(tag), inner)] = items.as_slice() {
                match (unicode::to_string(tag).as_deref(), inner) {
                    (Some("$tuple"), PyValue::List(v)) => {
                        return PyValue::Tuple(v.iter().cloned().map(decode).collect())
                    }
                    (Some("$datetime"), PyValue::Str(t)) => {
                        return PyValue::DateTime(DateTime::from_isoformat(t).expect("a time"))
                    }
                    (Some("$path"), PyValue::Str(t)) => return PyValue::Path(t.clone()),
                    (Some("$int"), PyValue::Str(t)) => {
                        let digits = unicode::to_string(t).expect("digits");
                        return PyValue::int_from_decimal(&digits).expect("an int");
                    }
                    (Some("$float"), PyValue::Str(t)) => {
                        let text = unicode::to_string(t).expect("a float");
                        return PyValue::Float(text.parse().expect("a float"));
                    }
                    (Some("$dict"), PyValue::List(pairs)) => {
                        return PyValue::Dict(
                            pairs
                                .iter()
                                .map(|p| match p {
                                    PyValue::List(kv) if kv.len() == 2 => {
                                        (decode(kv[0].clone()), decode(kv[1].clone()))
                                    }
                                    other => panic!("a $dict pair {other:?}"),
                                })
                                .collect(),
                        )
                    }
                    _ => {}
                }
            }
            PyValue::Dict(
                items
                    .iter()
                    .map(|(k, v)| (k.clone(), decode(v.clone())))
                    .collect(),
            )
        }
        PyValue::List(v) => PyValue::List(v.iter().cloned().map(decode).collect()),
        _ => value.clone(),
    }
}

/// The native fold check's replay of `file`: its status and problems as
/// `[seq, kind, detail]`, sorted (`gen_store_write`'s `refold`).
pub fn refold(file: &Path) -> Value {
    let store = bettermemory_core::store::StoreFile::open(file).expect("open");
    let report = store.refold().expect("refold");
    let mut problems: Vec<Value> = report
        .problems
        .iter()
        .map(|p| json!([p.seq, p.kind, p.detail]))
        .collect();
    problems.sort_by_key(|p| p.to_string());
    json!({"status": report.status, "problems": problems})
}

fn sha(text: &[u8]) -> String {
    hex(&Sha256::digest(text))
}

fn ro(file: &Path) -> Connection {
    Connection::open_with_flags(file, OpenFlags::SQLITE_OPEN_READ_ONLY).expect("read-only open")
}

/// `gen_store_write.log_rows`.
pub fn log_rows(file: &Path, after: i64) -> Value {
    let conn = ro(file);
    let mut st = conn
        .prepare(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log \
             WHERE seq > ? ORDER BY seq",
        )
        .expect("prepare");
    let rows = st
        .query_map([after], |r| {
            let payload: String = r.get(4)?;
            let payload = if payload.len() > LONG_PAYLOAD {
                json!({"bytes": payload.len(), "sha256": sha(payload.as_bytes())})
            } else {
                json!(payload)
            };
            Ok(json!([
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, String>(3)?,
                payload,
                r.get::<_, String>(5)?,
                r.get::<_, String>(6)?
            ]))
        })
        .expect("query")
        .collect::<rusqlite::Result<Vec<Value>>>()
        .expect("rows");
    Value::Array(rows)
}

pub fn last_seq(file: &Path) -> i64 {
    ro(file)
        .query_row("SELECT max(seq) FROM log", [], |r| r.get(0))
        .expect("max seq")
}

/// Python's `json.dumps(v, sort_keys=True, ensure_ascii=True,
/// separators=(",", ":"))` of a JSON value without floats.
pub fn py_dumps(v: &Value) -> String {
    let mut out = String::new();
    dump(v, &mut out);
    out
}

fn dump_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn dump(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            assert!(!n.is_f64(), "a float in a state");
            out.push_str(&n.to_string());
        }
        Value::String(s) => dump_str(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                dump(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                dump_str(k, out);
                out.push(':');
                dump(&map[k], out);
            }
            out.push('}');
        }
    }
}

/// `gen_store_write.state_digest`.
pub fn state_digest(full: &Value) -> Value {
    let head = full["keys"]
        .as_array()
        .and_then(|keys| keys.iter().find(|k| k["name"] == "<id>.head"))
        .and_then(|k| k.get("text").cloned())
        .unwrap_or(Value::Null);
    let opt = |k: &str| full.get(k).cloned().unwrap_or(Value::Null);
    json!({
        "sha256": sha(py_dumps(full).as_bytes()),
        "tables": opt("tables"),
        "log": opt("log"),
        "head": head,
    })
}

/// `gen_store_write.fts_view`.
pub fn fts_view(file: &Path) -> Value {
    let conn = ro(file);
    let mut words: Vec<String> = Vec::new();
    {
        let mut st = conn
            .prepare("SELECT body_fts, scopes_fts FROM memories")
            .expect("prepare");
        let mut rows = st.query([]).expect("query");
        while let Some(r) = rows.next().expect("row") {
            let b: String = r.get(0).expect("body_fts");
            let s: String = r.get(1).expect("scopes_fts");
            for w in format!("{b} {s}").split(' ').filter(|w| !w.is_empty()) {
                words.push(w.to_owned());
            }
        }
    }
    // Python sorts str by code point; UTF-8 byte order is the same.
    words.sort();
    words.dedup();
    let mut out = Vec::new();
    for w in &words {
        let query = format!("\"{}\"", w.replace('"', "\"\""));
        let hits = conn
            .prepare("SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rowid")
            .and_then(|mut st| {
                st.query_map([&query], |r| r.get::<_, i64>(0))?
                    .collect::<rusqlite::Result<Vec<i64>>>()
            });
        let hits = match hits {
            Ok(h) => json!(h),
            Err(_) => json!("OperationalError"),
        };
        out.push(json!([w, hits]));
    }
    json!({"words": out.len(), "sha256": sha(py_dumps(&Value::Array(out)).as_bytes())})
}

/// `gen_store_write._recipe`: the key ring edited before the open.
pub fn recipe(items: &[Value], keys: &Path, file: &Path, store_id: &str) {
    for item in items {
        let head = keys.join(format!("{store_id}.head"));
        if let Some(suffix) = item.get("unlink") {
            fs::remove_file(keys.join(format!("{store_id}{}", suffix.as_str().expect("s"))))
                .expect("unlink");
        } else if let Some(seq) = item.get("head") {
            let mac: String = ro(file)
                .query_row("SELECT mac FROM log ORDER BY seq DESC", [], |r| r.get(0))
                .expect("mac");
            let text =
                format!("{{\"seq\": {seq}, \"mac\": \"{mac}\", \"ts\": \"2001-01-01T00:00:00Z\"}}");
            fs::write(head, text).expect("head");
        } else if let Some(text) = item.get("head_text") {
            fs::write(head, text.as_str().expect("text")).expect("head");
        } else {
            panic!("an unknown recipe item {item}");
        }
    }
}

/// `gen_store_write._edit`: an edit between two steps.
pub fn edit(edit: &Value, store: &WritableStore, case: &Path, store_id: &str) {
    let keys = case.join("keys");
    let file = |e: &Value| keys.join(format!("{store_id}{}", e["file"].as_str().expect("file")));
    let conn = store.connection();
    match edit["edit"].as_str().expect("edit") {
        "journal" => {
            let mode = edit["mode"].as_str().expect("mode");
            conn.query_row(&format!("PRAGMA journal_mode = {mode}"), [], |_| Ok(()))
                .expect("journal mode");
        }
        "write" => {
            fs::write(file(edit), super::unhex(edit["hex"].as_str().expect("hex"))).expect("write")
        }
        "unlink" => fs::remove_file(file(edit)).expect("unlink"),
        "bind_elsewhere" => {
            fs::create_dir(case.join("other")).expect("other");
            let target = fs::canonicalize(case.join("other"))
                .expect("resolve")
                .join("memory.sqlite");
            let text = format!(
                "{{\"path\": {}}}",
                serde_json::to_string(target.to_str().expect("UTF-8")).expect("json")
            );
            fs::write(keys.join(format!("{store_id}.path")), text).expect("bind");
        }
        "meta_key" => {
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
                [match &edit["fingerprint"] {
                    Value::String(t) => rusqlite::types::Value::Text(t.clone()),
                    v => rusqlite::types::Value::Integer(v.as_i64().expect("an integer")),
                }],
            )
            .expect("meta");
        }
        "unknown_field" => {
            conn.execute(
                "UPDATE log SET payload = json_set(payload, '$.memory.later_field', 1) \
                 WHERE mac = (SELECT log_mac FROM memories WHERE id = ?)",
                [edit["id"].as_str().expect("id")],
            )
            .expect("unknown field");
        }
        other => panic!("an unknown edit {other}"),
    }
}

/// `gen_store_write.ulid`: a valid ULID numbered `n`.
pub fn ulid(n: u64, prefix: &str) -> String {
    const DIGITS: &[u8] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    let mut tail = Vec::new();
    let mut n = n;
    for _ in 0..(26 - prefix.len()) {
        tail.push(DIGITS[(n % 32) as usize]);
        n /= 32;
    }
    tail.reverse();
    format!("{prefix}{}", String::from_utf8(tail).expect("ASCII"))
}

/// A `memory_put` payload as `put_memory` builds it for a plain record:
/// the record's required fields (the model fills the rest), `local`.
pub fn memory_payload(id: &str, body: &str, scopes: &[&str], supersedes: &[&str]) -> PyValue {
    let links: Vec<Value> = supersedes
        .iter()
        .map(|t| json!({"type": "supersedes", "target_id": t, "note": null}))
        .collect();
    payload(&py_dumps(&json!({
        "memory": {
            "id": id,
            "created": "2001-02-03T04:05:06.000000Z",
            "updated": "2001-02-03T04:05:06.000000Z",
            "scopes": scopes,
            "confidence": "high",
            "source": "explicit-statement",
            "body": body,
            "links": links,
        },
        "provenance": "local",
        "filename": null,
    })))
}

/// A `tombstone_put` payload for a removed plain record.
pub fn tombstone_payload(id: &str, body: &str) -> PyValue {
    payload(&py_dumps(&json!({
        "tombstone": {
            "id": id,
            "created": "2001-02-03T04:05:06.000000Z",
            "updated": "2001-02-03T04:05:06.000000Z",
            "scopes": ["global"],
            "confidence": "high",
            "source": "explicit-statement",
            "body": body,
            "removed": "2001-02-04T00:00:00.000000Z",
            "removed_reason": "outdated",
            "removed_session": "s-remove",
        },
        "provenance": "local",
        "filename": null,
        "links": [],
        "corroborations": 0,
        "last_corroborated": null,
    })))
}

/// A key loader for `log_verify_loading`: the ring in `keys`.
pub fn loader(
    keys: std::path::PathBuf,
    id: String,
) -> impl FnMut(
    &bettermemory_core::store::StoreFile,
) -> Result<
    (bettermemory_core::log::KeyRing, String),
    bettermemory_core::store::StoreError,
> {
    use bettermemory_core::store::StoreError;
    move |s| {
        let fp = s
            .meta()?
            .into_iter()
            .find(|(k, _)| k == "key_fingerprint")
            .map(|(_, v)| v)
            .unwrap_or_default();
        let ring = bettermemory_core::store::keys::load_key_ring(&keys, &id)
            .map_err(|e| StoreError::Refused(format!("{e:?}")))?;
        Ok((ring, fp))
    }
}

/// The native check's status and the number of active records.
pub fn native_verify(c: &Path, id: &str) -> (&'static str, usize) {
    let file = bettermemory_core::store::StoreFile::open(&c.join("memory.sqlite"))
        .expect("the store opens natively");
    let report = file
        .log_verify_loading(&mut loader(c.join("keys"), id.to_owned()))
        .expect("native verify");
    let mut n = 0;
    file.each_memory(&mut |_| n += 1).expect("memories");
    (report.status, n)
}

/// The record tables as one sha256 (memories with their index text,
/// links, tombstones), and the log's row count.
pub fn tables(file: &Path) -> (String, i64) {
    let conn = ro(file);
    let mut text = String::new();
    for sql in [
        "SELECT id, body, body_fts, scopes_text, scopes_fts, log_mac FROM memories ORDER BY id",
        "SELECT source_id, type, target_id, coalesce(note, '') FROM memory_links ORDER BY 1, 2, 3, 4",
        "SELECT id, body, removed_reason, log_mac FROM tombstones ORDER BY id",
    ] {
        let mut st = conn.prepare(sql).expect("prepare");
        let n = st.column_count();
        let mut rows = st.query([]).expect("query");
        while let Some(r) = rows.next().expect("row") {
            for i in 0..n {
                text.push_str(&r.get::<_, String>(i).expect("text"));
                text.push('\u{1f}');
            }
            text.push('\u{1e}');
        }
        text.push('\u{1d}');
    }
    let count = conn
        .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
        .expect("count");
    (sha(text.as_bytes()), count)
}

/// The FTS5 index agrees with its content table (`integrity-check`), on
/// a connection that may write (the command writes nothing).
pub fn fts_intact(file: &Path) -> Result<(), String> {
    let conn = Connection::open(file).map_err(|e| e.to_string())?;
    conn.busy_timeout(std::time::Duration::from_secs(10))
        .map_err(|e| e.to_string())?;
    conn.execute(
        "INSERT INTO memories_fts(memories_fts, rank) VALUES ('integrity-check', 1)",
        [],
    )
    .map(|_| ())
    .map_err(|e| e.to_string())
}

/// The head's seq and mac, and whether the log holds that row.
pub fn head_in_log(keys: &Path, file: &Path, id: &str) -> Option<(i64, bool)> {
    let text = fs::read_to_string(keys.join(format!("{id}.head"))).ok()?;
    let v: Value = serde_json::from_str(&text).ok()?;
    let seq = v["seq"].as_i64()?;
    let mac = v["mac"].as_str()?.to_owned();
    let held: Option<String> = ro(file)
        .query_row("SELECT mac FROM log WHERE seq = ?", [seq], |r| r.get(0))
        .ok();
    Some((seq, held.as_deref() == Some(mac.as_str())))
}

/// A copy of a case directory (files and keys) at `to`, its binding
/// pointed at the copy.
pub fn keep_copy(c: &Path, to: &Path, id: &str) {
    fs::create_dir_all(to.join("keys")).expect("copy dir");
    for entry in fs::read_dir(c).expect("case").flatten() {
        if entry.file_type().expect("type").is_file() {
            fs::copy(entry.path(), to.join(entry.file_name())).expect("copy");
        }
    }
    for entry in fs::read_dir(c.join("keys")).expect("keys").flatten() {
        if entry.file_type().expect("type").is_file() {
            fs::copy(entry.path(), to.join("keys").join(entry.file_name())).expect("copy key");
        }
    }
    let binding = to.join("keys").join(format!("{id}.path"));
    if let Ok(text) = fs::read_to_string(&binding) {
        let from = fs::canonicalize(c).expect("c").display().to_string();
        let into = fs::canonicalize(to).expect("to").display().to_string();
        fs::write(&binding, text.replace(&from, &into)).expect("rebind");
    }
}
