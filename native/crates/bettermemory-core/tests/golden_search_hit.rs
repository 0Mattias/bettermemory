//! Every case of `search_hit.jsonl` (not gated) against `search::hit`:
//! `build_hit` over the generator's memories with its stand-in drift read
//! and anchor, and `claim_anchors` with its stand-in anchoring
//! (`gen_search_hit.py`).

mod common;

use bettermemory_core::records::Memory;
use bettermemory_core::search::hit::{build_hit, claim_anchors, RecordAnchors, Scored};
use bettermemory_core::search::snippet::SnippetMemo;
use common::parity::Ledger;
use common::search_hit::{anchor_desc, float_of, hit, memory, report, Anchor, Logged, Table};
use serde_json::{json, Value};

const FILE: &str = "search_hit.jsonl";

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .expect("a list")
        .iter()
        .map(|s| s.as_str().expect("text").to_string())
        .collect()
}

fn hit_case(case: &Value, memories: &[Memory], memo: &SnippetMemo) -> Value {
    let matched = strs(&case["matched"]);
    let scored = Scored {
        memory: &memories[case["m"].as_u64().expect("m") as usize],
        score: float_of(&case["score"]),
        matched: &matched,
        query_unique: case["qu"].as_u64().expect("qu") as usize,
    };
    let mut drift = Logged {
        report: report(&case["rep"]),
        log: Vec::new(),
    };
    let anchor = Anchor::of(&case["a"]);
    let read = anchor.as_ref().map(|a| a as _);
    let got = build_hit(
        memo,
        &mut drift,
        &scored,
        read,
        case["pd"].as_bool().expect("pd"),
    )
    .expect("the stand-in raises nothing");
    json!({"o": hit(&got), "log": drift.log})
}

fn ca_case(case: &Value, memories: &[Memory]) -> Value {
    let page: Vec<Memory> = case["ms"]
        .as_array()
        .expect("ms")
        .iter()
        .map(|i| memories[i.as_u64().expect("index") as usize].clone())
        .collect();
    let mut table = Table {
        table: case["t"].clone(),
        log: Vec::new(),
    };
    let anchoring = (!case["t"].is_null()).then_some(&mut table as &mut dyn RecordAnchors);
    let got = claim_anchors(anchoring, &page);
    let o: Vec<Value> = got.iter().map(anchor_desc).collect();
    json!({"o": o, "log": table.log})
}

#[test]
fn golden_search_hit() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    let memories: Vec<Memory> = header["memories"]
        .as_array()
        .expect("memories")
        .iter()
        .map(memory)
        .collect();
    let memo = SnippetMemo::default();
    let count = header["count"].as_u64().expect("a count") as usize;
    let mut ledger = Ledger::new("golden_search_hit", FILE);
    let mut checked = 0;
    for (i, case) in lines[1..].iter().enumerate() {
        let got = match case["f"].as_str().expect("f") {
            "hit" => hit_case(case, &memories, &memo),
            "ca" => ca_case(case, &memories),
            other => panic!("unknown case {other}"),
        };
        let want = json!({"o": case["o"], "log": case["log"]});
        let label = format!("line {}", i + 2);
        assert!(
            !ledger.differs_fatally(&label, got == want),
            "{label} {case}: got {got}"
        );
        checked += 1;
    }
    assert!(checked > 0);
    assert_eq!(checked, count);
    ledger.finish(count);
}
