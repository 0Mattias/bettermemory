//! Every case of `store_reads.jsonl` and `store_reads_python.jsonl`
//! against `store::reads`: the retryable codes, the error names of live
//! SQLite failures, the busy timeout setter, and each admitted read over
//! the stores the file builds, under each admission predicate.

mod common;

use std::collections::HashMap;

use bettermemory_core::float_repr;
use bettermemory_core::identity::Actor;
use bettermemory_core::pyvalue::{self, PyValue};
use bettermemory_core::store::reads::{
    brief_rows, category_rows, document_frequencies, error_code_name, failure_of, residue_message,
    retryable_code, scope_counts, scopes_for, set_busy_timeout, term_masks, ReadError, STORE_BUSY,
    STORE_FULL,
};
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const SCHEMA: &[&str] = &[
    "CREATE TABLE memories (id, scopes_json, category, provenance, updated, \
     actor_client, actor_model)",
    "CREATE TABLE homes (record_id, project)",
    "CREATE VIRTUAL TABLE memories_fts USING fts5(body_fts, scopes_fts)",
];

fn untag(v: &Value) -> Sql {
    if v.is_null() {
        return Sql::Null;
    }
    let t = v[1].clone();
    match v[0].as_str().unwrap() {
        "i" => Sql::Integer(t.as_i64().unwrap()),
        "f" => Sql::Real(t.as_str().unwrap().parse().unwrap()),
        "s" => Sql::Text(t.as_str().unwrap().to_owned()),
        _ => Sql::Blob(common::unhex(t.as_str().unwrap())),
    }
}

fn many_store(conn: &Connection, n: u64) {
    for i in 1..=n {
        let id = format!("m{i:04}");
        let scopes = format!("[\"s{}\"]", i % 3);
        conn.execute(
            "INSERT INTO memories (rowid, id, scopes_json, category, provenance, updated, \
             actor_client, actor_model) VALUES (?, ?, ?, 'fact', 'stated', '2026-01-01', NULL, NULL)",
            rusqlite::params![i as i64, id, scopes],
        )
        .unwrap();
        let body = if i % 2 == 1 { "word" } else { "other" };
        conn.execute(
            "INSERT INTO memories_fts (rowid, body_fts, scopes_fts) VALUES (?, ?, ?)",
            rusqlite::params![i as i64, body, scopes],
        )
        .unwrap();
    }
}

fn build(case: &Value) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    for s in SCHEMA {
        conn.execute_batch(s).unwrap();
    }
    if let Some(n) = case["many"].as_u64() {
        many_store(&conn, n);
        return conn;
    }
    for st in case["sql"].as_array().unwrap() {
        let params: Vec<Sql> = st[1].as_array().unwrap().iter().map(untag).collect();
        conn.execute(st[0].as_str().unwrap(), rusqlite::params_from_iter(params))
            .unwrap();
    }
    conn
}

fn text_is(v: &PyValue, want: &str) -> bool {
    matches!(v, PyValue::Str(s) if *s == unicode::cps(want))
}

/// The fixture's admission predicates.
fn admit2(name: &str) -> impl FnMut(&[PyValue], &SqlValue) -> bool + '_ {
    let (kind, arg) = name.split_once(':').unwrap_or((name, ""));
    move |scopes: &[PyValue], home: &SqlValue| match kind {
        "all" => true,
        "none" => false,
        "scope" => scopes.iter().any(|s| text_is(s, arg)),
        "project" => *home == SqlValue::Text(arg.to_owned()),
        "nohome" => *home == SqlValue::Null,
        _ => panic!("{name}"),
    }
}

fn admit3(name: &str) -> impl FnMut(&[PyValue], &SqlValue, Option<&Actor>) -> bool + '_ {
    let mut two = admit2(if name.starts_with("actor:") {
        "all"
    } else {
        name
    });
    move |scopes, home, actor| match name.strip_prefix("actor:") {
        Some(c) => actor.is_some_and(|a| a.client.as_deref() == Some(&unicode::cps(c)[..])),
        None => two(scopes, home),
    }
}

fn err(e: &ReadError) -> Value {
    json!({"raise": e.class()})
}

fn compact(v: Value) -> Value {
    let text = serde_json::to_string(&v).unwrap();
    if text.chars().count() <= 1500 {
        return v;
    }
    let digest = Sha256::digest(text.as_bytes());
    json!({"digest": [text.chars().count(), common::hex(&digest)]})
}

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap().to_owned())
        .collect()
}

fn read(case: &Value, stores: &HashMap<String, Value>) -> Value {
    let conn = build(&stores[case["store"].as_str().unwrap()]);
    let admit = case["admit"].as_str().unwrap_or("all");
    let out = match case["op"].as_str().unwrap() {
        "doc_freqs" => document_frequencies(&conn, &strs(&case["terms"]), &mut admit3(admit)).map(
            |r| match r {
                None => Value::Null,
                Some((n, body, scope)) => {
                    let map = |m: Vec<(String, usize)>| {
                        Value::Object(m.into_iter().map(|(k, v)| (k, json!(v))).collect())
                    };
                    json!([n, map(body), map(scope)])
                }
            },
        ),
        "scope_counts" => scope_counts(&conn, &mut admit2(admit)).map(|(n, counts)| {
            let keys: Vec<Value> = counts
                .iter()
                .map(|(k, c)| json!([unicode::to_string(&pyvalue::repr(k).unwrap()).unwrap(), c]))
                .collect();
            json!([n, keys])
        }),
        "category_rows" => category_rows(
            &conn,
            case["category"].as_str().unwrap(),
            &mut admit2(admit),
        )
        .map(|ids| json!(ids)),
        "brief_rows" => brief_rows(
            &conn,
            &strs(&case["provenance"]),
            case["exclude"].as_str(),
            &mut admit2(admit),
        )
        .map(|ids| json!(ids)),
        "scopes_for" => {
            let ids = match case["ids"]["many_desc"].as_u64() {
                Some(n) => (1..=n).rev().map(|i| format!("m{i:04}")).collect(),
                None => strs(&case["ids"]),
            };
            scopes_for(&conn, &ids).map(|rows| {
                Value::Array(
                    rows.into_iter()
                        .map(|(id, scopes)| {
                            let s: Vec<String> = scopes
                                .iter()
                                .map(|c| unicode::to_string(c).unwrap())
                                .collect();
                            json!([id, s])
                        })
                        .collect(),
                )
            })
        }
        "term_masks" => {
            let rids: Vec<i64> = conn
                .prepare("SELECT rowid FROM memories ORDER BY rowid DESC")
                .unwrap()
                .query_map([], |r| r.get(0))
                .unwrap()
                .map(Result::unwrap)
                .filter(|rid: &i64| rid % 4 != 0)
                .collect();
            let index: HashMap<i64, usize> =
                rids.iter().enumerate().map(|(i, rid)| (*rid, i)).collect();
            let width = index.len();
            term_masks(&conn, case["term"].as_str().unwrap(), &index, width).map(|(a, b)| {
                let bits = |m: &[bool]| -> Vec<usize> {
                    m.iter()
                        .enumerate()
                        .filter(|(_, on)| **on)
                        .map(|(p, _)| p)
                        .collect()
                };
                json!([bits(&a), bits(&b)])
            })
        }
        other => panic!("{other}"),
    };
    match out {
        Ok(v) => compact(v),
        Err(e) => err(&e),
    }
}

fn leaf(case: &Value) -> Value {
    match case["op"].as_str().unwrap() {
        "retryable" => {
            let code = match &case["code"] {
                Value::Number(n) => Some(n.as_i64().unwrap()),
                _ => None,
            };
            // A code that is not an int (the text "5") reads as none.
            json!(retryable_code(case["class"].as_str().unwrap(), code))
        }
        "names" => json!({"busy": STORE_BUSY, "full": STORE_FULL, "op": "names"}),
        "live" => {
            let conn = Connection::open_in_memory().unwrap();
            let mut out = Value::Null;
            for sql in case["sql"].as_array().unwrap() {
                let sql = sql.as_str().unwrap();
                let r = conn.prepare(sql).and_then(|mut s| {
                    let mut rows = s.raw_query();
                    while rows.next()?.is_some() {}
                    Ok(())
                });
                if let Err(e) = r {
                    let (class, code) = failure_of(&e).unwrap_or_else(|| panic!("{e:?}"));
                    out = json!([
                        class,
                        code,
                        error_code_name(class, code),
                        retryable_code(class, code)
                    ]);
                    break;
                }
            }
            out
        }
        "name_none" => json!(error_code_name(case["class"].as_str().unwrap(), None)),
        "residue" => json!(residue_message(
            case["connection"].as_str().unwrap(),
            case["then"].as_str().unwrap()
        )),
        "busy_timeout" => {
            let seconds: f64 = match case["seconds"].as_str().unwrap() {
                "nan" => f64::NAN,
                "inf" => f64::INFINITY,
                "-inf" => f64::NEG_INFINITY,
                t => t.parse().unwrap(),
            };
            assert_eq!(float_repr::repr(seconds), case["seconds"].as_str().unwrap());
            let conn = Connection::open_in_memory().unwrap();
            conn.execute_batch("PRAGMA busy_timeout = 4321").unwrap();
            match set_busy_timeout(&conn, seconds) {
                Ok(()) => {
                    let ms: i64 = conn
                        .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
                        .unwrap();
                    json!({"ok": ms})
                }
                Err(ReadError::Worded(class, text)) => json!({"raise": [class, text]}),
                Err(e) => panic!("{e:?}"),
            }
        }
        other => panic!("{other}"),
    }
}

fn want(case: &Value) -> Value {
    if case["op"] == "names" {
        let mut c = case.clone();
        c.as_object_mut().unwrap().remove("cases");
        return c;
    }
    case["out"].clone()
}

#[test]
fn store_reads_every_case() {
    let name = "store_reads.jsonl";
    let mut lines = jsonl(name);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let ledger = Ledger::new("golden_store_reads", name);
    let mut stores = HashMap::new();
    let mut failures = Vec::new();
    for (i, case) in lines.iter().enumerate() {
        let got = match case["op"].as_str().unwrap() {
            "store" => {
                stores.insert(case["name"].as_str().unwrap().to_owned(), case.clone());
                continue;
            }
            "doc_freqs" | "scope_counts" | "category_rows" | "brief_rows" | "scopes_for"
            | "term_masks" => read(case, &stores),
            _ => leaf(case),
        };
        if got != want(case) {
            failures.push(format!("#{i} {case}: native {got}"));
        }
    }
    assert!(stores.len() >= 9);
    ledger.settle(lines.len() - stores.len(), &failures);
}

#[test]
fn store_reads_python_every_case() {
    let name = "store_reads_python.jsonl";
    let mut lines = jsonl(name);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let mut ledger = Ledger::new("golden_store_reads_python", name);
    for (i, case) in lines.iter().enumerate() {
        let got = match case["op"].as_str().unwrap() {
            "name" => json!(error_code_name(
                "OperationalError",
                Some(case["code"].as_i64().unwrap())
            )),
            _ => leaf(case),
        };
        let same = got == case["out"];
        assert!(
            !ledger.differs_fatally(format!("#{i} {case}"), same),
            "#{i} {case}: native {got}"
        );
    }
    ledger.finish(lines.len());
}
