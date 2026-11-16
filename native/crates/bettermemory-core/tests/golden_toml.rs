//! Every document of `toml.jsonl`: what `tomllib.loads` (CPython 3.13)
//! returned for it, value for value and type for type, or the exception
//! it raised with its message, against `toml::loads`.

mod common;

use bettermemory_core::toml::{self, Value as Toml};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn tagged(value: &Toml) -> Value {
    match value {
        Toml::Boolean(b) => json!({"t": "bool", "v": b}),
        Toml::Integer(i) => match i.to_decimal() {
            Ok(text) => json!({"t": "int", "v": text}),
            Err(_) => json!({"t": "int", "hex": i.to_hex()}),
        },
        Toml::Float(f) => json!({"t": "float", "bits": format!("{:016x}", f.to_bits())}),
        Toml::String(s) => json!({"t": "str", "v": s}),
        Toml::DateTime(dt) => json!({"t": "datetime", "v": dt.isoformat()}),
        Toml::Date(d) => json!({"t": "date", "v": d.isoformat()}),
        Toml::Time(t) => json!({"t": "time", "v": t.isoformat()}),
        Toml::Array(items) => {
            json!({"t": "array", "v": items.iter().map(tagged).collect::<Vec<_>>()})
        }
        Toml::Table(table) => json!({
            "t": "table",
            "v": table.iter().map(|(k, v)| json!([k, tagged(v)])).collect::<Vec<_>>()
        }),
    }
}

#[test]
fn toml_every_document() {
    let lines = jsonl("toml.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "toml");
    let records = &lines[1..];
    assert_eq!(header["documents"].as_u64(), Some(records.len() as u64));
    let ledger = Ledger::new("golden_toml", "toml.jsonl");
    let mut failures = Vec::new();
    for (i, record) in records.iter().enumerate() {
        let hex_bytes;
        let doc = match record["doc"].as_str() {
            Some(doc) => doc,
            None => {
                hex_bytes = common::unhex(record["doc_hex"].as_str().expect("a document"));
                std::str::from_utf8(&hex_bytes).expect("UTF-8")
            }
        };
        let deep = record.get("ok_sha256").is_some();
        let got = match toml::loads(doc) {
            // serde_json writes a Value with sorted keys, compact, as the
            // generator's `dumps_line` does; these documents are ASCII.
            Ok(table) if deep => {
                let text = serde_json::to_string(&tagged(&Toml::Table(table))).expect("JSON");
                json!({"ok_sha256": Sha256::digest(text.as_bytes()).iter().map(|b| format!("{b:02x}")).collect::<String>()})
            }
            Ok(table) => json!({"ok": tagged(&Toml::Table(table))}),
            Err(toml::Error::Recursion) => json!({"err": "RecursionError"}),
            Err(e) => json!({"err": e.class(), "msg": e.message()}),
        };
        let mut want = record.clone();
        want.as_object_mut().expect("an object").remove("doc");
        want.as_object_mut().expect("an object").remove("doc_hex");
        if got != want {
            let shown: String = doc.chars().take(120).collect();
            failures.push(format!("#{i} {shown:?}: native {got} reference {want}"));
        }
    }
    ledger.settle(records.len(), &failures);
}
