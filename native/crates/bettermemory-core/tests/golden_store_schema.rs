//! Every case of `store_schema.jsonl` against `store::schema`: each
//! additive step on an in-memory store changed by the case's SQL (whether
//! the id was drawn, the warnings, the error, meta after, what the store
//! lacks or holds beyond the full schema, `checkouts`' text), `wanted`
//! through the steps, `orphan_names` under both path flavours,
//! `holds_v8_files` over names and a seeded sweep, and `absent_text`.

mod common;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::schema::{self, Absent};
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use common::schema_steps::{after, base, full, meta_of, run, wanted_fields};
use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "store_schema_steps.jsonl";

fn cps(s: &str) -> Vec<u32> {
    unicode::cps(s)
}

fn str_cps(v: &Value) -> Vec<u32> {
    cps(v.as_str().expect("a string"))
}

fn text(v: &[u32]) -> String {
    v.iter()
        .map(|&c| char::from_u32(c).map_or_else(|| format!("\\u{c:04x}"), String::from))
        .collect()
}

fn step_row(header: &Value, row: &Value, full: &[(String, String)]) -> Result<(), String> {
    let case = row["case"].as_str().unwrap();
    let setup: Vec<String> = header["setups"][case]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap().to_owned())
        .collect();
    let conn = Connection::open_in_memory().unwrap();
    base(&conn);
    for sql in &setup {
        conn.execute_batch(sql).unwrap();
    }
    let mut meta_in = meta_of(&conn);
    if case == "dict lacks id" {
        meta_in.clear();
    }
    meta_in.sort();
    let want_in: Vec<(String, String)> = serde_json::from_value(row["meta_in"].clone()).unwrap();
    if meta_in != want_in {
        return Err(format!("meta_in {meta_in:?} != {want_in:?}"));
    }
    let mut got = if case == "dict lacks id" {
        run_with_meta(&conn, Vec::new(), header["minted"].as_str().unwrap())
    } else {
        run(
            &conn,
            row["step"].as_str().unwrap(),
            header["minted"].as_str().unwrap(),
        )
    };
    let tail = after(&conn, full);
    for (k, v) in tail.as_object().unwrap() {
        got[k] = v.clone();
    }
    if let Some(sql) = got["checkouts"].as_str() {
        if setup.iter().any(|s| s == sql) {
            got["checkouts"] = json!("=setup");
        }
    }
    let want = wanted_fields(row);
    if got != want {
        return Err(format!("native {got}\n reference {want}"));
    }
    Ok(())
}

fn run_with_meta(conn: &Connection, mut meta: Vec<(String, String)>, minted: &str) -> Value {
    let mut draws = common::schema_steps::Draws {
        minted: minted.to_owned(),
        drawn: 0,
    };
    let w = schema::add_membership_tables(conn, "STORE", &mut meta, &mut draws).unwrap();
    meta.sort();
    json!({
        "drawn": draws.drawn > 0,
        "warnings": w.into_iter().collect::<Vec<_>>(),
        "meta_out": meta.iter().map(|(k, v)| json!([k, v])).collect::<Vec<_>>(),
    })
}

fn orphans_row(row: &Value) -> Result<(), String> {
    let value: Option<Vec<u32>> = match &row["value"] {
        Value::Null => None,
        Value::String(s) => Some(cps(s)),
        Value::Object(o) => {
            let open = o["open"].as_u64().unwrap() as usize;
            let close = o["close"].as_u64().unwrap() as usize;
            Some(cps(&("[".repeat(open) + &"]".repeat(close))))
        }
        other => panic!("{other}"),
    };
    for (flavour, windows) in [("posix", false), ("windows", true)] {
        let got: Vec<Value> = schema::orphan_names(value.as_deref(), windows)
            .iter()
            .map(|n| json!(text(n)))
            .collect();
        let want: Vec<Value> = row[flavour]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| match n.get("cps") {
                Some(c) => {
                    let c: Vec<u32> = serde_json::from_value(c.clone()).unwrap();
                    json!(text(&c))
                }
                None => json!(text(&str_cps(n))),
            })
            .collect();
        if got != want {
            return Err(format!("{flavour}: native {got:?} reference {want:?}"));
        }
    }
    Ok(())
}

/// `holds_v8_files` over a listing of `names` (a directory object that
/// lists them, as the generator's `_Listing` does).
fn holds(names: &[Vec<u8>]) -> bool {
    names.iter().any(|n| schema::is_v8_name(n))
}

/// `str.encode("utf-8", "surrogatepass")`: the bytes a listing gives.
fn name_bytes(v: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    for c in str_cps(v) {
        let mut buf = [0u8; 4];
        match char::from_u32(c) {
            Some(ch) => out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes()),
            None => out.extend_from_slice(&[
                0xe0 | (c >> 12) as u8,
                0x80 | ((c >> 6) & 0x3f) as u8,
                0x80 | (c & 0x3f) as u8,
            ]),
        }
    }
    out
}

/// The generator's `_sweep_names`.
pub fn sweep_names(count: usize) -> Vec<String> {
    const PARTS: [&str; 10] = [
        "-", "A", "I", "0", "Z", ".md", "\n", "x", ".events.", ".jsonl",
    ];
    let mut state: u64 = 1;
    let mut draw = || {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        state >> 33
    };
    (0..count)
        .map(|_| {
            let n = draw() % 8 + 26;
            (0..n).map(|_| PARTS[(draw() % 10) as usize]).collect()
        })
        .collect()
}

fn v8_row(row: &Value) -> Result<(), String> {
    let names: Vec<Vec<u8>> = match row.get("name") {
        Some(n) => vec![name_bytes(n)],
        None => Vec::new(),
    };
    let got = holds(&names);
    if json!(got) != row["holds"] {
        return Err(format!("{:?}: native {got}", row.get("name")));
    }
    Ok(())
}

fn sweep_row(row: &Value) -> Result<(), String> {
    let count = row["count"].as_u64().unwrap() as usize;
    let answers: String = sweep_names(count)
        .iter()
        .map(|n| {
            if schema::is_v8_name(n.as_bytes()) {
                '1'
            } else {
                '0'
            }
        })
        .collect();
    let digest = common::hex(&Sha256::digest(answers.as_bytes()));
    let held = answers.matches('1').count();
    if json!(digest) != row["sha256"] || json!(held) != row["held"] {
        return Err(format!("sweep: native {held} {digest}"));
    }
    Ok(())
}

fn tagged(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Object(o) => match o.iter().next().unwrap() {
            (k, Value::Bool(b)) if k == "b" => PyValue::Bool(*b),
            (k, i) if k == "i" => PyValue::int(i.as_i64().unwrap().into()),
            (k, s) if k == "s" => PyValue::Str(str_cps(s)),
            (k, Value::Array(t)) if k == "t" => PyValue::Tuple(t.iter().map(tagged).collect()),
            other => panic!("{other:?}"),
        },
        other => panic!("{other}"),
    }
}

fn absent_row(row: &Value) -> Result<(), String> {
    let kind = match row["class"].as_str().unwrap() {
        "NotFoundError" => Absent::NotFound,
        _ => Absent::Message,
    };
    let args: Vec<PyValue> = row["args"].as_array().unwrap().iter().map(tagged).collect();
    let got = schema::absent_text(kind, &args).map_err(|e| format!("{e:?}"))?;
    if got != str_cps(&row["text"]) {
        return Err(format!("native {}", text(&got)));
    }
    Ok(())
}

#[test]
fn store_schema_every_case() {
    let mut lines = jsonl(FILE);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let ledger = Ledger::new("golden_store_schema", FILE);
    let full = full();
    let mut failures = Vec::new();
    for (i, row) in lines.iter().enumerate() {
        let got = match row["k"].as_str().unwrap() {
            "step" => step_row(&header, row, &full),
            "orphans" => orphans_row(row),
            "v8" => v8_row(row),
            "v8_sweep" => sweep_row(row),
            "absent" => absent_row(row),
            other => panic!("kind {other}"),
        };
        if let Err(e) = got {
            failures.push(format!("#{i} {}: {e}", row["case"]));
        }
    }
    assert!(!lines.is_empty());
    ledger.settle(lines.len(), &failures);
}
