//! The native `mark_verified` and `record_corroboration` across the two
//! builds, under the Python `BM_REFERENCE_PYTHON` names when it imports
//! `bettermemory` (else set aside, said in a PARITY line). The reference
//! runs in a child process with HOME and every BETTERMEMORY_* location
//! inside the test's scratch directory.
//!
//! - (a) Native creates a store, puts records and makes the plan's calls
//!   (verifications with lists, target maps and heads, `check_expected`
//!   both ways, refusals; corroborations from new sessions, repeated
//!   sessions and no session), its wall clock fixed. The reference opens
//!   it, verifies and refolds it ok; a twin the reference writes with the
//!   same calls through its own API and the same wall clock reads the same:
//!   each outcome, the records, the verifications, the log's kinds,
//!   sessions and payloads. The reference then verifies and corroborates on
//!   the native store; native verifies it ok and continues, and the
//!   reference verifies that.
//! - (b) The reference creates a store, writes and verifies; native
//!   verifies and corroborates there; the reference verifies and refolds
//!   ok and reads what native wrote; the native check verifies ok.

mod common;

use std::fs;
use std::path::Path;

use bettermemory_core::store::create;
use bettermemory_core::store::open::open_for_writing;
use common::created::{fixed, fixture, reference_python, run_reference};
use common::opened;
use common::parity::record as parity_record;
use common::record_calls::{call_c, refusal, Minting};
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

def decode(v):
    if isinstance(v, dict) and set(v) == {"$datetime"}:
        return datetime.fromisoformat(v["$datetime"].replace("Z", "+00:00"))
    return v

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
        elif c["call"] == "verify":
            kw = {k: decode(v) for k, v in c["kw"].items()}
            got = store.mark_verified(c["id"], session=s, **kw)
        elif c["call"] == "corroborate":
            got = store.record_corroboration(c["id"], session=s)
            if got is None:
                return {"written": True, "returned": None}
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
        "verifications": q("SELECT * FROM verifications ORDER BY memory_id, verified_at"),
        "rollup": q("SELECT id, corroborations, last_corroborated, last_verified_at, "
                    "verified_head FROM memories ORDER BY id"),
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

fn k(n: u64) -> String {
    ulid(n, "01K")
}

fn put(n: u64, sup: &[&str], s: &str) -> Value {
    json!({"call": "put", "memory": record(&k(n), &format!("record {n} about sqlite"), sup, AT, json!({})), "session": s})
}

fn verify(n: u64, kw: Value, s: Option<&str>) -> Value {
    json!({"call": "verify", "id": k(n), "kw": kw, "session": s})
}

fn corr(n: u64, s: Option<&str>) -> Value {
    json!({"call": "corroborate", "id": k(n), "session": s})
}

/// (a)'s calls: per round a record put, verified with lists, a target map
/// and a head, checked against a stale and a matching snapshot,
/// corroborated from a new session, the same session again and none, and
/// refused for a bad head and a missing id.
fn plan() -> Vec<Value> {
    let mut out = Vec::new();
    for r in 0..5u64 {
        let n = r + 1;
        let s = format!("s{}", r % 2);
        let head = format!("{:040x}", r + 0xabc);
        out.push(put(n, &[], &s));
        out.push(verify(
            n,
            json!({"verified_paths": ["a.py", format!("r{r}.py")],
                   "verified_path_targets": {"a.py": null},
                   "claims": [format!("claim {r}")], "verified_head": head}),
            Some(&s),
        ));
        out.push(verify(
            n,
            json!({"check_expected": true, "expected_last_verified_at": null}),
            Some(&s),
        ));
        out.push(verify(
            n,
            json!({"check_expected": true, "expected_last_verified_at": {"$datetime": AT},
                   "verified_absent_paths": ["gone.py"], "verified_head": head.to_uppercase()}),
            None,
        ));
        out.push(verify(n, json!({"verified_head": "not a hash"}), Some(&s)));
        out.push(corr(n, Some(&s)));
        out.push(corr(n, Some(&s)));
        out.push(corr(n, Some("other")));
        out.push(corr(n, None));
        out.push(corr(90 + r, Some(&s)));
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
        .map(|one| match call_c(&mut store, &mut p, one) {
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
fn native_verification_continues_on_the_reference_and_the_reverse() {
    let test = "native_verification_continues_on_the_reference_and_the_reverse";
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
    let dir = scratch("records-c-cross");
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
        verify(
            1,
            json!({"verified_commits": ["c9"], "verified_head": "e".repeat(64)}),
            Some("ref"),
        ),
        corr(2, Some("ref")),
        corr(2, Some("ref")),
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
    let nothing = native_outcomes
        .iter()
        .filter(|o| o["written"] == true && o["returned"].is_null())
        .count();
    assert!(
        refused >= 10 && nothing >= 5,
        "{refused} refused, {nothing} None"
    );
    let after = said["after_calls"].as_array().expect("after");
    assert_eq!(after.len(), 3);
    assert!(
        after[2]["returned"].is_null(),
        "the same session corroborates once"
    );
    for part in ["records", "verifications", "rollup", "returned", "log"] {
        assert_eq!(
            said["native"][part], said["reference"][part],
            "{part}: native-written and reference-written stores differ"
        );
    }
    let (status, _) = written::native_verify(&c, &id);
    assert_eq!(status, "ok");
    let further = run_calls(
        &c,
        &[
            verify(2, json!({"verified_paths": ["n.py"]}), Some("after")),
            corr(1, Some("ref")),
            corr(1, Some("after")),
        ],
        900,
    );
    assert_eq!(further[0]["written"], true, "{further:?}");
    assert!(further[1]["returned"].is_string(), "{further:?}");
    assert!(further[2]["returned"].is_string(), "{further:?}");
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
        put(1, &[], "ref"),
        verify(
            1,
            json!({"verified_paths": ["x.py"], "verified_head": "f".repeat(40)}),
            Some("ref"),
        ),
        corr(1, Some("ref")),
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
            corr(1, Some("ref")),
            corr(1, Some("native")),
            verify(
                1,
                json!({"verified_absent_paths": ["y.py"]}),
                Some("native"),
            ),
        ],
        700,
    );
    assert!(native[0]["returned"].is_null(), "{native:?}");
    assert!(native[1]["returned"].is_string() && native[2]["returned"].is_string());
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
    let rollup = &said["view"]["rollup"][0];
    assert_eq!(
        rollup[1], 2,
        "two corroborations: the reference's and native's"
    );
    // One verification row per record and machine: native's replaced the
    // reference's.
    let rows = said["view"]["verifications"].as_array().expect("v");
    assert_eq!(rows.len(), 1);
    assert!(rows[0].to_string().contains("y.py"), "{rows:?}");
    let (status, _) = written::native_verify(&c, &ring_id(&c.join("keys")));
    assert_eq!(status, "ok");
    eprintln!(
        "(a): {} calls, {refused} refused, {nothing} corroborations writing nothing",
        calls.len()
    );
    parity_record(test, "reference Python", "i", 2, &[], &[]);
}
