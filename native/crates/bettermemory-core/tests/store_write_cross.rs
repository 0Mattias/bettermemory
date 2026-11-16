//! Native writes across the two builds, under the Python
//! `BM_REFERENCE_PYTHON` names when it imports `bettermemory` (else set
//! aside, said in a PARITY line). The reference runs in a child process
//! with HOME and every BETTERMEMORY_* location inside the test's scratch
//! directory, never the owner's files.
//!
//! - (a) Native creates a store, opens it and makes 300 writes (new
//!   records, re-puts, removals with their tombstones, tombstones
//!   dropped), and closes. The reference opens it, verifies it ok, reads
//!   every record (equal to what was written), and runs FTS5 queries; a
//!   twin the reference writes with the same mutations (`_mutate`) gives
//!   the same records, index text and hits. The reference then writes more
//!   with its public API (`put_memory`, `tombstone`, `restore`) and
//!   closes; the native check verifies ok and reads every record.
//! - (b) The reference creates and writes with its public API (a
//!   supersedes link, a removal and a restore, so returned marks are
//!   written), native opens for writing and adds `mutate` writes, and the
//!   reference verifies and reads; the native check verifies ok.

mod common;

use std::fs;
use std::path::Path;

use bettermemory_core::store::create;
use bettermemory_core::store::open::open_for_writing;
use common::created::{fixed, fixture, reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::stores::scratch;
use common::written::{self, instant, py_dumps, ulid};
use serde_json::{json, Value};

const SCRIPT: &str = r#"
import json, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
from bettermemory.models import Confidence, Memory, MemoryLink, LinkType, Source
from bettermemory.store import Store

action, store_file, keys = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
now = datetime.now(timezone.utc)
TERMS = ["sqlite", "docker", "kebab", "record", "staging", "postgres", "global", "nothing"]

def put(store, n, body, links=()):
    m = Memory(id=f"01K{n:023d}", created=now, updated=now, scopes=["cross"],
               confidence=Confidence.HIGH, source=Source.EXPLICIT, body=body,
               links=list(links))
    store.put_memory(m)
    return m.id

def view(store):
    rows = store.conn.execute(
        "SELECT id, body, body_fts, scopes_text, scopes_fts FROM memories ORDER BY id").fetchall()
    hits = {}
    for t in TERMS:
        hits[t] = [r[0] for r in store.conn.execute(
            "SELECT m.id FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts), m.id", (t,))]
    return {
        "records": {m.id: m.body for m in store.iter_memories()},
        "gone": sorted(t.id for t in store.iter_tombstones()),
        "rows": [list(r) for r in rows],
        "hits": hits,
    }

if action == "check_a":
    ops = [json.loads(l) for l in Path(sys.argv[4]).read_text().splitlines()]
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    first = store.log_verify()["status"]
    native = view(store)
    twin = Store.create(Path(sys.argv[5]), keys_dir=Path(sys.argv[5]) / "keys")
    for op in ops:
        with twin._transaction() as tx:
            twin._mutate(tx, op["kind"], json.loads(op["payload"]), session=op["session"])
    reference = view(twin)
    twin.close()
    a = put(store, 900001, "Written by the reference after native, about sqlite.")
    b = put(store, 900002, "Another, superseding the first.",
            [MemoryLink(type=LinkType.SUPERSEDES, target_id=a)])
    store.tombstone(b, "a test removes it")
    store.restore(b)
    after = store.log_verify()["status"]
    store.close()
    print(json.dumps({"first": first, "after": after, "native": native,
                      "reference": reference}))
elif action == "make_b":
    store = Store.create(store_file.parent, keys_dir=keys)
    a = put(store, 1, "Use docker compose for staging.")
    b = put(store, 2, "Postgres 16.3 replaced 15.4.", [MemoryLink(type=LinkType.SUPERSEDES, target_id=a)])
    store.tombstone(b, "wrong")
    store.restore(b)
    put(store, 3, "Prefer snake_case in Python.")
    store.close()
    print(json.dumps({"ok": True}))
elif action == "check_b":
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    out = {"status": store.log_verify()["status"],
           "records": {m.id: m.body for m in store.iter_memories()},
           "returned": sorted(store.returned_rows())}
    put(store, 900003, "The reference writes after native.")
    out["after"] = store.log_verify()["status"]
    store.close()
    print(json.dumps(out))
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

const WORDS: [&str; 8] = [
    "sqlite", "docker", "kebab", "record", "staging", "postgres", "lorem", "ipsum",
];

/// The 300 writes of (a): (kind, payload JSON, session), and the bodies
/// the active records end with.
type Op = (String, String, Option<String>);

fn ops() -> (Vec<Op>, Vec<(String, String)>) {
    let mut ops = Vec::new();
    let mut active: std::collections::BTreeMap<String, String> = Default::default();
    let mut n = 0u64;
    while ops.len() < 300 {
        n += 1;
        let id = ulid(n, "01J");
        let body = format!(
            "{} {} {} note {n} \u{e9}t\u{e9}",
            WORDS[(n % 8) as usize],
            WORDS[((n * 3) % 8) as usize],
            WORDS[((n * 5 + 1) % 8) as usize]
        );
        let session = Some(format!("s{}", n % 3));
        let put = |b: &str| {
            json!({
                "memory": {"id": id, "created": "2001-02-03T04:05:06.000000Z",
                    "updated": "2001-02-03T04:05:06.000000Z", "scopes": ["global", "cross"],
                    "confidence": "high", "source": "explicit-statement", "body": b},
                "provenance": "local", "filename": null,
            })
        };
        ops.push(("memory_put".into(), py_dumps(&put(&body)), session.clone()));
        active.insert(id.clone(), body.clone());
        if n.is_multiple_of(7) {
            let changed = format!("{body} revised");
            ops.push((
                "memory_put".into(),
                py_dumps(&put(&changed)),
                session.clone(),
            ));
            active.insert(id.clone(), changed);
        }
        if n.is_multiple_of(11) {
            ops.push((
                "memory_delete".into(),
                py_dumps(&json!({"id": id})),
                session.clone(),
            ));
            ops.push((
                "tombstone_put".into(),
                py_dumps(&json!({
                    "tombstone": {"id": id, "created": "2001-02-03T04:05:06.000000Z",
                        "updated": "2001-02-03T04:05:06.000000Z", "scopes": ["global"],
                        "confidence": "high", "source": "explicit-statement", "body": body,
                        "removed": "2001-02-04T00:00:00.000000Z", "removed_reason": "outdated",
                        "removed_session": "s"},
                    "provenance": "local", "filename": null, "links": [],
                    "corroborations": 0, "last_corroborated": null,
                })),
                session.clone(),
            ));
            active.remove(&id);
            if n.is_multiple_of(22) {
                ops.push((
                    "tombstone_delete".into(),
                    py_dumps(&json!({"id": id})),
                    session.clone(),
                ));
            }
        }
    }
    (ops, active.into_iter().collect())
}

fn native_records(c: &Path, id: &str) -> (String, usize) {
    let (status, n) = written::native_verify(c, id);
    (status.to_owned(), n)
}

#[test]
fn native_writes_read_back_on_the_reference_and_the_reverse() {
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            eprintln!("NOT RUN: {why}");
            record(
                "native_writes_read_back_on_the_reference_and_the_reverse",
                "reference Python",
                "i",
                0,
                &["both directions".to_owned()],
                &[why],
            );
            return;
        }
    };
    let dir = scratch("write-cross");
    let root = dir.0.clone();
    let id = common::stores::store_id();

    // (a)
    let c = root.join("a");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let mut store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open");
    let (ops, active) = ops();
    let mut clock = written::clock(&[instant("2001-02-03T07:00:00.000001Z")]);
    let mut lines = String::new();
    for (kind, payload, session) in &ops {
        store
            .mutate(
                kind,
                &written::payload(payload),
                session.as_deref(),
                &mut clock,
            )
            .unwrap_or_else(|e| panic!("{kind}: {e}"));
        lines.push_str(&py_dumps(
            &json!({"kind": kind, "payload": payload, "session": session}),
        ));
        lines.push('\n');
    }
    store.close().expect("close");
    let ops_file = root.join("ops.jsonl");
    fs::write(&ops_file, lines).expect("ops");
    let twin = root.join("twin");
    let said = reference(
        &python,
        &root,
        &[
            Path::new("check_a"),
            &c.join("memory.sqlite"),
            &c.join("keys"),
            &ops_file,
            &twin,
        ],
    );
    assert_eq!(said["first"], "ok");
    assert_eq!(said["after"], "ok");
    let want: serde_json::Map<String, Value> =
        active.iter().map(|(k, v)| (k.clone(), json!(v))).collect();
    assert_eq!(said["native"]["records"], Value::Object(want));
    for part in ["records", "gone", "rows", "hits"] {
        assert_eq!(
            said["native"][part], said["reference"][part],
            "{part}: native-written and reference-written stores differ"
        );
    }
    let (status, n) = native_records(&c, &id);
    assert_eq!(status, "ok");
    assert_eq!(n, active.len() + 2);

    // (b)
    let c = root.join("b");
    fs::create_dir(&c).expect("b");
    reference(
        &python,
        &root,
        &[
            Path::new("make_b"),
            &c.join("memory.sqlite"),
            &c.join("keys"),
        ],
    );
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
    let mut store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("a native writing open of a reference store");
    let mut clock = written::clock(&[instant("2001-02-03T08:00:00.000001Z")]);
    for k in 0..50u64 {
        let p = written::memory_payload(
            &ulid(500 + k, "01J"),
            &format!("native write {k} about docker"),
            &["cross"],
            &[],
        );
        store
            .mutate("memory_put", &p, Some("native"), &mut clock)
            .expect("write");
    }
    store
        .mutate(
            "memory_delete",
            &written::payload(&format!("{{\"id\": \"{}\"}}", ulid(510, "01J"))),
            Some("native"),
            &mut clock,
        )
        .expect("delete");
    store.close().expect("close");
    let said = reference(
        &python,
        &root,
        &[
            Path::new("check_b"),
            &c.join("memory.sqlite"),
            &c.join("keys"),
        ],
    );
    assert_eq!(said["status"], "ok");
    assert_eq!(said["after"], "ok");
    assert_eq!(said["records"].as_object().expect("records").len(), 3 + 49);
    let (status, n) = native_records(&c, &ring_id);
    assert_eq!(status, "ok");
    assert_eq!(n, 3 + 49 + 1);
    eprintln!("returned marks the reference wrote: {}", said["returned"]);
    record(
        "native_writes_read_back_on_the_reference_and_the_reverse",
        "reference Python",
        "i",
        2,
        &[],
        &[],
    );
}
