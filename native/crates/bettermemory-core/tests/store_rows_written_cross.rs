//! The row writers across the two builds, under the Python
//! `BM_REFERENCE_PYTHON` names when it imports `bettermemory` (else set
//! aside, said in a PARITY line). The reference runs in a child process
//! with HOME and every BETTERMEMORY_* location inside the test's scratch
//! directory, never the owner's files.
//!
//! - (a) Native creates a store and writes through `store::home` and
//!   `store::tables`: two records, a record's home, standing answers, homed
//!   and unhomed episodes, a floor, an episode deleted, verifications put
//!   and deleted, a conflict, an import and a quarantine entry, put and
//!   deleted. The reference opens it, verifies and refolds it ok and reads
//!   every row back as native wrote it; it then writes with the same
//!   writers on top, and native verifies ok, writes again, and the
//!   reference verifies and refolds ok.
//! - (b) The reference creates a store and writes with these writers;
//!   native opens it for writing and continues (an episode of the
//!   reference's deleted, a standing answer for its record, a homed
//!   episode, a second home refused with nothing written, a table row
//!   deleted); the reference verifies and refolds ok and reads native's
//!   rows; the native check verifies ok.

mod common;

use std::fs;
use std::path::Path;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::create;
use bettermemory_core::store::home::{self, Answer, Home};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::tables::{EpisodeArgs, Quarantine, Verification};
use bettermemory_core::store::write::WriteError;
use common::created::{fixed, fixture, reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::rows::Counting;
use common::stores::scratch;
use common::written::{self, instant, memory_payload};
use serde_json::{json, Value};

const SCRIPT: &str = r#"
import json, sys
from datetime import datetime, timezone
from pathlib import Path
from bettermemory.models import Confidence, Memory, Source
from bettermemory.store import Store
from bettermemory.store_schema import RecordHome, StandingAnswer

action, store_file, keys = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
now = datetime.now(timezone.utc)

def view(store):
    c = store.conn
    q = lambda sql: [list(r) for r in c.execute(sql)]
    return {
        "homes": q("SELECT record_id, kind, project, checkout, rule FROM homes ORDER BY record_id"),
        "standing": q("SELECT record_id, probability, body_sha256, source, answered_by "
                      "FROM standing ORDER BY record_id"),
        "episodes": q("SELECT id, session_id, body, scopes_json, takeaway, is_floor "
                      "FROM episodes ORDER BY id"),
        "verifications": q("SELECT memory_id, machine_id, verified_at FROM verifications "
                           "ORDER BY memory_id, machine_id"),
        "conflicts": [[r["id"], r["a_id"], r["b_id"], r["status"]] for r in store.list_conflicts()],
        "imports": [[r["source"], r["memory_id"]] for r in store.list_imports()],
        "quarantine": [[r["name"], r["reason"], r["size"]] for r in store.list_quarantine()],
        "kinds": [r[0] for r in c.execute("SELECT kind FROM log ORDER BY seq")],
    }

def checks(store):
    return {"status": store.log_verify()["status"], "refold": store.refold()["status"]}

if action == "check_a":
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    out = {**checks(store), "view": view(store)}
    e = store.write_episode(session_id="ref", body="the reference after native",
                            home=RecordHome("p-ref", None, "repository"))
    store.put_standing("01J00000000000000000000002", StandingAnswer(0.75, "hook", "ref"))
    store.put_conflict({"id": "c-ref", "a_id": "x", "b_id": "y", "summary_a": "a",
                        "summary_b": "b", "similarity": 0.5, "method": "lexical",
                        "detector": "write", "created": "2006-01-01T00:00:00Z",
                        "status": "open"})
    store.delete_quarantine("q-keep")
    out["after"] = checks(store)
    out["ref_episode"] = e.id
    store.close()
    print(json.dumps(out))
elif action == "verify":
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    out = {**checks(store), "view": view(store)}
    store.close()
    print(json.dumps(out))
elif action == "make_b":
    store = Store.create(store_file.parent, keys_dir=keys)
    m = Memory(id="01K00000000000000000000001", created=now, updated=now, scopes=["cross"],
               confidence=Confidence.HIGH, source=Source.EXPLICIT, body="A reference record.")
    store.put_memory(m, home=RecordHome("p-b", "c-b", "repository"))
    e1 = store.write_episode(session_id="ref", body="first", home=RecordHome("p-b", None, "r"))
    e2 = store.write_floor(session_id="ref")
    store.put_verification(m.id, verified_at=now, machine_id="m-ref")
    store.put_import("ref.md", content_hash="a" * 64, imported_at="t", memory_id=m.id)
    store.close()
    print(json.dumps({"e1": e1.id, "e2": e2.id}))
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

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn home_of(project: &str, checkout: Option<&str>, rule: &str) -> Home {
    Home {
        project: s(project),
        checkout: checkout.map_or(PyValue::None, s),
        rule: s(rule),
    }
}

fn args(session: &str, body: &str) -> EpisodeArgs {
    EpisodeArgs {
        session_id: s(session),
        body: s(body),
        scopes: PyValue::None,
        takeaway: PyValue::None,
        swarm_id: PyValue::None,
        origin: PyValue::None,
    }
}

fn answer(p: f64, by: &str) -> Answer {
    Answer {
        probability: PyValue::Float(p),
        source: s("user"),
        answered_by: s(by),
    }
}

fn open(c: &Path) -> WritableStore {
    open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open")
}

fn verification(id: &str, machine: Option<&str>) -> Verification {
    Verification {
        memory_id: s(id),
        verified_at: instant("2005-06-07T08:09:10.111213Z"),
        machine_id: machine.map_or(PyValue::None, s),
        verified_head: PyValue::None,
        verified_paths: vec![s("src/a.py")],
        verified_commits: Vec::new(),
        verified_versions: Vec::new(),
        verified_absent_paths: Vec::new(),
        verified_path_targets: None,
        verified_absent_path_targets: None,
    }
}

/// (a)'s native writes.
fn write_a(c: &Path) -> Vec<String> {
    let mut store = open(c);
    let mut p = Counting::at("2001-02-03T07:00:00.000001Z", 0);
    let one = written::ulid(1, "01J");
    let two = written::ulid(2, "01J");
    for (id, body) in [
        (&one, "  first record, padded  "),
        (&two, "second record\n"),
    ] {
        store
            .mutate(
                "memory_put",
                &memory_payload(id, body, &["global"], &[]),
                Some("n"),
                &mut p,
            )
            .expect("memory");
    }
    {
        let mut tx = store.begin(&mut p).expect("begin");
        let row = home::home_row(
            &mut tx,
            &one,
            "memory",
            Some(&home_of("p-a", Some("c-a"), "repository")),
        )
        .expect("a home")
        .expect("a new home");
        home::put_home(&mut tx, &row, Some("n")).expect("home_put");
        tx.commit().expect("commit");
    }
    store
        .put_standing(&one, &answer(0.5, "native"), Some("n"), &mut p)
        .expect("standing");
    store
        .put_standing(&one, &answer(1.0, "native"), Some("n"), &mut p)
        .expect("again");
    let (e1, _) = store
        .write_episode(
            &args("s-n", " homed entry "),
            None,
            Some(&home_of("p-a", None, "r")),
            &mut p,
        )
        .expect("episode");
    let (e2, _) = store
        .write_episode(&args("s-n", "plain entry"), None, None, &mut p)
        .expect("e2");
    let (f, _) = store
        .write_floor(&s("s-n"), &PyValue::None, None, None, &mut p)
        .expect("floor");
    let id2 = bettermemory_core::unicode::to_string(&e2.id).expect("id");
    store
        .delete_episode(&id2, Some("n"), &mut p)
        .expect("delete");
    store
        .put_verification(&verification(&one, None), Some("n"), &mut p)
        .expect("v");
    store
        .put_verification(&verification(&one, Some("m1")), Some("n"), &mut p)
        .expect("v m1");
    store
        .delete_verification(&one, None, Some("n"), &mut p)
        .expect("v delete");
    let conflict = written::payload(
        &json!({"id": "c-n", "a_id": one, "b_id": two,
        "summary_a": "a", "summary_b": "b", "similarity": 0.5, "method": "lexical", "detector": "write", "created": "2006-01-01T00:00:00Z", "status": "open"})
        .to_string(),
    );
    store
        .put_conflict(&conflict, Some("n"), &mut p)
        .expect("conflict");
    store
        .put_import(
            &s("n.md"),
            &s(&"b".repeat(64)),
            &s("t"),
            &s(&one),
            Some("n"),
            &mut p,
        )
        .expect("import");
    for name in ["q-keep", "q-gone"] {
        let q = Quarantine {
            name: s(name),
            reason: s("unreadable"),
            at: s("t"),
            detail: PyValue::None,
            remote: PyValue::None,
            size: PyValue::int(12),
            sha256: PyValue::None,
        };
        store
            .put_quarantine(&q, Some("n"), &mut p)
            .expect("quarantine");
    }
    store
        .delete_quarantine("q-gone", Some("n"), &mut p)
        .expect("q delete");
    store.close().expect("close");
    [e1.id, f.id]
        .iter()
        .map(|i| bettermemory_core::unicode::to_string(i).expect("id"))
        .collect()
}

#[test]
fn native_row_writers_continue_on_the_reference_and_the_reverse() {
    let name = "native_row_writers_continue_on_the_reference_and_the_reverse";
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            eprintln!("NOT RUN: {why}");
            record(
                name,
                "reference Python",
                "i",
                0,
                &["both directions".to_owned()],
                &[why],
            );
            return;
        }
    };
    let dir = scratch("rows-cross");
    let root = dir.0.clone();
    let id = common::stores::store_id();

    // (a)
    let c = root.join("a");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let ids = write_a(&c);
    let one = written::ulid(1, "01J");
    let said = reference(
        &python,
        &root,
        &[
            Path::new("check_a"),
            &c.join("memory.sqlite"),
            &c.join("keys"),
        ],
    );
    assert_eq!(said["status"], "ok");
    assert_eq!(said["refold"], "ok");
    assert_eq!(said["after"], json!({"status": "ok", "refold": "ok"}));
    let v = &said["view"];
    assert_eq!(
        v["homes"],
        json!([
            [one, "memory", "p-a", "c-a", "repository"],
            [ids[0], "episode", "p-a", null, "r"]
        ])
    );
    let standing = v["standing"].as_array().expect("standing");
    assert_eq!(standing.len(), 1);
    assert_eq!(standing[0][1], 1.0);
    assert_eq!(standing[0][4], "native");
    let episodes: Vec<&str> = v["episodes"]
        .as_array()
        .expect("episodes")
        .iter()
        .map(|e| e[0].as_str().expect("id"))
        .collect();
    assert_eq!(episodes, [ids[0].as_str(), ids[1].as_str()]);
    assert_eq!(v["episodes"][0][2], "homed entry\n");
    assert_eq!(v["episodes"][1][5], 1);
    assert_eq!(
        v["verifications"],
        json!([[one, "m1", "2005-06-07T08:09:10.111213+00:00"]])
    );
    assert_eq!(
        v["conflicts"],
        json!([["c-n", one, written::ulid(2, "01J"), "open"]])
    );
    assert_eq!(v["imports"], json!([["n.md", one]]));
    assert_eq!(v["quarantine"], json!([["q-keep", "unreadable", 12]]));
    for kind in [
        "home_put",
        "standing_put",
        "episode_put",
        "episode_delete",
        "verification_delete",
    ] {
        assert!(
            v["kinds"]
                .as_array()
                .expect("kinds")
                .iter()
                .any(|k| k == kind),
            "{kind}"
        );
    }
    let (status, _) = written::native_verify(&c, &id);
    assert_eq!(
        status, "ok",
        "native verifies what the reference wrote on top"
    );
    // Native continues the store the reference continued.
    {
        let mut store = open(&c);
        let mut p = Counting::at("2001-02-03T09:00:00.000001Z", 100);
        let ref_episode = said["ref_episode"].as_str().expect("id");
        let again = store.put_episode(
            &episode_of(&c, ref_episode),
            Some("n"),
            Some(&home_of("p-other", None, "repository")),
            &mut p,
        );
        assert!(matches!(
            again,
            Err(WriteError::Refused {
                class: "ValueError",
                ..
            })
        ));
        store
            .delete_episode(ref_episode, Some("n"), &mut p)
            .expect("delete theirs");
        store
            .delete_conflict("c-ref", Some("n"), &mut p)
            .expect("conflict");
        store.close().expect("close");
    }
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
    assert_eq!(
        said["view"]["conflicts"],
        json!([["c-n", one, written::ulid(2, "01J"), "open"]])
    );

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
    let theirs = "01K00000000000000000000001";
    {
        let mut store = open(&c);
        let mut p = Counting::at("2001-02-03T08:00:00.000001Z", 200);
        store
            .delete_episode(made["e2"].as_str().expect("e2"), Some("n"), &mut p)
            .expect("delete");
        store
            .put_standing(theirs, &answer(0.25, "native"), Some("n"), &mut p)
            .expect("standing");
        store
            .write_episode(
                &args("s-n", "native on theirs"),
                None,
                Some(&home_of("p-b", None, "r")),
                &mut p,
            )
            .expect("episode");
        let mut tx = store.begin(&mut p).expect("begin");
        let other = home::home_row(&mut tx, theirs, "memory", Some(&home_of("p-x", None, "r")));
        assert!(matches!(
            other,
            Err(WriteError::Refused {
                class: "ValueError",
                ..
            })
        ));
        drop(tx);
        store
            .delete_import("ref.md", Some("n"), &mut p)
            .expect("import");
        store.close().expect("close");
    }
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
    let v = &said["view"];
    assert_eq!(v["homes"].as_array().expect("homes").len(), 3);
    assert_eq!(
        v["homes"][0],
        json!([theirs, "memory", "p-b", "c-b", "repository"])
    );
    assert_eq!(v["standing"][0][4], "native");
    assert_eq!(v["episodes"].as_array().expect("episodes").len(), 2);
    assert_eq!(v["imports"], json!([]));
    let (status, _) = written::native_verify(&c, &ring_id);
    assert_eq!(status, "ok");
    record(name, "reference Python", "i", 2, &[], &[]);
}

/// The episode `id` as the store holds it.
fn episode_of(c: &Path, id: &str) -> bettermemory_core::records::Episode {
    let file = bettermemory_core::store::StoreFile::open(&c.join("memory.sqlite")).expect("open");
    let read = file.episode_records().expect("episodes");
    read.into_iter()
        .filter_map(|(_, e)| e.ok())
        .find(|e| bettermemory_core::unicode::to_string(&e.id).as_deref() == Some(id))
        .expect("the episode")
}
