//! The writing open's side of the restore lock against the `admit` rows of
//! `store_restore_lock.json` and `store_restore_lock_python.json` (the
//! marker nested 70,000 lists deep, gated on the Python release)
//! (`restore_lock.admit`, its `confirm`, and
//! `restore_state`): each marker shape, a helper holding the store's
//! directory (here an open file of this process, which `flock` sets
//! against this one's as it does another process's), an unreadable
//! marker, a missing directory, and the record mode with a store file
//! swapped and a marker left between `admit` and `confirm`. POSIX only,
//! as the fixture; `restore_lock_not_posix.rs` records the gap.

#![cfg(unix)]

mod common;

use std::fs::{self, File};
use std::path::Path;
use std::time::Duration;

use bettermemory_core::store::restore_lock::{
    admit, admit_in_record_mode, describe, lock_path, restore_state, restore_state_in_record_mode,
    Admission, AdmitError, Marker, State,
};
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::stores::scratch;
use serde_json::Value;

const FILE: &str = "store_restore_lock.json";
const FILE_PYTHON: &str = "store_restore_lock_python.json";
const WAIT: Duration = Duration::from_millis(200);

fn marker_bytes(pieces: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    for piece in pieces.as_array().expect("pieces") {
        let bytes = common::unhex(piece[0].as_str().expect("hex"));
        for _ in 0..piece[1].as_u64().expect("count") {
            out.extend_from_slice(&bytes);
        }
    }
    out
}

fn keys(marker: &Option<Marker>) -> Value {
    match marker {
        None => Value::Null,
        Some(m) => {
            let mut k: Vec<String> = m
                .iter()
                .map(|(k, _)| match k {
                    bettermemory_core::pyvalue::PyValue::Str(s) => {
                        s.iter().filter_map(|&c| char::from_u32(c)).collect()
                    }
                    other => panic!("key {other:?}"),
                })
                .collect();
            k.sort();
            Value::from(k)
        }
    }
}

fn refusal(e: &AdmitError, store: &Path) -> Value {
    match e {
        AdmitError::Recursion => serde_json::json!({"refused": true, "error": "RecursionError"}),
        AdmitError::Locked(l) => {
            let lock = lock_path(store);
            let message = l
                .to_string()
                .replace(&lock.display().to_string(), "<lock>")
                .replace(&store.display().to_string(), "<store>");
            serde_json::json!({
                "refused": true,
                "error": "RestoreLockedError",
                "message": message,
                "running": l.running,
                "marker_keys": keys(&l.marker),
            })
        }
    }
}

fn state_row(got: Result<(State, Option<Marker>), AdmitError>) -> Value {
    match got {
        Err(_) => serde_json::json!({"error": "RecursionError"}),
        Ok((state, record)) => serde_json::json!({
            "state": state.name(),
            "marker_keys": keys(&record),
            "described": record.as_ref().map(|m| {
                describe(m).iter().filter_map(|&c| char::from_u32(c)).collect::<String>()
            }),
        }),
    }
}

fn run(case: &Value) {
    let name = case["name"].as_str().expect("name");
    let root = scratch("admit");
    let store_dir = root.0.join("store");
    let store = store_dir.join("memory.sqlite");
    let lock = lock_path(&store);
    let record_mode = case["record_mode"].as_bool().expect("record_mode");
    if !case["missing_dir"].as_bool().expect("missing_dir") {
        fs::create_dir(&store_dir).expect("dir");
        fs::write(&store, b"").expect("store");
    }
    if !case["marker"].is_null() {
        fs::write(&lock, marker_bytes(&case["marker"])).expect("marker");
    }
    if case["lock_dir"].as_bool().expect("lock_dir") {
        fs::create_dir(&lock).expect("lock dir");
    }
    let helper = case["helper"].as_str().map(|mode| {
        let f = File::open(if record_mode { &lock } else { &store_dir }).expect("helper");
        if mode == "shared" {
            f.try_lock_shared().expect("helper shared");
        } else {
            f.try_lock().expect("helper exclusive");
        }
        f
    });
    let mine = |_| false;
    let admitted = if record_mode {
        admit_in_record_mode(&store, WAIT, mine)
    } else {
        admit(&store, WAIT, mine)
    };
    let mut row = serde_json::Map::new();
    match admitted {
        Err(e) => {
            row.insert("admit".into(), refusal(&e, &store));
        }
        Ok(hold) => {
            let kind = match hold {
                Admission::Shared(_) => "shared",
                Admission::Record(_) => "record",
                Admission::None => "none",
            };
            row.insert(
                "admit".into(),
                serde_json::json!({"refused": false, "admission": kind}),
            );
            if case["swap"].as_bool().expect("swap") {
                let fresh = store_dir.join("fresh.sqlite");
                fs::write(&fresh, b"").expect("fresh");
                fs::rename(&fresh, &store).expect("swap");
            }
            if case["marker_after"].as_bool().expect("marker_after") {
                fs::write(&lock, b"{\"stage\": \"late\"}").expect("late marker");
            }
            if !matches!(hold, Admission::None) {
                let confirmed = match hold.confirm() {
                    Ok(()) => serde_json::json!({"refused": false}),
                    Err(e) => refusal(&e, &store),
                };
                row.insert("confirm".into(), confirmed);
            }
            drop(hold);
        }
    }
    let state = if record_mode {
        restore_state_in_record_mode(&store, WAIT, mine)
    } else {
        restore_state(&store, WAIT, mine)
    };
    row.insert("restore_state".into(), state_row(state));
    drop(helper);
    for key in ["admit", "confirm", "restore_state"] {
        assert_eq!(
            row.get(key).unwrap_or(&Value::Null),
            &case[key],
            "{name}: {key}"
        );
    }
}

fn admit_rows(test: &str, file: &str) {
    let text = fs::read_to_string(common::golden_path(file)).expect("file");
    let doc: Value = serde_json::from_str(&text).expect("JSON");
    let cases = doc["admit"].as_array().expect("admit rows");
    let mut ledger = Ledger::new(test, file);
    for case in cases {
        ledger.case(case["name"].as_str().expect("name"), || run(case));
    }
    assert!(!cases.is_empty());
    ledger.finish(cases.len());
}

#[test]
fn writing_opens_meet_the_restore_lock_as_the_reference() {
    admit_rows("writing_opens_meet_the_restore_lock_as_the_reference", FILE);
}

#[test]
fn a_deep_marker_meets_the_restore_lock_as_the_reference() {
    admit_rows(
        "a_deep_marker_meets_the_restore_lock_as_the_reference",
        FILE_PYTHON,
    );
}

/// `describe` of an empty marker, through `printable`.
#[test]
fn an_empty_marker_reads_as_no_record() {
    assert_eq!(describe(&Vec::new()), unicode::cps("no record of it reads"));
}
