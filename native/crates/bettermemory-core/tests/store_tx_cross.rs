//! Native transactions across the two builds, under the Python
//! `BM_REFERENCE_PYTHON` names when it imports `bettermemory` (else set
//! aside, said in a PARITY line). The reference runs in a child process
//! with HOME and every BETTERMEMORY_* location inside the test's scratch
//! directory, never the owner's files.
//!
//! - (a) Native creates a store and runs 40 transactions (supersedes
//!   chains made and undone, removals, nested scopes that fail, releases),
//!   so its commits write `returned` marks. The reference opens it,
//!   verifies it ok, refolds it ok, and reads it; a twin the reference
//!   writes with the same transactions (`_transaction`, `_mutate`, nested
//!   `_transaction`s, `release_returned`) reads the same: records, index
//!   text, FTS5 hits, marks (record and via), and the log's kinds,
//!   sessions and payloads (a mark's stamp aside: the clocks differ). The
//!   reference then writes with its public API (a supersedes link, a
//!   removal and a restore, which write marks); native verifies ok, opens
//!   it for writing and commits a transaction on top, which the reference
//!   verifies.
//! - (b) The reference creates a store and writes marks with its public
//!   API; native opens it for writing and runs transactions that remove a
//!   superseding record, release a mark and fail a nested scope; the
//!   reference verifies and refolds ok and reads the marks native left;
//!   the native check verifies ok.

mod common;

use std::fs;
use std::path::Path;

use bettermemory_core::store::create;
use bettermemory_core::store::open::open_for_writing;
use common::created::{fixed, fixture, reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::stores::scratch;
use common::txs::run_items;
use common::written::{self, instant, ulid};
use serde_json::{json, Value};

const SCRIPT: &str = r#"
import json, sys
from datetime import datetime, timezone
from pathlib import Path
from bettermemory.models import Confidence, Memory, MemoryLink, LinkType, Source
from bettermemory.store import Store

action, store_file, keys = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
now = datetime.now(timezone.utc)
TERMS = ["sqlite", "docker", "record", "staging", "chain", "nothing"]

class Injected(Exception):
    pass

def run_items(store, conn, items):
    for item in items:
        if "scope" in item:
            try:
                with store._transaction():
                    run_items(store, conn, item["scope"])
                    if item["fail"]:
                        raise Injected()
            except Exception:
                pass
        elif "fail" in item:
            raise Injected()
        elif "release" in item:
            store.release_returned(item["release"], session=item.get("session"))
        else:
            store._mutate(conn, item["op"], item["payload"], session=item.get("session"))

def put(store, n, body, links=()):
    m = Memory(id=f"01K{n:023d}", created=now, updated=now, scopes=["cross"],
               confidence=Confidence.HIGH, source=Source.EXPLICIT, body=body,
               links=list(links))
    store.put_memory(m)
    return m.id

def unstamped(kind, payload):
    p = json.loads(payload)
    if kind == "returned_put":
        p["returned"].pop("returned")
    return p

def view(store):
    rows = store.conn.execute(
        "SELECT id, body, body_fts, scopes_text, scopes_fts FROM memories ORDER BY id").fetchall()
    hits = {t: [r[0] for r in store.conn.execute(
        "SELECT m.id FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid "
        "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts), m.id", (t,))] for t in TERMS}
    log = [[k, s, unstamped(k, p)] for k, s, p in store.conn.execute(
        "SELECT kind, session, payload FROM log WHERE seq > 1 ORDER BY seq")]
    return {
        "records": {m.id: m.body for m in store.iter_memories()},
        "gone": sorted(t.id for t in store.iter_tombstones()),
        "rows": [list(r) for r in rows],
        "hits": hits,
        "returned": {k: v["via"] for k, v in store.returned_rows().items()},
        "log": log,
    }

if action == "check_a":
    txs = json.loads(Path(sys.argv[4]).read_text())
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    first = store.log_verify()["status"]
    refold = store.refold()["status"]
    native = view(store)
    twin = Store.create(Path(sys.argv[5]), keys_dir=Path(sys.argv[5]) / "keys")
    for items in txs:
        try:
            with twin._transaction() as tx:
                run_items(twin, tx, items)
        except Injected:
            pass
    reference = view(twin)
    twin.close()
    a = put(store, 900001, "Written by the reference after native, about sqlite.")
    b = put(store, 900002, "Another, superseding the first.",
            [MemoryLink(type=LinkType.SUPERSEDES, target_id=a)])
    store.tombstone(b, "a test removes it")
    store.restore(b)
    after = store.log_verify()["status"]
    marks = sorted(store.returned_rows())
    store.close()
    print(json.dumps({"first": first, "refold": refold, "after": after, "native": native,
                      "reference": reference, "marks": marks}))
elif action == "verify":
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    out = {"status": store.log_verify()["status"], "refold": store.refold()["status"],
           "returned": {k: v["via"] for k, v in store.returned_rows().items()},
           "records": sorted(m.id for m in store.iter_memories())}
    store.close()
    print(json.dumps(out))
elif action == "make_b":
    store = Store.create(store_file.parent, keys_dir=keys)
    a = put(store, 1, "Use docker compose for staging.")
    b = put(store, 2, "Postgres 16.3 replaced 15.4.", [MemoryLink(type=LinkType.SUPERSEDES, target_id=a)])
    c = put(store, 3, "A chain record superseding the second.", [MemoryLink(type=LinkType.SUPERSEDES, target_id=b)])
    store.tombstone(c, "wrong")
    put(store, 4, "Prefer snake_case in Python.")
    marks = {k: v["via"] for k, v in store.returned_rows().items()}
    store.close()
    print(json.dumps({"marks": marks}))
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
    json!({
        "memory": {"id": ulid(n, "01J"), "created": "2001-02-03T04:05:06.000000Z",
            "updated": "2001-02-03T04:05:06.000000Z", "scopes": ["global", "cross"],
            "confidence": "high", "source": "explicit-statement", "body": body,
            "links": links},
        "provenance": "local", "filename": null,
    })
}

fn put(n: u64, sup: &[u64], s: &str) -> Value {
    let body = format!("record {n} about sqlite chain {}", sup.len());
    json!({"op": "memory_put", "payload": memory(n, &body, sup), "session": s})
}

fn delete(n: u64, s: &str) -> Value {
    json!({"op": "memory_delete", "payload": {"id": ulid(n, "01J")}, "session": s})
}

/// (a)'s transactions: rounds of a chain made, a nested scope that fails
/// part way, the chain's head removed (its target back in force, marked),
/// a release, a superseding put again (the mark deleted).
fn plan() -> Vec<Value> {
    let mut out = Vec::new();
    for r in 0..8u64 {
        let base = r * 10 + 1;
        let s = format!("s{}", r % 3);
        out.push(json!([
            put(base, &[], &s),
            put(base + 1, &[base], &s),
            put(base + 2, &[base + 1], &s)
        ]));
        out.push(json!([
            {"scope": [put(base + 3, &[base], &s), {"fail": true}], "fail": false},
            delete(base + 2, &s),
        ]));
        out.push(json!([
            {"release": ulid(base + 1, "01J"), "session": "roster"},
            put(base + 4, &[], &s),
            {"scope": [delete(base + 4, &s)], "fail": r % 2 == 0},
        ]));
        out.push(json!([put(base + 5, &[base + 1, base], &s), {"fail": r % 4 == 3}]));
        out.push(json!([delete(base + 5, &s), delete(base + 1, &s)]));
    }
    out
}

fn native_records(c: &Path, id: &str) -> (String, usize) {
    let (status, n) = written::native_verify(c, id);
    (status.to_owned(), n)
}

fn run_plan(c: &Path, txs: &[Value], at: &str) {
    let mut store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open");
    let mut clock = written::clock(&[instant(at)]);
    for items in txs {
        let items = items.as_array().expect("items");
        let mut errors = Vec::new();
        let mut tx = store.begin(&mut clock).expect("begin");
        match run_items(&mut tx, items, &mut errors) {
            Ok(()) => {
                tx.commit().expect("commit");
            }
            Err(common::txs::Failed::Injected(_)) => drop(tx),
            Err(e) => panic!("a transaction failed: {e:?}"),
        }
    }
    store.close().expect("close");
}

#[test]
fn native_transactions_continue_on_the_reference_and_the_reverse() {
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            eprintln!("NOT RUN: {why}");
            record(
                "native_transactions_continue_on_the_reference_and_the_reverse",
                "reference Python",
                "i",
                0,
                &["both directions".to_owned()],
                &[why],
            );
            return;
        }
    };
    let dir = scratch("tx-cross");
    let root = dir.0.clone();
    let id = common::stores::store_id();

    // (a)
    let c = root.join("a");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let txs = plan();
    run_plan(&c, &txs, "2001-02-03T07:00:00.000001Z");
    let plan_file = root.join("plan.json");
    fs::write(&plan_file, Value::Array(txs).to_string()).expect("plan");
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
        ],
    );
    assert_eq!(said["first"], "ok");
    assert_eq!(said["refold"], "ok");
    assert_eq!(said["after"], "ok");
    let marks = said["native"]["returned"].as_object().expect("marks");
    assert!(!marks.is_empty(), "native's transactions wrote no mark");
    for part in ["records", "gone", "rows", "hits", "returned", "log"] {
        assert_eq!(
            said["native"][part], said["reference"][part],
            "{part}: native-written and reference-written stores differ"
        );
    }
    let (status, n) = native_records(&c, &id);
    assert_eq!(status, "ok");
    let records = said["native"]["records"]
        .as_object()
        .expect("records")
        .len();
    assert_eq!(n, records + 2);
    // Native continues the store the reference continued.
    run_plan(
        &c,
        &[
            json!([put(700, &[], "after"), put(701, &[700], "after")]),
            json!([delete(701, "after")]),
        ],
        "2001-02-03T09:00:00.000001Z",
    );
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
    assert_eq!(said["returned"][ulid(700, "01J")], ulid(701, "01J"));

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
        ],
    );
    let two = "01K00000000000000000000002";
    let one = "01K00000000000000000000001";
    assert_eq!(made["marks"], json!({two: "01K00000000000000000000003"}));
    let ring_id: String = fs::read_dir(c.join("keys"))
        .expect("keys")
        .flatten()
        .filter_map(|e| {
            e.file_name()
                .to_str()
                .and_then(|n| n.strip_suffix(".key"))
                .map(str::to_owned)
        })
        .next()
        .expect("a key file");
    let del = |id: &str| json!({"op": "memory_delete", "payload": {"id": id}, "session": "native"});
    run_plan(
        &c,
        &[
            json!([{"scope": [del(two), {"fail": true}], "fail": false}]),
            json!([{"release": two, "session": "native"}]),
            json!([del(two)]),
        ],
        "2001-02-03T08:00:00.000001Z",
    );
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
    // Two released, then removed: one back in force, marked via two.
    assert_eq!(said["returned"], json!({one: two}));
    let (status, _) = native_records(&c, &ring_id);
    assert_eq!(status, "ok");
    eprintln!(
        "marks native wrote in (a): {}; in (b): {}",
        marks.len(),
        said["returned"]
    );
    record(
        "native_transactions_continue_on_the_reference_and_the_reverse",
        "reference Python",
        "i",
        2,
        &[],
        &[],
    );
}
