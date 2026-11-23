//! Replay real Python binding documents and lexical comparisons in both flavours.
mod common;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::log::ring::binding_path::{read, BindingError, BindingPath};
use common::parity::Ledger;
use serde_json::{json, Value};
use std::fs;

fn points(value: &Value) -> Vec<u32> {
    serde_json::from_value(value.clone()).expect("code points")
}

fn flavour(row: &Value) -> Flavour {
    if row[0].as_bool().unwrap() {
        Flavour::Windows
    } else {
        Flavour::Posix
    }
}

#[test]
fn binding_documents() {
    let file = "ring_binding_paths.jsonl";
    let cases = common::jsonl(file);
    let mut ledger = Ledger::new("golden_ring_binding_paths", file);
    let dir = common::stores::scratch("binding-documents");
    let document = dir.0.join("binding.path");
    for (i, row) in cases[1..].iter().enumerate() {
        let raw = if let Some(depth) = row[1].get("depth") {
            let depth = depth.as_u64().unwrap() as usize;
            format!(
                r#"{{"data":{}0{},"path":"/x"}}"#,
                "[".repeat(depth),
                "]".repeat(depth)
            )
            .into_bytes()
        } else {
            serde_json::from_value::<Vec<u8>>(row[1].clone()).expect("document bytes")
        };
        fs::write(&document, raw).unwrap();
        ledger.case(i, || {
            let actual = match read(&document, flavour(row)) {
                Ok(path) => json!(path.as_ref().map(BindingPath::text)),
                Err(BindingError::Recursion(message)) => json!({"recursion":message}),
                Err(error) => panic!("unexpected read error: {error:?}"),
            };
            assert_eq!(actual, row[2]);
        });
    }
    ledger.finish(cases[0]["rows"].as_u64().unwrap() as usize);
}

#[test]
fn binding_codecs() {
    let file = "ring_binding_path_codecs.jsonl";
    let cases = common::jsonl(file);
    let mut ledger = Ledger::new("golden_ring_binding_paths", file);
    for (i, row) in cases[1..].iter().enumerate() {
        ledger.case(i, || {
            let flavour = flavour(row);
            let path = BindingPath::parse(flavour, &points(&row[1])).unwrap();
            let encoded = match flavour {
                Flavour::Posix => path.posix_bytes(),
                Flavour::Windows => Some(
                    path.windows_units()
                        .into_iter()
                        .flat_map(u16::to_le_bytes)
                        .collect(),
                ),
            };
            assert_eq!(json!(encoded), row[2]);
            assert_eq!(json!(path.json().unwrap().as_bytes()), row[3]);
            for other in row[4].as_array().unwrap() {
                let p = BindingPath::parse(flavour, &points(&other[0])).unwrap();
                assert_eq!(path.lexical_eq(&p, flavour), other[1].as_bool().unwrap());
            }
        });
    }
    ledger.finish(cases[0]["rows"].as_u64().unwrap() as usize);
}
