//! Every case of `use_tokens.jsonl` against `use_tokens`: scripted steps
//! over a `SessionState` with a stub recorder, each step's return, clock
//! reads, draws, rows written, log reads and the state after compared;
//! the digest of the file's seeded random cases; and the digest of
//! `_event_ts_epoch` over random instants.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode;
use bettermemory_core::use_tokens::event_ts_epoch;
use common::parity::Ledger;
use common::session::splitmix;
use common::use_tokens::{random_case, run};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// Compact JSON with sorted keys, as `json.dumps(sort_keys=True)`.
fn dumps(v: &Value) -> String {
    match v {
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            let parts: Vec<String> = keys
                .iter()
                .map(|k| format!("{}:{}", Value::String((*k).clone()), dumps(&m[*k])))
                .collect();
            format!("{{{}}}", parts.join(","))
        }
        Value::Array(a) => format!("[{}]", a.iter().map(dumps).collect::<Vec<_>>().join(",")),
        Value::String(s) => {
            // ensure_ascii: every code point past ASCII escaped.
            let mut out = String::from("\"");
            for u in s.encode_utf16() {
                match u {
                    0x22 => out.push_str("\\\""),
                    0x5c => out.push_str("\\\\"),
                    0x20..=0x7e => out.push(char::from(u as u8)),
                    0x0a => out.push_str("\\n"),
                    _ => out.push_str(&format!("\\u{u:04x}")),
                }
            }
            out.push('"');
            out
        }
        other => other.to_string(),
    }
}

const KEYS: [&str; 8] = ["r", "n", "k", "w", "q", "it", "pl", "st"];

fn every_case(test: &str, name: &str, trailer: usize, python: bool) {
    let lines = common::jsonl(name);
    let ledger = Ledger::new(test, name);
    let header = &lines[0];
    let cases = &lines[1..lines.len() - trailer];
    assert_eq!(header["cases"].as_u64().unwrap() as usize, cases.len());
    assert!(!cases.is_empty());
    let mut failures = Vec::new();
    let mut steps = 0;
    for (i, case) in cases.iter().enumerate() {
        let got = run(case, python);
        let want = case["s"].as_array().unwrap();
        assert_eq!(got.len(), want.len());
        for (j, (g, w)) in got.iter().zip(want).enumerate() {
            steps += 1;
            let w: Value = KEYS
                .iter()
                .map(|k| ((*k).to_owned(), w[*k].clone()))
                .collect::<serde_json::Map<_, _>>()
                .into();
            if *g != w {
                failures.push(format!("#{i} step {j}: native {g}\n reference {w}"));
                break;
            }
        }
    }
    assert!(steps > 0);
    ledger.settle(cases.len(), &failures);
}

#[test]
fn use_tokens_every_case() {
    every_case("golden_use_tokens", "use_tokens.jsonl", 2, false);
}

#[test]
fn use_tokens_python_every_case() {
    every_case(
        "golden_use_tokens_python",
        "use_tokens_python.jsonl",
        0,
        true,
    );
}

#[test]
fn use_tokens_digest() {
    let lines = common::jsonl("use_tokens.jsonl");
    let d = &lines[lines.len() - 2]["digest"];
    let count = d["count"].as_u64().unwrap();
    assert!(count > 0);
    let mut next = splitmix(d["seed"].as_u64().unwrap());
    let mut h = Sha256::new();
    let mut seen = 0;
    for _ in 0..count {
        for ans in run(&random_case(&mut next), false) {
            h.update(dumps(&ans).as_bytes());
            h.update(b"\n");
        }
        seen += 1;
    }
    assert_eq!(seen, count);
    let got: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(got, d["sha256"].as_str().unwrap());
}

/// Microseconds from the epoch to 0001-01-01, and the span to the end of
/// 9999 (`gen_use_tokens.US_MIN`, `US_SPAN`).
const US_MIN: i64 = -62_135_596_800_000_000;
const US_SPAN: u64 = (253_402_300_800_000_000 - US_MIN) as u64;

#[test]
fn use_tokens_event_ts_digest() {
    let lines = common::jsonl("use_tokens.jsonl");
    let d = &lines.last().unwrap()["ets_digest"];
    let count = d["count"].as_u64().unwrap();
    assert!(count > 0);
    let mut next = splitmix(d["seed"].as_u64().unwrap());
    let mut h = Sha256::new();
    let mut seen = 0;
    for _ in 0..count {
        let us = US_MIN + (next() % US_SPAN) as i64;
        let text = DateTime::from_unix_micros_utc(us).unwrap().isoformat();
        let got = event_ts_epoch(Some(&PyValue::Str(unicode::cps(&text))));
        let r = got.map_or("None".to_owned(), float_repr::repr);
        h.update(r.as_bytes());
        h.update(b"\n");
        seen += 1;
    }
    assert_eq!(seen, count);
    let got: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(got, d["sha256"].as_str().unwrap());
}
