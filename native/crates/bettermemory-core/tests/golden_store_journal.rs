//! Every call of `store_journal.jsonl` against `store::journal`: each
//! case's rows inserted into an in-memory store made from the header's
//! schema, then each read compared with the reference's answer.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::records::Episode;
use bettermemory_core::store::journal::{self, JournalError};
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use rusqlite::Connection;
use serde_json::{json, Value};

fn store(header: &Value, rows: &Value) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(header["schema"].as_str().unwrap())
        .unwrap();
    let columns: Vec<&str> = header["columns"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c.as_str().unwrap())
        .collect();
    let marks = vec!["?"; columns.len()].join(", ");
    let sql = format!(
        "INSERT INTO episodes ({}) VALUES ({marks})",
        columns.join(", ")
    );
    for row in rows.as_array().unwrap() {
        let values: Vec<rusqlite::types::Value> = row
            .as_array()
            .unwrap()
            .iter()
            .map(|v| match v {
                Value::Null => rusqlite::types::Value::Null,
                Value::Number(n) => rusqlite::types::Value::Integer(n.as_i64().unwrap()),
                Value::String(s) => rusqlite::types::Value::Text(s.clone()),
                other => panic!("a column {other}"),
            })
            .collect();
        conn.execute(&sql, rusqlite::params_from_iter(values))
            .unwrap();
    }
    conn
}

fn episodes(list: &[Episode]) -> Value {
    Value::Array(
        list.iter()
            .map(|e| common::rows::value(&e.dump()))
            .collect(),
    )
}

fn raised(e: &JournalError) -> Value {
    let arg = match e {
        JournalError::NotFound(id) => json!(id),
        _ => Value::Null,
    };
    json!({"raised": e.class(), "arg": arg})
}

fn answer(conn: &Connection, call: &Value) -> Value {
    let a = call["a"].as_str();
    let ttl = call["ttl"]
        .as_i64()
        .unwrap_or(journal::DEFAULT_EPISODE_TTL_DAYS);
    let now = call["now"]
        .as_str()
        .map(|t| DateTime::from_isoformat(&unicode::cps(t)).unwrap());
    let got = match call["do"].as_str().unwrap() {
        "get" => journal::get_episode(conn, a.unwrap()).map(|e| episodes(&[e])[0].clone()),
        "iter" => journal::iter_episodes(conn, a).map(|l| episodes(&l)),
        "count" => journal::count_episodes(conn).map(|n| json!(n)),
        "session" => journal::episodes_by_session(conn, a.unwrap()).map(|l| episodes(&l)),
        "swarm" => journal::episodes_by_swarm(conn, a.unwrap()).map(|l| episodes(&l)),
        "ids" => journal::episode_session_ids(conn).map(|l| json!(l)),
        "prunable" => journal::prunable_episode_sessions(conn, ttl, now).map(|l| json!(l)),
        "volume" => journal::episode_volume(conn, ttl, now).map(|v| {
            json!({
                "sessions": v.sessions,
                "episodes": v.episodes,
                "bytes": v.bytes,
                "prunable_sessions": v.prunable_sessions,
                "ttl_days": v.ttl_days,
            })
        }),
        other => panic!("a call {other}"),
    };
    got.unwrap_or_else(|e| raised(&e))
}

#[test]
fn store_journal_every_call() {
    let name = "store_journal.jsonl";
    let lines = jsonl(name);
    let header = &lines[0];
    let mut ledger = Ledger::new("golden_store_journal", name);
    let mut in_file = 0;
    for case in &lines[1..] {
        let conn = store(header, &case["rows"]);
        for (i, call) in case["calls"].as_array().unwrap().iter().enumerate() {
            in_file += 1;
            let label = format!("{} call {i} {}", case["case"], call["do"]);
            let got = answer(&conn, call);
            ledger.case(&label, || {
                assert_eq!(got, call["r"], "{label}");
            });
        }
    }
    ledger.finish(in_file);
}
