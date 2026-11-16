//! Chunked runs across the two builds, with the reference the Python
//! `BM_REFERENCE_PYTHON` names (else set aside, said in a PARITY line),
//! run in a child with HOME and every BETTERMEMORY_* location inside the
//! test's scratch directory.
//!
//! - (a) Native creates a store, puts 30 records and removes 6, then runs
//!   `rename_scope` (active and removed), `prune_tombstones` and a `batch`.
//!   The reference opens it, verifies and refolds it ok, and reads every
//!   record's and tombstone's scopes, which equal a twin the reference
//!   writes with the same calls; the log's kinds and sessions are the same
//!   in both. The reference then renames back and prunes the rest; native
//!   verifies ok and renames again on top; the reference verifies that.
//! - (b) The reference creates a store and renames with its public API;
//!   native renames and prunes; the reference verifies, refolds and reads.
//!
//! Both sides run each call under one fixed monotonic schedule (100, then
//! 1/32 s a reading) with a pause that is only recorded, never slept: each
//! call's chunk begins, commits and pauses are asserted (chunks of four)
//! and equal the reference's for the same calls.

mod common;

use std::fs;
use std::io;
use std::path::Path;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::store::create::{self, Fixed, Provider};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::unicode;
use common::bulk::{boundaries, memory, put_records, Order};
use common::created::{fixed, fixture, reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::stores::scratch;
use common::written::{self, instant, ulid};
use serde_json::{json, Value};

const SCRIPT: &str = r#"
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from bettermemory.models import Memory
from bettermemory.store import Store

action, store_file, keys = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])

def view(store):
    return {
        "records": {m.id: m.scopes for m in store.iter_memories()},
        "gone": {t.id: t.scopes for t in store.iter_tombstones()},
        "log": [[k, s] for k, s in store.conn.execute(
            "SELECT kind, session FROM log WHERE seq > 1 ORDER BY seq")],
    }

import bettermemory.store as store_module

class Paced:
    """`time` with a fixed monotonic clock (100, then 1/32 s a reading,
    from the start of each call) and a sleep that is only recorded."""
    def __init__(self):
        self.events, self.reads = [], 0
    def monotonic(self):
        self.reads += 1
        return 100.0 + (self.reads - 1) / 32
    def sleep(self, seconds):
        self.events.append(f"sleep {seconds!r}")
    def __getattr__(self, name):
        return getattr(store_module.__dict__["_real_time"], name)

store_module._real_time = store_module.time

def traced(store, pace):
    def trace(statement):
        if statement == "BEGIN IMMEDIATE":
            pace.events.append("begin")
        elif statement == "COMMIT":
            pace.events.append("commit")
    store.conn.set_trace_callback(trace)

def calls(store, plan, events=None):
    out = []
    for c in plan:
        pace = Paced()
        store_module.time = pace
        traced(store, pace)
        try:
            out.append(call(store, c))
        finally:
            store_module.time = store_module._real_time
            store.conn.set_trace_callback(None)
        if events is not None:
            events.append(pace.events)
    return out

def call(store, c):
    if c[0] == "rename":
        return store.rename_scope(c[1], c[2], include_tombstones=c[3], session="cross")
    if c[0] == "prune":
        return store.prune_tombstones(timedelta(seconds=c[1]),
                   now=datetime.fromisoformat(c[2]), session="cross")
    with store.batch():
        for r in c[1]:
            store.put_memory(Memory.model_validate(r), session="cross")
    return None

if action == "check_a":
    plan = json.loads(Path(sys.argv[4]).read_text())
    setup = json.loads(Path(sys.argv[5]).read_text())
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    first, refold = store.log_verify()["status"], store.refold()["status"]
    native = view(store)
    twin = Store.create(Path(sys.argv[6]), keys_dir=Path(sys.argv[6]) / "keys")
    for r in setup["active"]:
        twin.put_memory(Memory.model_validate(r), session="setup")
    for r, at in setup["removed"]:
        twin.put_memory(Memory.model_validate(r), session="setup")
        twin.tombstone(r["id"], "old", session="setup", now=datetime.fromisoformat(at))
    events = []
    twin_out = calls(twin, plan, events)
    reference = view(twin)
    twin.close()
    store.rename_scope("project:b", "project:a", session="ref")
    store.prune_tombstones(timedelta(0), session="ref")
    after = store.log_verify()["status"]
    store.close()
    print(json.dumps({"first": first, "refold": refold, "after": after,
                      "native": native, "reference": reference, "twin_out": twin_out,
                      "events": events}))
elif action == "verify":
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    out = {"status": store.log_verify()["status"], "refold": store.refold()["status"]}
    out.update(view(store))
    store.close()
    print(json.dumps(out))
elif action == "make_b":
    store = Store.create(store_file.parent, keys_dir=keys)
    setup = json.loads(Path(sys.argv[4]).read_text())
    for r in setup["active"]:
        store.put_memory(Memory.model_validate(r), session="setup")
    for r, at in setup["removed"]:
        store.put_memory(Memory.model_validate(r), session="setup")
        store.tombstone(r["id"], "old", session="setup", now=datetime.fromisoformat(at))
    events = []
    calls(store, [["rename", "project:a", "project:c", True]], events)
    store.close()
    print(json.dumps({"events": events}))
"#;

fn reference(python: &Path, root: &Path, args: &[&Path]) -> Value {
    let out = run_reference(python, &root.join("home"), SCRIPT, args);
    assert!(
        out.status.success(),
        "the reference failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(out.stdout.trim_ascii()).expect("the reference's JSON")
}

fn rec(n: u64, scopes: &[&str]) -> Value {
    json!({
        "id": ulid(n, "01J"), "created": "2001-02-03T04:05:06.000000Z",
        "updated": "2001-02-03T04:05:06.000000Z", "scopes": scopes,
        "confidence": "high", "source": "explicit-statement",
        "body": format!("record {n} about sqlite"),
    })
}

fn setup() -> Value {
    let active: Vec<Value> = (1..=30)
        .map(|n| {
            rec(
                n,
                if n % 3 == 0 {
                    &["global", "project:a", "project:b"]
                } else {
                    &["project:a"]
                },
            )
        })
        .collect();
    let removed: Vec<Value> = (40..46)
        .map(|n| {
            json!([
                rec(n, &["project:a"]),
                format!("2001-01-{:02}T00:00:00+00:00", n - 30)
            ])
        })
        .collect();
    json!({"active": active, "removed": removed})
}

fn plan() -> Value {
    json!([
        ["rename", "project:a", "project:b", true],
        ["prune", 86400 * 3, "2001-01-15T00:00:00+00:00"],
        ["batch", [rec(60, &["project:b"]), rec(61, &["global"])]],
        ["rename", "global", "project:g", false],
    ])
}

/// The fixed clocks of `written::clock`, with the reference script's
/// monotonic schedule (100, then 1/32 s a reading, from the start of each
/// call) and a pause that is only recorded, beside each chunk's begin and
/// commit, into `events`.
struct Paced {
    inner: Fixed,
    reads: u32,
    events: Order,
}

impl Provider for Paced {
    fn store_id(&mut self) -> io::Result<String> {
        self.inner.store_id()
    }
    fn key(&mut self) -> io::Result<Key> {
        self.inner.key()
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        self.inner.membership_store_id()
    }
    fn now(&mut self) -> io::Result<DateTime> {
        self.inner.now()
    }
    fn wall_now(&mut self) -> io::Result<DateTime> {
        self.inner.wall_now()
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn ulid(&mut self) -> io::Result<String> {
        self.inner.ulid()
    }
    fn monotonic(&mut self) -> io::Result<f64> {
        self.reads += 1;
        Ok(100.0 + f64::from(self.reads - 1) / 32.0)
    }
    fn pause(&mut self, seconds: f64) {
        self.events.borrow_mut().push(format!("sleep {seconds}"));
    }
}

/// Runs `plan`; each call's begins, commits and pauses in order.
fn run_plan(store: &mut WritableStore, plan: &Value) -> Vec<Vec<String>> {
    let mut all = Vec::new();
    let inner = written::clock(&[instant("2001-02-03T07:00:00.000001Z")]);
    let mut p = Paced {
        inner,
        reads: 0,
        events: Order::default(),
    };
    for c in plan.as_array().expect("plan") {
        p.reads = 0;
        p.events.borrow_mut().clear();
        let events = p.events.clone();
        let mut hook = boundaries(&events);
        match c[0].as_str().expect("call") {
            "rename" => {
                store
                    .rename_scope_hooked(
                        &mut p,
                        &unicode::cps(c[1].as_str().expect("old")),
                        &unicode::cps(c[2].as_str().expect("new")),
                        c[3].as_bool().expect("tomb"),
                        Some("cross"),
                        Some(&mut hook),
                    )
                    .expect("rename");
            }
            "prune" => {
                let now = DateTime::from_isoformat(&unicode::cps(c[2].as_str().expect("now")))
                    .expect("now");
                let older = i128::from(c[1].as_i64().expect("s")) * 1_000_000;
                store
                    .prune_tombstones_hooked(
                        &mut p,
                        older,
                        Some(now),
                        Some("cross"),
                        Some(&mut hook),
                    )
                    .expect("prune");
            }
            _ => {
                let records = c[1].as_array().expect("records");
                store
                    .batch(&mut p, Some(&mut hook), |tx| {
                        for r in records {
                            bettermemory_core::store::bulk::put_plain(
                                tx,
                                &memory(r),
                                Some("cross"),
                            )?;
                        }
                        Ok::<(), bettermemory_core::store::writers::RecordWriteError>(())
                    })
                    .expect("batch");
            }
        }
        drop(hook);
        all.push(events.borrow().clone());
    }
    all
}

/// A run of `n` items in chunks of `per`: each chunk's begin and commit,
/// a pause between chunks.
fn chunked(n: usize, per: usize) -> Vec<String> {
    let mut out = Vec::new();
    let chunks = n.div_ceil(per);
    for i in 0..chunks {
        out.extend(["begin".to_owned(), "commit".to_owned()]);
        if i + 1 < chunks {
            out.push("sleep 0.05".to_owned());
        }
    }
    out
}

fn open(c: &Path) -> WritableStore {
    open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open")
}

#[test]
fn chunked_runs_continue_on_the_reference_and_the_reverse() {
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            eprintln!("NOT RUN: {why}");
            record(
                "chunked_runs_continue_on_the_reference_and_the_reverse",
                "reference Python",
                "i",
                0,
                &["both directions".to_owned()],
                &[why],
            );
            return;
        }
    };
    let dir = scratch("bulk-cross");
    let root = dir.0.clone();
    let id = common::stores::store_id();
    let setup = setup();
    let setup_file = root.join("setup.json");
    fs::write(&setup_file, setup.to_string()).expect("setup");
    let plan_file = root.join("plan.json");
    fs::write(&plan_file, plan().to_string()).expect("plan");

    // (a)
    let c = root.join("a");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let mut store = open(&c);
    put_records(&mut store, &setup);
    let events = run_plan(&mut store, &plan());
    store.close().expect("close");
    // 30 active records in chunks of four (the fourth reading is 0.125
    // past the first), then 6 removed; the prune's 3 removed before the
    // cutoff (after the rename, 6 remain); the batch's one transaction;
    // the 10 records holding `global`.
    // Between the active and the removed runs no pause: each `_in_chunks`
    // pauses only while its own items remain.
    let mut rename = chunked(30, 4);
    rename.extend(chunked(6, 4));
    assert_eq!(events[0], rename, "the rename's chunks");
    assert_eq!(events[1], chunked(3, 4), "the prune's chunks");
    assert_eq!(events[2], chunked(1, 1), "the batch's transaction");
    assert_eq!(events[3], chunked(10, 4), "the second rename's chunks");
    let twin = root.join("twin");
    let said = reference(
        &python,
        &root,
        &[
            Path::new("check_a"),
            &c.join("memory.sqlite"),
            &c.join("keys"),
            &plan_file,
            &setup_file,
            &twin,
        ],
    );
    assert_eq!(said["first"], "ok");
    assert_eq!(said["refold"], "ok");
    assert_eq!(said["after"], "ok");
    assert_eq!(said["events"], json!(events), "the reference's chunks");
    for part in ["records", "gone", "log"] {
        assert_eq!(
            said["native"][part], said["reference"][part],
            "{part} differs"
        );
    }
    let renamed = said["native"]["records"]
        .as_object()
        .expect("records")
        .len();
    assert_eq!(renamed, 32);
    let (status, _) = written::native_verify(&c, &id);
    assert_eq!(status, "ok");
    let mut store = open(&c);
    run_plan(
        &mut store,
        &json!([["rename", "project:a", "project:z", true]]),
    );
    store.close().expect("close");
    let said = reference(
        &python,
        &root,
        &[
            Path::new("verify"),
            &c.join("memory.sqlite"),
            &c.join("keys"),
        ],
    );
    assert_eq!(said["status"], "ok");
    assert_eq!(said["refold"], "ok");
    assert!(said["records"]
        .as_object()
        .expect("records")
        .values()
        .all(|s| !s.as_array().expect("scopes").contains(&json!("project:a"))));

    // (b)
    let c = root.join("b");
    fs::create_dir(&c).expect("b");
    let made = reference(
        &python,
        &root,
        &[
            Path::new("make_b"),
            &c.join("memory.sqlite"),
            &c.join("keys"),
            &setup_file,
        ],
    );
    let ring: String = fs::read_dir(c.join("keys"))
        .expect("keys")
        .flatten()
        .filter_map(|e| {
            e.file_name()
                .to_str()
                .and_then(|n| n.strip_suffix(".key"))
                .map(str::to_owned)
        })
        .next()
        .expect("a key");
    let mut store = open(&c);
    let events = run_plan(
        &mut store,
        &json!([
            ["rename", "project:c", "project:d", true],
            ["prune", 0, "2002-01-01T00:00:00+00:00"]
        ]),
    );
    // The reference's rename and native's after it cut the same chunks.
    assert_eq!(made["events"][0], json!(events[0]), "the renames' chunks");
    assert_eq!(events[1], chunked(6, 4), "the prune's chunks");
    store.close().expect("close");
    let said = reference(
        &python,
        &root,
        &[
            Path::new("verify"),
            &c.join("memory.sqlite"),
            &c.join("keys"),
        ],
    );
    assert_eq!(said["status"], "ok");
    assert_eq!(said["refold"], "ok");
    assert_eq!(said["gone"], json!({}));
    assert!(said["records"]
        .as_object()
        .expect("records")
        .values()
        .all(|s| s.as_array().expect("scopes").contains(&json!("project:d"))));
    let (status, _) = written::native_verify(&c, &ring);
    assert_eq!(status, "ok");
    record(
        "chunked_runs_continue_on_the_reference_and_the_reverse",
        "reference Python",
        "i",
        2,
        &[],
        &[],
    );
}
