//! A native create under `store_create.json`'s fixed values equals the
//! store the reference created under them: `sqlite_master`, the pragmas,
//! every table's rows (meta, the first log row whole, the digest's), the
//! key ring's names, texts and modes, the store directory; and each
//! refusal's class and what it leaves. Class (ii), Unicode: the
//! tokenizer's and digest's fingerprints read `unicodedata`.

mod common;

use std::fs;
use std::path::Path;

use bettermemory_core::lexicon;
use bettermemory_core::store::create::{self, CreateError};
use bettermemory_core::store::digest;
use common::created::{fixed, fixture, key_ring, mode_of, read_store, scrub};
use common::parity::Ledger;
use common::stores::scratch;
use serde_json::{json, Value};

const PRAGMAS: [&str; 8] = [
    "page_size",
    "journal_mode",
    "auto_vacuum",
    "encoding",
    "user_version",
    "application_id",
    "schema_version",
    "freelist_count",
];

/// `gen_store_create._created` of a native create.
fn native_created(orphan: bool) -> Value {
    let doc = fixture();
    let dir = scratch("created");
    let root = &dir.0;
    let store_dir = root.join("store");
    if orphan {
        fs::create_dir(&store_dir).expect("dir");
        fs::write(
            store_dir.join("memory.sqlite.orphaned-20010203T040506Z.sqlite"),
            b"",
        )
        .expect("orphan");
    }
    create::create(&store_dir, &root.join("keys"), &mut fixed(&doc)).expect("created");
    let file = store_dir.join("memory.sqlite");
    let mut listing: Vec<String> = fs::read_dir(&store_dir)
        .expect("dir")
        .map(|e| e.expect("entry").file_name().to_string_lossy().into_owned())
        .collect();
    listing.sort();
    let directory: Vec<Value> = listing
        .iter()
        .map(|n| {
            json!({"name": n, "mode": if n == "memory.sqlite" {
                mode_of(&file, "0600")
            } else {
                "(not the store's)".to_owned()
            }})
        })
        .collect();
    let (master, pragmas, tables) = read_store(&file, &PRAGMAS);
    json!({
        "store_dir_mode": mode_of(&store_dir, "0700"),
        "keys_dir_mode": mode_of(&root.join("keys"), "0700"),
        "store_dir": directory,
        "sqlite_master": master,
        "pragmas": pragmas,
        "tables": tables,
        "key_ring": key_ring(&root.join("keys"), root),
    })
}

/// Each part of a created store, compared on its own so a difference
/// names the part.
fn compare(ledger: &mut Ledger, case: &str, want: &Value, got: &Value) -> usize {
    let mut n = 0;
    let parts = want.as_object().expect("an object");
    for (part, value) in parts {
        if part == "tables" {
            let tables = value.as_object().expect("tables");
            assert_eq!(
                tables.keys().collect::<Vec<_>>(),
                got["tables"]
                    .as_object()
                    .expect("tables")
                    .keys()
                    .collect::<Vec<_>>(),
                "{case}: the tables"
            );
            for (name, rows) in tables {
                ledger.case(format!("{case} table {name}"), || {
                    assert_eq!(rows, &got["tables"][name], "{case}: table {name}")
                });
                n += 1;
            }
            continue;
        }
        if part == "sqlite_master" {
            let want_rows = value.as_array().expect("rows");
            let got_rows = got[part].as_array().expect("rows");
            assert_eq!(
                want_rows.len(),
                got_rows.len(),
                "{case}: sqlite_master rows"
            );
            for (w, g) in want_rows.iter().zip(got_rows) {
                ledger.case(format!("{case} sqlite_master {}", w[1]), || {
                    assert_eq!(w, g, "{case}: sqlite_master")
                });
                n += 1;
            }
            continue;
        }
        ledger.case(format!("{case} {part}"), || {
            assert_eq!(value, &got[part.as_str()], "{case}: {part}")
        });
        n += 1;
    }
    n
}

#[test]
fn a_native_create_equals_the_references() {
    let doc = fixture();
    let mut ledger = Ledger::new("a_native_create_equals_the_references", "store_create.json");
    let mut cases = 0;
    for (case, orphan) in [("created", false), ("created_after_orphans", true)] {
        cases += compare(&mut ledger, case, &doc[case], &native_created(orphan));
    }
    ledger.case("digest_schema", || {
        assert_eq!(doc["digest_schema"], json!(digest::DIGEST_SCHEMA))
    });
    ledger.case("digest_fingerprint", || {
        assert_eq!(doc["digest_fingerprint"], json!(digest::DIGEST_FINGERPRINT))
    });
    ledger.case("tokenizer_fingerprint", || {
        let meta = &doc["created"]["tables"]["meta"]["rows"];
        let found = meta
            .as_array()
            .expect("rows")
            .iter()
            .find(|r| r[1] == "tokenizer_fingerprint")
            .expect("row");
        assert_eq!(found[2], json!(lexicon::python_tokenizer_fingerprint()))
    });
    eprintln!("store_create.json: {} parts compared", cases + 3);
    ledger.finish(cases + 3);
    // No mode bits on Windows: the modes compared above are the ones asked
    // for, read back from nowhere.
    #[cfg(not(unix))]
    common::parity::record(
        "a_native_create_equals_the_references",
        "file modes",
        "os",
        0,
        &["the key ring's and the store's modes".to_owned()],
        &["Windows has no mode bits".to_owned()],
    );
}

fn listing(root: &Path) -> Vec<String> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for e in fs::read_dir(&dir).expect("dir").flatten() {
            let p = e.path();
            let rel = p
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .replace('\\', "/");
            if p.is_dir() {
                out.push(format!("{rel}/"));
                stack.push(p);
            } else {
                out.push(rel);
            }
        }
    }
    out.sort();
    out
}

fn errno_name(e: &CreateError) -> Option<&'static str> {
    match e {
        CreateError::Io { error, .. } => Some(match error.kind() {
            std::io::ErrorKind::AlreadyExists => "EEXIST",
            std::io::ErrorKind::NotFound => "ENOENT",
            std::io::ErrorKind::PermissionDenied => "EACCES",
            std::io::ErrorKind::NotADirectory => "ENOTDIR",
            _ => "?",
        }),
        _ => None,
    }
}

/// Each refusal the reference has for create, under the same condition:
/// the same class (and errno, or message), and the same entries left,
/// less the unusable store file the reference leaves when its keys
/// directory cannot be made (a deliberate difference: native leaves none).
#[test]
fn every_refusal_is_the_references() {
    let doc = fixture();
    let refusals = doc["refusals"].as_object().expect("refusals");
    let mut ledger = Ledger::new("every_refusal_is_the_references", "store_create.json");
    for (case, want) in refusals {
        let dir = scratch("refusal");
        let root = &dir.0;
        match case.as_str() {
            "store_file_exists" => {
                fs::create_dir(root.join("store")).unwrap();
                fs::write(root.join("store/memory.sqlite"), b"").unwrap();
            }
            "store_path_is_a_directory" => {
                fs::create_dir_all(root.join("store/memory.sqlite")).unwrap()
            }
            "store_parent_is_a_file" => fs::write(root.join("store"), b"").unwrap(),
            "keys_dir_is_a_file" => fs::write(root.join("keys"), b"").unwrap(),
            "restore_marker_beside" => {
                fs::create_dir(root.join("store")).unwrap();
                fs::write(
                    root.join("store/memory.sqlite.restore-lock"),
                    doc["marker"].as_str().expect("marker"),
                )
                .unwrap();
            }
            other => panic!("a refusal case this test cannot set up: {other}"),
        }
        let before = listing(root);
        assert_eq!(json!(before), want["before"], "{case}: setup");
        let err = create::create(&root.join("store"), &root.join("keys"), &mut fixed(&doc))
            .expect_err("refused");
        let left: Vec<String> = listing(root)
            .into_iter()
            .filter(|p| !before.contains(p))
            .collect();
        ledger.case(case, || {
            assert_eq!(json!(err.class()), want["class"], "{case}: {err}");
            if let Some(errno) = want.get("errno") {
                assert_eq!(json!(errno_name(&err)), *errno, "{case}: {err}");
            } else {
                assert_eq!(
                    json!(scrub(&err.to_string(), root)),
                    want["message"],
                    "{case}"
                );
            }
            let mut expected: Vec<String> = want["left"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
                .collect();
            if case == "keys_dir_is_a_file" {
                assert!(expected.contains(&"store/memory.sqlite".to_owned()));
                expected.retain(|p| p != "store/memory.sqlite");
            }
            assert_eq!(left, expected, "{case}: left");
        });
    }
    ledger.finish(refusals.len());
}

/// The digest's fingerprint is not computed natively (it reads the
/// reference's supersession rules); the constant is the committed
/// fixture's. A change to those rules, the tokenizer or the digest's
/// shape changes the reference's fingerprint.
#[test]
fn the_pinned_digest_fingerprint_is_the_committed_fixtures() {
    let text = fs::read_to_string(common::committed_path("store_create.json")).expect("fixture");
    let doc: Value = serde_json::from_str(&text).expect("JSON");
    assert_eq!(
        doc["digest_fingerprint"].as_str(),
        Some(digest::DIGEST_FINGERPRINT),
        "the reference's digest fingerprint changed (store_digest.fingerprint(): the \
         supersession rules, the tokenizer or the digest's shape). Port what changed, then \
         set store::digest::DIGEST_FINGERPRINT to the fixture's digest_fingerprint; a \
         store created with the old one has its digest rebuilt by the reference's next \
         writing open."
    );
}

/// What `gen_store_create._SIDECARS` sets up, natively.
fn stale_sidecars(case: &str, root: &Path) {
    let store = root.join("store");
    let other = root.join("other.sqlite");
    match case {
        "another_stores_live_wal" => {
            let conn = rusqlite::Connection::open(&other).unwrap();
            conn.query_row("PRAGMA journal_mode = WAL", [], |_| Ok(()))
                .unwrap();
            conn.execute_batch(
                "PRAGMA wal_autocheckpoint = 0;
                 CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
            )
            .unwrap();
            conn.execute("INSERT INTO meta VALUES ('store_id', ?)", ["f".repeat(32)])
                .unwrap();
            for suffix in ["-wal", "-shm"] {
                fs::copy(
                    root.join(format!("other.sqlite{suffix}")),
                    store.join(format!("memory.sqlite{suffix}")),
                )
                .unwrap();
            }
        }
        "junk_sidecars" => {
            for suffix in ["-wal", "-shm", "-journal"] {
                fs::write(
                    store.join(format!("memory.sqlite{suffix}")),
                    b"junk".repeat(64),
                )
                .unwrap();
            }
        }
        "hot_journal" => {
            let conn = rusqlite::Connection::open(&other).unwrap();
            conn.execute_batch(
                "PRAGMA journal_mode = DELETE; CREATE TABLE a (b BLOB);
                 INSERT INTO a VALUES (zeroblob(4000)); PRAGMA cache_size = 1; BEGIN;",
            )
            .unwrap();
            for _ in 0..64 {
                conn.execute("INSERT INTO a VALUES (zeroblob(4000))", [])
                    .unwrap();
            }
            fs::copy(
                root.join("other.sqlite-journal"),
                store.join("memory.sqlite-journal"),
            )
            .unwrap();
            conn.execute_batch("ROLLBACK").unwrap();
        }
        other => panic!("a sidecar case this test cannot set up: {other}"),
    }
}

/// Stale `-wal`, `-shm` and `-journal` at the store's name: after a native
/// create the directory holds what the reference's create leaves, and the
/// file that opens there is the new store (its id, `log_verify` ok).
#[test]
fn stale_sidecars_end_as_the_references() {
    use bettermemory_core::store::keys::load_key_ring;
    use bettermemory_core::store::{StoreError, StoreFile};
    let doc = fixture();
    let cases = doc["created_over_sidecars"].as_object().expect("cases");
    let mut ledger = Ledger::new("stale_sidecars_end_as_the_references", "store_create.json");
    for (case, want) in cases {
        let dir = scratch("sidecars");
        let root = &dir.0;
        fs::create_dir(root.join("store")).unwrap();
        stale_sidecars(case, root);
        let mut before: Vec<String> = fs::read_dir(root.join("store"))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        before.sort();
        assert_eq!(json!(before), want["before"], "{case}: setup");
        let made = create::create(&root.join("store"), &root.join("keys"), &mut fixed(&doc))
            .expect("created");
        let mut after: Vec<String> = fs::read_dir(root.join("store"))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        after.sort();
        let store = StoreFile::open(&made.path).expect("opens");
        let id = store
            .meta()
            .unwrap()
            .into_iter()
            .find(|(k, _)| k == "store_id")
            .map(|(_, v)| v);
        let keys = root.join("keys");
        let report = store
            .log_verify_loading(&mut |s: &StoreFile| {
                let fp = s
                    .meta()?
                    .into_iter()
                    .find(|(k, _)| k == "key_fingerprint")
                    .map(|(_, v)| v)
                    .unwrap_or_default();
                let ring = load_key_ring(&keys, &made.store_id)
                    .map_err(|e| StoreError::Refused(format!("{e:?}")))?;
                Ok((ring, fp))
            })
            .expect("verify");
        ledger.case(case, || {
            assert_eq!(json!(after), want["after_create"], "{case}");
            assert_eq!(json!(id), want["store_id"], "{case}");
            assert_eq!(json!(report.status), want["log_verify"], "{case}");
        });
    }
    ledger.finish(cases.len());
}
