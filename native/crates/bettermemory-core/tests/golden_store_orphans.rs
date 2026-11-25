//! `store::create::store_orphans` (`Store.store_orphans`) against
//! `store_orphans.jsonl`: for every case, the same meta value (a text, a
//! value of another SQL type, or no row) and the same files, directories
//! and links beside the store, then the orphans found in order, or the
//! class of what was raised.
//!
//! POSIX only, as the fixture is (a name is plain by the host's path
//! rules); `store_orphans_not_posix.rs` records the gap.

#![cfg(unix)]

mod common;

use std::fs;
use std::path::Path;

use bettermemory_core::store::create::store_orphans;
use bettermemory_core::store::StoreError;
use common::parity::Ledger;
use common::stores::scratch;
use rusqlite::Connection;
use serde_json::{json, Value};

fn text_as(name: &str, points: &[u32]) -> Value {
    match points
        .iter()
        .map(|&p| char::from_u32(p))
        .collect::<Option<String>>()
    {
        Some(text) => json!({ name: text }),
        None => json!({ format!("{name}_cps"): points }),
    }
}

fn names(case: &Value, key: &str) -> Vec<String> {
    case.get(key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .map(|n| n.as_str().expect("name").to_owned())
                .collect()
        })
        .unwrap_or_default()
}

fn run(case: &Value) -> Result<(), String> {
    let name = case["case"].as_str().expect("case");
    let dir = scratch("orphans");
    let home = dir.0.join("home");
    fs::create_dir(&home).expect("home");
    for outer in names(case, "outer") {
        fs::write(dir.0.join(outer), b"").expect("outer");
    }
    for d in names(case, "dirs") {
        fs::create_dir(home.join(d)).expect("dir");
    }
    for f in names(case, "files") {
        fs::write(home.join(f), b"").expect("file");
    }
    if let Some(links) = case.get("links").and_then(Value::as_object) {
        for (link, target) in links {
            std::os::unix::fs::symlink(target.as_str().expect("target"), home.join(link))
                .expect("link");
        }
    }
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        .expect("meta");
    if let Some(value) = case.get("value").and_then(Value::as_str) {
        conn.execute(
            "INSERT INTO meta VALUES ('created_after_orphans', ?)",
            [value],
        )
        .expect("value");
    } else if let Some(sql) = case.get("sql").and_then(Value::as_str) {
        conn.execute_batch(&format!(
            "INSERT INTO meta VALUES ('created_after_orphans', {sql})"
        ))
        .expect("value");
    }
    let got = match store_orphans(&conn, &home.join("memory.sqlite")) {
        Ok(found) => {
            let found: Vec<Value> = found
                .iter()
                .map(|p| {
                    let relative = p.strip_prefix(&home).expect("beside the store");
                    text_as(
                        "name",
                        &bettermemory_core::fsutil::fsdecode(relative.as_os_str()),
                    )
                })
                .collect();
            json!({ "found": found })
        }
        Err(e) => {
            let class = match e {
                StoreError::NotUtf8 { .. } | StoreError::Sqlite(_) => "OperationalError",
                StoreError::Py(_) => "ValueError",
                _ => "other",
            };
            json!({ "raises": { "class": class } })
        }
    };
    let want = match case.get("found") {
        Some(found) => json!({ "found": found }),
        None => json!({ "raises": case["raises"] }),
    };
    if got != want {
        return Err(format!("{name}: {got}, the reference {want}"));
    }
    Ok(())
}

#[test]
fn every_case_finds_the_orphans_the_reference_finds() {
    let mut lines = common::jsonl("store_orphans.jsonl");
    let header = lines.remove(0);
    assert_eq!(header["fixture"], "store_orphans");
    let failures: Vec<String> = lines.iter().filter_map(|c| run(c).err()).collect();
    assert!(!lines.is_empty(), "no case checked");
    common::assert_count("store_orphans.jsonl", lines.len(), lines.len());
    Ledger::new(
        "every_case_finds_the_orphans_the_reference_finds",
        "store_orphans.jsonl",
    )
    .settle(lines.len(), &failures);
}

/// A counter past any machine integer sorts by its value, as `int()`
/// reads it (a name holds at most 255 bytes, so `int()`'s 4300-digit
/// limit is never reached here).
#[test]
fn counters_past_a_machine_integer_sort_by_value() {
    let dir = scratch("orphans-digits");
    let home = dir.0.join("home");
    fs::create_dir(&home).expect("home");
    let name = |digits: &str| format!("memory.sqlite.orphaned-x-{digits}.sqlite");
    let (long, longer, padded) = (
        name(&"9".repeat(199)),
        name(&format!("1{}", "0".repeat(199))),
        name(&format!("{}2", "0".repeat(190))),
    );
    for n in [&long, &longer, &padded] {
        fs::write(home.join(n), b"").expect("file");
    }
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        .expect("meta");
    conn.execute(
        "INSERT INTO meta VALUES ('created_after_orphans', ?)",
        [json!([&longer, &long, &padded]).to_string()],
    )
    .expect("value");
    let found = store_orphans(&conn, &home.join("memory.sqlite")).expect("found");
    assert_eq!(
        found,
        vec![home.join(&padded), home.join(&long), home.join(&longer)]
    );
    assert!(Path::new(&home.join(&long)).is_file());
}
