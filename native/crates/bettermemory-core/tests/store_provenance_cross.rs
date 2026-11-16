//! The provenance readers against the reference itself, run as a child
//! (`BM_REFERENCE_PYTHON`): on a store the reference made (`Store.create`
//! and `_mutate`) and on one native made (`store::create` and
//! `WritableStore::mutate`), each then planted alike (a pointer cleared,
//! a log row altered, links, a stamp, and a log payload nested 9,998,
//! 9,999 and 100,000 deep under a record's pointer), every reader's answer
//! on both sides is the same, without and with `strict_provenance`,
//! except where the reference's own recursion limit raises: there only
//! native's answer is held, exactly, at each depth. The
//! reference opens read-only; native verifies when the reference does,
//! under the same key. POSIX only, as the reference's create is here.

#![cfg(unix)]

mod common;

use std::path::{Path, PathBuf};

use bettermemory_core::log::fingerprint;
use bettermemory_core::store::open::open_for_writing;
use common::created::{reference_python, run_reference};
use common::parity::record;
use common::provenance::{answers, created_store, ring, same, scratch};
use common::written;
use rusqlite::Connection;
use serde_json::{json, Value};

/// The worktree root, where `tests.contract` imports from.
fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .expect("root")
}

const MAKE: &str = r#"
import sys
from pathlib import Path
from bettermemory.models import Episode, Memory, record_payload
from bettermemory.store import Store

d = Path(sys.argv[1])
store = Store.create(d, keys_dir=d / "keys")
for n in range(1, 5):
    m = Memory.model_validate({
        "id": "01J" + str(n).zfill(23),
        "created": "2001-02-03T04:05:06.000000Z",
        "updated": "2001-02-03T04:05:06.000000Z",
        "scopes": ["global"], "confidence": "high",
        "source": "explicit-statement", "body": f"record {n}",
    })
    payload = {"memory": record_payload(m),
               "provenance": "imported" if n == 2 else "local", "filename": None}
    with store._transaction() as tx:
        store._mutate(tx, "memory_put", payload, session="s1")
store.put_episode(Episode.model_validate({
    "id": "01E" + "1".zfill(23), "session_id": "s-ep",
    "created": "2002-03-04T05:06:07.000008Z", "body": "episode 1\n"}))
print(store.meta()["store_id"])
store.close()
"#;

const ASK: &str = r#"
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[5])
from bettermemory.store import Store
from bettermemory.log import fingerprint
from tests.contract.gen_store_provenance import ask

store = Store.open(Path(sys.argv[1]), keys_dir=Path(sys.argv[2]), read_only=True,
                   strict_provenance=sys.argv[4] == "1")
queries = json.loads(Path(sys.argv[3]).read_text())
try:
    key = store._verify_key
    out = {"verifies": None if key is None else fingerprint(key),
           "answers": [ask(store, q) for q in queries]}
finally:
    store.close()
print(json.dumps(out))
"#;

fn m(n: u32) -> String {
    format!("01J{n:023}")
}

fn queries() -> Vec<Value> {
    let ids: Vec<String> = [1, 2, 3, 4, 5, 6, 1, 99].iter().map(|n| m(*n)).collect();
    vec![
        json!({"call": "provenance_for", "ids": ids}),
        json!({"call": "trust_rows", "ids": ids}),
        json!({"call": "unaccounted_ids"}),
        json!({"call": "provenance_counts"}),
        json!({"call": "episode_provenance", "ids": [format!("01E{:023}", 1), "01E9"]}),
        json!({"call": "links_with_status", "id": m(1)}),
        json!({"call": "links_with_status", "id": m(9)}),
    ]
}

/// The plants both sides get; `depth` nests a pointed-at payload.
fn plant(file: &Path, depth: Option<usize>) {
    let conn = Connection::open(file).expect("open");
    conn.execute_batch(&format!(
        "UPDATE log SET payload = payload || ' ' \
           WHERE mac = (SELECT log_mac FROM memories WHERE id = '{m3}');
         UPDATE memories SET log_mac = NULL WHERE id = '{m4}';
         INSERT INTO memory_links (source_id, type, target_id, note) VALUES
           ('{m1}', 'refines', '{m2}', NULL), ('{m1}', 'refines', '{m9}', 'gone'),
           ('{m3}', 'cites', '{m1}', 'x');
         INSERT INTO verifications (memory_id, machine_id, verified_at)
           VALUES ('{m1}', NULL, '2005-01-01T00:00:00Z');",
        m1 = m(1),
        m2 = m(2),
        m3 = m(3),
        m4 = m(4),
        m9 = m(9),
    ))
    .expect("plant");
    if let Some(depth) = depth {
        // {"memory": {"id": [[...]]}}: two objects around depth - 2 lists.
        let lists = depth - 2;
        let payload = format!(
            "{{\"memory\":{{\"id\":{}{}}}}}",
            "[".repeat(lists),
            "]".repeat(lists)
        );
        conn.execute(
            "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) \
             VALUES (900, 't', NULL, 'memory_put', ?, 'p', 'deep')",
            [&payload],
        )
        .expect("deep row");
        conn.execute(
            "INSERT INTO memories (id, created, updated, confidence, body, scopes_text, \
             scopes_json, provenance, source, log_mac) VALUES (?, '2001', '2001', 'high', \
             'b', 'global', '[\"global\"]', 'local', 'explicit-statement', 'deep')",
            [m(5)],
        )
        .expect("deep record");
    }
}

fn ask_reference(python: &Path, home: &Path, file: &Path, keys: &Path, strict: bool) -> Value {
    let qfile = home.join("queries.json");
    std::fs::create_dir_all(home).expect("home");
    std::fs::write(&qfile, serde_json::to_string(&queries()).unwrap()).expect("queries");
    let flag = PathBuf::from(if strict { "1" } else { "0" });
    let out = run_reference(python, home, ASK, &[file, keys, &qfile, &flag, &root()]);
    assert!(
        out.status.success(),
        "the reference failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).expect("JSON")
}

fn compare(
    label: &str,
    python: &Path,
    home: &Path,
    file: &Path,
    keys: &Path,
    id: &str,
    deep_raises: bool,
) {
    for strict in [false, true] {
        let want = ask_reference(python, home, file, keys, strict);
        // Each store here is the reader's own: it verifies.
        assert!(
            !want["verifies"].is_null(),
            "{label}: the reference does not verify"
        );
        let verifies = !want["verifies"].is_null();
        if verifies {
            let current = ring(keys, id).current.expect("a current key");
            assert_eq!(want["verifies"], fingerprint(current.bytes()), "{label}");
        }
        let got = answers(file, keys, id, verifies, strict, &queries())
            .unwrap_or_else(|e| panic!("{label}: refused: {e}"));
        // Native's own answer at each depth, whatever the reference's
        // limit: it raises past json::MAX_DEPTH (9,998), CPython 3.13's
        // release limit on Linux and macOS, and only there.
        let raised = got[0].get("raised").map(|r| r[0].clone());
        assert_eq!(
            raised,
            deep_raises.then(|| json!("RecursionError")),
            "{label}: native {}",
            got[0]
        );
        let want = want["answers"].as_array().expect("answers");
        assert_eq!(want.len(), got.len());
        for (q, (w, g)) in queries().iter().zip(want.iter().zip(&got)) {
            // Where the reference raises RecursionError its limit decides
            // (500 in a debug build, 3,000 on Windows, the real stack from
            // 3.14): native's own answer is held above, not this one.
            if w.get("raised").is_some_and(|r| r[0] == "RecursionError") {
                continue;
            }
            assert!(
                same(w, g),
                "{label} strict={strict} {q}:\n reference {w}\n native    {g}"
            );
        }
    }
}

fn depths() -> [Option<usize>; 4] {
    [None, Some(9_998), Some(9_999), Some(100_000)]
}

/// The reference, or None with the test recorded as not run (the parity
/// ledger lists it; a run with `BM_REFERENCE_PYTHON` set runs it).
fn python(test: &str) -> Option<PathBuf> {
    match reference_python() {
        Ok(p) => Some(p),
        Err(why) => {
            eprintln!("NOT RUN {test}: {why}");
            record(test, "reference Python", "i", 0, &[test.to_owned()], &[why]);
            None
        }
    }
}

#[test]
fn native_reads_a_store_the_reference_wrote_as_the_reference_does() {
    let Some(python) = python("native_reads_a_store_the_reference_wrote_as_the_reference_does")
    else {
        return;
    };
    for depth in depths() {
        let dir = scratch("cross-ref");
        let store_dir = dir.0.join("s");
        std::fs::create_dir(&store_dir).expect("dir");
        let home = dir.0.join("home");
        let out = run_reference(&python, &home, MAKE, &[&store_dir]);
        assert!(
            out.status.success(),
            "make: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        let id = String::from_utf8(out.stdout).expect("id").trim().to_owned();
        let file = store_dir.join("memory.sqlite");
        plant(&file, depth);
        compare(
            &format!("reference-made {depth:?}"),
            &python,
            &home,
            &file,
            &store_dir.join("keys"),
            &id,
            depth.is_some_and(|d| d > 9_998),
        );
    }
}

#[test]
fn the_reference_reads_a_store_native_wrote_as_native_does() {
    let Some(python) = python("the_reference_reads_a_store_native_wrote_as_native_does") else {
        return;
    };
    let doc = common::created::fixture();
    let id = doc["fixed"]["store_id"].as_str().expect("id").to_owned();
    for depth in depths() {
        let dir = scratch("cross-native");
        let store_dir = dir.0.join("s");
        std::fs::create_dir(&store_dir).expect("dir");
        let (file, keys) = created_store(&store_dir);
        {
            let mut store = open_for_writing(&file, &keys, &common::opened::engine_provider())
                .expect("open for writing");
            let stamps = [written::instant("2003-04-05T06:07:08.000000Z")];
            let mut clock = written::clock(&stamps);
            for n in 1..5u32 {
                let mut payload =
                    written::memory_payload(&m(n), &format!("record {n}"), &["global"], &[]);
                if n == 2 {
                    payload = written::payload(&written::py_dumps(&json!({
                            "memory": {"id": m(2), "created": "2001-02-03T04:05:06.000000Z",
                                "updated": "2001-02-03T04:05:06.000000Z", "scopes": ["global"],
                                "confidence": "high", "source": "explicit-statement",
                                "body": "record 2"},
                            "provenance": "imported", "filename": null})));
                }
                store
                    .mutate("memory_put", &payload, Some("s1"), &mut clock)
                    .expect("put");
            }
            let episode = written::payload(&written::py_dumps(&json!({"episode": {
                "id": format!("01E{:023}", 1), "session_id": "s-ep",
                "created": "2002-03-04T05:06:07.000008Z", "body": "episode 1\n"}})));
            store
                .mutate("episode_put", &episode, Some("s1"), &mut clock)
                .expect("episode");
            store.close().expect("close");
        }
        plant(&file, depth);
        compare(
            &format!("native-made {depth:?}"),
            &python,
            &dir.0.join("home"),
            &file,
            &keys,
            &id,
            depth.is_some_and(|d| d > 9_998),
        );
    }
}
