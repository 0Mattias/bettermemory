//! Every case of `store_rows_leaves.jsonl`, `store_rows_leaves_python.jsonl`
//! and `store_rows_columns.jsonl` against `store::rows`: the upsert
//! statement, the link rows, the membership spec and column checks, the
//! row sort (and its seeded sweep, as a digest), a verification row read
//! back, a log row as a telemetry event, and the kind sets.

mod common;
#[path = "common/store_rows_leaves.rs"]
mod leaves;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::rows::Row;
use bettermemory_core::records::RecordError;
use bettermemory_core::store::rows::{
    self, event_from_columns, event_from_raw, iso_opt, known_columns, membership_column,
    membership_spec, membership_spec_by_table, sort_rows, verification_row, Fail, Field,
};
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use leaves::{answer_line, nest, sql, sweep, tag, text, Lcg};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn dumps(v: &PyValue) -> Value {
    let out = json::dumps(v, DumpOptions::DEFAULT).expect("dumps");
    Value::String(unicode::to_string(&out).expect("ascii"))
}

fn fail(f: &Fail) -> Value {
    match f {
        Fail::Caught(class, text) => json!({"raise": [class, text]}),
        Fail::Raised(class) => json!({"raise": [class, null]}),
    }
}

fn record_error(e: &RecordError) -> Value {
    match e {
        RecordError::Raised(class) => json!({"raise": [class, null]}),
        other => json!({"raise": [format!("{other:?}"), null]}),
    }
}

fn row_of(columns: &Value) -> Row {
    Row {
        columns: columns
            .as_array()
            .unwrap()
            .iter()
            .map(|c| (c[0].as_str().unwrap().to_owned(), sql(&c[1])))
            .collect(),
    }
}

fn verification_answer(row: &Row) -> Value {
    match verification_row(row) {
        Ok(fields) => Value::Array(
            fields
                .iter()
                .map(|(k, f)| match f {
                    Field::Column(v) => json!([k, tag(v)]),
                    Field::Py(v) => json!([k, dumps(v)]),
                })
                .collect(),
        ),
        Err(e) => record_error(&e),
    }
}

/// `gen_store_rows._sweep_large`: the same seeded lists of 64 to 400 rows,
/// one answer line each, as a digest.
fn sweep_large(case: &Value) -> (usize, String) {
    let pool = case["values"].as_array().unwrap();
    let mut rng = Lcg(case["seed"].as_u64().unwrap());
    let mut digest = Sha256::new();
    let lists = case["lists"].as_u64().unwrap() as usize;
    for _ in 0..lists {
        let n = 64 + rng.next(337);
        let rows: Vec<Value> = (0..n)
            .map(|_| {
                let a = pool[rng.next(pool.len() as u64) as usize].clone();
                let b = pool[rng.next(pool.len() as u64) as usize].clone();
                json!({"a": a, "b": b})
            })
            .collect();
        let by = [
            json!(["a", "b"]),
            json!(["-a", "b"]),
            json!(["a", "-b"]),
            json!(["-a", "-b"]),
        ][rng.next(4) as usize]
            .clone();
        digest.update(answer_line(&sort_answer(&Value::Array(rows), &by)).as_bytes());
    }
    let hex: String = digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    (lists, hex)
}

fn sort_answer(rows: &Value, by: &Value) -> Value {
    let mut rows: Vec<Row> = rows
        .as_array()
        .unwrap()
        .iter()
        .enumerate()
        .map(|(i, r)| {
            let mut columns: Vec<(String, SqlValue)> = r
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (k.clone(), sql(v)))
                .collect();
            columns.push(("n".into(), SqlValue::Integer(i as i64)));
            Row { columns }
        })
        .collect();
    let by: Vec<&str> = by
        .as_array()
        .unwrap()
        .iter()
        .map(|b| b.as_str().unwrap())
        .collect();
    match sort_rows(&mut rows, &by) {
        Ok(()) => Value::Array(
            rows.iter()
                .map(|r| match r.get("n") {
                    Some(SqlValue::Integer(n)) => json!(n),
                    _ => panic!("n"),
                })
                .collect(),
        ),
        Err(f) => fail(&f),
    }
}

fn kinds_of(name: &str) -> Vec<String> {
    let mut all: Vec<String> = rows::MUTATION_TABLES
        .iter()
        .flat_map(|t| [format!("{t}_put"), format!("{t}_delete")])
        .collect();
    let mut out: Vec<String> = match name {
        "MUTATION_KINDS" => all.clone(),
        "MEMBERSHIP_MUTATION_KINDS" => all
            .iter()
            .filter(|k| rows::is_membership_mutation(k))
            .cloned()
            .collect(),
        "CONTROL_KINDS" => rows::CONTROL_KINDS
            .iter()
            .map(|k| (*k).to_owned())
            .collect(),
        "MEMBERSHIP_EVENT_KINDS" => rows::MEMBERSHIP_EVENT_KINDS
            .iter()
            .map(|k| (*k).to_owned())
            .collect(),
        "_NOT_TELEMETRY" => {
            all.extend(rows::CONTROL_KINDS.iter().map(|k| (*k).to_owned()));
            all.extend(rows::MEMBERSHIP_EVENT_KINDS.iter().map(|k| (*k).to_owned()));
            all
        }
        other => panic!("{other}"),
    };
    out.sort();
    out.dedup();
    out
}

fn run(case: &Value) -> Value {
    match case["op"].as_str().unwrap() {
        "upsert" => {
            let columns: Vec<&str> = case["columns"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| c.as_str().unwrap())
                .collect();
            json!(rows::upsert(
                case["table"].as_str().unwrap(),
                &columns,
                case["key"].as_str().unwrap()
            ))
        }
        "links" => {
            let id = PyValue::str(case["id"].as_str().unwrap());
            let py = |v: &Value| match v {
                Value::Null => PyValue::None,
                v => PyValue::str(v.as_str().unwrap()),
            };
            let links: Vec<[PyValue; 3]> = case["links"]
                .as_array()
                .unwrap()
                .iter()
                .map(|l| [py(&l[0]), py(&l[1]), py(&l[2])])
                .collect();
            let mut calls = Vec::new();
            rows::sync_links(&id, links, |statement, values| {
                let values: Vec<Value> = values
                    .iter()
                    .map(|v| match v {
                        PyValue::None => Value::Null,
                        PyValue::Str(s) => json!(["s", unicode::to_string(s).unwrap()]),
                        other => panic!("{other:?}"),
                    })
                    .collect();
                calls.push(json!([statement, values]));
                Ok::<(), ()>(())
            })
            .unwrap();
            Value::Array(calls)
        }
        "spec" => match membership_spec(&text(&case["k"])) {
            Ok(spec) => json!({"table": spec.table, "kind": spec.kind}),
            Err(f) => fail(&f),
        },
        "by_table" => {
            json!(membership_spec_by_table(case["k"].as_str().unwrap()).map(|s| s.kind))
        }
        "column" => {
            let spec = membership_spec(&unicode::cps(case["kind"].as_str().unwrap())).unwrap();
            match membership_column(spec, &text(&case["k"])) {
                Ok(()) => Value::Null,
                Err(f) => fail(&f),
            }
        }
        "sort" => sort_answer(&case["rows"], &case["by"]),
        "sort_sweep" => {
            let values: Vec<Value> = case["values"].as_array().unwrap().clone();
            let (count, digest) = sweep(
                &mut Lcg(case["seed"].as_u64().unwrap()),
                case["lists"].as_u64().unwrap() as usize,
                case["palettes"].as_u64().unwrap(),
                &values,
                |rows, by| answer_line(&sort_answer(rows, by)),
            );
            json!({"lists": count, "sha256": digest})
        }
        "verification" => verification_answer(&row_of(&case["row"])),
        "verification_nest" => {
            let mut row = row_of(&case["row"]);
            row.columns.push((
                "attestation_payload".into(),
                SqlValue::Text(nest(&case["nest"])),
            ));
            verification_answer(&row)
        }
        "sort_sweep_large" => {
            let (count, digest) = sweep_large(case);
            json!({"lists": count, "sha256": digest})
        }
        "iso_opt" => {
            let dt = case["in"].as_str().map(|t| {
                DateTime::from_isoformat(&unicode::cps(t)).expect("the reference parsed it")
            });
            json!(iso_opt(dt.as_ref()))
        }
        "event" => {
            let session = case["session"].as_str().map(unicode::cps);
            match event_from_columns(
                &unicode::cps(case["ts"].as_str().unwrap()),
                session.as_deref(),
                &text(&case["kind"]),
                &sql(&case["payload"]),
            ) {
                Ok(Some(event)) => dumps(&event),
                Ok(None) => Value::Null,
                Err(e) => record_error(&e),
            }
        }
        "raw" => match event_from_raw(&row_of(&case["row"])) {
            Ok(Some(event)) => dumps(&event),
            Ok(None) => Value::Null,
            Err(e) => record_error(&e),
        },
        "kinds" => json!(kinds_of(case["name"].as_str().unwrap())),
        "event_nest" => {
            let payload = SqlValue::Text(nest(&case["nest"]));
            match event_from_columns(&unicode::cps("T"), None, &unicode::cps("recall"), &payload) {
                Ok(Some(event)) => Value::Array(
                    leaves::dict_keys(&event)
                        .into_iter()
                        .map(Value::String)
                        .collect(),
                ),
                Ok(None) => Value::Null,
                Err(e) => record_error(&e),
            }
        }
        other => panic!("op {other}"),
    }
}

fn want(case: &Value) -> Value {
    match case["op"].as_str().unwrap() {
        "kinds" => case["kinds"].clone(),
        "sort_sweep" | "sort_sweep_large" => {
            json!({"lists": case["lists"], "sha256": case["sha256"]})
        }
        _ => case["out"].clone(),
    }
}

fn file(test: &str, name: &str) {
    let mut lines = jsonl(name);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let ledger = Ledger::new(test, name);
    let mut failures = Vec::new();
    for (i, case) in lines.iter().enumerate() {
        let got = run(case);
        if got != want(case) {
            failures.push(format!(
                "#{i} {}: native {got}\n reference {}",
                case["op"],
                want(case)
            ));
        }
    }
    assert!(!lines.is_empty());
    ledger.settle(lines.len(), &failures);
}

#[test]
fn store_rows_leaves_every_case() {
    file("golden_store_rows", "store_rows_leaves.jsonl");
}

#[test]
fn store_rows_leaves_python_every_case() {
    file("golden_store_rows_python", "store_rows_leaves_python.jsonl");
}

#[test]
fn store_rows_columns_every_table() {
    let mut lines = jsonl("store_rows_columns.jsonl");
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let mut ledger = Ledger::new("golden_store_rows_columns", "store_rows_columns.jsonl");
    let known = known_columns();
    let got: Vec<Value> = known
        .iter()
        .map(|(t, c)| json!({"table": t, "columns": c}))
        .collect();
    assert_eq!(got.len(), known.len());
    for (i, case) in lines.iter().enumerate() {
        let same = got.get(i) == Some(case);
        assert!(
            !ledger.differs_fatally(format!("#{i} {}", case["table"]), same),
            "#{i}: native {:?}\n reference {case}",
            got.get(i)
        );
    }
    assert!(
        !ledger.differs_fatally("table count", got.len() == lines.len()),
        "native has {} tables, the reference {}",
        got.len(),
        lines.len()
    );
    ledger.finish(lines.len() + 1);
}
