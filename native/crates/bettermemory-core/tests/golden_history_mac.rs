//! Every case of `history_rows_mac.jsonl` against `history`'s second
//! unit: `RowVerifier` over the reference's `log` table (the header's
//! statements, in memory) with a key ring of keys named in the file, and
//! `set`.

mod common;

use bettermemory_core::history as h;
use bettermemory_core::json;
use bettermemory_core::log::{compute_mac, fingerprint, Key, KeyRing};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::StoreError;
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::{jsonl, unhex};
use rusqlite::{types::Value as Sql, Connection};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// A key of the file: SHA-256 of its name's bytes.
fn key(name: &str) -> Vec<u8> {
    Sha256::digest(name.as_bytes()).to_vec()
}

fn fp(name: &str) -> String {
    fingerprint(&key(name))
}

fn cell(v: &Value) -> (Sql, bool) {
    match v {
        Value::Null => (Sql::Null, false),
        Value::String(t) => (Sql::Text(t.clone()), false),
        other if other.get("b").is_some() => {
            (Sql::Blob(unhex(other["b"].as_str().expect("hex"))), false)
        }
        other => (Sql::Blob(unhex(other["x"].as_str().expect("hex"))), true),
    }
}

fn table(header: &Value, rows: &Value) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(header["ddl"].as_str().expect("ddl"))
        .expect("ddl");
    for row in rows.as_array().expect("rows") {
        let text = |k: &str, d: &str| row.get(k).cloned().unwrap_or(json!(d));
        let seq = row["s"].as_i64().expect("seq");
        let mut mac = text("m", "m");
        if let Some(name) = row.get("mk").and_then(Value::as_str) {
            mac = json!(compute_mac(
                &key(name),
                seq,
                text("ts", "t").as_str().expect("ts"),
                row.get("se").and_then(Value::as_str),
                row["k"].as_str().expect("kind"),
                row["p"].as_str().expect("payload"),
                text("pm", "pm").as_str().expect("pm"),
            ));
        }
        let given = [
            text("ts", "t"),
            row.get("se").cloned().unwrap_or(Value::Null),
            row["k"].clone(),
            row["p"].clone(),
            text("pm", "pm"),
            mac,
        ];
        let mut marks = vec!["?"];
        let mut params = vec![Sql::Integer(seq)];
        for v in &given {
            let (value, cast) = cell(v);
            marks.push(if cast { "CAST(? AS TEXT)" } else { "?" });
            params.push(value);
        }
        conn.execute(
            &format!("INSERT INTO log VALUES ({})", marks.join(", ")),
            rusqlite::params_from_iter(params),
        )
        .expect("insert");
    }
    conn
}

fn ring(case: &Value) -> KeyRing {
    let mut ring = KeyRing::default();
    let current = case.get("current").cloned().unwrap_or(json!("k1"));
    ring.current = current.as_str().map(|n| Key::new(key(n)));
    for name in case
        .get("retired")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        ring.retire(Key::new(key(name.as_str().expect("name"))));
    }
    ring
}

fn verify(header: &Value, case: &Value) -> Value {
    let conn = table(header, &case["t"]);
    let ring = ring(case);
    let store_fp = match case.get("fp_text") {
        Some(text) => text.as_str().expect("text").to_owned(),
        None => fp(case.get("fp").and_then(Value::as_str).unwrap_or("k1")),
    };
    let mut verifier = match h::RowVerifier::new(&conn, &ring, &store_fp) {
        Ok(v) => v,
        Err(StoreError::NotUtf8 { column, .. }) => {
            return json!({"raised": "OperationalError", "col": column})
        }
        Err(e) => return json!({"raised": format!("{e:?}")}),
    };
    let rows = h::rows_naming(&conn, &unicode::cps("{"), &[]).expect("rows");
    let seqs: Vec<i64> = match case.get("check") {
        Some(list) => list
            .as_array()
            .expect("seqs")
            .iter()
            .map(|s| s.as_i64().expect("seq"))
            .collect(),
        None => rows.iter().map(|r| r.seq).collect(),
    };
    let out: Vec<Value> = seqs
        .iter()
        .map(|s| {
            let row = rows.iter().find(|r| r.seq == *s).expect("row");
            json!([s, verifier.verified(row)])
        })
        .collect();
    json!({"r": out})
}

fn py(v: &Value) -> PyValue {
    json::loads(&unicode::cps(&v.to_string())).expect("value")
}

fn pairs(v: &Value) -> Vec<(String, Option<PyValue>)> {
    v.as_array()
        .expect("pairs")
        .iter()
        .map(|p| {
            let value = (!p[1].is_null()).then(|| py(&p[1]));
            (p[0].as_str().expect("name").to_owned(), value)
        })
        .collect()
}

fn set_case(case: &Value) -> bool {
    let mut detail: Vec<(String, PyValue)> = pairs(&case["d"])
        .into_iter()
        .map(|(k, v)| (k, v.unwrap_or(PyValue::None)))
        .collect();
    let values = pairs(&case["v"]);
    h::set(
        &mut detail,
        values
            .iter()
            .map(|(k, v)| (k.as_str(), v.clone()))
            .collect(),
    );
    let want: Vec<(String, PyValue)> = pairs(&case["r"])
        .into_iter()
        .map(|(k, v)| (k, v.unwrap_or(PyValue::None)))
        .collect();
    detail == want
}

#[test]
fn every_history_mac_answer_is_the_reference_s() {
    let file = "history_rows_mac.jsonl";
    let lines = jsonl(file);
    let (header, cases) = lines.split_first().expect("header");
    assert_eq!(header["cases"].as_u64(), Some(cases.len() as u64));
    let mut failures = Vec::new();
    let mut checked = 0;
    for case in cases {
        checked += 1;
        if case["name"] == "set" {
            if !set_case(case) {
                failures.push(format!("{case}"));
            }
            continue;
        }
        let got = verify(header, case);
        let want = match case.get("raised") {
            Some(raised) => json!({"raised": raised, "col": case["col"]}),
            None => json!({"r": case["r"]}),
        };
        if got != want {
            failures.push(format!("{}: got {got}", case["name"]));
        }
    }
    assert!(checked > 0, "{file}: no case checked");
    assert_eq!(checked, cases.len());
    Ledger::new("every_history_mac_answer_is_the_reference_s", file).settle(checked, &failures);
}
