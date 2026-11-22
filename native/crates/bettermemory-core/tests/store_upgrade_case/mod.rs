//! One case of `store_upgrade.jsonl` or `store_upgrade_posix.jsonl` run
//! as `gen_store_upgrade._run` runs it, with `store::upgrade` in place of
//! `_prove_exclusive`.

use std::path::Path;

use bettermemory_core::store::upgrade::{prove_exclusive, UpgradeError};
use rusqlite::Connection;
use serde_json::{json, Value};

fn one(conn: &Connection, sql: &str) -> Value {
    conn.query_row(sql, [], |r| r.get::<_, rusqlite::types::Value>(0))
        .map(|v| match v {
            rusqlite::types::Value::Integer(i) => json!(i),
            rusqlite::types::Value::Text(t) => json!(t),
            other => panic!("{other:?}"),
        })
        .unwrap()
}

/// What `_prove_exclusive` raised, as the fixture records it.
pub fn raised(e: &UpgradeError) -> Value {
    match e {
        UpgradeError::Version(v) => json!({"raised": "StoreVersionError", "message": v.message}),
        UpgradeError::Sqlite { class, error } => {
            let message = match error {
                rusqlite::Error::SqliteFailure(_, Some(m)) => m.clone(),
                rusqlite::Error::SqliteFailure(f, None) => f.to_string(),
                other => other.to_string(),
            };
            json!({"raised": class, "message": message})
        }
    }
}

/// The case run in `dir`: `r` and `after`.
pub fn run(case: &Value, dir: &Path) -> Value {
    let file = dir.join("memory.sqlite");
    let journal = case["journal"].as_str().unwrap();
    {
        let setup = Connection::open(&file).unwrap();
        one(&setup, &format!("PRAGMA journal_mode = {journal}"));
        setup.execute_batch("CREATE TABLE t (x INTEGER)").unwrap();
    }
    let conn = Connection::open(&file).unwrap();
    one(&conn, &format!("PRAGMA busy_timeout = {}", case["timeout"]));
    one(
        &conn,
        &format!("PRAGMA locking_mode = {}", case["mode"].as_str().unwrap()),
    );
    let other = case["other"].as_str().map(|kind| {
        let other = Connection::open(&file).unwrap();
        one(&other, "PRAGMA busy_timeout = 0");
        match kind {
            "reserved" => other.execute_batch("BEGIN IMMEDIATE").unwrap(),
            "exclusive" => other.execute_batch("BEGIN EXCLUSIVE").unwrap(),
            "read" => other.execute_batch("BEGIN").unwrap(),
            _ => {}
        }
        one(&other, "SELECT COUNT(*) FROM t");
        other
    });
    if case["own"].as_bool().unwrap() {
        conn.execute_batch("BEGIN; INSERT INTO t VALUES (1)")
            .unwrap();
    }
    let r = match prove_exclusive(&conn, case["name"].as_str().unwrap()) {
        Ok(()) => Value::Null,
        Err(e) => raised(&e),
    };
    let mut after = json!({
        "timeout": one(&conn, "PRAGMA busy_timeout"),
        "mode": one(&conn, "PRAGMA locking_mode"),
        "in_tx": !conn.is_autocommit(),
    });
    if let Some(other) = &other {
        if !other.is_autocommit() {
            other.execute_batch("ROLLBACK").unwrap();
        }
    }
    after["rows"] = one(&conn, "SELECT COUNT(*) FROM t");
    if let Some(other) = other {
        after["other_writes"] = json!(other.execute_batch("BEGIN IMMEDIATE; ROLLBACK").is_ok());
    }
    drop(conn);
    json!({"r": r, "after": after})
}

/// Every case of `name` against [`run`], each in its own folder.
pub fn every_case(test: &str, name: &str) {
    let lines = crate::common::jsonl(name);
    let mut ledger = crate::common::parity::Ledger::new(test, name);
    let base = crate::common::stores::scratch("upgrade");
    for (i, case) in lines[1..].iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        let got = run(case, &dir);
        let want = json!({"r": case["r"], "after": case["after"]});
        ledger.case(format!("{} {}", name, case["case"]), || {
            assert_eq!(got, want, "{}", case["case"]);
        });
    }
    ledger.finish(lines.len() - 1);
}
