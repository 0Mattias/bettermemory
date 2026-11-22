//! `store::returned::missing_links` and `returned_rows` against
//! `store_returned.jsonl`: each case's bare tables built in memory from
//! its typed cells, the function called, its answer (or the class it
//! raised) compared.

mod common;

use bettermemory_core::store::returned::{missing_links, returned_rows};
use common::jsonl;
use common::parity::Ledger;
use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::{json, Value};

/// `gen_store_write.ulid`: `01J` and `n` in Crockford base 32, 23 digits.
pub fn ulid(mut n: u64) -> String {
    let digits = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    let mut tail = vec![b'0'; 23];
    for slot in tail.iter_mut().rev() {
        *slot = digits[(n % 32) as usize];
        n /= 32;
    }
    format!("01J{}", String::from_utf8(tail).expect("ascii"))
}

fn cell(v: &Value) -> Sql {
    let Some(map) = v.as_object() else {
        return Sql::Null;
    };
    let (kind, v) = map.iter().next().expect("a typed cell");
    match kind.as_str() {
        "text" => Sql::Text(v.as_str().expect("text").to_owned()),
        "int" => Sql::Integer(v.as_i64().expect("int")),
        "real" => Sql::Real(v.as_f64().expect("real")),
        "blob" => {
            let hex = v.as_str().expect("hex");
            Sql::Blob(
                (0..hex.len() / 2)
                    .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).expect("hex"))
                    .collect(),
            )
        }
        other => panic!("a cell kind {other}"),
    }
}

/// The case's tables, as `gen_store_returned._pure_run` makes them.
pub fn tables(header: &Value, case: &Value) -> Connection {
    let conn = Connection::open_in_memory().expect("memory");
    let ddl = header["tables"].as_array().expect("tables");
    conn.execute_batch(ddl[0].as_str().expect("ddl"))
        .expect("memories");
    conn.execute_batch(ddl[1].as_str().expect("ddl"))
        .expect("links");
    if !case["returned"].is_null() {
        conn.execute_batch(ddl[2].as_str().expect("ddl"))
            .expect("returned");
    }
    for (table, key) in [
        ("memories", "memories"),
        ("memory_links", "links"),
        ("returned", "returned"),
    ] {
        for row in case[key].as_array().into_iter().flatten() {
            let cells: Vec<Sql> = row.as_array().expect("row").iter().map(cell).collect();
            let marks = vec!["?"; cells.len()].join(", ");
            conn.execute(
                &format!("INSERT INTO {table} VALUES ({marks})"),
                rusqlite::params_from_iter(cells),
            )
            .expect("a row");
        }
    }
    conn
}

/// The ids a `returned_rows` case asks for.
pub fn ids(v: &Value) -> Option<Vec<String>> {
    match v {
        Value::Null => None,
        Value::Array(ids) => Some(
            ids.iter()
                .map(|i| i.as_str().expect("id").to_owned())
                .collect(),
        ),
        Value::Object(spec) => {
            let range = spec["range"].as_array().expect("range");
            let (a, b) = (
                range[0].as_u64().expect("start"),
                range[1].as_u64().expect("end"),
            );
            let mut out: Vec<String> = (a..b).map(ulid).collect();
            out.extend(
                spec["then"]
                    .as_array()
                    .expect("then")
                    .iter()
                    .map(|i| i.as_str().expect("id").to_owned()),
            );
            Some(out)
        }
        other => panic!("ids {other}"),
    }
}

/// The case's call, as the fixture records its outcome.
pub fn outcome(header: &Value, case: &Value) -> Value {
    let conn = tables(header, case);
    let got = match case["call"].as_str().expect("call") {
        "missing_links" => missing_links(&conn).map(|pairs| json!(pairs)),
        _ => {
            let wanted = ids(&case["ids"]);
            returned_rows(&conn, wanted.as_deref()).map(|rows| {
                json!(rows
                    .into_iter()
                    .map(|(a, b, c)| vec![a, b, c])
                    .collect::<Vec<_>>())
            })
        }
    };
    match got {
        Ok(answer) => json!({"answer": answer}),
        Err(e) => json!({"raised": e.class()}),
    }
}

#[test]
fn every_case_answers_as_the_reference() {
    let mut lines = jsonl("store_returned.jsonl");
    let header = lines.remove(0);
    let mut failures = Vec::new();
    for case in &lines {
        let want = match case.get("answer") {
            Some(a) => json!({"answer": a}),
            None => json!({"raised": case["raised"]}),
        };
        let got = outcome(&header, case);
        if got != want {
            failures.push(format!("{}: got {got}, the reference {want}", case["name"]));
        }
    }
    assert!(!lines.is_empty(), "no case checked");
    eprintln!("store_returned.jsonl: {} cases checked", lines.len());
    Ledger::new(
        "every_case_answers_as_the_reference",
        "store_returned.jsonl",
    )
    .settle(lines.len(), &failures);
}
