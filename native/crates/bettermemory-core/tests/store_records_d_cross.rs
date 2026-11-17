//! The native `put_tombstone`, `restore` and `restore_trimmed` across the two
//! builds, under the Python `BM_REFERENCE_PYTHON` names when it imports
//! `bettermemory` (else set aside, said in a PARITY line). The reference
//! runs in a child process with HOME and every BETTERMEMORY_* location
//! inside the test's scratch directory.
//!
//! - (a) Native creates a store and makes the plan's calls (records put,
//!   some linking to others; tombstoned and restored, relinking the
//!   records that link to them; restored trimmed; tombstones put directly
//!   with a label, links and a rollup, then restored; refusals), its wall
//!   clock fixed. The reference opens it, verifies and refolds it ok; a
//!   twin the reference writes with the same calls through its own API
//!   reads the same: each outcome, the records and their labels, the
//!   tombstones, the links index, the returned marks, the log's kinds,
//!   sessions and payloads. The reference then tombstones and restores on
//!   the native store; native verifies it ok and continues, and the
//!   reference verifies that.
//! - (b) The reference creates a store, writes and tombstones; native
//!   restores there; the reference verifies and refolds ok and reads what
//!   native wrote; the native check verifies ok.

mod common;

use std::fs;
use std::path::Path;

use bettermemory_core::store::create;
use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::writers::Relink;
use common::created::{fixed, fixture, reference_python, run_reference};
use common::opened;
use common::parity::record as parity_record;
use common::record_calls::{call_d, refusal, Minting};
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
from bettermemory.models import Memory, TombstonedMemory, record_payload
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
        elif c["call"] == "tombstone":
            store.tombstone(c["id"], "removed", session=s)
            return {"written": True, "returned": None}
        elif c["call"] == "restore":
            got = store.restore(c["id"], session=s)
        elif c["call"] == "restore_trimmed":
            got = store.restore_trimmed(c["id"], session=s, **c["kw"])
        elif c["call"] == "put_tombstone":
            kw = dict(c["kw"])
            if kw.get("last_corroborated"):
                kw["last_corroborated"] = datetime.fromisoformat(kw["last_corroborated"])
            got = store.put_tombstone(TombstonedMemory.model_validate(c["dead"]), session=s, **kw)
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
        if isinstance(exc, RecursionError):
            text = None  # the interpreter's wording; the class is compared
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
        "tombstones": q("SELECT id, body, provenance, filename, links_json, corroborations, "
                        "last_corroborated, claims_json FROM tombstones ORDER BY id"),
        "links": q("SELECT * FROM memory_links ORDER BY source_id, type, target_id"),
        "trust": q("SELECT id, claims_json, verified_paths_json, last_verified_at, "
                   "verified_head, corroborations FROM memories ORDER BY id"),
        "log": [[k, s, json.loads(p)] for k, s, p in store.conn.execute(
            "SELECT kind, session, payload FROM log WHERE seq > 1 ORDER BY seq")],
    }

if action == "check_a":
    calls = json.loads(Path(sys.argv[4]).read_text())
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True, strict_provenance=False)
    first = store.log_verify()["status"]
    refold = store.refold()["status"]
    native = view(store)
    twin = Store.create(Path(sys.argv[5]), keys_dir=Path(sys.argv[5]) / "keys", strict_provenance=False)
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
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True, strict_provenance=False)
    out = {"status": store.log_verify()["status"], "refold": store.refold()["status"],
           "view": view(store)}
    store.close()
    print(json.dumps(out))
elif action == "restore":
    # A hand-edited store restored in one process, a refold first when
    # asked; a minute's watchdog, so a hang fails rather than waits.
    import faulthandler
    faulthandler.dump_traceback_later(60, exit=True)
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True, strict_provenance=False)
    for statement in json.loads(Path(sys.argv[6]).read_text()):
        store.conn.execute(statement)
    store.conn.commit()
    memo = []
    if sys.argv[5] == "refold":
        store.refold()
        memo = sorted(store._unaccounted_memory_ids)
    outcome = fixed_run(store, [{"call": "restore", "id": sys.argv[4], "session": "x"}], 600)[0]
    count = store.conn.execute("SELECT count(*) FROM log").fetchone()[0]
    out = {"outcome": outcome, "memo": memo, "log": count,
           "view": view(store) if outcome["written"] else None}
    store.close()
    print(json.dumps(out))
elif action == "view":
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True, strict_provenance=False)
    out = {"view": view(store)}
    store.close()
    print(json.dumps(out))
elif action == "make_b":
    calls = json.loads(Path(sys.argv[4]).read_text())
    store = Store.create(store_file.parent, keys_dir=keys, strict_provenance=False)
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

fn linked(n: u64, to: &[u64], s: &str, extra: Value) -> Value {
    let mut m = record(&k(n), &format!("record {n} about sqlite"), &[], AT, extra);
    m["links"] = to
        .iter()
        .map(|t| json!({"type": "extends", "target_id": k(*t), "note": null}))
        .collect();
    json!({"call": "put", "memory": m, "session": s})
}

fn tomb(n: u64, s: &str) -> Value {
    json!({"call": "tombstone", "id": k(n), "session": s})
}

fn restore(n: u64, s: Option<&str>) -> Value {
    json!({"call": "restore", "id": k(n), "session": s})
}

fn trimmed(n: u64, kw: Value, s: Option<&str>) -> Value {
    json!({"call": "restore_trimmed", "id": k(n), "kw": kw, "session": s})
}

fn put_dead(n: u64, kw: Value, s: Option<&str>) -> Value {
    let mut dead = record(&k(n), &format!("removed record {n}"), &[], AT, json!({}));
    let fields = dead.as_object_mut().expect("record");
    fields.remove("links");
    fields.insert("removed".into(), json!(AT));
    fields.insert("removed_reason".into(), json!("outdated"));
    fields.insert("removed_session".into(), json!(null));
    json!({"call": "put_tombstone", "dead": dead, "kw": kw, "session": s})
}

/// (a)'s calls: per round a record with claims and paths, another linking
/// to it, the first tombstoned and restored (relinking the second), then
/// tombstoned and restored trimmed; a tombstone put directly with a label,
/// links and a rollup, restored; and the refusals.
fn plan() -> Vec<Value> {
    let mut out = Vec::new();
    for r in 0..5u64 {
        let n = r + 1;
        let s = format!("s{}", r % 2);
        let trust = json!({"claims": ["a", format!("c{r}")], "verified_paths": ["p", "q"],
            "last_verified_at": AT, "verified_head": "b".repeat(40)});
        out.push(linked(n, &[], &s, trust));
        out.push(linked(n + 50, &[n], &s, json!({})));
        out.push(tomb(n, &s));
        out.push(restore(n, Some(&s)));
        out.push(restore(n, Some(&s)));
        out.push(tomb(n, &s));
        out.push(trimmed(
            n,
            json!({"drop_claims": ["a"], "drop_verified_paths": ["q"],
                   "clear_verification": true, "drop_verified_head": r % 2 == 0}),
            None,
        ));
        out.push(trimmed(n, json!({}), Some(&s)));
        out.push(put_dead(
            n + 70,
            json!({"provenance": "imported", "filename": format!("d{r}.md"),
                   "links": [{"type": "depends_on", "target_id": k(n)}],
                   "corroborations": r, "last_corroborated": AT}),
            Some(&s),
        ));
        out.push(restore(n + 70, None));
        out.push(put_dead(n + 70, json!({}), Some(&s)));
        out.push(restore(90 + r, Some(&s)));
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
        .map(|one| {
            match call_d(
                &mut store,
                &mut p,
                one,
                &common::record_calls::Reads::default(),
            ) {
                Ok(returned) => json!({"written": true, "returned": returned}),
                Err(e) => json!({"written": false, "refusal": refusal(&e)}),
            }
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
fn native_restore_continues_on_the_reference_and_the_reverse() {
    let test = "native_restore_continues_on_the_reference_and_the_reverse";
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
    let dir = scratch("records-d-cross");
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
        tomb(51, "ref"),
        restore(51, Some("ref")),
        tomb(1, "ref"),
        trimmed(1, json!({"drop_claims": ["c0"]}), Some("ref")),
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
    assert_eq!(refused, 20, "{native_outcomes:?}");
    let after = said["after_calls"].as_array().expect("after");
    assert!(after.iter().all(|o| o["written"] == true), "{after:?}");
    for part in ["records", "tombstones", "links", "trust", "returned", "log"] {
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
            tomb(2, "after"),
            restore(2, Some("after")),
            tomb(51, "after"),
            restore(51, None),
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
        put(1, &[], "ref"),
        linked(2, &[1], "ref", json!({})),
        tomb(1, "ref"),
        put_dead(
            3,
            json!({"provenance": "synced", "links": [], "corroborations": 4}),
            Some("ref"),
        ),
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
            restore(1, Some("native")),
            trimmed(3, json!({"drop_claims": ["x"]}), Some("native")),
        ],
        700,
    );
    assert!(
        native.iter().all(|o| o["returned"].is_string()),
        "{native:?}"
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
    let records = said["view"]["records"].as_array().expect("records");
    // Both restored records labelled local; the linking one relinked.
    assert_eq!(records.len(), 3, "{records:?}");
    assert!(records.iter().all(|r| r[2] == "local"), "{records:?}");
    let kinds: Vec<&str> = said["view"]["log"]
        .as_array()
        .expect("log")
        .iter()
        .filter(|r| r[1] == "native")
        .map(|r| r[0].as_str().expect("kind"))
        .collect();
    assert_eq!(
        kinds,
        [
            "tombstone_delete",
            "memory_put",
            "memory_put",
            "returned_put",
            "tombstone_delete",
            "memory_put",
            "returned_put"
        ],
        "native's rows: the relink put and the returned marks"
    );
    let (status, _) = written::native_verify(&c, &ring_id(&c.join("keys")));
    assert_eq!(status, "ok");
    eprintln!("(a): {} calls, {refused} refused", calls.len());
    parity_record(test, "reference Python", "i", 2, &[], &[]);
}

/// A store native built, then on each build's own copy opened, edited by
/// hand as `edit` says (on the open connection: the reference's open reads
/// the payloads written since its digest mark, so a deep one edited in
/// first would stop the open itself), refolded when asked and restored:
/// the outcomes, the refolds' ids, the log's length and (when written)
/// the stores after are the same. Returns the outcome.
fn same_restore(python: &Path, root: &Path, name: &str, edit: &[String], refold: bool) -> Value {
    let id = common::stores::store_id();
    let c = root.join(name);
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let calls = vec![
        linked(1, &[], "s", json!({})),
        linked(2, &[1], "s", json!({})),
        linked(3, &[1], "s", json!({})),
        linked(4, &[], "s", json!({})),
        tomb(4, "s"),
        tomb(1, "s"),
    ];
    let made = run_calls(&c, &calls, 0);
    assert!(made.iter().all(|o| o["written"] == true), "{made:?}");
    let (mine, theirs) = (
        root.join(format!("{name}-native")),
        root.join(format!("{name}-ref")),
    );
    written::keep_copy(&c, &mine, &id);
    written::keep_copy(&c, &theirs, &id);
    let file = mine.join("memory.sqlite");
    let mut store =
        open_for_writing(&file, &mine.join("keys"), &opened::engine_provider()).expect("open");
    for statement in edit {
        store
            .connection()
            .execute_batch(statement)
            .expect("the hand edit");
    }
    let memo: Vec<String> = if refold {
        let read = bettermemory_core::store::StoreFile::open(&file).expect("read");
        let report = read.refold().expect("refold");
        let mut ids: Vec<String> = report
            .unaccounted_ids()
            .iter()
            .map(|l| match l {
                bettermemory_core::store::fold::Label::One(
                    bettermemory_core::store::fold::Cell::Text(t),
                ) => t.clone(),
                other => panic!("an id {other:?}"),
            })
            .collect();
        ids.sort();
        ids
    } else {
        Vec::new()
    };
    let mut p = Minting {
        inner: written::clock(&[instant(AT)]),
        next: 600,
    };
    let reads = Relink {
        strict: false,
        unaccounted: &memo,
    };
    let native = match store.restore(&mut p, &k(1), Some("x"), reads) {
        Ok(done) => json!({"written": true, "returned": done.value.payload_text().expect("text")}),
        Err(e) => json!({"written": false, "refusal": refusal(&e)}),
    };
    let count: i64 = store
        .connection()
        .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
        .expect("count");
    store.close().expect("close");
    let edits = root.join(format!("{name}-edits.json"));
    fs::write(&edits, json!(edit).to_string()).expect("edits");
    let said = reference(
        python,
        root,
        &[
            Path::new("restore"),
            &theirs.join("memory.sqlite"),
            &theirs.join("keys"),
            Path::new(&k(1)),
            Path::new(if refold { "refold" } else { "plain" }),
            &edits,
        ],
    );
    assert_eq!(said["outcome"], native, "{name}: the outcomes differ");
    assert_eq!(
        said["memo"],
        json!(memo),
        "{name}: the refolds found other ids"
    );
    assert_eq!(said["log"], count, "{name}: the logs' lengths differ");
    if native["written"] == true {
        let seen = reference(
            python,
            root,
            &[Path::new("view"), &file, &mine.join("keys")],
        );
        assert_eq!(
            seen["view"], said["view"],
            "{name}: the stores differ after"
        );
    }
    native
}

#[test]
fn hand_edited_sources_restore_as_the_reference() {
    let test = "hand_edited_sources_restore_as_the_reference";
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            eprintln!("NOT RUN: {why}");
            parity_record(
                test,
                "reference Python",
                "i",
                0,
                &["three stores".to_owned()],
                &[why],
            );
            return;
        }
    };
    let dir = scratch("records-d-cross-edited");
    let root = dir.0.clone();
    // Two sources, both put again, in the reference's order (oldest id
    // first): the stores after, logs included, are the same.
    let out = same_restore(&python, &root, "order", &[], false);
    assert_eq!(out["written"], true);
    let order = reference(
        &python,
        &root,
        &[
            Path::new("view"),
            &root.join("order-native").join("memory.sqlite"),
            &root.join("order-native").join("keys"),
        ],
    );
    let relinked: Vec<&str> = order["view"]["log"]
        .as_array()
        .expect("log")
        .iter()
        .filter(|r| r[1] == "x" && r[0] == "memory_put")
        .map(|r| r[2]["memory"]["id"].as_str().expect("id"))
        .collect();
    assert_eq!(
        relinked,
        [k(1), k(2), k(3)],
        "the restored record, then its sources by id"
    );
    // A source whose pointer is broken reads `unaccounted`: passed over,
    // only the other one put again.
    let broken = format!(
        "UPDATE memories SET log_mac = 'broken' WHERE id = '{}'",
        k(2)
    );
    let out = same_restore(&python, &root, "unaccounted", &[broken], false);
    assert_eq!(out["written"], true);
    // A row edited by hand whose pointer still verifies: after a refold in
    // the same process it reads `unaccounted` too.
    let edited = format!("UPDATE memories SET body = 'edited' WHERE id = '{}'", k(2));
    let out = same_restore(&python, &root, "memo", &[edited], true);
    assert_eq!(out["written"], true);
    // A source's tombstone row pointing at a payload nested past the
    // parser's limit: `_unknown_record_fields` raises RecursionError, the
    // restore writes nothing.
    let deep = format!(
        "{{\"tombstone\": {}1{}}}",
        "[".repeat(100_000),
        "]".repeat(100_000)
    );
    let payload = format!(
        "UPDATE log SET payload = '{deep}' WHERE mac = (SELECT log_mac FROM tombstones WHERE id = '{}')",
        k(4)
    );
    let moved = format!(
        "UPDATE tombstones SET id = '{}' WHERE id = '{}'",
        k(2),
        k(4)
    );
    let out = same_restore(&python, &root, "deep", &[payload, moved], false);
    assert_eq!(out["refusal"]["class"], "RecursionError", "{out}");
    parity_record(test, "reference Python", "i", 4, &[], &[]);
}
