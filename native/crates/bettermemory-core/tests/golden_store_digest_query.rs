//! Every case of `store_digest_query.jsonl` against `store::digest_query`:
//! `_mark` over a scripted `digest_state` and log, and the steps of
//! `digest_candidates` and `digest_shares` on one connection (rows sorted
//! by id, whether a warning came, `temp.digest_asked` afterwards); and
//! `store_digest_query_sqlite.jsonl`, the rows' order as SQLite returns
//! them, gated on the SQLite version.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::digest::{self, DIGEST_SCHEMA};
use bettermemory_core::store::digest_query::{digest_candidates, digest_shares, mark, QueryError};
use common::jsonl;
use common::parity::Ledger;
use rusqlite::{params, Connection};
use serde_json::{json, Value};

fn s(v: &Value) -> &str {
    v.as_str().unwrap()
}

/// An `int` as the golden file's JSON parser reads one: past 64 bits it
/// is the same float either side; the exact digits are a kill case.
fn int(v: &PyValue) -> Value {
    match v {
        PyValue::Int(t) => serde_json::from_str(t).unwrap(),
        other => panic!("not an int: {other:?}"),
    }
}

fn put(conn: &Connection, row: &[Value]) {
    let tokens = s(&row[6]);
    conn.execute(
        "INSERT INTO digest_records(doc, removed, id, n, claim, stamped, tokens) \
         VALUES (?, ?, ?, ?, ?, ?, ?)",
        params![
            row[0].as_i64().unwrap(),
            row[1].as_i64().unwrap(),
            s(&row[2]),
            row[3].as_i64().unwrap(),
            row[4].as_i64().unwrap(),
            row[5].as_i64().unwrap(),
            tokens
        ],
    )
    .unwrap();
    let held: BTreeSet<String> = tokens
        .split(' ')
        .filter(|t| !t.is_empty())
        .map(String::from)
        .collect();
    for token in digest::expanded(&held) {
        conn.execute(
            "INSERT OR IGNORE INTO digest_tokens(token, doc) VALUES (?, ?)",
            params![token, row[0].as_i64().unwrap()],
        )
        .unwrap();
    }
}

fn asked(conn: &Connection) -> Value {
    match conn.query_row("SELECT asked FROM temp.digest_asked", [], |r| {
        r.get::<_, String>(0)
    }) {
        Ok(a) => json!(a),
        Err(_) => Value::Null,
    }
}

fn mark_case(case: &Value) -> Value {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch("CREATE TABLE log(seq INTEGER PRIMARY KEY, kind TEXT, mac TEXT)")
        .unwrap();
    for row in case["log"].as_array().unwrap() {
        conn.execute(
            "INSERT INTO log(seq, kind, mac) VALUES (?, ?, ?)",
            params![row[0].as_i64().unwrap(), s(&row[1]), row[2].as_str()],
        )
        .unwrap();
    }
    if let Some(state) = case["st"].as_array() {
        conn.execute_batch("CREATE TABLE digest_state(key TEXT PRIMARY KEY, value TEXT)")
            .unwrap();
        for kv in state {
            let v = if s(&kv[1]) == "@fp" {
                digest::fingerprint()
            } else {
                s(&kv[1])
            };
            conn.execute(
                "INSERT INTO digest_state(key, value) VALUES (?, ?)",
                params![s(&kv[0]), v],
            )
            .unwrap();
        }
    }
    json!(mark(&conn).unwrap())
}

/// Each step's answer as the fixture spells it, and its ids in order.
fn query_case(case: &Value) -> (Vec<Value>, Vec<Value>) {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(DIGEST_SCHEMA).unwrap();
    for row in case["recs"].as_array().unwrap() {
        put(&conn, row.as_array().unwrap());
    }
    let mut current: Option<String> = Some(s(&case["mark"]).to_owned());
    let mut out = Vec::new();
    let mut orders = Vec::new();
    for step in case["s"].as_array().unwrap() {
        if let Some(m) = step.get("mark") {
            current = m.as_str().map(String::from);
        }
        if let Some(pre) = step.get("pre") {
            let pre = pre.as_array().unwrap();
            if s(&pre[0]) == "row" {
                put(&conn, &pre[1..]);
            } else {
                conn.execute_batch(s(&pre[1])).unwrap();
            }
        }
        let tokens: BTreeSet<String> = step["t"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| s(t).to_owned())
            .collect();
        let floor: f64 = s(&step["f"]).parse().unwrap();
        let rm = step["rm"].as_bool().unwrap();
        let cl = step["cl"].as_bool().unwrap();
        let m = current.as_deref();
        let raised = |e: QueryError| match e {
            QueryError::Value(_) => json!({"raise": "ValueError"}),
            QueryError::Overflow(_) => json!({"raise": "OverflowError"}),
            QueryError::Type(_) => json!({"raise": "TypeError"}),
        };
        let (r, w, order) = if s(&step["do"]) == "c" {
            match digest_candidates(&conn, "S", m, &tokens, rm, floor, cl) {
                Err(e) => (raised(e), false, Value::Null),
                Ok(a) => {
                    let w = a.warning.is_some();
                    match a.rows {
                        None => (Value::Null, w, Value::Null),
                        Some(rows) => {
                            let order = json!(rows
                                .iter()
                                .map(|c| String::from_iter(
                                    c.doc.id.iter().map(|&u| char::from_u32(u).unwrap())
                                ))
                                .collect::<Vec<_>>());
                            let mut v: Vec<Value> = rows
                                .iter()
                                .map(|c| {
                                    let id: String = c
                                        .doc
                                        .id
                                        .iter()
                                        .map(|&u| char::from_u32(u).unwrap())
                                        .collect();
                                    let t: Vec<&str> =
                                        c.doc.tokens.iter().map(|t| t.as_str()).collect();
                                    json!([id, int(&c.n), int(&c.claim), c.stamped, t.join(" ")])
                                })
                                .collect();
                            v.sort_by_key(|x| x.to_string());
                            (json!(v), w, order)
                        }
                    }
                }
            }
        } else {
            match digest_shares(&conn, "S", m, &tokens, rm, floor, cl) {
                Err(e) => (raised(e), false, Value::Null),
                Ok(a) => {
                    let w = a.warning.is_some();
                    match a.rows {
                        None => (Value::Null, w, Value::Null),
                        Some(rows) => {
                            let order =
                                json!(rows.iter().map(|c| c.id.clone()).collect::<Vec<_>>());
                            let mut v: Vec<Value> = rows
                                .iter()
                                .map(|c| {
                                    json!([c.id, int(&c.n), int(&c.claim), c.stamped, c.shared])
                                })
                                .collect();
                            v.sort_by_key(|x| x.to_string());
                            (json!(v), w, order)
                        }
                    }
                }
            }
        };
        out.push(json!({"r": r, "w": w, "a": asked(&conn)}));
        orders.push(order);
    }
    (out, orders)
}

#[test]
fn store_digest_query_every_case() {
    let cases = jsonl("store_digest_query.jsonl");
    let mut ledger = Ledger::new("golden_store_digest_query", "store_digest_query.jsonl");
    let mut steps = 0;
    for (i, case) in cases.iter().enumerate() {
        if s(&case["k"]) == "mark" {
            ledger.case(format!("#{i} mark"), || {
                assert_eq!(mark_case(case), case["r"], "#{i}")
            });
            continue;
        }
        let (got, _) = query_case(case);
        let want = case["s"].as_array().unwrap();
        assert_eq!(got.len(), want.len());
        for (j, (g, w)) in got.iter().zip(want).enumerate() {
            let w = json!({"r": w["r"], "w": w["w"], "a": w["a"]});
            ledger.case(format!("#{i} step {j}"), || {
                assert_eq!(*g, w, "#{i} step {j}")
            });
            steps += 1;
        }
    }
    assert!(steps > 40, "{steps} steps");
    let total = cases.len() - 1 + steps;
    ledger.finish(total);
}

#[test]
fn store_digest_query_sqlite_order() {
    let lines = jsonl("store_digest_query_sqlite.jsonl");
    let cases = jsonl("store_digest_query.jsonl");
    let case = cases.iter().find(|c| s(&c["k"]) == "q").unwrap();
    let (_, orders) = query_case(case);
    let want = lines[0]["orders"].as_array().unwrap();
    assert_eq!(orders.len(), want.len());
    let mut ledger = Ledger::new(
        "golden_store_digest_query_sqlite",
        "store_digest_query_sqlite.jsonl",
    );
    for (j, (g, w)) in orders.iter().zip(want).enumerate() {
        ledger.case(format!("step {j}"), || assert_eq!(g, w, "step {j}"));
    }
    ledger.finish(want.len());
}
