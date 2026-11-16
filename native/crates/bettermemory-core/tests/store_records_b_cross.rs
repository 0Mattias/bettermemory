//! The native `write` and `update` across the two builds, under the
//! Python `BM_REFERENCE_PYTHON` names when it imports `bettermemory` (else
//! set aside, said in a PARITY line). The reference runs in a child
//! process with HOME and every BETTERMEMORY_* location inside the test's
//! scratch directory.
//!
//! - (a) Native creates a store and makes the plan's calls (writes with
//!   and without a home and a standing answer, updates that drop and add
//!   supersedes links, keep verification, refuse a stale snapshot or a bad
//!   answer), its ids minted from a counter and its wall clock fixed. The
//!   reference opens it, verifies it ok and refolds it ok; a twin the
//!   reference writes with the same calls through its own `write`,
//!   `update` and `put_memory`, with the same ids and the same wall clock
//!   patched in, reads the same: each call's outcome, the records, homes,
//!   standing rows and marks, and the log's kinds, sessions and payloads.
//!   The reference then writes and updates on the native store; native
//!   verifies it ok and continues with its own calls, which the reference
//!   verifies.
//! - (b) The reference creates a store and writes; native updates and
//!   writes there; the reference verifies and refolds ok and reads what
//!   native wrote; the native check verifies ok.

mod common;

use std::fs;
use std::path::Path;

use bettermemory_core::store::create;
use bettermemory_core::store::open::open_for_writing;
use common::created::{fixed, fixture, reference_python, run_reference};
use common::opened;
use common::parity::record as parity_record;
use common::record_calls::{call_b, refusal, Minting};
use common::stores::scratch;
use common::written::{self, instant, ulid};
use serde_json::{json, Value};

/// The wall clock both builds read in (a): native's fixed clock.
const AT: &str = "2001-02-03T07:00:00.000001Z";

const SCRIPT: &str = r#"
import json, sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from bettermemory import store as bstore, store_records, store_returned
from bettermemory.log import canonical_payload
from bettermemory.models import Memory, record_payload
from bettermemory.store import Store
from bettermemory.store_schema import RecordHome, StandingAnswer

action, store_file, keys = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
AT = datetime(2001, 2, 3, 7, 0, 0, 1, tzinfo=timezone.utc)

def ulid(n, prefix="01K"):
    digits = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    tail = ""
    for _ in range(26 - len(prefix)):
        tail = digits[n % 32] + tail
        n //= 32
    return prefix + tail

class Wall(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT

def run(store, c):
    s = c["session"]
    try:
        if c["call"] == "put":
            got = store.put_memory(Memory.model_validate(c["memory"]), session=s)
        elif c["call"] == "write":
            a = dict(c["args"])
            h, st = c["home"], c["standing"]
            got = store.write(**a, session=s, home=None if h is None else RecordHome(*h),
                              standing=None if st is None else StandingAnswer(*st))
        else:
            st = c["standing"]
            got = store.update(Memory.model_validate(c["memory"]), force=c["force"],
                               preserve_verification=c["preserve_verification"], session=s,
                               standing=None if st is None else StandingAnswer(*st))
        return {"written": True, "returned": canonical_payload(record_payload(got))}
    except Exception as exc:
        text = str(exc)
        if type(exc).__name__ == "ValidationError":
            text = text.splitlines()[0]
        return {"written": False, "refusal": {"class": type(exc).__name__, "text": text}}

def fixed_run(store, calls, first):
    count = [first]
    def mint():
        count[0] += 1
        return ulid(count[0])
    with patch.object(store_records, "generate_ulid", mint), \
         patch.object(store_records, "utcnow", lambda: AT), \
         patch.object(store_records, "datetime", Wall), \
         patch.object(bstore, "datetime", Wall), \
         patch.object(store_returned, "datetime", Wall):
        return [run(store, c) for c in calls]

def view(store):
    q = lambda sql: [list(r) for r in store.conn.execute(sql)]
    return {
        "records": q("SELECT id, body, provenance, links_json, updated FROM memories ORDER BY id"),
        "homes": q("SELECT * FROM homes ORDER BY record_id"),
        "standing": q("SELECT * FROM standing ORDER BY record_id"),
        "returned": q("SELECT * FROM returned ORDER BY record_id"),
        "log": [[k, s, json.loads(p)] for k, s, p in store.conn.execute(
            "SELECT kind, session, payload FROM log WHERE seq > 1 ORDER BY seq")],
    }

if action == "check_a":
    calls = json.loads(Path(sys.argv[4]).read_text())
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    first = store.log_verify()["status"]
    refold = store.refold()["status"]
    native = view(store)
    twin = Store.create(Path(sys.argv[5]), keys_dir=Path(sys.argv[5]) / "keys")
    outcomes = fixed_run(twin, calls, 0)
    reference = view(twin)
    twin.close()
    more = json.loads(Path(sys.argv[6]).read_text())
    after_calls = fixed_run(store, more, 500)
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

const HOME: [&str; 3] = ["global", "", "fallback"];

fn write(content: &str, home: bool, standing: Option<f64>, s: &str) -> Value {
    json!({"call": "write", "args": {"content": content, "scopes": ["global", "cross"]},
        "home": if home { json!([HOME[0], null, HOME[2]]) } else { Value::Null },
        "standing": standing.map_or(Value::Null, |p| json!([p, "explicit", "cross"])),
        "session": s})
}

fn record(id: &str, body: &str, sup: &[&str], updated: &str, extra: Value) -> Value {
    let links: Vec<Value> = sup
        .iter()
        .map(|t| json!({"type": "supersedes", "target_id": t, "note": null}))
        .collect();
    let mut m = json!({"id": id, "created": "2001-02-03T04:05:06.000000Z",
        "updated": updated, "scopes": ["global", "cross"], "confidence": "high",
        "source": "explicit-statement", "body": body, "links": links});
    if let Value::Object(more) = extra {
        m.as_object_mut().expect("record").extend(more);
    }
    m
}

fn update(m: Value, force: bool, preserve: bool, standing: Option<f64>, s: &str) -> Value {
    json!({"call": "update", "memory": m, "force": force, "preserve_verification": preserve,
        "standing": standing.map_or(Value::Null, |p| json!([p, "explicit", "cross"])),
        "session": s})
}

fn k(n: u64) -> String {
    ulid(n, "01K")
}

/// (a)'s calls. Ids are minted `01K` from 1, in call order.
fn plan() -> Vec<Value> {
    let mut out = Vec::new();
    for r in 0..5u64 {
        let s = format!("s{}", r % 2);
        let b = r * 3;
        out.push(write(
            &format!("  round {r} first about sqlite \n"),
            r % 2 == 0,
            None,
            &s,
        ));
        out.push(write(
            &format!("round {r} second"),
            false,
            Some(0.25 * r as f64),
            &s,
        ));
        out.push(write(&format!("round {r} third"), true, Some(1.0), &s));
        let (one, two) = (k(b + 1), k(b + 2));
        let sup = record(
            &two,
            "now superseding the first",
            &[one.as_str()],
            AT,
            json!({}),
        );
        out.push(update(sup, true, false, None, &s));
        let stale = record(
            &two,
            "a stale snapshot",
            &[],
            "2000-01-01T00:00:00Z",
            json!({}),
        );
        out.push(update(stale, false, false, None, &s));
        let fresh = record(&two, "link dropped, fresh snapshot", &[], AT, json!({}));
        out.push(update(fresh, false, true, Some(0.5), &s));
        let bad = record(&one, "a bad answer", &[], AT, json!({}));
        out.push(update(bad, true, false, Some(2.0), &s));
    }
    out
}

fn run_calls(c: &Path, calls: &[Value], first: u64) -> Vec<Value> {
    let mut store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open");
    let mut p = Minting {
        inner: written::clock(&[instant(AT)]),
        next: first,
    };
    let out = calls
        .iter()
        .map(|one| match call_b(&mut store, &mut p, one) {
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
fn native_write_and_update_continue_on_the_reference_and_the_reverse() {
    let test = "native_write_and_update_continue_on_the_reference_and_the_reverse";
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            eprintln!("NOT RUN: {why}");
            parity_record(
                test,
                "reference Python",
                "i",
                0,
                &["both directions".to_owned()],
                &[why],
            );
            return;
        }
    };
    let dir = scratch("records-b-cross");
    let root = dir.0.clone();
    let id = common::stores::store_id();

    // (a)
    let c = root.join("a");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let calls = plan();
    let native_outcomes = run_calls(&c, &calls, 0);
    let plan_file = root.join("plan.json");
    fs::write(&plan_file, Value::Array(calls.clone()).to_string()).expect("plan");
    let more = vec![
        write("the reference writes on", true, Some(0.75), "ref"),
        update(
            record(&k(1), "the reference updates", &[], AT, json!({})),
            true,
            false,
            None,
            "ref",
        ),
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
    assert_eq!(said["outcomes"], Value::Array(native_outcomes.clone()));
    let refused = native_outcomes
        .iter()
        .filter(|o| o["written"] == false)
        .count();
    assert!(refused >= 10, "the plan's refusals: {refused}");
    for o in said["after_calls"].as_array().expect("after") {
        assert_eq!(o["written"], true, "{o}");
    }
    for part in ["records", "homes", "standing", "returned", "log"] {
        assert_eq!(
            said["native"][part], said["reference"][part],
            "{part}: native-written and reference-written stores differ"
        );
    }
    let marks = said["native"]["log"]
        .as_array()
        .expect("log")
        .iter()
        .filter(|r| r[0] == "returned_put" || r[0] == "returned_delete")
        .count();
    assert!(marks > 0, "no mark written");
    let (status, _) = written::native_verify(&c, &id);
    assert_eq!(status, "ok");
    let further = run_calls(
        &c,
        &[
            write("native after the reference", true, Some(0.1), "after"),
            update(
                record(
                    &k(501),
                    "native updates the reference's record",
                    &[],
                    AT,
                    json!({}),
                ),
                true,
                true,
                Some(0.9),
                "after",
            ),
        ],
        900,
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

    // (b)
    let c = root.join("b");
    fs::create_dir(&c).expect("b");
    let made_calls = vec![
        json!({"call": "put", "memory": record(&k(1), "made by the reference", &[], AT, json!({"claims": ["c"]})), "session": "ref"}),
        json!({"call": "put", "memory": record(&k(2), "the second", &[], AT, json!({})), "session": "ref"}),
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
            update(
                record(
                    &k(2),
                    "native supersedes the first",
                    &[&k(1)],
                    AT,
                    json!({}),
                ),
                true,
                false,
                Some(0.5),
                "native",
            ),
            update(
                record(&k(1), "native edits the first", &[], AT, json!({})),
                true,
                true,
                None,
                "native",
            ),
            write("native writes here", true, Some(0.3), "native"),
        ],
        700,
    );
    assert!(native.iter().all(|o| o["written"] == true), "{native:?}");
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
    let records = said["view"]["records"].as_array().expect("records");
    assert!(records.iter().any(|r| r[0] == k(701).as_str()));
    assert_eq!(
        said["view"]["standing"].as_array().expect("standing").len(),
        2
    );
    assert_eq!(said["view"]["homes"].as_array().expect("homes").len(), 1);
    let (status, _) = written::native_verify(&c, &ring_id(&c.join("keys")));
    assert_eq!(status, "ok");
    eprintln!(
        "(a): {} calls, {refused} refused, {marks} mark rows; (b): {} native calls",
        calls.len(),
        native.len()
    );
    parity_record(test, "reference Python", "i", 2, &[], &[]);
}
