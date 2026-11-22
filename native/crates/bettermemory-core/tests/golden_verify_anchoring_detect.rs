//! Every case of `verify_anchoring_detect.jsonl` (POSIX only) against
//! `verify_anchoring::detect`: the drift rows over the scripted host and
//! the closure rules `gen_verify_anchoring_detect` describes, the holds
//! rows over its repositories rebuilt in memory.
#![cfg(unix)]

mod common;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::passes::ReadAnchor;
use common::attestation_paths::cps;
use common::parity::Ledger;
use common::verify_anchoring_attest::paths;
use common::verify_anchoring_detect::{drift, error_text, holds, report_json, repos, Extended};
use common::verify_anchoring_passes::{log_json, Stub};
use serde_json::{json, Value};

const VP: [&str; 10] = [
    "/r/ok/a.py",
    "/r/gone/b.py",
    "src/gone.py",
    "src/ok.py",
    "/hq/gone",
    "/un/x",
    "~/gone.py",
    "/ab/r/gone",
    "../up.py",
    "/api/v1/events/x",
];
const AP: [&str; 6] = [
    "/r/sp/q",
    "src/x.py",
    "/r/ok/a.py",
    "/r/gone/f.py",
    "/hq/ok",
    "sp/x",
];

fn list_of(row: &Value, key: &str, default: &[&str]) -> Vec<PyStr> {
    match row.get(key) {
        Some(v) => v
            .as_array()
            .unwrap()
            .iter()
            .map(|s| cps(s.as_str().unwrap()))
            .collect(),
        None => default.iter().map(|s| cps(s)).collect(),
    }
}

fn spans(v: &Value) -> Vec<(usize, usize)> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| {
            (
                s[0].as_u64().unwrap() as usize,
                s[1].as_u64().unwrap() as usize,
            )
        })
        .collect()
}

fn check_drift(row: &Value) {
    let nt = row["nt"].as_bool().unwrap();
    let kind = row["kind"].as_str().unwrap();
    let vp = paths(kind, &list_of(row, "vp", &VP));
    let ap = paths(kind, &list_of(row, "ap", &AP));
    let body = cps(row["body"].as_str().unwrap());
    let root = row["root"].as_str().map(cps);
    let mut stub = (!row["a"].is_null()).then(|| Stub::from_json(&row["a"]));
    let wide = if row.get("x").is_some() {
        stub.take().map(Extended)
    } else {
        None
    };
    let anchor = match (&wide, &stub) {
        (Some(w), _) => Some(w as &dyn ReadAnchor),
        (None, Some(s)) => Some(s as &dyn ReadAnchor),
        (None, None) => None,
    };
    let (r, reads) = drift(
        nt,
        &body,
        &vp,
        &ap,
        root.as_deref(),
        anchor,
        &spans(&row["m"]),
    );
    let got = match r {
        Ok(report) => report_json(&report),
        Err(e) => json!(error_text(e)),
    };
    assert_eq!(got, row["o"]);
    assert_eq!(log_json(&reads.inner.log), row["c"]);
}

#[test]
fn verify_anchoring_detect_every_case() {
    let file = "verify_anchoring_detect.jsonl";
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let world = rows
        .iter()
        .find(|r| r["k"] == "w")
        .map(|r| repos(r["w"].as_object().unwrap()))
        .unwrap();
    let mut ledger = Ledger::new("verify_anchoring_detect_every_case", file);
    let (mut drifts, mut asks) = (0, 0);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(format!("line {}", i + 2), || {
            match row["k"].as_str().unwrap() {
                "d" => check_drift(row),
                "w" => {}
                "h" => {
                    let got = holds(
                        &world,
                        row["root"].as_str().unwrap(),
                        row["sha"].as_str().unwrap(),
                    );
                    assert_eq!(got, row["o"]);
                }
                k => panic!("unknown row kind {k}"),
            }
        });
        match row["k"].as_str().unwrap() {
            "d" => drifts += 1,
            "h" => asks += 1,
            _ => {}
        }
    }
    assert!(drifts > 0 && asks > 0);
    ledger.finish(rows.len());
}
