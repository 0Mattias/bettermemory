//! Every case of `origin_remote.jsonl`, `origin_remote_unicode.jsonl` and
//! `origin_remote_python.jsonl`: the reference's `_parse_remote`,
//! `urlparse` (with `hostname`), `_canonicalize` and `_canonicalize_azure`
//! against the native ones.

mod common;

use bettermemory_core::origin_remote::{
    canonicalize, canonicalize_azure, hostname, parse_remote, urlparse, Triple, UrlError,
};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

/// A text: a JSON string, or its code points where it holds a surrogate.
fn text(value: &Value) -> Vec<u32> {
    match value {
        Value::String(s) => s.chars().map(u32::from).collect(),
        Value::Array(cps) => cps.iter().map(|c| c.as_u64().unwrap() as u32).collect(),
        other => panic!("no text {other}"),
    }
}

fn spell(cps: &[u32]) -> Value {
    match cps
        .iter()
        .map(|&c| char::from_u32(c))
        .collect::<Option<String>>()
    {
        Some(s) => json!(s),
        None => json!(cps),
    }
}

fn spell_triple(t: Option<Triple>) -> Value {
    match t {
        Some((h, o, n)) => json!([spell(&h), spell(&o), spell(&n)]),
        None => Value::Null,
    }
}

fn triple(value: &Value) -> (Vec<u32>, Vec<u32>, Vec<u32>) {
    let v = value.as_array().expect("a triple");
    (text(&v[0]), text(&v[1]), text(&v[2]))
}

fn every_case(file: &str, test: &str) {
    let lines = jsonl(file);
    let header = &lines[0];
    assert_eq!(header["fixture"], file.trim_end_matches(".jsonl"));
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    let mut counts = [0u64; 4];
    for (i, row) in lines[1..].iter().enumerate() {
        let row = row.as_array().expect("a row");
        let got = match row[0].as_str().expect("a kind") {
            "p" => {
                counts[0] += 1;
                spell_triple(parse_remote(&text(&row[1])))
            }
            "u" => {
                counts[1] += 1;
                match urlparse(&text(&row[1])) {
                    Ok(p) => json!([
                        spell(&p.scheme),
                        spell(&p.netloc),
                        spell(&p.path),
                        spell(&p.params),
                        spell(&p.query),
                        spell(&p.fragment),
                        hostname(&p.netloc).map(|h| spell(&h)),
                    ]),
                    Err(e) => json!(match e {
                        UrlError::Ipv6 => "ipv6",
                        UrlError::Future => "future",
                        UrlError::Ipv4 => "ipv4",
                        UrlError::Address => "address",
                        UrlError::Nfkc => "nfkc",
                    }),
                }
            }
            "c" => {
                counts[2] += 1;
                let (h, o, n) = triple(&row[1]);
                spell_triple(Some(canonicalize(&h, &o, &n)))
            }
            "z" => {
                counts[3] += 1;
                let (h, o, n) = triple(&row[1]);
                spell_triple(canonicalize_azure(&h, &o, &n))
            }
            other => panic!("no kind {other}"),
        };
        if got != row[2] {
            failures.push(format!("#{i} {}: native {got}", Value::Array(row.clone())));
        }
    }
    for (k, n) in ["p", "u", "c", "z"].iter().zip(counts) {
        assert_eq!(header[k].as_u64(), Some(n), "{file}: count of {k}");
    }
    let total: u64 = counts.iter().sum();
    assert!(total > 0, "{file}: no case checked");
    assert_eq!(total as usize, lines.len() - 1);
    ledger.settle(total as usize, &failures);
}

#[test]
fn origin_remote_every_case() {
    every_case("origin_remote.jsonl", "golden_origin_remote");
}

#[test]
fn origin_remote_unicode_every_case() {
    every_case("origin_remote_unicode.jsonl", "golden_origin_remote");
}

#[test]
fn origin_remote_python_every_case() {
    every_case("origin_remote_python.jsonl", "golden_origin_remote");
}
