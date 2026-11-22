//! Every case of `location_leaves.jsonl` (not gated) and
//! `location_leaves_python.jsonl` (gated on the Python release) against
//! `location::leaves`: `_statx_birth` over scripted answers, `_birth` over
//! scripted stat attributes, `_file_url_path` under both flavours and
//! `nturl2path.url2pathname` (a Windows answer's backslashes spelled
//! `<b>`, as the host-fact guard asks). A text answer is `r`, or `r_cps`
//! when it holds a surrogate (`text_field`), and is read either way.

mod common;

use bettermemory_core::fsdata::{BirthAttr, BirthFacts, Flavour};
use bettermemory_core::location::leaves::{
    birth, file_url_path, statx_birth, url2pathname_nt, BirthError, Platform,
};
use common::parity::Ledger;
use serde_json::{json, Value};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn text(t: &[u32]) -> Value {
    Value::String(
        t.iter()
            .map(|&c| char::from_u32(c).expect("no surrogate"))
            .collect(),
    )
}

/// The generator's `_bs`: each backslash spelled `<b>`.
fn bs(v: Value) -> Value {
    json!(v.as_str().unwrap().replace('\\', "<b>"))
}

fn platform(p: &str) -> Platform {
    match p {
        "linux" => Platform::Linux,
        "win32" => Platform::Windows,
        _ => Platform::Other,
    }
}

fn attr(v: &Value) -> BirthAttr {
    if v.is_null() {
        BirthAttr::Absent
    } else if let Some(i) = v.get("i") {
        BirthAttr::Int(i.as_str().unwrap().parse().unwrap())
    } else if let Some(b) = v.get("b") {
        BirthAttr::Bool(b.as_bool().unwrap())
    } else {
        BirthAttr::Float(v["f"].as_str().unwrap().parse().unwrap())
    }
}

/// The raw `statx` buffer the generator's scripted call writes.
pub fn raw(mask: u32, seconds: i64, nanos: u32) -> Vec<u8> {
    let mut raw = vec![0u8; 256];
    raw[0..4].copy_from_slice(&mask.to_le_bytes());
    raw[80..88].copy_from_slice(&seconds.to_le_bytes());
    raw[88..92].copy_from_slice(&nanos.to_le_bytes());
    raw
}

fn answer(row: &Value) -> Value {
    let flavour = Flavour::Posix;
    match row["k"].as_str().unwrap() {
        "statx" => {
            let a = &row["a"];
            let got = if a.is_null() {
                statx_birth(platform(row["p"].as_str().unwrap()), None)
            } else {
                let n = |i: usize| a[i].as_i64().unwrap();
                let buf = raw(n(1) as u32, n(2), n(3) as u32);
                statx_birth(
                    platform(row["p"].as_str().unwrap()),
                    Some((n(0) as i32, &buf)),
                )
            };
            got.map_or(Value::Null, |n| json!(n.to_string()))
        }
        "birth" => {
            let facts = BirthFacts {
                birthtime_ns: attr(&row["ns"]),
                birthtime: attr(&row["s"]),
                ctime_ns: row["c"].as_i64().map(i128::from),
                statx: row["x"].as_i64().map(i128::from),
            };
            let rep = row["rep"].as_bool().unwrap();
            match birth(rep, &facts, platform(row["p"].as_str().unwrap())) {
                Ok(v) => v.map_or(Value::Null, Value::String),
                Err(BirthError::Overflow) => json!({"raise": "OverflowError"}),
                Err(BirthError::Value) => json!({"raise": "ValueError"}),
                Err(BirthError::NotPorted) => json!("not ported"),
            }
        }
        "file" => text(&file_url_path(flavour, &cps(row["u"].as_str().unwrap()))),
        "filent" => bs(text(&file_url_path(
            Flavour::Windows,
            &cps(row["u"].as_str().unwrap()),
        ))),
        "nt" => match url2pathname_nt(&cps(row["u"].as_str().unwrap())) {
            Ok(p) => bs(text(&p)),
            Err(_) => json!({"raise": "OSError"}),
        },
        other => panic!("unknown kind {other}"),
    }
}

/// The answer a row records, and the port's, alike spelled: a text as its
/// code points when the row writes it as `r_cps` (a text holding a
/// surrogate, which the port, reproducing 3.13.13, never answers), else
/// as JSON.
fn compared(row: &Value, got: Value) -> (Value, Value) {
    match row.get("r_cps") {
        Some(want) => {
            let got = match got {
                Value::String(s) => json!(cps(&s)),
                other => other,
            };
            (want.clone(), got)
        }
        None => (row["r"].clone(), got),
    }
}

fn run(name: &str) {
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("golden_location_leaves", name);
    for (i, row) in rows.iter().enumerate().skip(1) {
        ledger.case(format!("{name}:{i}"), || {
            let (want, got) = compared(row, answer(row));
            assert_eq!(got, want, "{name} row {i}: {row}");
        });
    }
    assert!(rows.len() > 1, "{name} holds no case");
    assert_eq!(rows[0]["cases"].as_u64(), Some(rows.len() as u64 - 1));
    ledger.finish(rows.len() - 1);
}

#[test]
fn location_leaves_every_case() {
    run("location_leaves.jsonl");
}

#[test]
fn location_leaves_python_every_case() {
    run("location_leaves_python.jsonl");
}
