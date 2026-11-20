//! Every case of `history_rows.jsonl` and `history_rows_python.jsonl`
//! against `history`'s first unit: `read_snapshot`, `rows_naming`,
//! `with_linking_versions`, `with_restores`, `replaced_rows` over the
//! reference's `log` table (the header's statements, in memory), and
//! `payload`, `subject`, `names_as_event` and `link_types` over one
//! payload each.

mod common;

use std::collections::BTreeMap;

use bettermemory_core::history as h;
use bettermemory_core::log::LogRow;
use bettermemory_core::pyvalue::{PyError, PyStr};
use bettermemory_core::store::StoreError;
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::{cps_of, jsonl, neutral, unhex};
use rusqlite::{types::Value as Sql, Connection};
use serde_json::{json, Value};

/// A `nest` spelling: `pre`, `open` n times, `inner`, `close` n times,
/// `post`.
fn nest(spec: &Value) -> String {
    let s = |i: usize| spec.get(i).and_then(Value::as_str).unwrap_or("");
    let n = usize::try_from(spec[1].as_u64().expect("n")).expect("n");
    format!(
        "{}{}{}{}{}",
        s(4),
        s(0).repeat(n),
        s(2),
        s(3).repeat(n),
        s(5)
    )
}

/// A cell as the table is given it, and whether it is plain text.
fn cell(v: &Value) -> (Sql, bool, bool) {
    match v {
        Value::Null => (Sql::Null, false, true),
        Value::String(t) => (Sql::Text(t.clone()), false, true),
        Value::Number(n) => (Sql::Integer(n.as_i64().expect("i64")), false, false),
        other if other.get("b").is_some() => (
            Sql::Blob(unhex(other["b"].as_str().expect("hex"))),
            false,
            false,
        ),
        other if other.get("x").is_some() => (
            Sql::Blob(unhex(other["x"].as_str().expect("hex"))),
            true,
            false,
        ),
        other => (Sql::Text(nest(&other["nest"])), false, true),
    }
}

type Plain = BTreeMap<i64, Vec<Option<String>>>;

fn table(header: &Value, rows: &Value) -> (Connection, Plain) {
    let conn = Connection::open_in_memory().expect("memory");
    conn.execute_batch(header["ddl"].as_str().expect("ddl"))
        .expect("ddl");
    let mut plain = Plain::new();
    for row in rows.as_array().expect("rows") {
        let given = [
            row["s"].clone(),
            row.get("ts").cloned().unwrap_or(json!("t")),
            row.get("se").cloned().unwrap_or(Value::Null),
            row["k"].clone(),
            row["p"].clone(),
            row.get("pm").cloned().unwrap_or(json!("pm")),
            row.get("m").cloned().unwrap_or(json!("m")),
        ];
        let mut marks = Vec::new();
        let mut params = Vec::new();
        let mut texts = Vec::new();
        let mut is_plain = true;
        for (i, v) in given.iter().enumerate() {
            let (value, cast, text) = cell(v);
            marks.push(if cast { "CAST(? AS TEXT)" } else { "?" });
            if i > 0 {
                is_plain &= text;
                texts.push(match &value {
                    Sql::Text(t) => Some(t.clone()),
                    _ => None,
                });
            }
            params.push(value);
        }
        conn.execute(
            &format!("INSERT INTO log VALUES ({})", marks.join(", ")),
            rusqlite::params_from_iter(params),
        )
        .expect("insert");
        if is_plain {
            plain.insert(row["s"].as_i64().expect("seq"), texts);
        }
    }
    (conn, plain)
}

fn row_out(row: &LogRow, plain: &Plain) -> Value {
    let cells = vec![
        Some(row.ts.clone()),
        row.session.clone(),
        Some(row.kind.clone()),
        Some(row.payload.clone()),
        Some(row.prev_mac.clone()),
        Some(row.mac.clone()),
    ];
    if plain.get(&row.seq) == Some(&cells) {
        return json!(row.seq);
    }
    let mut out = vec![json!(row.seq)];
    out.extend(cells.into_iter().map(|c| json!(c)));
    Value::Array(out)
}

fn text_of(v: &Value) -> PyStr {
    match v {
        Value::String(t) => unicode::cps(t),
        other => cps_of(other),
    }
}

fn text_out(t: &[u32]) -> Value {
    match unicode::to_string(t) {
        Some(s) => json!({ "v": s }),
        None => json!({ "v_cps": t }),
    }
}

fn id_of(q: &Value) -> PyStr {
    q.get("id")
        .map(text_of)
        .unwrap_or_else(|| cps_of(&q["id_cps"]))
}

fn raised(e: &StoreError) -> Value {
    match e {
        StoreError::Py(p) => json!({ "raised": p.name() }),
        StoreError::NotUtf8 { column, .. } => {
            json!({ "raised": "OperationalError", "col": column })
        }
        StoreError::Sqlite(rusqlite::Error::SqliteFailure(_, Some(msg))) => {
            json!({ "raised": "OperationalError", "msg": msg })
        }
        other => json!({ "raised": format!("{other:?}") }),
    }
}

fn found_out(found: &[h::Found], plain: &Plain) -> Value {
    json!(found
        .iter()
        .map(|(row, p)| json!([row_out(row, plain), i32::from(p.is_some())]))
        .collect::<Vec<_>>())
}

fn pick(conn: &Connection, seqs: &Value) -> Vec<LogRow> {
    let every: BTreeMap<i64, LogRow> = h::rows_naming(conn, &[], &[])
        .expect("every row")
        .into_iter()
        .map(|r| (r.seq, r))
        .collect();
    seqs.as_array()
        .expect("seqs")
        .iter()
        .map(|s| every[&s.as_i64().expect("seq")].clone())
        .collect()
}

fn call(conn: &Connection, q: &Value, plain: &Plain) -> Value {
    let answer = |r: Result<Value, StoreError>| match r {
        Ok(v) => json!({ "r": v }),
        Err(e) => raised(&e),
    };
    match q["do"].as_str().expect("do") {
        "rows" => {
            let kinds: Vec<PyStr> = q
                .get("k")
                .map(|k| k.as_array().expect("kinds").iter().map(text_of).collect())
                .unwrap_or_default();
            answer(
                h::rows_naming(conn, &id_of(q), &kinds)
                    .map(|rows| json!(rows.iter().map(|r| row_out(r, plain)).collect::<Vec<_>>())),
            )
        }
        "links" => {
            let id = id_of(q);
            let rows = match q.get("from") {
                Some(seqs) => Ok(pick(conn, seqs)),
                None => h::rows_naming(conn, &id, &[]),
            };
            answer(rows.and_then(|rows| {
                let (found, sources) = h::with_linking_versions(conn, &id, rows)?;
                Ok(json!({
                    "f": found_out(&found, plain),
                    "src": sources.iter().map(|s| text_out(s)).collect::<Vec<_>>(),
                }))
            }))
        }
        "restores" => {
            let found: Result<Vec<h::Found>, PyError> = pick(conn, &q["from"])
                .into_iter()
                .map(|r| h::payload(&r).map(|p| (r, p)))
                .collect();
            answer(
                found
                    .map_err(StoreError::from)
                    .and_then(|found| h::with_restores(conn, found))
                    .map(|f| found_out(&f, plain)),
            )
        }
        "replaced" => {
            let mut out = Vec::new();
            let r = h::replaced_rows(conn, q["after"].as_i64().expect("after"), &mut |row| {
                out.push(row_out(&row, plain));
                Ok(())
            });
            answer(r.map(|()| json!(out)))
        }
        "snapshot" => {
            let mode = q["mode"].as_str().expect("mode");
            if mode == "held" {
                conn.execute_batch("BEGIN").expect("begin");
            }
            let r = h::read_snapshot(conn, || {
                let inside = !conn.is_autocommit();
                if mode == "ends" {
                    conn.execute_batch("COMMIT").expect("commit");
                }
                inside
            });
            let after = !conn.is_autocommit();
            if mode == "held" {
                conn.execute_batch("ROLLBACK").expect("rollback");
            }
            let mut out = answer(r.map(|inside| json!([inside, after])));
            if out.get("raised").is_some() {
                out["after"] = json!(after);
            }
            out
        }
        other => panic!("unknown call {other}"),
    }
}

fn payload_text(case: &Value) -> String {
    match case.get("pn") {
        Some(spec) => nest(spec),
        None => case["p"].as_str().expect("p").to_owned(),
    }
}

fn pure(case: &Value) -> Value {
    let kind = case["kind"].as_str().expect("kind");
    let row = LogRow {
        seq: 1,
        ts: "t".into(),
        session: None,
        kind: kind.into(),
        payload: payload_text(case),
        prev_mac: "pm".into(),
        mac: "m".into(),
    };
    let err = |e: PyError| json!({ "raised": e.name() });
    let read = match h::payload(&row) {
        Ok(read) => read,
        Err(e) => return err(e),
    };
    let f = case["f"].as_str().expect("f");
    match f {
        "parsed" => return json!({ "r": read.is_some() }),
        "payload" => {
            return json!({ "r": read.map(|v| format!("{v:?}")) });
        }
        _ => {}
    }
    let read = read.expect("a dict");
    match f {
        "subject" => json!({ "r": h::subject(kind, &read).map(|s| text_out(s)) }),
        "names" => json!({ "r": h::names_as_event(kind, &read, &id_of(case)) }),
        "types" => match h::link_types(kind, &read, &id_of(case)) {
            Ok(types) => json!({ "r": types.iter().map(|t| text_out(t)).collect::<Vec<_>>() }),
            Err(e) => err(e),
        },
        other => panic!("unknown case {other}"),
    }
}

/// The reference's answer, as `pure` and `call` spell theirs.
fn wanted(case: &Value) -> Value {
    let mut want = serde_json::Map::new();
    for k in ["r", "raised", "col", "msg", "after"] {
        if let Some(v) = case.get(k) {
            want.insert(k.to_owned(), v.clone());
        }
    }
    if case["f"] == "payload" {
        if let Some(r) = want.get_mut("r") {
            if !r.is_null() {
                *r = json!(format!("{:?}", neutral(r)));
            }
        }
    }
    // A snapshot call's `after` is a key of the call when it raised.
    Value::Object(want)
}

fn check(file: &str, test: &str) {
    let lines = jsonl(file);
    let header = &lines[0];
    let cases = &lines[1..];
    assert_eq!(
        header["cases"].as_u64(),
        Some(cases.len() as u64),
        "{file}: header count"
    );
    assert_eq!(header["depth"].as_u64(), Some(9998), "{file}: depth");
    let mut failures = Vec::new();
    let mut checked = 0;
    for case in cases {
        if case["f"] == "table" {
            let (conn, plain) = table(header, &case["t"]);
            for q in case["q"].as_array().expect("calls") {
                let mut want = wanted(q);
                if q["do"] != "snapshot" {
                    want.as_object_mut().expect("map").remove("after");
                }
                let got = call(&conn, q, &plain);
                checked += 1;
                if got != want {
                    failures.push(format!("{} {q}: got {got}", case["case"]));
                }
            }
        } else {
            let got = pure(case);
            checked += 1;
            if got != wanted(case) {
                failures.push(format!("{case}: got {got}"));
            }
        }
    }
    assert!(checked > 0, "{file}: no case checked");
    eprintln!("{file}: {} cases, {checked} answers checked", cases.len());
    Ledger::new(test, file).settle(checked, &failures);
}

#[test]
fn every_history_rows_answer_is_the_reference_s() {
    check(
        "history_rows.jsonl",
        "every_history_rows_answer_is_the_reference_s",
    );
}

#[test]
fn every_nested_history_rows_answer_is_the_reference_s() {
    check(
        "history_rows_python.jsonl",
        "every_nested_history_rows_answer_is_the_reference_s",
    );
}
