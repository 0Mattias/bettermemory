//! The stores and records of `store_schema.jsonl` and
//! `store_schema_posix.jsonl` (`gen_store_schema`): a store made from the
//! reference's five schema texts with the fixture's store id, a case's
//! SQL, a step run with a provider that counts its draws, and what the
//! store holds after, spelled as the generator spells it.

use std::io;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::store::admission::reference_schema_json;
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::schema::{self, StepError};
use rusqlite::Connection;
use serde_json::{json, Value};

/// The reference's five schema texts.
pub fn texts() -> Vec<String> {
    let doc: Value = serde_json::from_str(reference_schema_json()).unwrap();
    doc["texts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t.as_str().unwrap().to_owned())
        .collect()
}

/// The generator's `_base`.
pub fn base(conn: &Connection) {
    for text in texts() {
        conn.execute_batch(&text).unwrap();
    }
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("membership_store_id", "cd".repeat(16)),
    )
    .unwrap();
}

pub fn master(conn: &Connection) -> Vec<(String, String)> {
    let mut s = conn
        .prepare("SELECT name, sql FROM sqlite_master WHERE name IS NOT NULL")
        .unwrap();
    s.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, Option<String>>(1)?.unwrap_or_default(),
        ))
    })
    .unwrap()
    .map(Result::unwrap)
    .collect()
}

/// The full schema's `sqlite_master`.
pub fn full() -> Vec<(String, String)> {
    let conn = Connection::open_in_memory().unwrap();
    base(&conn);
    master(&conn)
}

/// meta's rows as the generator's `_meta_of` reads them.
pub fn meta_of(conn: &Connection) -> Vec<(String, String)> {
    let Ok(mut s) = conn.prepare("SELECT key, value FROM meta") else {
        return Vec::new();
    };
    s.query_map([], |r| Ok((text(r.get_ref(0)?), text(r.get_ref(1)?))))
        .unwrap()
        .map(Result::unwrap)
        .collect()
}

fn text(v: rusqlite::types::ValueRef<'_>) -> String {
    bettermemory_core::store::sql_value(v).unwrap().py_str()
}

/// A provider whose membership id is the fixture's, counting draws.
pub struct Draws {
    pub minted: String,
    pub drawn: usize,
}

impl Provider for Draws {
    fn store_id(&mut self) -> io::Result<String> {
        unreachable!("no step draws a store id")
    }
    fn key(&mut self) -> io::Result<Key> {
        unreachable!("no step draws a key")
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        self.drawn += 1;
        Ok(self.minted.clone())
    }
    fn now(&mut self) -> io::Result<DateTime> {
        unreachable!("no step reads the clock")
    }
    fn engine_version(&self) -> String {
        unreachable!("no step reads the version")
    }
}

fn sorted(meta: &[(String, String)]) -> Value {
    let mut m = meta.to_vec();
    m.sort();
    json!(m.iter().map(|(k, v)| json!([k, v])).collect::<Vec<_>>())
}

/// Runs `step` and spells its outcome as the generator does: `drawn`,
/// `warnings`, `error`, `meta_out`.
pub fn run(conn: &Connection, step: &str, minted: &str) -> Value {
    let mut meta = meta_of(conn);
    let mut draws = Draws {
        minted: minted.to_owned(),
        drawn: 0,
    };
    let (warnings, got): (Vec<String>, Result<(), StepError>) = match step {
        "membership" => match schema::add_membership_tables(conn, "STORE", &mut meta, &mut draws) {
            Ok(w) => (w.into_iter().collect(), Ok(())),
            Err(e) => (Vec::new(), Err(e)),
        },
        "returned" => schema::add_returned_marks(conn, "STORE", &mut meta, &mut draws),
        "proposals" => schema::add_proposals_index(conn, "STORE", &mut meta, &mut draws),
        "session" => match schema::add_session_index(conn, "STORE") {
            Ok(w) => (w.into_iter().collect(), Ok(())),
            Err(e) => (Vec::new(), Err(e)),
        },
        "path_targets" => {
            schema::add_path_targets(conn, "STORE");
            (Vec::new(), Ok(()))
        }
        other => panic!("step {other}"),
    };
    let mut out =
        json!({"drawn": draws.drawn > 0, "meta_out": sorted(&meta), "warnings": warnings});
    match got {
        Ok(()) => {}
        Err(StepError::Sqlite { class, message }) => out["error"] = json!([class, message]),
        Err(e) => panic!("{e:?}"),
    }
    out
}

/// The generator's `_after`.
pub fn after(conn: &Connection, full: &[(String, String)]) -> Value {
    let now = master(conn);
    let names = |m: &[(String, String)]| m.iter().map(|(n, _)| n.clone()).collect::<Vec<_>>();
    let (had, has) = (names(full), names(&now));
    let mut lacks: Vec<&String> = had.iter().filter(|n| !has.contains(n)).collect();
    let mut extra: Vec<&String> = has.iter().filter(|n| !had.contains(n)).collect();
    lacks.sort();
    extra.sort();
    let sql = |m: &[(String, String)]| {
        m.iter()
            .find(|(n, _)| n == "checkouts")
            .map(|(_, s)| s.clone())
    };
    let checkouts = if sql(&now) == sql(full) {
        Value::Null
    } else {
        json!(sql(&now))
    };
    let meta_rows = conn
        .prepare("SELECT key, value FROM meta ORDER BY key")
        .ok()
        .map(|mut s| {
            s.query_map([], |r| {
                Ok(json!([text(r.get_ref(0)?), text(r.get_ref(1)?)]))
            })
            .unwrap()
            .map(Result::unwrap)
            .collect::<Vec<_>>()
        });
    json!({"lacks": lacks, "extra": extra, "checkouts": checkouts, "meta_rows": meta_rows})
}

/// The fields of a row this module spells.
pub fn wanted_fields(row: &Value) -> Value {
    let mut out = json!({});
    for k in [
        "drawn",
        "warnings",
        "error",
        "meta_out",
        "lacks",
        "extra",
        "checkouts",
        "meta_rows",
    ] {
        if let Some(v) = row.get(k) {
            out[k] = v.clone();
        }
    }
    out
}
