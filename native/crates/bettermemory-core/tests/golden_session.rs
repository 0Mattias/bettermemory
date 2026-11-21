//! Every case of `session.jsonl` against `session`: scripted sequences
//! over a `SessionState` or a `SessionRegistry`, each step's return, clock
//! reads, draws and the state after compared, and the digest of the
//! file's seeded random cases.

mod common;

use common::parity::Ledger;
use common::session::{random_case, run, splitmix};
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
        other => other.to_string(),
    }
}

#[test]
fn session_every_case() {
    let lines = common::jsonl("session.jsonl");
    let ledger = Ledger::new("golden_session", "session.jsonl");
    let header = &lines[0];
    let cases = &lines[1..lines.len() - 1];
    assert_eq!(header["cases"].as_u64().unwrap() as usize, cases.len());
    assert!(!cases.is_empty());
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let got = run(case);
        let want = case["s"].as_array().unwrap();
        assert_eq!(got.len(), want.len());
        for (j, (g, w)) in got.iter().zip(want).enumerate() {
            let w: Value = ["r", "n", "k", "st"]
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
    let n = cases.len();
    ledger.settle(n, &failures);
}

#[test]
fn session_digest() {
    let lines = common::jsonl("session.jsonl");
    let d = &lines.last().unwrap()["digest"];
    let count = d["count"].as_u64().unwrap();
    assert!(count > 0);
    let mut next = splitmix(d["seed"].as_u64().unwrap());
    let mut h = Sha256::new();
    let mut seen = 0;
    for _ in 0..count {
        for ans in run(&random_case(&mut next)) {
            h.update(dumps(&ans).as_bytes());
            h.update(b"\n");
        }
        seen += 1;
    }
    assert_eq!(seen, count);
    let got: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(got, d["sha256"].as_str().unwrap());
}
