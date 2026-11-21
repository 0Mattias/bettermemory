//! A restore's own hold on the restore lock against
//! `store_restore_lock_hold.json`: `acquire`, `RestoreLock.mark` and
//! `release`, `held_here` from the holding thread and another,
//! `unlock`, `state_finding` (`holds`), and `probe` (`probes`). Each hold
//! row is a list of steps run in order on one store, with a helper
//! holding the store's directory or record file where the row says (here
//! an open file of this process, which `flock` sets against this one's as
//! it does another process's). POSIX only, as the fixture;
//! `restore_lock_hold_not_posix.rs` records the gap.

#![cfg(unix)]

mod common;

use std::collections::HashMap;
use std::fs::{self, File};
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use bettermemory_core::config::{pure_path, Flavour};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::restore_lock::{
    acquire, acquire_in_record_mode, held_here, held_here_in_record_mode, lock_path, probe,
    state_finding, state_finding_in_record_mode, unlock, unlock_in_record_mode, AdmitError,
    Finding, HoldError, Holder, Marker, RestoreLock, UnlockStatus, MARKER_FORMAT, RESTORE_WAIT,
};
use common::parity::Ledger;
use common::stores::scratch;
use serde_json::{json, Value};

const FILE: &str = "store_restore_lock_hold.json";
const WAIT: Duration = Duration::from_millis(200);

fn marker_bytes(pieces: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    for piece in pieces.as_array().expect("pieces") {
        let bytes = common::unhex(piece[0].as_str().expect("hex"));
        for _ in 0..piece[1].as_u64().expect("count") {
            out.extend_from_slice(&bytes);
        }
    }
    out
}

fn s(cps: &[u32]) -> String {
    cps.iter().filter_map(|&c| char::from_u32(c)).collect()
}

/// Paths as `str(Path(...))` spells them, and the placeholders.
struct Scrub {
    store: String,
    lock: String,
    pid: String,
    started: Option<String>,
}

impl Scrub {
    fn text(&self, value: &str) -> String {
        let mut out = value
            .replace(&self.lock, "<lock>")
            .replace(&self.store, "<store>");
        out = out.replace(&format!("\"pid\": {}", self.pid), "\"pid\": \"<pid>\"");
        out = out.replace(&format!("by process {}", self.pid), "by process <pid>");
        if let Some(started) = &self.started {
            out = out.replace(started, "<started_at>");
        }
        out
    }

    fn value(&self, value: &PyValue) -> Value {
        match value {
            PyValue::None => Value::Null,
            PyValue::Bool(b) => Value::Bool(*b),
            PyValue::Int(n) => serde_json::from_str(n).expect("int"),
            PyValue::Float(f) => json!(f),
            PyValue::Str(t) => Value::String(self.text(&s(t))),
            PyValue::List(items) | PyValue::Tuple(items) => {
                Value::Array(items.iter().map(|v| self.value(v)).collect())
            }
            PyValue::Dict(items) => self.marker(items),
            other => panic!("value {other:?}"),
        }
    }

    fn marker(&self, marker: &Marker) -> Value {
        let mut out = serde_json::Map::new();
        for (k, v) in marker {
            let PyValue::Str(k) = k else {
                panic!("key {k:?}")
            };
            let k = s(k);
            let v = if k == "unreadable" {
                json!("<unreadable>")
            } else if k == "pid" && matches!(v, PyValue::Int(n) if *n == self.pid) {
                json!("<pid>")
            } else {
                self.value(v)
            };
            out.insert(k, v);
        }
        Value::Object(out)
    }

    fn maybe(&self, marker: &Option<Marker>) -> Value {
        marker.as_ref().map_or(Value::Null, |m| self.marker(m))
    }
}

fn os_class(e: &io::Error) -> String {
    match e.kind() {
        io::ErrorKind::NotFound => "FileNotFoundError".into(),
        io::ErrorKind::IsADirectory => "IsADirectoryError".into(),
        other => format!("OSError {other:?}"),
    }
}

fn hold_error(e: &HoldError) -> Value {
    match e {
        HoldError::Os(e) => json!({"error": os_class(e)}),
        HoldError::Recursion => json!({"error": "RecursionError"}),
        other => json!({"error": format!("{other:?}")}),
    }
}

fn finding(got: Result<Finding, AdmitError>, scrub: &Scrub) -> Value {
    match got {
        Err(_) => json!({"error": "RecursionError"}),
        Ok(f) => json!([
            f.state.name(),
            scrub.text(&s(&f.summary)),
            f.remedy.as_ref().map(|r| s(r)),
            scrub.marker(&f.details),
        ]),
    }
}

fn names(dir: &Path) -> Value {
    let Ok(read) = fs::read_dir(dir) else {
        return Value::Null;
    };
    let mut out: Vec<String> = read
        .map(|e| e.expect("entry").file_name().to_string_lossy().into_owned())
        .collect();
    out.sort();
    json!(out)
}

fn now() -> DateTime {
    let us = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_micros();
    DateTime::from_unix_micros_utc(i64::try_from(us).expect("micros")).expect("now")
}

fn field(value: &Value) -> PyValue {
    match value {
        Value::String(t) => PyValue::str(t),
        Value::Number(n) => PyValue::Int(n.to_string()),
        other => panic!("field {other:?}"),
    }
}

struct Run<'a> {
    store: &'a Path,
    record_mode: bool,
    scrub: Scrub,
    lock: Option<RestoreLock>,
    helper: Option<File>,
}

impl Run<'_> {
    fn held_here(&self) -> Value {
        let got = if self.record_mode {
            held_here_in_record_mode(self.store)
        } else {
            held_here(self.store)
        };
        match got {
            Ok(b) => json!(b),
            Err(e) => json!({"error": os_class(&e)}),
        }
    }

    fn finding(&self) -> Value {
        let got = if self.record_mode {
            state_finding_in_record_mode(self.store)
        } else {
            state_finding(self.store)
        };
        finding(got, &self.scrub)
    }

    fn op(&mut self, op: &Value) -> Value {
        let kind = op[0].as_str().expect("op");
        let lock = lock_path(self.store);
        match kind {
            "acquire" | "acquire_again" => {
                let got = if self.record_mode {
                    acquire_in_record_mode(self.store, WAIT)
                } else {
                    acquire(self.store, WAIT)
                };
                match got {
                    Err(e) => hold_error(&e),
                    Ok(None) => json!({"held": false}),
                    Ok(Some(held)) if kind == "acquire_again" => {
                        held.release(false);
                        json!({"held": true})
                    }
                    Ok(Some(held)) => {
                        let mode = fs::metadata(&lock).expect("lock").permissions().mode() & 0o777;
                        let row = json!({
                            "held": true,
                            "previous": self.scrub.maybe(&held.previous),
                            "record": held.record,
                            "mode": format!("0o{mode:o}"),
                        });
                        assert_eq!(held.store_file, self.store);
                        assert_eq!(held.thread, std::thread::current().id());
                        self.lock = Some(held);
                        row
                    }
                }
            }
            "mark" => {
                let fields: Vec<(&str, PyValue)> = op[1]
                    .as_object()
                    .expect("fields")
                    .iter()
                    .map(|(k, v)| (k.as_str(), field(v)))
                    .collect();
                let held = self.lock.as_mut().expect("held");
                held.mark(&fields, &now()).expect("mark");
                let data = fs::read_to_string(&lock).expect("marker");
                let parsed: Value = serde_json::from_str(&data).expect("JSON");
                assert_eq!(parsed["format"], MARKER_FORMAT);
                self.scrub.started = parsed["started_at"].as_str().map(str::to_owned);
                json!(self.scrub.text(&data))
            }
            "release" => match self.lock.take() {
                None => Value::Null,
                Some(held) => {
                    held.release(op[1].as_bool().expect("clear"));
                    names(self.store.parent().expect("dir"))
                }
            },
            "held_here" => self.held_here(),
            "held_here_thread" => {
                std::thread::scope(|t| t.spawn(|| self.held_here()).join()).expect("thread")
            }
            "state_finding" => self.finding(),
            "state_finding_thread" => {
                std::thread::scope(|t| t.spawn(|| self.finding()).join()).expect("thread")
            }
            "unlock" => {
                let apply = op[1].as_bool().expect("apply");
                let got = if self.record_mode {
                    unlock_in_record_mode(self.store, apply, WAIT)
                } else {
                    unlock(self.store, apply, WAIT)
                };
                match got {
                    Err(e) => hold_error(&e),
                    Ok(r) => json!({
                        "status": r.status.name(),
                        "lock": self.scrub.text(&s(&r.lock)),
                        "marker": self.scrub.maybe(&r.marker),
                        "holder": r.holder.map(Holder::name),
                    }),
                }
            }
            "close_helper" => {
                self.helper = None;
                Value::Null
            }
            "names" => names(self.store.parent().expect("dir")),
            other => panic!("op {other}"),
        }
    }
}

fn run(case: &Value) {
    let name = case["name"].as_str().expect("name");
    let root = scratch("hold");
    let store_dir = root.0.join("store");
    let store = store_dir.join("memory.sqlite");
    let lock = lock_path(&store);
    let record_mode = case["record_mode"].as_bool().expect("record_mode");
    if !case["missing_dir"].as_bool().expect("missing_dir") {
        fs::create_dir(&store_dir).expect("dir");
        fs::write(&store, b"").expect("store");
    }
    if !case["marker"].is_null() {
        fs::write(&lock, marker_bytes(&case["marker"])).expect("marker");
    }
    if case["lock_dir"].as_bool().expect("lock_dir") {
        fs::create_dir(&lock).expect("lock dir");
    }
    let helper = case["helper"].as_str().map(|mode| {
        let f = File::open(if record_mode { &lock } else { &store_dir }).expect("helper");
        if mode == "shared" {
            f.try_lock_shared().expect("helper shared");
        } else {
            f.try_lock().expect("helper exclusive");
        }
        f
    });
    let given: PathBuf = if case["dotted"].as_bool().expect("dotted") {
        store_dir.join(".").join("memory.sqlite")
    } else {
        store.clone()
    };
    let text = |p: &Path| pure_path(&p.to_string_lossy(), Flavour::Posix);
    let mut runner = Run {
        store: &given,
        record_mode,
        scrub: Scrub {
            store: text(&store),
            lock: text(&lock),
            pid: std::process::id().to_string(),
            started: None,
        },
        lock: None,
        helper,
    };
    let ops = case["ops"].as_array().expect("ops");
    let want = case["results"].as_array().expect("results");
    assert_eq!(ops.len(), want.len(), "{name}");
    for (i, (op, want)) in ops.iter().zip(want).enumerate() {
        let got = runner.op(op);
        assert_eq!(&got, want, "{name}: step {i} {op}");
    }
    drop(runner);
}

#[test]
fn a_restores_hold_answers_as_the_reference() {
    let text = fs::read_to_string(common::golden_path(FILE)).expect("file");
    let doc: Value = serde_json::from_str(&text).expect("JSON");
    assert_eq!(doc["marker_format"], MARKER_FORMAT);
    assert_eq!(
        doc["restore_wait_seconds"],
        json!(RESTORE_WAIT.as_secs_f64())
    );
    let exits: HashMap<String, i64> =
        serde_json::from_value(doc["unlock_exit"].clone()).expect("exits");
    for status in [
        UnlockStatus::Clear,
        UnlockStatus::Cleared,
        UnlockStatus::Held,
        UnlockStatus::Planned,
    ] {
        assert_eq!(exits[status.name()], i64::from(status.exit()));
    }
    assert_eq!(exits.len(), 4);
    assert_eq!(
        doc["holders"],
        json!([Holder::Restore.name(), Holder::Reader.name()])
    );
    let cases = doc["holds"].as_array().expect("holds");
    let mut ledger = Ledger::new("a_restores_hold_answers_as_the_reference", FILE);
    for case in cases {
        ledger.case(case["name"].as_str().expect("name"), || run(case));
    }
    assert!(!cases.is_empty());
    ledger.finish(cases.len());
}

/// The other connection a probe row holds open, as the generator makes it.
fn other_connection(store: &Path, setup: &str) -> Option<rusqlite::Connection> {
    let conn = rusqlite::Connection::open(store).expect("open");
    if setup.starts_with("wal") {
        let mode: String = conn
            .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
            .expect("wal");
        assert_eq!(mode, "wal");
    }
    conn.execute_batch("CREATE TABLE t (x); INSERT INTO t VALUES (1);")
        .expect("table");
    if setup == "rollback_closed" || setup == "wal_closed" {
        return None;
    }
    if setup == "rollback_reading" {
        conn.execute_batch("BEGIN").expect("begin");
    }
    {
        let mut st = conn.prepare("SELECT x FROM t").expect("select");
        let rows: Vec<i64> = st
            .query_map([], |r| r.get(0))
            .expect("rows")
            .map(|r| r.expect("row"))
            .collect();
        assert_eq!(rows, vec![1]);
    }
    Some(conn)
}

fn probe_case(case: &Value) {
    let name = case["name"].as_str().expect("name");
    let setup = case["setup"].as_str().expect("setup");
    let root = scratch("probe");
    let store = root.0.join("memory.sqlite");
    let mut given = store.clone();
    let mut other = None;
    match setup {
        "missing" => {}
        "dir" => fs::create_dir(&store).expect("dir"),
        "empty" => fs::write(&store, b"").expect("empty"),
        "text" => fs::write(
            &store,
            b"not a database, and longer than a header could be".repeat(4),
        )
        .expect("text"),
        _ => {
            other = other_connection(&store, setup);
            if setup == "wal_open_dotted" {
                given = root.0.join(".").join("memory.sqlite");
            }
            if setup == "wal_open_link" {
                given = root.0.join("link.sqlite");
                std::os::unix::fs::symlink(&store, &given).expect("link");
            }
        }
    }
    let found = probe(&given);
    drop(other);
    let text = |p: &Path| pure_path(&p.to_string_lossy(), Flavour::Posix);
    let got = found.map(|m| {
        s(&m)
            .replace(&text(&given), "<given>")
            .replace(&text(&store), "<store>")
    });
    assert_eq!(json!(got), case["probe"], "{name}");
    assert_eq!(names(&root.0), case["names_after"], "{name}");
}

#[test]
fn the_sqlite_probe_answers_as_the_reference() {
    let text = fs::read_to_string(common::golden_path(FILE)).expect("file");
    let doc: Value = serde_json::from_str(&text).expect("JSON");
    let cases = doc["probes"].as_array().expect("probes");
    let mut ledger = Ledger::new("the_sqlite_probe_answers_as_the_reference", FILE);
    for case in cases {
        ledger.case(case["name"].as_str().expect("name"), || probe_case(case));
    }
    assert!(!cases.is_empty());
    ledger.finish(cases.len());
}
