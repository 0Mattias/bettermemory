//! The owner's writing open settling a restore's late rows
//! (`open_for_writing_mut`: `restore_rows.settle` after the upkeep with the
//! key present, after `_reclaim` and `_settle_origin`, after `_open_rekeyed`
//! and `_take_over_origin`) against `store_open_restore.jsonl` and its
//! Python-gated sibling `store_open_restore_python.jsonl`: for every
//! case, the same starting store and recipe (a restore record among it),
//! the native open with the log clock replaying the reference's readings
//! and the key draw replaying its keys, and then
//!
//! - `same`: the whole after-state equal (the restore record's text among
//!   the key ring's files), the rows the open appended column by column,
//!   every warning in the reference's order (the upkeep's, then
//!   `restore_rows`'), and how many keys were drawn; the head checkpoint's
//!   warning is compared apart, in its own order;
//! - a refusal: the reference's class and text, after the same writes,
//!   with what was logged before;
//!
//! then a second open, which changes, appends, logs and draws what the
//! reference's did. Hook: `readonly_keys` (the keys directory 0500;
//! skipped as root).
//!
//! POSIX only, as the fixture is; `store_open_restore_not_posix.rs`
//! records the gap.

#![cfg(unix)]

mod common;
mod restore_rows;

use std::cell::RefCell;
use std::collections::VecDeque;
use std::fs;
use std::io;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::rc::Rc;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::exception::StoreException;
use bettermemory_core::store::open::{
    open_for_writing_mut, OpenError, UpkeepArg, UpkeepWarning, WritableStore,
};
use common::opened::{self, scrub};
use common::parity::Ledger;
use common::stores::scratch;
use common::written::instant;
use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// `gen_store_open_upkeep.LONG_TEXT`.
const LONG_TEXT: usize = 4096;

/// `gen_store_open_rekey.drawn_key`.
fn drawn_key(n: usize) -> Key {
    Key::new(vec![0xa0 + u8::try_from(n).expect("a few draws"); 32])
}

/// The head checkpoint's warning, as `_move_head` logs it.
const CHECKPOINT: &str = "head checkpoint for %s not written: %s";

/// The reference's fixed clock and key draw, shared by the opens of one
/// case (a second owner's open inside the first reads the same clock and
/// draws the next key), and the head checkpoint's warnings they delivered.
struct Draws {
    log: VecDeque<DateTime>,
    drawn: usize,
    root: PathBuf,
    checkpoints: Vec<Value>,
}

#[derive(Clone)]
struct Shared(Rc<RefCell<Draws>>);

impl Shared {
    fn new(stamps: &Value, drawn: usize, root: &Path) -> Shared {
        let log = stamps
            .as_array()
            .expect("clock")
            .iter()
            .map(|t| instant(t.as_str().expect("stamp")))
            .collect();
        Shared(Rc::new(RefCell::new(Draws {
            log,
            drawn,
            root: root.to_path_buf(),
            checkpoints: Vec::new(),
        })))
    }
    fn checkpoints(&self) -> Vec<Value> {
        self.0.borrow().checkpoints.clone()
    }
    fn drained(&self) -> bool {
        self.0.borrow().log.is_empty()
    }
    fn drawn(&self) -> usize {
        self.0.borrow().drawn
    }
}

impl Provider for Shared {
    fn store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("an open draws no store id"))
    }
    fn key(&mut self) -> io::Result<Key> {
        let mut draws = self.0.borrow_mut();
        let key = drawn_key(draws.drawn);
        draws.drawn += 1;
        Ok(key)
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("an open draws no membership id"))
    }
    fn now(&mut self) -> io::Result<DateTime> {
        self.0
            .borrow_mut()
            .log
            .pop_front()
            .ok_or_else(|| io::Error::other("the log clock was read more often than fixed"))
    }
    fn wall_now(&mut self) -> io::Result<DateTime> {
        Err(io::Error::other("an open reads no wall clock"))
    }
    fn engine_version(&self) -> String {
        opened::ENGINE.to_owned()
    }
    fn checkpoint_warning(
        &mut self,
        store: &Path,
        error: &StoreException,
    ) -> Result<(), StoreException> {
        let mut draws = self.0.borrow_mut();
        let path = scrub(&store.display().to_string(), &draws.root);
        let sqlite = error.sqlite_code.map(|code| format!("code {code}"));
        draws.checkpoints.push(json!({
            "level": "WARNING",
            "msg": CHECKPOINT,
            "args": [{ "path": path }, { "exc": error.exception.class, "sqlite": sqlite }],
        }));
        Ok(())
    }
}

/// The reference's warnings: the upkeep's, then the head checkpoint's.
fn split(warnings: &Value) -> (Value, Value) {
    let (head, upkeep): (Vec<Value>, Vec<Value>) = warnings
        .as_array()
        .expect("warnings")
        .iter()
        .cloned()
        .partition(|w| w["msg"] == CHECKPOINT);
    (Value::Array(upkeep), Value::Array(head))
}

/// `gen_store_open_upkeep._nested`.
fn nested(deep: usize, shape: Option<&str>) -> String {
    match shape {
        Some("objects") => format!("{}1{}", "{\"a\":".repeat(deep), "}".repeat(deep)),
        _ => format!("{}{}", "[".repeat(deep), "]".repeat(deep)),
    }
}

/// `gen_store_open_restore._setup_nested`, as far as these recipes go.
fn setup(recipe: &[Value], c: &Path, store_id: &str, deep: usize) {
    for op in recipe {
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
            fs::write(
                c.join("keys").join(format!("{store_id}.origin")),
                format!("{{\"seq\": {}}}", nested(deep, shape)),
            )
            .expect("nested origin");
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

/// A text as `_common.text_field(name, ...)` writes it.
fn text_as(name: &str, points: &[u32]) -> Value {
    match points
        .iter()
        .map(|&p| char::from_u32(p))
        .collect::<Option<String>>()
    {
        Some(text) => json!({ name: text }),
        None => json!({ format!("{name}_cps"): points }),
    }
}

/// The warnings as `gen_store_open_restore._warnings` records them: a
/// restore's as `gen_restore_rows._warnings` does.
fn warnings(logged: &[UpkeepWarning], store: &Path, root: &Path) -> Vec<Value> {
    let path = scrub(&store.display().to_string(), root);
    logged
        .iter()
        .map(|w| {
            if let UpkeepWarning::Restore(w) = w {
                return restore_rows::warnings(std::slice::from_ref(w), store, root).remove(0);
            }
            let args: Vec<Value> = w
                .args()
                .into_iter()
                .map(|a| match a {
                    UpkeepArg::Path => json!({ "path": path }),
                    UpkeepArg::Text(points) => text_as("text", &points),
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

/// `errno.errorcode` for the errnos the fixtures hold.
fn errno_name(error: &OpenError) -> Option<&'static str> {
    let OpenError::Io { error, .. } = error else {
        return None;
    };
    match error.raw_os_error() {
        Some(13) => Some("EACCES"),
        Some(1) => Some("EPERM"),
        Some(2) => Some("ENOENT"),
        _ => None,
    }
}

/// Whether the native error is the reference's refusal: its class and its
/// text, or an OSError's errno.
fn same_refusal(e: &OpenError, want: &Value, root: &Path) -> bool {
    let text = unicode_points(&scrub(&e.to_string(), root));
    e.class() == want["class"]
        && match want.get("errno") {
            Some(errno) => errno_name(e).is_some_and(|n| n == errno),
            None => text_as("text", &text) == text_of(want),
        }
}

fn unicode_points(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

fn text_of(want: &Value) -> Value {
    match (want.get("text"), want.get("text_cps")) {
        (Some(t), _) => json!({ "text": t }),
        (None, Some(cps)) => json!({ "text_cps": cps }),
        _ => Value::Null,
    }
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

/// The first open under the case's hook.
fn first_open(hook: Option<&str>, file: &Path, keys: &Path, draws: &Shared, root: &Path) -> Ran {
    let mut provider = draws.clone();
    match hook {
        Some("readonly_keys") => {
            fs::set_permissions(keys, fs::Permissions::from_mode(0o500)).expect("0500");
            let got = open_for_writing_mut(file, keys, &mut provider);
            fs::set_permissions(keys, fs::Permissions::from_mode(0o700)).expect("0700");
            opened_or(got, root, Vec::new())
        }
        Some(other) => panic!("no hook {other}"),
        None => opened_or(
            open_for_writing_mut(file, keys, &mut provider),
            root,
            Vec::new(),
        ),
    }
}

fn run(case: &Value, header: &Value) -> Result<(), String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let deep = header["deep"].as_u64().expect("deep") as usize;
    let dir = scratch("open-restore");
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
    let draws = Shared::new(&case["clock"], 0, &root);
    let ran = first_open(
        case.get("hook").and_then(Value::as_str),
        &file,
        &keys,
        &draws,
        &root,
    );
    if !draws.drained() {
        return Err(format!(
            "{name}: the log clock was read less often than the reference's"
        ));
    }
    if json!(draws.drawn()) != case["drawn"] {
        return Err(format!(
            "{name}: drew {} keys, the reference {}",
            draws.drawn(),
            case["drawn"]
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
    let (logged, checkpoints) = split(&case["warnings"]);
    if Value::Array(draws.checkpoints()) != checkpoints {
        return Err(format!(
            "{name}: warned of the head checkpoint {:?}, the reference {checkpoints}",
            draws.checkpoints()
        ));
    }
    match (case["native"].as_str().expect("native"), ran) {
        ("same", Ran::Opened(log)) => {
            if Value::Array(log.clone()) != logged {
                return Err(format!("{name}: logged {log:?}, the reference {logged}"));
            }
        }
        (_, Ran::Raised(e)) if case["outcome"]["opened"] == json!(false) => {
            let want = &case["outcome"]["refusal"];
            if !same_refusal(&e, want, &root) {
                return Err(format!(
                    "{name}: raised {} {}, the reference {want}",
                    e.class(),
                    scrub(&e.to_string(), &root)
                ));
            }
            let log = Value::Array(warnings(e.upkeep(), &file, &root));
            if log != logged {
                return Err(format!(
                    "{name}: logged {log} before raising, the reference {logged}"
                ));
            }
        }
        (n, Ran::Opened(_)) => return Err(format!("{name}: expected {n}, opened")),
        (n, Ran::Raised(e)) => return Err(format!("{name}: expected {n}, got {} {e}", e.class())),
    }
    // The second open.
    let second = &case["second"];
    let again_draws = Shared::new(&second["clock"], draws.drawn(), &root);
    let ran = opened_or(
        open_for_writing_mut(&file, &keys, &mut again_draws.clone()),
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
    let last = case["appended"]
        .as_array()
        .and_then(|rows| rows.last())
        .map_or(seq, |row| row["seq"].as_i64().expect("seq"));
    if rows_after(&c, last) != second["appended"] {
        return Err(format!(
            "{name}: the second open appended {}, the reference {}",
            rows_after(&c, last),
            second["appended"]
        ));
    }
    let (logged, checkpoints) = split(&second["warnings"]);
    if Value::Array(again_draws.checkpoints()) != checkpoints {
        return Err(format!(
            "{name}: the second open warned of the head checkpoint {:?}, the reference {checkpoints}",
            again_draws.checkpoints()
        ));
    }
    match ran {
        Ran::Opened(log) => {
            if second["outcome"]["opened"] != json!(true) || Value::Array(log.clone()) != logged {
                return Err(format!(
                    "{name}: the second open logged {log:?}, the reference {second}"
                ));
            }
        }
        Ran::Raised(e) => {
            if second["outcome"]["opened"] != json!(false)
                || !same_refusal(&e, &second["outcome"]["refusal"], &root)
            {
                return Err(format!("{name}: the second open raised {} {e}", e.class()));
            }
        }
    }
    if !again_draws.drained() || json!(again_draws.drawn() - draws.drawn()) != second["drawn"] {
        return Err(format!(
            "{name}: the second open read the clock or drew keys otherwise"
        ));
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
    if !root {
        common::assert_count(file, total, lines.len());
    }
    Ledger::new(test, file).settle(total, &failures);
}

#[test]
fn every_case_settles_as_the_reference() {
    every_case(
        "store_open_restore.jsonl",
        "every_case_settles_as_the_reference",
    );
}

/// The cases whose answer is the release's: `sqlite3`'s OverflowError
/// text, and the tokenizer's read nested past where `json.loads` stops,
/// raised after the open's writes and a restore's row.
#[test]
fn every_python_case_settles_as_the_reference() {
    every_case(
        "store_open_restore_python.jsonl",
        "every_python_case_settles_as_the_reference",
    );
}
