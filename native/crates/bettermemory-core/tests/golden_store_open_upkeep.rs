//! The owner's writing open's upkeep (`open_for_writing_mut`: a foreign
//! tail closed, the origin anchor settled) against
//! `store_open_upkeep.jsonl` and `store_open_upkeep_python.jsonl`: for
//! every case, the same starting store and recipe, the native open with
//! the log clock replaying the reference's readings, and then
//!
//! - `same`: the whole after-state equal (tables, `log`, meta, the key
//!   ring's texts, the binding, modes), the rows the open appended column
//!   by column, and every warning the upkeep logged (its format string,
//!   its arguments, the exception's class and SQLite error name);
//! - `raises`: the reference's class and text, after the same writes;
//!
//! then a second open, which changes and logs what the reference's did
//! (nothing, once a store is settled). Hooks: `busy` (another connection
//! holds the write lock until the tail's step is done), `race` (a second
//! owner opens between the tail's first read and its lock),
//! `readonly_keys` (the keys directory 0500; skipped as root).
//!
//! POSIX only, as the fixtures are; `store_open_upkeep_not_posix.rs`
//! records the gap.

#![cfg(unix)]

mod common;

use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;

use bettermemory_core::store::open::{
    open_for_writing_mut, open_for_writing_mut_with, Boundary, OpenError, UpkeepArg, UpkeepWarning,
    WritableStore,
};
use common::opened::{self, scrub};
use common::parity::Ledger;
use common::stores::scratch;
use common::txs::Replay;
use common::written::instant;
use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// `gen_store_open_upkeep.LONG_TEXT`.
const LONG_TEXT: usize = 4096;

/// `gen_store_open_upkeep._nested`: arrays, or objects for `shape`
/// "objects".
fn nested(deep: usize, shape: Option<&str>) -> String {
    match shape {
        Some("objects") => format!("{}1{}", "{\"a\":".repeat(deep), "}".repeat(deep)),
        _ => format!("{}{}", "[".repeat(deep), "]".repeat(deep)),
    }
}

/// `gen_store_open_upkeep._setup`.
fn setup(recipe: &[Value], c: &Path, store_id: &str, deep: usize) {
    for op in recipe {
        let shape = op.get("shape").and_then(Value::as_str);
        if let Some(seq) = op.get("nested_rekey") {
            let conn = Connection::open(c.join("memory.sqlite")).expect("open");
            conn.execute(
                "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES \
                 (?, '2001-02-03T04:05:06.000000Z', NULL, 'rekey', ?, ?, ?)",
                rusqlite::params![
                    seq.as_i64().expect("seq"),
                    nested(deep, shape),
                    op["prev_mac"].as_str().expect("prev"),
                    op["mac"].as_str().expect("mac"),
                ],
            )
            .expect("nested rekey");
        } else if op.get("nested_origin").is_some() {
            fs::write(
                c.join("keys").join(format!("{store_id}.origin")),
                format!("{{\"seq\": {}}}", nested(deep, shape)),
            )
            .expect("nested origin");
        } else if let Some(head) = op.get("padded_origin").and_then(Value::as_str) {
            let pad = op["pad"].as_u64().expect("pad") as usize;
            fs::write(
                c.join("keys").join(format!("{store_id}.origin")),
                format!("{head}{}\"}}", "x".repeat(pad)),
            )
            .expect("padded origin");
        } else {
            opened::setup(std::slice::from_ref(op), c, store_id);
        }
    }
}

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

fn last_seq(c: &Path) -> i64 {
    let conn = Connection::open(c.join("memory.sqlite")).expect("open");
    conn.query_row("SELECT coalesce(max(seq), 0) FROM log", [], |r| r.get(0))
        .expect("seq")
}

fn rows_after(c: &Path, seq: i64) -> Value {
    let conn = Connection::open(c.join("memory.sqlite")).expect("open");
    let mut statement = conn
        .prepare(
            "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log \
             WHERE seq > ? ORDER BY seq",
        )
        .expect("prepare");
    let rows = statement
        .query_map([seq], |r| {
            Ok(json!({
                "seq": r.get::<_, i64>(0)?,
                "ts": r.get::<_, String>(1)?,
                "session": r.get::<_, Option<String>>(2)?,
                "kind": r.get::<_, String>(3)?,
                "payload": r.get::<_, String>(4)?,
                "prev_mac": r.get::<_, String>(5)?,
                "mac": r.get::<_, String>(6)?,
            }))
        })
        .expect("rows")
        .collect::<Result<Vec<_>, _>>()
        .expect("rows");
    Value::Array(rows)
}

/// A text argument as `_common.text_field("text", ...)` writes it.
fn text_arg(points: &[u32]) -> Value {
    match points
        .iter()
        .map(|&p| char::from_u32(p))
        .collect::<Option<String>>()
    {
        Some(text) => json!({ "text": text }),
        None => json!({ "text_cps": points }),
    }
}

/// The warnings as `gen_store_open_upkeep._warnings` records them.
fn warnings(logged: &[UpkeepWarning], store: &Path, root: &Path) -> Vec<Value> {
    let path = scrub(&store.display().to_string(), root);
    logged
        .iter()
        .map(|w: &UpkeepWarning| {
            let args: Vec<Value> = w
                .args()
                .into_iter()
                .map(|a| match a {
                    UpkeepArg::Path => json!({ "path": path }),
                    UpkeepArg::Text(points) => text_arg(&points),
                    UpkeepArg::Int(n) => json!({ "int": n }),
                    UpkeepArg::Cause(cause) => {
                        json!({ "exc": cause.class, "sqlite": cause.sqlite })
                    }
                })
                .collect();
            json!({ "level": "WARNING", "msg": w.format(), "args": args })
        })
        .collect()
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

fn clock(stamps: &Value) -> Replay {
    let log: Vec<_> = stamps
        .as_array()
        .expect("clock")
        .iter()
        .map(|t| instant(t.as_str().expect("stamp")))
        .collect();
    Replay::new(&log, &[])
}

enum Ran {
    Opened(Vec<Value>),
    Raised(OpenError),
}

fn opened_or(got: Result<WritableStore, OpenError>, root: &Path, mut log: Vec<Value>) -> Ran {
    match got {
        Ok(store) => {
            assert!(
                store.connection().is_autocommit(),
                "the open left its connection in a transaction"
            );
            log.extend(warnings(store.upkeep(), store.path(), root));
            store.close().expect("close");
            Ran::Opened(log)
        }
        Err(e) => Ran::Raised(e),
    }
}

fn run(case: &Value, header: &Value) -> Result<(), String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let deep = header["deep"].as_u64().expect("deep") as usize;
    let dir = scratch("upkeep");
    let root = dir.0.clone();
    let c = root.join("c");
    opened::made(case["base"].as_str().expect("base"), &c);
    setup(
        case["recipe"].as_array().expect("recipe"),
        &c,
        store_id,
        deep,
    );
    let before = state(&c, &root, store_id, pragmas);
    for part in ["master", "tables"] {
        if before[part] != case["before"][part] {
            return Err(format!("{name}: the rebuilt store's {part} differs"));
        }
    }
    let seq = last_seq(&c);
    let file = c.join("memory.sqlite");
    let keys = c.join("keys");
    let hook = case.get("hook").and_then(Value::as_str);
    let mut replay = clock(&case["clock"]);
    let ran = match hook {
        Some("busy") => {
            let holder = Connection::open(&file).expect("holder");
            holder.execute_batch("BEGIN IMMEDIATE").expect("lock");
            let got = open_for_writing_mut_with(&file, &keys, &mut replay, &mut |b| {
                if b == Boundary::TailClosed && !holder.is_autocommit() {
                    holder.execute_batch("ROLLBACK").expect("release");
                }
            });
            opened_or(got, &root, Vec::new())
        }
        Some("race") => {
            let mut inner_log = Vec::new();
            let mut outer = Replay::new(&[], &[]);
            let got = open_for_writing_mut_with(&file, &keys, &mut outer, &mut |b| {
                if b == Boundary::TailNamed && inner_log.is_empty() {
                    let inner = open_for_writing_mut(&file, &keys, &mut replay).expect("inner");
                    inner_log = warnings(inner.upkeep(), inner.path(), &root);
                    inner_log.push(Value::Null);
                    inner.close().expect("close");
                }
            });
            inner_log.retain(|v| !v.is_null());
            if !replay.drained() || !outer.drained() {
                return Err(format!(
                    "{name}: the clocks were not read as the reference's"
                ));
            }
            opened_or(got, &root, inner_log)
        }
        Some("readonly_keys") => {
            fs::set_permissions(&keys, fs::Permissions::from_mode(0o500)).expect("0500");
            let got = open_for_writing_mut(&file, &keys, &mut replay);
            fs::set_permissions(&keys, fs::Permissions::from_mode(0o700)).expect("0700");
            opened_or(got, &root, Vec::new())
        }
        Some(other) => panic!("{name}: no hook {other}"),
        None => opened_or(
            open_for_writing_mut(&file, &keys, &mut replay),
            &root,
            Vec::new(),
        ),
    };
    if !replay.drained() {
        return Err(format!(
            "{name}: the log clock was read less often than the reference's"
        ));
    }
    let after = state(&c, &root, store_id, pragmas);
    let parts = opened::differing(&case["after"], &after);
    if !parts.is_empty() {
        return Err(format!(
            "{name}: differs in {parts:?}\nwant {}\ngot  {after}",
            case["after"]
        ));
    }
    let appended = rows_after(&c, seq);
    if appended != case["appended"] {
        return Err(format!(
            "{name}: appended {appended}, the reference {}",
            case["appended"]
        ));
    }
    match (case["native"].as_str().expect("native"), ran) {
        ("same", Ran::Opened(log)) => {
            if Value::Array(log.clone()) != case["warnings"] {
                return Err(format!(
                    "{name}: logged {log:?}, the reference {}",
                    case["warnings"]
                ));
            }
        }
        ("raises", Ran::Raised(e)) => {
            let want = &case["outcome"]["refusal"];
            let text = scrub(&e.to_string(), &root);
            if e.class() != want["class"] || text != want["text"] {
                return Err(format!(
                    "{name}: raised {} {text:?}, the reference {want}",
                    e.class()
                ));
            }
            // What the upkeep logged before the raise (a closed tail).
            let log = Value::Array(warnings(e.upkeep(), &file, &root));
            if log != case["warnings"] {
                return Err(format!(
                    "{name}: logged {log} before raising, the reference {}",
                    case["warnings"]
                ));
            }
        }
        (n, Ran::Opened(_)) => return Err(format!("{name}: expected {n}, opened")),
        (n, Ran::Raised(e)) => return Err(format!("{name}: expected {n}, got {} {e}", e.class())),
    }
    // The second open: nothing more written on a settled store.
    let second = &case["second"];
    let mut again_clock = clock(&second["clock"]);
    let ran = opened_or(
        open_for_writing_mut(&file, &keys, &mut again_clock),
        &root,
        Vec::new(),
    );
    let again = state(&c, &root, store_id, pragmas);
    let parts = changed(&after, &again);
    if json!(parts) != second["changed"] {
        return Err(format!(
            "{name}: the second open changed {parts:?}, the reference {}",
            second["changed"]
        ));
    }
    match ran {
        Ran::Opened(log) => {
            if second["outcome"]["opened"] != json!(true)
                || Value::Array(log.clone()) != second["warnings"]
            {
                return Err(format!(
                    "{name}: the second open logged {log:?}, the reference {second}"
                ));
            }
        }
        Ran::Raised(e) => {
            if second["outcome"]["class"] != e.class() {
                return Err(format!("{name}: the second open raised {} {e}", e.class()));
            }
        }
    }
    if !again_clock.drained() {
        return Err(format!("{name}: the second open read the clock less often"));
    }
    Ok(())
}

/// Whether this process runs as root, where a mode does not refuse (the
/// generator leaves the `readonly_keys` cases out there too).
fn as_root() -> bool {
    let dir = scratch("whoami");
    fs::metadata(&dir.0).is_ok_and(|m| m.uid() == 0)
}

fn every_case(file: &str, test: &str) {
    let mut lines = common::jsonl(file);
    let header = lines.remove(0);
    assert_eq!(header["engine_version"], opened::ENGINE);
    let mut failures = Vec::new();
    let mut total = 0;
    let root = as_root();
    for case in &lines {
        if root && case.get("hook").and_then(Value::as_str) == Some("readonly_keys") {
            continue;
        }
        total += 1;
        if let Err(e) = run(case, &header) {
            failures.push(e);
        }
    }
    assert!(total > 0, "{file}: no case checked");
    Ledger::new(test, file).settle(total, &failures);
}

#[test]
fn every_case_upkeeps_as_the_reference() {
    every_case(
        "store_open_upkeep.jsonl",
        "every_case_upkeeps_as_the_reference",
    );
}

#[test]
fn every_nested_case_raises_as_the_reference() {
    every_case(
        "store_open_upkeep_python.jsonl",
        "every_nested_case_raises_as_the_reference",
    );
}
