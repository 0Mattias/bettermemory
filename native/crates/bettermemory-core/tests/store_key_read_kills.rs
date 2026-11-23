//! Authentication order, held transactions and synchronous rotation notifications.
mod common;
mod key_read_support;
use bettermemory_core::{
    log::fingerprint,
    store::{
        key_read::{key_state_on, ring_fit, LogLevel, ReadError},
        key_state::{KeyStatus, StateError},
        StoreFile,
    },
    unicode::{cps, to_string},
};
use common::{created, parity, stores::scratch};
use key_read_support::Signed;
use rusqlite::{
    hooks::{AuthAction, AuthContext, Authorization},
    Connection, OpenFlags,
};
use std::{
    fs,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
};

#[test]
fn each_sql_failure_stops_direct_fit_and_is_ignored_only_by_unclaimed() {
    let root = scratch("reader-existing-tx");
    let owner = Signed::new(&root.0);
    fs::remove_file(owner.ring.binding_path()).unwrap();
    for fail_at in 1..=3 {
        for joined in [false, true] {
            for direct in [false, true] {
                let mut conn =
                    Connection::open_with_flags(&owner.path, OpenFlags::SQLITE_OPEN_READ_ONLY)
                        .unwrap();
                if joined {
                    conn.execute_batch("BEGIN").unwrap();
                }
                let calls = Arc::new(AtomicUsize::new(0));
                let count = calls.clone();
                conn.authorizer(Some(move |ctx: AuthContext<'_>| {
                    if matches!(ctx.action, AuthAction::Select)
                        && count.fetch_add(1, Ordering::Relaxed) + 1 == fail_at
                    {
                        return Authorization::Deny;
                    }
                    Authorization::Allow
                }))
                .unwrap();
                if direct {
                    assert!(matches!(
                        ring_fit(&conn, &owner.ring, Some(&owner.current)),
                        Err(StateError::Read(
                            bettermemory_core::store::StoreError::Sqlite(_)
                        ))
                    ));
                } else {
                    let st = key_state_on(
                        &conn,
                        &owner.ring,
                        &owner.path,
                        &cps(&owner.named),
                        true,
                        |_| -> Result<(), ()> { panic!("a transient query failure does not warn") },
                    )
                    .unwrap();
                    assert_eq!(st.state.state, KeyStatus::Present);
                }
                assert_eq!(calls.load(Ordering::Relaxed), fail_at);
                assert_eq!(conn.is_autocommit(), !joined);
            }
        }
    }
}

#[test]
fn signed_anchor_and_checkpoint_are_read_in_source_order() {
    let root = scratch("reader-sidefile-order");
    let owner = Signed::new(&root.0);
    let conn = StoreFile::open(&owner.path).unwrap();

    let pending = owner.ring.current_path().with_extension("key.pending");
    let original = fs::read(owner.ring.origin_path()).unwrap();
    for (text, class, words) in [
        (
            ["[".repeat(12000), "]".repeat(12000)].concat(),
            "RecursionError",
            "JSON array",
        ),
        (
            [
                r#"{"nested":"#.repeat(12000),
                "null".to_owned(),
                "}".repeat(12000),
            ]
            .concat(),
            "RecursionError",
            "JSON object",
        ),
    ] {
        fs::write(owner.ring.origin_path(), text).unwrap();
        fs::write(owner.ring.head_path(), r#"{"seq":Infinity}"#).unwrap();
        let error = ring_fit(conn.connection(), &owner.ring, None).unwrap_err();
        assert!(
            matches!(error,StateError::Raised {class:c,message} if c==class && to_string(&message).unwrap().contains(words))
        );
    }
    fs::write(owner.ring.origin_path(), original).unwrap();
    let error = ring_fit(conn.connection(), &owner.ring, None).unwrap_err();
    assert!(
        matches!(error,StateError::Raised{class:"OverflowError",message} if to_string(&message).unwrap()=="cannot convert float infinity to integer")
    );
    fs::write(&pending, b"incomplete transaction").unwrap();
    let got = key_state_on(
        conn.connection(),
        &owner.ring,
        &owner.path,
        &cps(&owner.named),
        false,
        |_| -> Result<(), ()> { panic!("invalid pending file stays untouched") },
    )
    .unwrap();
    assert_eq!(got.state.state, KeyStatus::Present);

    assert_eq!(fs::read(&pending).unwrap(), b"incomplete transaction");
}

#[test]
fn legitimate_committed_rotation_completes_before_info_handler() {
    // The normal source harness below captures the same state after rotate_key.
    let root = scratch("reader-pending-complete");
    let owner = Signed::new(&root.0);
    let pending = owner.ring.current_path().with_extension("key.pending");
    let intended = [37u8; 32];
    let recorded = fingerprint(&intended);
    let retired = owner.ring.retired_path(&owner.named);
    fs::write(&pending, intended).unwrap();
    let conn = StoreFile::open(&owner.path).unwrap();
    conn.connection().execute_batch("BEGIN").unwrap();
    let error = key_state_on(
        conn.connection(),
        &owner.ring,
        &owner.path,
        &cps(&recorded),
        false,
        |info| {
            assert_eq!(info.level, LogLevel::Info);
            assert_eq!(
                info.format,
                "store %s: the key %s a rotation committed is now the current key"
            );
            assert_eq!(info.args[1], cps(&recorded[..16]));
            assert!(info.cause.is_none());
            assert!(!pending.exists());
            assert_eq!(fs::read(&retired).unwrap(), owner.current.bytes());
            assert_eq!(fs::read(owner.ring.current_path()).unwrap(), intended);
            Err("original handler outcome")
        },
    )
    .unwrap_err();
    assert!(matches!(
        error,
        ReadError::Logger("original handler outcome")
    ));
    assert!(!conn.connection().is_autocommit());
    let finished = key_state_on(
        conn.connection(),
        &owner.ring,
        &owner.path,
        &cps(&recorded),
        true,
        |_| -> Result<(), ()> { panic!("already finished") },
    )
    .unwrap();
    assert_eq!(finished.state.state, KeyStatus::Present);
    assert_eq!(finished.state.held().as_deref(), Some(recorded.as_str()));
}

#[test]
fn actual_supported_rotation_preserves_the_origin_signer() {
    let name = "actual_supported_rotation_preserves_the_origin_signer";
    let python = match created::reference_python() {
        Ok(p) => p,
        Err(why) => {
            parity::record(name, "live", "i", 0, &[name.to_owned()], &[why]);
            return;
        }
    };
    let root = scratch("reader-real-rotation");
    let home = root.0.join("home");
    let owner = Signed::new(&root.0.join("owner"));
    let script = r#"
import json,sys
from pathlib import Path
from bettermemory.key_lifecycle import rotate_key
from bettermemory.store import Store
from bettermemory.store_keys import ring_fit
path, keys, tracker = map(Path,sys.argv[1:])
result=rotate_key(path,keys_dir=keys,state_dir=tracker,apply=True)
assert result["status"]=="done", result.get("status")
with Store.open(path,keys_dir=keys,read_only=True) as reader:
    assert reader.key_state.key is not None
    fit=ring_fit(reader._conn,reader.keyring,current_key=reader.key_state.key)
    print(json.dumps([reader.key_fingerprint,fit.origin.signer,fit.head_status,fit.log_end.seq]))
"#;
    let status = created::run_reference(
        &python,
        &home,
        script,
        &[
            &owner.path,
            owner.ring.keys_dir(),
            &root.0.join("rotation-state"),
        ],
    );
    assert!(
        status.status.success(),
        "{}",
        String::from_utf8_lossy(&status.stderr)
    );
    let answer: serde_json::Value = serde_json::from_slice(&status.stdout).unwrap();
    let new_name = answer[0].as_str().unwrap();
    assert_ne!(new_name, owner.named);
    assert_eq!(answer[1], owner.named);
    let store = StoreFile::open(&owner.path).unwrap();
    let held = owner.ring.load_current().unwrap().unwrap();
    for k in [Some(&owner.current), Some(&held), None] {
        let fit = ring_fit(store.connection(), &owner.ring, k).unwrap();
        assert_eq!(fit.origin.signer.as_deref(), Some(owner.named.as_str()));
        assert_eq!(fit.head_status.name(), answer[2]);
        assert_eq!(fit.log_end.unwrap().seq, answer[3].as_i64().unwrap());
    }
    parity::record(name, "live", "i", 3, &[], &[]);
}

#[test]
fn a_failed_sql_text_decode_keeps_the_column_and_the_callers_transaction() {
    use bettermemory_core::log::Head;
    use bettermemory_core::pyvalue::PyValue;
    use bettermemory_core::store::key_state::head_status;
    let root = scratch("reader-text-decoding");
    let owner = Signed::new(&root.0);
    let conn = Connection::open_with_flags(&owner.path, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let cols = ["ts", "session", "kind", "payload", "prev_mac", "mac"];
    // A TEMP view injects text from another encoding into the caller's query
    // without changing any signed row or key file.
    conn.execute_batch("BEGIN").unwrap();
    let ring = owner.copy_ring(&root.0.join("unbound"));
    fs::remove_file(ring.binding_path()).unwrap();
    let mut pairs = Vec::new();
    for (idx, col) in cols.iter().enumerate() {
        let data = match idx {
            0 => vec![0xc3, 0xa9, 0xff, b'A'],
            1 => vec![b'X', 0, b'Y', 0xff],
            2 => [vec![b'Z'; 233], vec![0xff]].concat(),
            _ => vec![0xff],
        };
        let hex: String = data.iter().map(|b| format!("{b:02x}")).collect();
        let columns = cols
            .iter()
            .map(|&name| {
                if name == *col {
                    format!("CASE seq WHEN 1 THEN CAST(x'{hex}' AS TEXT) ELSE {name} END AS {name}")
                } else {
                    name.to_owned()
                }
            })
            .collect::<Vec<_>>()
            .join(",");
        conn.execute_batch(&format!(
            "CREATE TEMP VIEW log AS SELECT seq,{columns} FROM main.log"
        ))
        .unwrap();
        let error = ring_fit(&conn, &ring, Some(&owner.current)).unwrap_err();
        let StateError::Raised { class, message } = error else {
            panic!("unclassified decode error {error:?}")
        };
        assert_eq!(class, "OperationalError");
        let shown = to_string(&message).unwrap();
        assert!(shown.starts_with(&format!("Could not decode to UTF-8 column '{col}'")));
        assert!(shown.chars().count() <= 198);
        if idx == 0 {
            assert!(shown.ends_with("'���A'"));
        }
        if idx == 1 {
            assert!(shown.ends_with("'X'"));
        }
        if idx == 2 {
            assert_eq!(shown.chars().count(), 198);
        }
        let old = Head {
            seq: PyValue::int(1),
            mac: cps("checkpoint"),
        };
        let head_error = head_status(Some(&old), &conn).unwrap_err();
        assert!(
            matches!(head_error,StateError::Raised{class:"OperationalError",message:retrieved} if retrieved==message)
        );
        let observed = key_state_on(
            &conn,
            &ring,
            &owner.path,
            &cps(&owner.named),
            false,
            |_| -> Result<(), ()> { panic!("no warning") },
        )
        .unwrap();
        assert_eq!(observed.state.state, KeyStatus::Present);
        assert!(!conn.is_autocommit());
        conn.execute_batch("DROP VIEW temp.log").unwrap();
        pairs.push((col.to_string(), hex, shown));
    }
    conn.execute_batch("ROLLBACK").unwrap();
    let name = "a_failed_sql_text_decode_keeps_the_column_and_the_callers_transaction";
    if let Ok(python) = created::reference_python() {
        let input = root.0.join("decode.json");
        fs::write(&input, serde_json::to_vec(&pairs).unwrap()).unwrap();
        let script = r#"
import json,sqlite3,sys
source=sqlite3.connect(':memory:')
for col,raw,native in json.load(open(sys.argv[1],encoding='utf-8')):
    try: source.execute('SELECT CAST(? AS TEXT) AS '+col,(bytes.fromhex(raw),)).fetchone()
    except sqlite3.OperationalError as e:
        assert str(e)==native and getattr(e,'sqlite_errorcode',None) is None, (str(e),native)
    else: raise AssertionError(col)
source.close()
"#;
        let status = created::run_reference(&python, &root.0.join("reference"), script, &[&input]);
        assert!(
            status.status.success(),
            "{}",
            String::from_utf8_lossy(&status.stderr)
        );
        parity::record(name, "reference Python", "i", pairs.len(), &[], &[]);
    }
}
