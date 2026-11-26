//! Every case of `recall_state_held.jsonl` against `recall_state::RecallState`:
//! scripted call sequences over a scripted store, each call's answer, the
//! keys held after it and the store reads it made, then every held entry
//! whole; and the seeded sweep of call sequences by count and SHA-256.

mod common;

use common::recall_state::{draw_calls, run, Rng, T0, T0_IST};
use common::{assert_count, hex, jsonl};
use serde_json::Value;
use sha2::{Digest, Sha256};

const FILE: &str = "recall_state_held.jsonl";

fn sweep(case: &Value, header: &Value) {
    let count = case["count"].as_u64().expect("count") as usize;
    let mut rng = Rng(case["seed"].as_u64().expect("seed"));
    let mut blob = String::new();
    let mut calls = 0;
    for _ in 0..count {
        let (spec, steps) = draw_calls(&mut rng, header);
        calls += steps.len();
        let (answers, end) = run(&spec, &steps);
        for line in answers.iter().chain([&end]) {
            blob.push_str(&serde_json::to_string(line).unwrap());
            blob.push('\n');
        }
    }
    assert!(count > 0 && calls > 0);
    assert_eq!(
        hex(&Sha256::digest(blob.as_bytes())),
        case["sha256"].as_str().expect("sha256"),
        "the sweep of {count} call sequences"
    );
}

#[test]
fn every_case_of_recall_state_held() {
    let lines = jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "consts");
    assert_eq!(header["t0"], T0);
    assert_eq!(header["t0_ist"], T0_IST);
    let cases = &lines[1..];
    assert_eq!(header["n"].as_u64().unwrap() as usize, cases.len());
    let mut checked = 0;
    for case in cases {
        match case["k"].as_str().expect("a kind") {
            "held" => {
                let name = &case["name"];
                let mut spec = case["state"].clone();
                for (k, v) in case["store"].as_object().unwrap() {
                    spec[k] = v.clone();
                }
                let steps = case["steps"].as_array().unwrap();
                let (answers, end) = run(&spec, steps);
                assert_eq!(answers.len(), steps.len());
                for (i, (got, step)) in answers.iter().zip(steps).enumerate() {
                    assert_eq!(got, &step["r"], "{name}: step {i} {}", step["op"]);
                }
                assert_eq!(end, case["end"], "{name}: the entries held at the end");
            }
            "sweep" => sweep(case, header),
            other => panic!("unknown case kind {other}"),
        }
        checked += 1;
    }
    assert_count("recall_state_held", checked, cases.len());
}
