//! The store fixtures of `store_bases.json` and `store_files.jsonl`,
//! rebuilt with SQLite into scratch directories under the target dir: a
//! base's statements, its journal mode and its keys directory, then a
//! case's recipe. Test-only writes; no reference Python.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use rusqlite::Connection;
use serde_json::Value;

use super::{golden_path, jsonl, unhex};

pub fn bases() -> Value {
    let text = fs::read_to_string(golden_path("store_bases.json")).expect("store_bases.json");
    serde_json::from_str(&text).expect("JSON")
}

pub fn store_id() -> String {
    bases()["store_id"].as_str().expect("store_id").to_owned()
}

/// The header and the cases of `store_files.jsonl`.
pub fn cases() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_files.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// A fresh empty directory under the target dir; removed by [`Scratch`]'s
/// drop.
pub struct Scratch(pub PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

pub fn scratch(name: &str) -> Scratch {
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!(
        "stores-{}-{}-{name}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).expect("scratch");
    Scratch(fs::canonicalize(&dir).expect("dir"))
}

/// Write a base as `case/memory.sqlite` and `case/keys`.
pub fn rebuild(base: &Value, case: &Path) {
    fs::create_dir_all(case.join("keys")).expect("keys dir");
    let conn = Connection::open(case.join("memory.sqlite")).expect("create");
    conn.execute_batch(&format!(
        "PRAGMA page_size = {}",
        base["page_size"].as_i64().expect("page_size")
    ))
    .expect("page size");
    for statement in base["statements"].as_array().expect("statements") {
        conn.execute_batch(statement.as_str().expect("sql"))
            .unwrap_or_else(|e| panic!("{e}: {statement}"));
    }
    let mode = base["journal_mode"].as_str().expect("journal_mode");
    conn.query_row(&format!("PRAGMA journal_mode = {mode}"), [], |_| Ok(()))
        .expect("journal mode");
    drop(conn);
    let id = store_id();
    for (suffix, hex) in base["keys"].as_object().expect("keys") {
        fs::write(
            case.join("keys").join(format!("{id}{suffix}")),
            unhex(hex.as_str().expect("hex")),
        )
        .expect("key file");
    }
}

/// Apply a case's recipe to its rebuilt base.
pub fn apply(recipe: &[Value], case: &Path) {
    let id = store_id();
    for op in recipe {
        if let Some(sql) = op.get("sql") {
            let conn = Connection::open(case.join("memory.sqlite")).expect("open");
            conn.execute_batch(sql.as_str().expect("sql"))
                .expect("recipe sql");
        } else if let Some(pattern) = op.get("unlink") {
            let pattern = pattern.as_str().expect("suffix");
            for entry in fs::read_dir(case.join("keys")).expect("keys") {
                let name = entry
                    .expect("entry")
                    .file_name()
                    .to_string_lossy()
                    .into_owned();
                let Some(rest) = name.strip_prefix(&id) else {
                    continue;
                };
                // `glob(id + suffix)`: the suffix is literal or `.*.key`.
                let hit = if pattern == ".*.key" {
                    rest.len() >= 5 && rest.starts_with('.') && rest.ends_with(".key")
                } else {
                    rest == pattern
                };
                if hit {
                    fs::remove_file(case.join("keys").join(&name)).expect("unlink");
                }
            }
        } else if let Some(suffix) = op.get("write") {
            fs::write(
                case.join("keys")
                    .join(format!("{id}{}", suffix.as_str().expect("suffix"))),
                unhex(op["hex"].as_str().expect("hex")),
            )
            .expect("write");
        } else if let Some(n) = op.get("truncate") {
            let file = fs::OpenOptions::new()
                .write(true)
                .open(case.join("memory.sqlite"))
                .expect("open");
            file.set_len(n.as_u64().expect("length")).expect("truncate");
        } else {
            panic!("unknown recipe op {op}");
        }
    }
}

/// A case's store, rebuilt under `dir`.
pub fn materialize(case: &Value, dir: &Path) {
    let bases = bases();
    let base = case["base"].as_str().expect("base");
    if base == "rollback_journal_base" {
        rebuild(&bases["bases"]["intact"], dir);
        apply(
            &[serde_json::json!({"sql": "PRAGMA journal_mode = DELETE;"})],
            dir,
        );
    } else {
        rebuild(&bases["bases"][base], dir);
    }
    apply(case["recipe"].as_array().expect("recipe"), dir);
}

/// The case named `name`, rebuilt in a scratch directory.
pub fn case_store(name: &str) -> (Scratch, Value) {
    let (_, cases) = cases();
    let case = cases
        .into_iter()
        .find(|c| c["case"] == name)
        .unwrap_or_else(|| panic!("no case {name}"));
    let dir = scratch(name);
    materialize(&case, &dir.0);
    (dir, case)
}
