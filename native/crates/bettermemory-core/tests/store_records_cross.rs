//! The native record writers across the two builds, under the Python
//! `BM_REFERENCE_PYTHON` names when it imports `bettermemory` (else set
//! aside, said in a PARITY line). The reference runs in a child process
//! with HOME and every BETTERMEMORY_* location inside the test's scratch
//! directory, never the owner's files.
//!
//! - (a) Native creates a store and makes 60 writer calls (`put_memory`
//!   with labels and names kept and replaced, supersedes links made and
//!   dropped, `tombstone` of superseding and superseded records,
//!   `delete_tombstone`, and refusals), so its commits write `returned`
//!   marks. The reference opens it, verifies it ok, refolds it ok, and
//!   reads it; a twin the reference writes with the same calls through its
//!   own `put_memory`, `tombstone` and `delete_tombstone` reads the same:
//!   records with their labels and names, tombstones, marks, each call's
//!   outcome, and the log's kinds, sessions and payloads (a mark's stamp
//!   aside: the clocks differ; every removal passes `now`). The reference
//!   then writes with the same public API; native verifies ok and makes
//!   further calls, which the reference verifies.
//! - (b) The reference creates a store and writes records (labels, names,
//!   links) with its public API; native re-puts, removes and deletes
//!   tombstones there; the reference verifies and refolds ok and reads the
//!   labels and names native kept; the native check verifies ok.

mod common;

use std::fs;
use std::path::Path;

use bettermemory_core::store::create;
use bettermemory_core::store::open::open_for_writing;
use common::created::{fixed, fixture, reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::record_calls::{call, refusal};
use common::stores::scratch;
use common::written::{self, instant, ulid};
use serde_json::{json, Value};

const SCRIPT: &str = r#"
import json, sys
from datetime import datetime
from pathlib import Path
from bettermemory.log import canonical_payload
from bettermemory.models import Memory, record_payload
from bettermemory.store import Store

action, store_file, keys = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])

def refusal(exc):
    return {"class": type(exc).__name__, "text": str(exc)}

def run(store, c):
    s = c["session"]
    try:
        if c["call"] == "put":
            kw = {"session": s}
            for k in ("provenance", "filename"):
                if k in c:
                    kw[k] = c[k]
            got = store.put_memory(Memory.model_validate(c["memory"]), **kw)
            return {"written": True, "returned": canonical_payload(record_payload(got))}
        if c["call"] == "tombstone":
            now = None if c["now"] is None else datetime.fromisoformat(c["now"])
            got = store.tombstone(c["id"], c["reason"], session=s, now=now)
            return {"written": True, "returned": canonical_payload(record_payload(got))}
        store.delete_tombstone(c["id"], session=s)
        return {"written": True, "returned": None}
    except Exception as exc:
        return {"written": False, "refusal": refusal(exc)}

def unstamped(kind, payload):
    p = json.loads(payload)
    if kind == "returned_put":
        p["returned"].pop("returned")
    return p

def view(store):
    return {
        "records": [list(r) for r in store.conn.execute(
            "SELECT id, body, provenance, filename, links_json FROM memories ORDER BY id")],
        "gone": [list(r) for r in store.conn.execute(
            "SELECT id, removed, removed_reason, removed_session, provenance, filename "
            "FROM tombstones ORDER BY id")],
        "returned": {k: v["via"] for k, v in store.returned_rows().items()},
        "log": [[k, s, unstamped(k, p)] for k, s, p in store.conn.execute(
            "SELECT kind, session, payload FROM log WHERE seq > 1 ORDER BY seq")],
    }

if action == "check_a":
    calls = json.loads(Path(sys.argv[4]).read_text())
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    first = store.log_verify()["status"]
    refold = store.refold()["status"]
    native = view(store)
    twin = Store.create(Path(sys.argv[5]), keys_dir=Path(sys.argv[5]) / "keys")
    outcomes = [run(twin, c) for c in calls]
    reference = view(twin)
    twin.close()
    more = json.loads(Path(sys.argv[6]).read_text())
    after_calls = [run(store, c) for c in more]
    after = store.log_verify()["status"]
    store.close()
    print(json.dumps({"first": first, "refold": refold, "after": after, "native": native,
                      "reference": reference, "outcomes": outcomes,
                      "after_calls": after_calls}))
elif action == "verify":
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    out = {"status": store.log_verify()["status"], "refold": store.refold()["status"],
           "view": view(store)}
    store.close()
    print(json.dumps(out))
elif action == "make_b":
    calls = json.loads(Path(sys.argv[4]).read_text())
    store = Store.create(store_file.parent, keys_dir=keys)
    outcomes = [run(store, c) for c in calls]
    store.close()
    print(json.dumps({"outcomes": outcomes}))
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

fn memory(n: u64, body: &str, sup: &[u64]) -> Value {
    let links: Vec<Value> = sup
        .iter()
        .map(|t| json!({"type": "supersedes", "target_id": ulid(*t, "01J"), "note": null}))
        .collect();
    json!({"id": ulid(n, "01J"), "created": "2001-02-03T04:05:06.000000Z",
        "updated": "2001-02-03T04:05:06.000000Z", "scopes": ["global", "cross"],
        "confidence": "high", "source": "explicit-statement", "body": body,
        "links": links})
}

fn put(n: u64, sup: &[u64], s: &str, extra: Value) -> Value {
    let body = format!("record {n} about sqlite chain {}", sup.len());
    let mut c = json!({"call": "put", "memory": memory(n, &body, sup), "session": s});
    if let Value::Object(more) = extra {
        c.as_object_mut().expect("call").extend(more);
    }
    c
}

fn tomb(n: u64, s: &str, k: u64) -> Value {
    json!({"call": "tombstone", "id": ulid(n, "01J"), "reason": format!("reason {k}"),
        "session": s, "now": format!("2002-01-01T00:00:{:02}.{k:06}Z", k % 60)})
}

fn undo(n: u64, s: &str) -> Value {
    json!({"call": "delete_tombstone", "id": ulid(n, "01J"), "session": s})
}

/// (a)'s calls: rounds of a chain made, re-puts with labels and names,
/// removals of the head and of a superseded record, a tombstone deleted,
/// and calls the reference refuses.
fn plan() -> Vec<Value> {
    let mut out = Vec::new();
    let labels = ["local", "imported", "synced"];
    for r in 0..6u64 {
        let b = r * 10 + 1;
        let s = format!("s{}", r % 3);
        let label = labels[(r % 3) as usize];
        out.push(put(b, &[], &s, json!({})));
        out.push(put(b + 1, &[b], &s, json!({"provenance": label})));
        out.push(put(
            b + 1,
            &[b],
            &s,
            json!({"filename": format!("f{r}.md")}),
        ));
        out.push(tomb(b + 1, &s, b));
        out.push(put(b + 1, &[], &s, json!({})));
        out.push(put(b + 2, &[b], &s, json!({"provenance": "bogus"})));
        out.push(put(b + 2, &[b], &s, json!({})));
        out.push(put(b + 2, &[], &s, json!({"provenance": "imported"})));
        out.push(tomb(b, &s, b + 100));
        out.push(undo(b + 1, &s));
    }
    out
}

fn run_calls(c: &Path, calls: &[Value], at: &str) -> Vec<Value> {
    let mut store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open");
    let mut clock = written::clock(&[instant(at)]);
    let out = calls
        .iter()
        .map(|one| match call(&mut store, &mut clock, one) {
            Ok(returned) => json!({"written": true, "returned": returned}),
            Err(e) => json!({"written": false, "refusal": refusal(&e)}),
        })
        .collect();
    store.close().expect("close");
    out
}

fn ring_id(keys: &Path) -> String {
    fs::read_dir(keys)
        .expect("keys")
        .flatten()
        .filter_map(|e| {
            e.file_name()
                .to_str()
                .and_then(|n| n.strip_suffix(".key"))
                .map(str::to_owned)
        })
        .next()
        .expect("a key file")
}

#[test]
fn native_writers_continue_on_the_reference_and_the_reverse() {
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            eprintln!("NOT RUN: {why}");
            record(
                "native_writers_continue_on_the_reference_and_the_reverse",
                "reference Python",
                "i",
                0,
                &["both directions".to_owned()],
                &[why],
            );
            return;
        }
    };
    let dir = scratch("records-cross");
    let root = dir.0.clone();
    let id = common::stores::store_id();

    // (a)
    let c = root.join("a");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let calls = plan();
    let native_outcomes = run_calls(&c, &calls, "2001-02-03T07:00:00.000001Z");
    let plan_file = root.join("plan.json");
    fs::write(&plan_file, Value::Array(calls.clone()).to_string()).expect("plan");
    let more = vec![
        put(500, &[], "ref", json!({"filename": "r.md"})),
        put(501, &[500], "ref", json!({})),
        tomb(501, "ref", 7),
        put(500, &[], "ref", json!({"provenance": "synced"})),
    ];
    let more_file = root.join("more.json");
    fs::write(&more_file, Value::Array(more).to_string()).expect("more");
    let twin = root.join("twin");
    let said = reference(
        &python,
        &root,
        &[
            Path::new("check_a"),
            &c.join("memory.sqlite"),
            &c.join("keys"),
            &plan_file,
            &twin,
            &more_file,
        ],
    );
    assert_eq!(said["first"], "ok");
    assert_eq!(said["refold"], "ok");
    assert_eq!(said["after"], "ok");
    assert!(said["after_calls"]
        .as_array()
        .expect("after")
        .iter()
        .all(|o| o["written"] == true));
    assert_eq!(said["outcomes"], Value::Array(native_outcomes.clone()));
    let marks = said["native"]["log"]
        .as_array()
        .expect("log")
        .iter()
        .filter(|r| r[0] == "returned_put" || r[0] == "returned_delete")
        .count();
    assert!(marks > 0, "native's calls wrote no mark");
    for part in ["records", "gone", "returned", "log"] {
        assert_eq!(
            said["native"][part], said["reference"][part],
            "{part}: native-written and reference-written stores differ"
        );
    }
    let (status, _) = written::native_verify(&c, &id);
    assert_eq!(status, "ok");
    // Native continues the store the reference continued.
    let further = run_calls(
        &c,
        &[
            put(500, &[], "after", json!({})),
            tomb(500, "after", 9),
            undo(501, "after"),
            put(700, &[], "after", json!({})),
        ],
        "2001-02-03T09:00:00.000001Z",
    );
    assert!(further.iter().all(|o| o["written"] == true), "{further:?}");
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
    let gone: Vec<&Value> = said["view"]["gone"]
        .as_array()
        .expect("gone")
        .iter()
        .filter(|g| g[0] == ulid(500, "01J").as_str())
        .collect();
    // 500's tombstone keeps the label and name the reference gave it.
    assert_eq!(gone.len(), 1);
    assert_eq!(gone[0][4], "synced");
    assert_eq!(gone[0][5], "r.md");

    // (b)
    let c = root.join("b");
    fs::create_dir(&c).expect("b");
    let made_calls = vec![
        put(
            1,
            &[],
            "ref",
            json!({"provenance": "imported", "filename": "one.md"}),
        ),
        put(2, &[1], "ref", json!({"provenance": "synced"})),
        put(3, &[], "ref", json!({})),
        tomb(3, "ref", 3),
    ];
    let made_file = root.join("made.json");
    fs::write(&made_file, Value::Array(made_calls).to_string()).expect("made");
    let made = reference(
        &python,
        &root,
        &[
            Path::new("make_b"),
            &c.join("memory.sqlite"),
            &c.join("keys"),
            &made_file,
        ],
    );
    assert!(made["outcomes"]
        .as_array()
        .expect("outcomes")
        .iter()
        .all(|o| o["written"] == true));
    let native = run_calls(
        &c,
        &[
            put(1, &[], "native", json!({})),
            tomb(2, "native", 4),
            put(3, &[], "native", json!({})),
            undo(3, "native"),
            put(3, &[1], "native", json!({})),
            tomb(3, "native", 5),
        ],
        "2001-02-03T08:00:00.000001Z",
    );
    let written: Vec<bool> = native.iter().map(|o| o["written"] == true).collect();
    assert_eq!(written, [true, true, false, true, true, true], "{native:?}");
    assert_eq!(native[2]["refusal"]["class"], "TombstonedError");
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
    let records = &said["view"]["records"];
    assert_eq!(records[0][0], ulid(1, "01J").as_str());
    assert_eq!(records[0][2], "imported");
    assert_eq!(records[0][3], "one.md");
    let gone = &said["view"]["gone"];
    assert_eq!(gone[0][0], ulid(2, "01J").as_str());
    assert_eq!(gone[0][4], "synced");
    let (status, _) = written::native_verify(&c, &ring_id(&c.join("keys")));
    assert_eq!(status, "ok");
    eprintln!(
        "(a): {} calls, {marks} mark rows native wrote; (b): {} native calls",
        calls.len(),
        native.len()
    );
    record(
        "native_writers_continue_on_the_reference_and_the_reverse",
        "reference Python",
        "i",
        2,
        &[],
        &[],
    );
}
