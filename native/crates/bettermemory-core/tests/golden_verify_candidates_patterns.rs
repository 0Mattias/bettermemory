//! Every case of `verify_candidates_patterns.jsonl` and
//! `verify_candidates_patterns_unicode.jsonl` against
//! `verify_candidates::patterns`: `finditer` over chosen texts, and
//! digests of every text joined from a few fragments.

mod common;

use bettermemory_core::verify_candidates::patterns::{
    backtick_iter, bare_iter, domain_route_iter, Hit,
};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn text(v: &Value) -> Vec<u32> {
    match v {
        Value::String(s) => s.chars().map(u32::from).collect(),
        _ => v["cps"]
            .as_array()
            .expect("a text")
            .iter()
            .map(|c| c.as_u64().expect("a code point") as u32)
            .collect(),
    }
}

fn finditer(p: &str, t: &[u32]) -> Vec<Hit> {
    match p {
        "backtick" => backtick_iter(t),
        "bare" => bare_iter(t),
        "domain" => domain_route_iter(t),
        _ => panic!("unknown pattern {p}"),
    }
}

fn sweep(row: &Value) {
    let p = row["p"].as_str().unwrap();
    let frags: Vec<Vec<u32>> = row["frags"].as_array().unwrap().iter().map(text).collect();
    let depth = row["depth"].as_u64().unwrap() as usize;
    let mut digest = Sha256::new();
    let mut count = 0u64;
    for k in 0..=depth {
        // `itertools.product(frags, repeat=k)`: the last index moves fastest.
        let mut idx = vec![0usize; k];
        loop {
            let t: Vec<u32> = idx.iter().flat_map(|&i| frags[i].iter().copied()).collect();
            let spelled: Vec<String> = finditer(p, &t)
                .iter()
                .map(|h| format!("{},{},{},{}", h.start, h.end, h.group_start, h.group_end))
                .collect();
            digest.update(spelled.join(";").as_bytes());
            digest.update(b"\n");
            count += 1;
            let mut j = k;
            while j > 0 {
                idx[j - 1] += 1;
                if idx[j - 1] < frags.len() {
                    break;
                }
                idx[j - 1] = 0;
                j -= 1;
            }
            if j == 0 {
                break;
            }
        }
    }
    assert_eq!(json!(count), row["count"], "{p} sweep count");
    let hex: String = digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(json!(hex), row["sha256"], "{p} sweep {:?}", row["frags"]);
}

fn check(row: &Value) {
    if row.get("sha256").is_some() {
        return sweep(row);
    }
    let p = row["p"].as_str().unwrap();
    let got: Vec<Value> = finditer(p, &text(&row["t"]))
        .iter()
        .map(|h| json!([h.start, h.end, h.group_start, h.group_end]))
        .collect();
    assert_eq!(json!(got), row["m"], "{p} {:?}", row["t"]);
}

fn run(name: &str, test: &str) -> Vec<Value> {
    let all = jsonl(name);
    let mut ledger = Ledger::new(test, name);
    for (i, row) in all[1..].iter().enumerate() {
        ledger.case(format!("line {}", i + 2), || check(row));
    }
    assert_eq!(all[0]["cases"], json!(all.len() - 1), "{name} header");
    ledger.finish(all.len() - 1);
    all
}

#[test]
fn verify_candidates_patterns_every_case() {
    let all = run(
        "verify_candidates_patterns.jsonl",
        "verify_candidates_patterns_every_case",
    );
    for p in ["backtick", "bare", "domain"] {
        assert!(all.iter().any(|r| r["p"] == p && r.get("sha256").is_some()));
    }
}

#[test]
fn verify_candidates_patterns_unicode_every_case() {
    let all = run(
        "verify_candidates_patterns_unicode.jsonl",
        "verify_candidates_patterns_unicode_every_case",
    );
    assert_eq!(all[0]["unicode"], "15.1.0");
}
