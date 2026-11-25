//! `WritableStore::reopen` against `store_reopen.jsonl` and its
//! Python-gated sibling `store_reopen_python.jsonl` (the reopens a
//! RecursionError ends): for every case,
//! the same base bound to its file and opened by its owner, the case's
//! edits, then each reopen in turn (all of them open until the last, under
//! the case's hook), and for each: whether it opened or the reference's
//! class and text, whether it holds the key it writes with
//! (`has_key`), whether it checks pointers (`can_verify`), the
//! fingerprints of its key and of a reader's read key, the key state's
//! name, its busy timeout and SQLite's, `query_only`, whether it is in a
//! transaction, and every warning (format string and arguments, the
//! scratch directory as `<scratch>`). Then the parts of the store and the
//! keys directory the reopens changed (none). Hook: `busy` (another
//! connection holds the write lock throughout).
//!
//! The warnings a process says once (a later writer's, a drift's) are
//! said once here too: the told sets are the process's, and each case's
//! store has its own path.
//!
//! POSIX only, as the fixture is; `store_reopen_not_posix.rs` records the
//! gap.

#![cfg(unix)]

mod common;

use std::path::Path;

use bettermemory_core::log::fingerprint;
use bettermemory_core::store::key_read::{KeyLog, LogLevel};
use bettermemory_core::store::open::{open_for_writing, Reopened};
use bettermemory_core::unicode;
use common::opened::{self, scrub};
use common::parity::Ledger;
use common::stores::scratch;
use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// `gen_store_open_upkeep.LONG_TEXT`.
const LONG_TEXT: usize = 4096;

/// `gen_store_open_upkeep.state`.
fn state(c: &Path, root: &Path, store_id: &str, pragmas: &[Value]) -> Value {
    let mut out = opened::state_not_utf8(c, root, store_id, pragmas);
    if let Some(keys) = out.get_mut("keys").and_then(Value::as_array_mut) {
        for item in keys {
            let Some(text) = item.get("text").and_then(Value::as_str) else {
                continue;
            };
            if text.chars().count() > LONG_TEXT {
                let digest = common::hex(&Sha256::digest(text.as_bytes()));
                let item = item.as_object_mut().expect("item");
                item.remove("text");
                item.insert("text_sha256".into(), json!(digest));
            }
        }
    }
    out
}

/// `gen_store_open_upkeep._nested`: `deep` arrays, or objects.
fn nested(deep: usize, shape: Option<&str>) -> String {
    match shape {
        Some("objects") => format!("{}1{}", "{\"a\":".repeat(deep), "}".repeat(deep)),
        _ => format!("{}{}", "[".repeat(deep), "]".repeat(deep)),
    }
}

/// `gen_store_open_restore._setup_nested`: a rekey or `index_rebuilt` row,
/// or an anchor, nested `deep`; any other op as `opened::setup` runs it.
fn setup(edits: &[Value], c: &Path, store_id: &str, deep: usize) {
    for op in edits {
        let shape = op.get("shape").and_then(Value::as_str);
        let row = [("nested_rekey", "rekey"), ("nested_index", "index_rebuilt")]
            .into_iter()
            .find_map(|(op_name, kind)| op.get(op_name).map(|seq| (seq, kind)));
        if let Some((seq, kind)) = row {
            let conn = Connection::open(c.join("memory.sqlite")).expect("open");
            conn.execute(
                "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES \
                 (?, '2001-02-03T04:05:06.000000Z', NULL, ?, ?, ?, ?)",
                rusqlite::params![
                    seq.as_i64().expect("seq"),
                    kind,
                    nested(deep, shape),
                    op["prev_mac"].as_str().expect("prev"),
                    op["mac"].as_str().expect("mac"),
                ],
            )
            .expect("nested row");
        } else if op.get("nested_origin").is_some() {
            std::fs::write(
                c.join("keys").join(format!("{store_id}.origin")),
                format!("{{\"seq\": {}}}", nested(deep, shape)),
            )
            .expect("nested origin");
        } else {
            opened::setup(std::slice::from_ref(op), c, store_id);
        }
    }
}

/// `gen_store_open._changed`.
fn changed(before: &Value, after: &Value) -> Vec<String> {
    let mut parts: Vec<String> = Vec::new();
    for key in ["dir", "mode", "master", "pragmas"] {
        if before.get(key) != after.get(key) {
            parts.push(key.to_owned());
        }
    }
    let items = |v: &Value| -> Vec<Value> {
        v.get("keys")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
    };
    let (was, now) = (items(before), items(after));
    let name = |i: &Value| i["name"].as_str().unwrap_or_default().to_owned();
    for item in &now {
        if !was.iter().any(|w| w == item) {
            parts.push(format!("keys/{}", name(item)));
        }
    }
    for item in &was {
        if !now.iter().any(|n| name(n) == name(item)) {
            parts.push(format!("keys/{} (removed)", name(item)));
        }
    }
    for key in [
        "tables",
        "meta",
        "digest_state",
        "log",
        "digest_records",
        "digest_tokens",
    ] {
        if before.get(key) != after.get(key) {
            parts.push(key.to_owned());
        }
    }
    parts.sort();
    parts
}

/// A text as `_common.text_field("text", ...)` writes it, scrubbed.
fn text_arg(points: &[u32], root: &Path) -> Value {
    match unicode::to_string(points) {
        Some(text) => json!({ "text": scrub(&text, root) }),
        None => json!({ "text_cps": points }),
    }
}

/// The warnings as `gen_store_reopen._warnings` records them: WARNING and
/// above.
fn warnings(logged: &[KeyLog], root: &Path) -> Value {
    Value::Array(
        logged
            .iter()
            .filter(|l| l.level == LogLevel::Warning)
            .map(|l| {
                let args: Vec<Value> = l.args.iter().map(|a| text_arg(a, root)).collect();
                json!({ "level": "WARNING", "msg": l.format, "args": args })
            })
            .collect(),
    )
}

/// `gen_store_reopen._held`.
fn held(other: &Reopened) -> Value {
    let conn = other.connection();
    let pragma = |name: &str| -> i64 {
        conn.query_row(&format!("PRAGMA {name}"), [], |r| r.get(0))
            .expect("pragma")
    };
    let (key, read_key, key_state) = match other {
        Reopened::Writer(s) => (Some(s.key_fingerprint().to_owned()), None, "present"),
        Reopened::Reader(r) => (
            None,
            r.keys().read_key().map(|k| fingerprint(k.bytes())),
            r.key_state().name(),
        ),
    };
    json!({
        "opened": true,
        "has_key": other.has_key(),
        "can_verify": other.can_verify(),
        "key": key,
        "read_key": read_key,
        "key_state": key_state,
        "busy_timeout": other.busy_timeout(),
        "sqlite_busy_timeout": pragma("busy_timeout"),
        "query_only": pragma("query_only"),
        "in_transaction": !conn.is_autocommit(),
    })
}

/// Whether two records agree, a number by its value (the reference's `0`
/// and this build's `0.0` are one busy timeout).
fn same(want: &Value, got: &Value) -> bool {
    match (want, got) {
        (Value::Number(a), Value::Number(b)) => a.as_f64() == b.as_f64(),
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len() && a.iter().all(|(k, v)| b.get(k).is_some_and(|w| same(v, w)))
        }
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(x, y)| same(x, y))
        }
        _ => want == got,
    }
}

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let dir = scratch("reopen");
    let root = dir.0.clone();
    let c = root.join("c");
    opened::made(case["base"].as_str().expect("base"), &c);
    opened::setup(case["recipe"].as_array().expect("recipe"), &c, store_id);
    let file = c.join("memory.sqlite");
    let keys = c.join("keys");
    let provider = opened::engine_provider();
    let owner = open_for_writing(&file, &keys, &provider).map_err(|e| format!("{name}: {e}"))?;
    let deep = header["deep"].as_u64().expect("deep");
    let deep = usize::try_from(deep).expect("deep");
    setup(case["edits"].as_array().expect("edits"), &c, store_id, deep);
    let before = state(&c, &root, store_id, pragmas);
    for part in ["master", "tables"] {
        if before[part] != case["before"][part] {
            return Err(format!("{name}: the store's {part} differs"));
        }
    }
    let holder = (case.get("hook").and_then(Value::as_str) == Some("busy")).then(|| {
        let holder = Connection::open(&file).expect("holder");
        holder.execute_batch("BEGIN IMMEDIATE").expect("lock");
        holder
    });
    let mut open = Vec::new();
    let mut failures = Vec::new();
    let wanted = case["reopened"].as_array().expect("reopened");
    for (i, (wait, want)) in case["reopens"]
        .as_array()
        .expect("reopens")
        .iter()
        .zip(wanted)
        .enumerate()
    {
        let mut logged = Vec::new();
        let mut got = match owner.reopen(&provider, wait.as_f64(), &mut logged) {
            Ok(other) => {
                let got = held(&other);
                open.push(other);
                got
            }
            Err(e) => {
                let text = unicode::cps(&e.to_string());
                let mut refusal = text_arg(&text, &root);
                refusal["class"] = json!(e.class());
                json!({ "opened": false, "refusal": refusal })
            }
        };
        got["warnings"] = warnings(&logged, &root);
        if !same(want, &got) {
            failures.push(format!("{name} reopen {i}: got {got}\nwant {want}"));
        }
    }
    drop(holder);
    let after = state(&c, &root, store_id, pragmas);
    for other in open {
        other.close().expect("close");
    }
    owner.close().expect("close");
    let parts = changed(&before, &after);
    if json!(parts) != case["changed"] {
        failures.push(format!(
            "{name}: the reopens changed {parts:?}, the reference {}",
            case["changed"]
        ));
    }
    if !failures.is_empty() {
        return Err(failures.join("\n"));
    }
    Ok(wanted.len())
}

#[test]
fn every_reopen_opens_as_the_reference() {
    every_case("store_reopen.jsonl", "every_reopen_opens_as_the_reference");
}

/// The reopens a RecursionError ends: an anchor, a rekey payload or an
/// `index_rebuilt` payload nested past where `json.loads` stops.
#[test]
fn every_nested_reopen_raises_as_the_reference() {
    every_case(
        "store_reopen_python.jsonl",
        "every_nested_reopen_raises_as_the_reference",
    );
}

fn every_case(file: &str, test: &str) {
    let mut lines = common::jsonl(file);
    let header = lines.remove(0);
    assert_eq!(header["engine_version"], opened::ENGINE);
    let mut failures = Vec::new();
    let mut total = 0;
    let mut reopens = 0;
    for case in &lines {
        total += 1;
        match run(case, &header) {
            Ok(n) => reopens += n,
            Err(e) => failures.push(e),
        }
    }
    assert!(total > 0, "{file}: no case checked");
    common::assert_count(file, total, lines.len());
    eprintln!("{file}: {total} cases, {reopens} reopens");
    Ledger::new(test, file).settle(total, &failures);
}
