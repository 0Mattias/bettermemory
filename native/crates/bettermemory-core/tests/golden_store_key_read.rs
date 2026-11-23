//! Compare retained keys and physical origin, head and warning behavior with Python.
mod common;
mod key_read_support;
use bettermemory_core::{
    log::Key,
    store::{
        key_read::{key_state_on, ring_fit, LogLevel},
        StoreFile,
    },
    unicode::cps,
};
use common::{parity::Ledger, stores::scratch};
use key_read_support::{selects, Signed};
use serde_json::json;
use std::{fs, sync::atomic::Ordering};

#[test]
fn classifies_reopened_signed_stores() {
    let root = scratch("physical-key-states");
    let first = Signed::new(&root.0.join("first"));
    let independent = Signed::new(&root.0.join("independent"));
    let backup = root.0.join("backup.sqlite");
    fs::copy(&first.path, &backup).unwrap();
    let name = "store_key_read.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("classifies_reopened_signed_stores", name);
    for group in rows.chunk_by(|a, b| a["mode"] == b["mode"]) {
        let mode = group[0]["mode"].as_str().unwrap();
        let ring = first.copy_ring(&root.0.join(mode).join("keys"));
        let target = match mode {
            "backup" => &backup,
            "unclaimed" => &independent.path,
            _ => &first.path,
        };
        let mut expected = first.named.clone();
        match mode {
            "empty" => {
                for item in fs::read_dir(ring.keys_dir()).unwrap() {
                    fs::remove_file(item.unwrap().path()).unwrap();
                }
            }
            "unbound" | "unclaimed" => fs::remove_file(ring.binding_path()).unwrap(),
            "wrong-expected" => expected = "0".repeat(64),
            "original" | "backup" => {}
            other => panic!("{other}"),
        }
        let store = StoreFile::open(target).unwrap();
        let conn = store.connection();
        for row in group {
            let joined = row["joined"].as_bool().unwrap();
            if joined {
                conn.execute_batch("BEGIN").unwrap();
            }
            ledger.case(format!("{mode}-{joined}-{}", row["warn"]), || {
                let mut logs = Vec::new();
                let physical = key_state_on(
                    conn,
                    &ring,
                    target,
                    &cps(&expected),
                    row["warn"].as_bool().unwrap(),
                    |record| {
                        logs.push(match record.level {
                            LogLevel::Debug => "DEBUG",
                            LogLevel::Info => "INFO",
                            LogLevel::Warning => "WARNING",
                        });
                        Ok::<_, ()>(())
                    },
                )
                .unwrap();
                let s = &physical.state;
                let actual = json!([
                    s.state.name(),
                    s.held().as_deref() == Some(first.named.as_str()),
                    s.bound_to.as_deref() == Some(&first.path),
                    s.unfit.is_some(),
                    logs,
                    !conn.is_autocommit()
                ]);
                assert_eq!(actual, row["result"]);
            });
            if joined {
                conn.execute_batch("ROLLBACK").unwrap();
            }
        }
    }
    ledger.finish(rows.len());
}

#[test]
fn reads_the_held_connection_and_physical_checkpoints() {
    let root = scratch("physical-key-fits");
    let first = Signed::new(&root.0.join("owner"));
    let independent = Signed::new(&root.0.join("independent"));
    let name = "store_key_read_signed.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("reads_the_held_connection_and_physical_checkpoints", name);
    for group in rows.chunk_by(|a, b| a["mode"] == b["mode"]) {
        let mode = group[0]["mode"].as_str().unwrap();
        let ring = first.copy_ring(&root.0.join(mode));
        match mode {
            "current" => {}
            "stale" => fs::write(ring.head_path(), &first.first_head).unwrap(),
            "no-head" => fs::remove_file(ring.head_path()).unwrap(),
            "no-origin" => fs::remove_file(ring.origin_path()).unwrap(),
            "no-key" => fs::remove_file(ring.current_path()).unwrap(),
            "retired" => fs::rename(
                ring.current_path(),
                ring.keys_dir()
                    .join(format!("{}.ordinary.key", ring.store_id())),
            )
            .unwrap(),
            _ => panic!("{mode}"),
        }
        for row in group {
            let target = if row["target"] == "owner" {
                &first.path
            } else {
                &independent.path
            };
            let mut snapshot = rusqlite::Connection::open_with_flags(
                target,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )
            .unwrap();
            let conn = &mut snapshot;
            conn.execute_batch("BEGIN").unwrap();
            let count = selects(conn);
            let other = Key::new(b"other".to_vec());
            let held = match row["hold"].as_str().unwrap() {
                "file" => None,
                "retained" => Some(&first.current),
                "different" => Some(&other),
                s => panic!("{s}"),
            };
            ledger.case(format!("{mode}/{}/{}", row["target"], row["hold"]), || {
                let f = ring_fit(conn, &ring, held).unwrap();
                assert_eq!(
                    json!([
                        f.origin.status,
                        f.origin.reason,
                        f.origin.signer.as_deref() == Some(&first.named),
                        f.head_status.name(),
                        f.log_end.as_ref().unwrap().seq,
                        f.fits(),
                        count.load(Ordering::Relaxed),
                        !conn.is_autocommit()
                    ]),
                    row["result"]
                );
            });
        }
    }
    ledger.finish(rows.len());
}
