//! Every case of `store_reads_seams.jsonl`,
//! `store_reads_seams_platform.jsonl` and `store_reads_seams_python.jsonl` against `store::index_reads` and
//! `store::record_reads`: the header's tables replayed into an in-memory
//! store holding the reference's schema, each call's answer (a record as
//! its canonical payload text) or exception compared, and the bulk
//! store's answers compared by count and SHA-256.

mod common;

use common::jsonl;
use common::parity::Ledger;
use common::store_reads::{bulk, bulk_arg, call, digest, empty, full};
use serde_json::Value;

fn check(name: &str) {
    let lines = jsonl(name);
    let header = &lines[0];
    let cases = &lines[1..];
    assert_eq!(header["cases"].as_u64().unwrap() as usize, cases.len());
    let full_store = if header.get("tables").is_some() {
        Some(full(header))
    } else {
        None
    };
    let python_header;
    let bulk_store = if header.get("bulk").is_some() {
        Some(bulk(header))
    } else {
        None
    };
    // The gated files carry no tables: their `full` store is the main
    // file's.
    let full_store = match full_store {
        Some(s) => s,
        None => {
            python_header = jsonl("store_reads_seams.jsonl").remove(0);
            full(&python_header)
        }
    };
    let empty_store = empty();
    let mut ledger = Ledger::new("golden_store_reads_seams", name);
    for (i, case) in cases.iter().enumerate() {
        let func = case["fn"].as_str().unwrap();
        let store = case["store"].as_str().unwrap();
        let args = case["args"].as_array().unwrap();
        let got = match store {
            "full" => call(&full_store, func, args),
            "empty" => call(&empty_store, func, args),
            "bulk" => {
                let n = header["bulk"]["n"].as_u64().unwrap();
                let real: Vec<Value> = args.iter().map(|a| bulk_arg(a, n)).collect();
                digest(&call(bulk_store.as_ref().unwrap(), func, &real))
            }
            other => panic!("no store {other}"),
        };
        let label = format!("case {i} {func} {store} {args:?}");
        ledger.case(&label, || assert_eq!(got, case["out"], "{label}"));
    }
    ledger.finish(cases.len());
}

#[test]
fn store_reads_seams() {
    check("store_reads_seams.jsonl");
}

#[test]
fn store_reads_seams_python() {
    check("store_reads_seams_python.jsonl");
}

#[test]
fn store_reads_seams_platform() {
    check("store_reads_seams_platform.jsonl");
}
