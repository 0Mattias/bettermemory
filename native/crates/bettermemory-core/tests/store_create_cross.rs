//! A natively created store, taken up by the reference: `Store.open`
//! (the owner's writing open) verifies it ok, writes three records,
//! tombstones one and restores it; then the native check verifies the
//! result ok and the native readers read the records. Under the Python
//! `BM_REFERENCE_PYTHON` names, when it imports `bettermemory` (the parity
//! job's step 2 passes the project environment's); otherwise the test is
//! set aside, said in a PARITY line. The reference runs in a child process
//! with HOME and every BETTERMEMORY_* location inside the test's scratch
//! directory.

mod common;

use bettermemory_core::log::KeyRing;
use bettermemory_core::store::create::{self, Fixed};
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::{StoreError, StoreFile};
use common::created::{fixed, fixture, reference_python, run_reference};
use common::parity::record;
use common::stores::scratch;

const TEST: &str = "a_native_store_takes_reference_writes";

const SCRIPT: &str = r#"
import json, sys
from datetime import datetime, timezone
from bettermemory.models import Confidence, Memory, Source
from bettermemory.store import Store

store_file, keys = sys.argv[1], sys.argv[2]
store = Store.open(store_file, keys_dir=keys)
first = store.log_verify()["status"]
now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
ids = []
for i, body in enumerate(["Native stores open here.", "The second record.", "A third one."]):
    m = Memory(id=f"01J{i:023d}", created=now, updated=now, scopes=["native"],
               confidence=Confidence.HIGH, source=Source.EXPLICIT, body=body)
    store.put_memory(m)
    ids.append(m.id)
store.tombstone(ids[1], "a test removes it")
store.restore(ids[1])
after = store.log_verify()["status"]
store.close()
print(json.dumps({"first": first, "after": after, "ids": ids}))
"#;

fn loader(
    keys: std::path::PathBuf,
    id: String,
) -> impl FnMut(&StoreFile) -> Result<(KeyRing, String), StoreError> {
    move |s: &StoreFile| {
        let fp = s
            .meta()?
            .into_iter()
            .find(|(k, _)| k == "key_fingerprint")
            .map(|(_, v)| v)
            .unwrap_or_default();
        let ring = load_key_ring(&keys, &id).map_err(|e| StoreError::Refused(format!("{e:?}")))?;
        Ok((ring, fp))
    }
}

#[test]
fn a_native_store_takes_reference_writes() {
    let python = match reference_python() {
        Ok(p) => p,
        Err(why) => {
            eprintln!("NOT RUN {TEST}: {why}");
            record(TEST, "reference Python", "i", 0, &[TEST.to_owned()], &[why]);
            return;
        }
    };
    let dir = scratch("cross");
    let root = &dir.0;
    let mut provider: Fixed = fixed(&fixture());
    // A clock of now, so the reference's own rows come after it.
    provider.clock = vec![create::system_now()];
    let made =
        create::create(&root.join("store"), &root.join("keys"), &mut provider).expect("created");
    let out = run_reference(
        &python,
        &root.join("home"),
        SCRIPT,
        &[&made.path, &root.join("keys")],
    );
    assert!(
        out.status.success(),
        "the reference failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let said: serde_json::Value =
        serde_json::from_slice(out.stdout.trim_ascii()).expect("the reference's JSON");
    eprintln!("reference ({}): {said}", python.display());
    assert_eq!(
        said["first"], "ok",
        "the reference verifies the native store"
    );
    assert_eq!(said["after"], "ok");
    let store = StoreFile::open(&made.path).expect("native open");
    let report = store
        .log_verify_loading(&mut loader(root.join("keys"), made.store_id.clone()))
        .expect("native verify");
    assert_eq!(report.status, "ok", "{report:?}");
    let mut ids = Vec::new();
    store
        .each_memory(&mut |row| ids.push(row.id.clone()))
        .expect("memories");
    ids.sort();
    let want: Vec<String> = said["ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect();
    assert_eq!(ids, want);
    let mut tombstones = 0;
    store
        .each_tombstone(&mut |_| tombstones += 1)
        .expect("tombstones");
    assert_eq!(tombstones, 0, "the restored record is active again");
    record(TEST, "reference Python", "i", 1, &[], &[]);
}
