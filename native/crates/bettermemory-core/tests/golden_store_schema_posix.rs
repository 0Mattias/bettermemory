//! Every case of `store_schema_posix.jsonl` against `store::schema`: the
//! steps on a store file made read-only (mode 0444; the warnings, nothing
//! written) and `refuse_unmigrated_v8` over real folders through `StdFs`,
//! the scratch root spelled `<T>`. POSIX only: modes, and the message
//! spells the host's path.

#![cfg(unix)]

mod common;

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use bettermemory_core::fsdata::StdFs;
use bettermemory_core::store::schema;
use common::jsonl;
use common::parity::Ledger;
use common::schema_steps::{after, base, full, run, wanted_fields};
use rusqlite::Connection;
use serde_json::{json, Value};

const FILE: &str = "store_schema_posix.jsonl";

fn scratch() -> PathBuf {
    let root = Path::new(env!("CARGO_TARGET_TMPDIR"))
        .join(format!("store-schema-posix-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).unwrap();
    root.canonicalize().unwrap()
}

fn chmod(p: &Path, mode: u32) {
    fs::set_permissions(p, fs::Permissions::from_mode(mode)).unwrap();
}

fn read_only_row(root: &Path, i: usize, row: &Value, full: &[(String, String)]) -> Value {
    let path = root.join(format!("ro-{i}.sqlite"));
    let conn = Connection::open(&path).unwrap();
    base(&conn);
    for sql in row["setup"].as_array().unwrap() {
        conn.execute_batch(sql.as_str().unwrap()).unwrap();
    }
    drop(conn);
    chmod(&path, 0o444);
    assert!(
        fs::OpenOptions::new().write(true).open(&path).is_err(),
        "a mode-0444 file opened for writing: the read-only rows are a user's, not root's"
    );
    let conn = Connection::open(&path).unwrap();
    let mut got = run(&conn, row["step"].as_str().unwrap(), &"ab".repeat(16));
    for (k, v) in after(&conn, full).as_object().unwrap() {
        got[k] = v.clone();
    }
    got
}

fn v8_dir_row(root: &Path, row: &Value) -> Value {
    let named = row["path"].as_str().unwrap().strip_prefix("<T>/").unwrap();
    let top = root.join(named.split('/').next().unwrap());
    fs::create_dir(&top).unwrap();
    for entry in row["made"].as_array().unwrap() {
        let entry = entry.as_str().unwrap();
        match entry.strip_suffix('/') {
            Some(dir) => fs::create_dir(top.join(dir)).unwrap(),
            None => fs::write(top.join(entry), "").unwrap(),
        }
    }
    let unlistable = row["case"] == "unlistable";
    if unlistable {
        chmod(&top.join("sub"), 0o300);
    }
    let shown = root.display().to_string();
    let path = row["path"].as_str().unwrap().replace("<T>", &shown);
    let refused = schema::refuse_unmigrated_v8(&StdFs, &path);
    if unlistable {
        chmod(&top.join("sub"), 0o700);
    }
    json!(refused.map(|r| r.replace(&shown, "<T>")))
}

#[test]
fn store_schema_posix_every_case() {
    let mut lines = jsonl(FILE);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let ledger = Ledger::new("golden_store_schema_posix", FILE);
    let full = full();
    let root = scratch();
    let mut failures = Vec::new();
    for (i, row) in lines.iter().enumerate() {
        let (got, want) = match row["k"].as_str().unwrap() {
            "read_only" => (read_only_row(&root, i, row, &full), {
                let mut w = wanted_fields(row);
                w.as_object_mut().unwrap().remove("meta_in");
                w
            }),
            "v8_dir" => (v8_dir_row(&root, row), row["refused"].clone()),
            other => panic!("kind {other}"),
        };
        if got != want {
            failures.push(format!(
                "#{i} {}: native {got}\n reference {want}",
                row["case"]
            ));
        }
    }
    for entry in walk(&root) {
        chmod(&entry, 0o700);
    }
    let _ = fs::remove_dir_all(&root);
    assert!(!lines.is_empty());
    ledger.settle(lines.len(), &failures);
}

fn walk(dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for e in fs::read_dir(dir).into_iter().flatten().flatten() {
        let p = e.path();
        if p.is_dir() {
            out.extend(walk(&p));
        }
        out.push(p);
    }
    out
}
