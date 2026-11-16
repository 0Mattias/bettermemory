//! The writing open across the two builds, under the Python
//! `BM_REFERENCE_PYTHON` names when it imports `bettermemory` (else set
//! aside, said in a PARITY line). The reference runs in a child process
//! with HOME and every BETTERMEMORY_* location inside the test's scratch
//! directory, never the owner's files.
//!
//! - A reference store (created, written: puts, a tombstone, a restore,
//!   another tombstone, so its digest is behind by every record kind),
//!   opened for writing natively and closed; its digest tables then equal
//!   the ones the reference's own writing open makes of a copy; the
//!   reference opens it again, verifies it ok, reads every record and
//!   writes more; the native check verifies it ok.
//! - The reverse: a native create, a native writing open, the reference's
//!   writes, a native writing open (the digest behind again), the same
//!   digest comparison, and both verifies.

mod common;

use std::path::Path;

use bettermemory_core::log::KeyRing;
use bettermemory_core::store::create::{self, Fixed};
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::open::{open_for_writing, DigestOutcome};
use bettermemory_core::store::{StoreError, StoreFile};
use common::created::{fixed, fixture, reference_python, run_reference};
use common::parity::record;
use common::stores::scratch;
use serde_json::Value;

/// argv: action, store file, keys dir[, copy dir].
const SCRIPT: &str = r#"
import json, shutil, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
from bettermemory.models import Confidence, Memory, Source
from bettermemory.store import Store

action, store_file, keys = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
now = datetime.now(timezone.utc)

def put(store, n, body):
    m = Memory(id=f"01K{n:023d}", created=now, updated=now, scopes=["cross"],
               confidence=Confidence.HIGH, source=Source.EXPLICIT, body=body)
    store.put_memory(m)
    return m.id

def digest_rows(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for t in ("digest_records", "digest_tokens", "digest_state"):
            out[t] = [list(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
        return out
    finally:
        conn.close()

if action == "make":
    store = Store.create(store_file.parent, keys_dir=keys)
    a = put(store, 1, "Use docker compose for staging.")
    b = put(store, 2, "Postgres 16.3 replaced 15.4 on the db-primary host.")
    c = put(store, 3, "Prefer snake_case in Python.\n\n_(consolidate --llm --from-transcript: x)_")
    store.tombstone(a, "superseded")
    store.restore(a)
    store.tombstone(c, "wrong")
    store.close()
    print(json.dumps({"ok": True}))
elif action == "write":
    store = Store.open(store_file, keys_dir=keys, allow_rekey=True)
    first = store.log_verify()["status"]
    ids = sorted(m.id for m in store.iter_memories())
    gone = sorted(t.id for t in store.iter_tombstones())
    n = 10 + len(ids) + len(gone)
    new = put(store, n, f"Record {n} written by the reference.")
    store.tombstone(new, "a test removes it")
    store.restore(new)
    put(store, n + 1, "Another one, kebab-case and 3.12.1 inside.")
    after = store.log_verify()["status"]
    store.close()
    print(json.dumps({"first": first, "after": after, "ids": ids, "gone": gone}))
elif action == "digest":
    copy = Path(sys.argv[4])
    (copy / "keys").mkdir(parents=True)
    shutil.copyfile(store_file, copy / "memory.sqlite")
    for p in keys.iterdir():
        if p.suffix in (".key", ".head", ".origin"):
            shutil.copyfile(p, copy / "keys" / p.name)
    Store.open(copy / "memory.sqlite", keys_dir=copy / "keys", allow_rekey=True).close()
    print(json.dumps(digest_rows(copy / "memory.sqlite")))
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

fn native_digest(store: &Path) -> Value {
    let conn = rusqlite::Connection::open_with_flags(
        format!("file:{}?mode=ro", store.display()),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
    )
    .expect("open");
    let mut out = serde_json::Map::new();
    for t in ["digest_records", "digest_tokens", "digest_state"] {
        let mut s = conn
            .prepare(&format!("SELECT * FROM {t} ORDER BY 1, 2"))
            .expect("select");
        let n = s.column_count();
        let rows: Vec<Value> = s
            .query_map([], |r| {
                Ok(Value::Array(
                    (0..n)
                        .map(|i| match r.get_ref(i).unwrap() {
                            rusqlite::types::ValueRef::Null => Value::Null,
                            rusqlite::types::ValueRef::Integer(v) => Value::from(v),
                            rusqlite::types::ValueRef::Text(t) => {
                                Value::from(String::from_utf8_lossy(t).into_owned())
                            }
                            other => panic!("{other:?}"),
                        })
                        .collect(),
                ))
            })
            .expect("rows")
            .collect::<Result<_, _>>()
            .expect("rows");
        out.insert(t.to_owned(), Value::Array(rows));
    }
    Value::Object(out)
}

fn verify(store_file: &Path, keys: &Path, id: &str) -> usize {
    let store = StoreFile::open(store_file).expect("native open");
    let keys = keys.to_path_buf();
    let id = id.to_owned();
    let mut load = move |s: &StoreFile| -> Result<(KeyRing, String), StoreError> {
        let fp = s
            .meta()?
            .into_iter()
            .find(|(k, _)| k == "key_fingerprint")
            .map(|(_, v)| v)
            .unwrap_or_default();
        let ring = load_key_ring(&keys, &id).map_err(|e| StoreError::Refused(format!("{e:?}")))?;
        Ok((ring, fp))
    };
    let report = store.log_verify_loading(&mut load).expect("native verify");
    assert_eq!(report.status, "ok", "{report:?}");
    let mut n = 0;
    store.each_memory(&mut |_| n += 1).expect("memories");
    n
}

fn engine() -> Fixed {
    let mut provider = fixed(&fixture());
    provider.engine_version = "9.0.0rc19".to_owned();
    provider
}

fn set_aside(test: &str) -> Option<std::path::PathBuf> {
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
fn a_reference_store_opened_natively_goes_on_in_the_reference() {
    const TEST: &str = "a_reference_store_opened_natively_goes_on_in_the_reference";
    let Some(python) = set_aside(TEST) else {
        return;
    };
    let dir = scratch("open-cross");
    let root = &dir.0;
    let file = root.join("store").join("memory.sqlite");
    let keys = root.join("keys");
    reference(&python, root, &[Path::new("make"), &file, &keys]);
    let want = reference(
        &python,
        root,
        &[Path::new("digest"), &file, &keys, &root.join("twin")],
    );
    let opened = open_for_writing(&file, &keys, &engine()).expect("native writing open");
    assert_eq!(opened.digest(), &DigestOutcome::Ingested);
    assert!(
        !opened.bound(),
        "the reference's create bound the ring already"
    );
    let id = opened.store_id().to_owned();
    opened.close().expect("close");
    assert_eq!(
        native_digest(&file),
        want,
        "the digest the native open wrote"
    );
    let said = reference(&python, root, &[Path::new("write"), &file, &keys]);
    assert_eq!(said["first"], "ok");
    assert_eq!(said["after"], "ok");
    assert_eq!(said["ids"].as_array().unwrap().len(), 2);
    assert_eq!(said["gone"].as_array().unwrap().len(), 1);
    assert_eq!(verify(&file, &keys, &id), 4);
    record(TEST, "reference Python", "i", 1, &[], &[]);
}

#[test]
fn a_native_store_written_by_the_reference_opens_natively() {
    const TEST: &str = "a_native_store_written_by_the_reference_opens_natively";
    let Some(python) = set_aside(TEST) else {
        return;
    };
    let dir = scratch("open-cross-native");
    let root = &dir.0;
    let keys = root.join("keys");
    let mut provider = engine();
    provider.clock = vec![create::system_now()];
    let made = create::create(&root.join("store"), &keys, &mut provider).expect("create");
    let opened = open_for_writing(&made.path, &keys, &provider).expect("first open");
    assert_eq!(opened.digest(), &DigestOutcome::Current);
    assert!(!opened.bound());
    opened.close().expect("close");
    let said = reference(&python, root, &[Path::new("write"), &made.path, &keys]);
    assert_eq!(said["first"], "ok");
    assert_eq!(said["after"], "ok");
    let want = reference(
        &python,
        root,
        &[Path::new("digest"), &made.path, &keys, &root.join("twin")],
    );
    let opened = open_for_writing(&made.path, &keys, &provider).expect("second open");
    assert_eq!(opened.digest(), &DigestOutcome::Ingested);
    opened.close().expect("close");
    assert_eq!(native_digest(&made.path), want);
    assert_eq!(verify(&made.path, &keys, &made.store_id), 2);
    record(TEST, "reference Python", "i", 1, &[], &[]);
}
