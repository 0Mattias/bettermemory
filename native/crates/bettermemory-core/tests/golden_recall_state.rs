//! Every case of `recall_state.jsonl` against `recall_state`: the
//! constants, `is_settlement`, `agent_of`, `taken_back`, `Subagent`, and
//! `rebuild_session` over scripted rows with the entry and its views, then
//! the seeded sweep by count and SHA-256.

mod common;

use bettermemory_core::recall_settle;
use bettermemory_core::recall_state::{
    agent_of, is_settlement, taken_back, Subagent, AGENT_FIELD, AGENT_FROM_PAYLOAD,
    AGENT_FROM_TRANSCRIPT, DEFAULT_HARNESS, REVIEW_UNANSWERED, RULE, STATE_IDLE_SECONDS,
    STATE_KINDS, STATE_MAX_SESSIONS, SURFACE_BRIEF, SURFACE_OF_KIND, SURFACE_RECALL,
    WITHDRAWN_FIELD,
};
use bettermemory_core::unicode::cps;
use common::recall_state::{
    asked_once, draw_rows, event, events, rebuild, s, Rng, Rows, HARNESS, NOW, SESSION,
};
use common::{assert_count, hex, jsonl};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "recall_state.jsonl";

fn text(v: &Value) -> Option<Vec<u32>> {
    v.as_str().map(cps)
}

fn check_header(h: &Value) {
    assert_eq!(h["rule"], RULE);
    assert_eq!(h["harness"], DEFAULT_HARNESS);
    assert_eq!(h["idle"], STATE_IDLE_SECONDS);
    assert_eq!(h["max"], STATE_MAX_SESSIONS as u64);
    assert_eq!(h["kinds"], json!(STATE_KINDS));
    assert_eq!(
        h["surfaces"],
        json!(SURFACE_OF_KIND
            .iter()
            .map(|(k, v)| [k, v])
            .collect::<Vec<_>>())
    );
    assert_eq!(h["settlement"], json!([SURFACE_BRIEF, SURFACE_RECALL]));
    assert_eq!(h["brief"], SURFACE_BRIEF);
    assert_eq!(h["recall"], SURFACE_RECALL);
    assert_eq!(h["agent_field"], AGENT_FIELD);
    assert_eq!(h["from_payload"], AGENT_FROM_PAYLOAD);
    assert_eq!(h["from_transcript"], AGENT_FROM_TRANSCRIPT);
    assert_eq!(h["withdrawn"], WITHDRAWN_FIELD);
    assert_eq!(h["review"], REVIEW_UNANSWERED);
    assert_eq!(h["key"], json!([HARNESS, SESSION]));
    assert_eq!(h["now"], NOW);
    // recall_settle reads the same constants and the moved view.
    assert_eq!(recall_settle::RULE, RULE);
    assert_eq!(recall_settle::REVIEW_UNANSWERED, REVIEW_UNANSWERED);
}

fn sweep(case: &Value) {
    let count = case["count"].as_u64().expect("count") as usize;
    let mut rng = Rng(case["seed"].as_u64().expect("seed"));
    let mut blob = String::new();
    for _ in 0..count {
        let (rows, agent) = draw_rows(&mut rng);
        let mut store = Rows::new(rows);
        let answer = rebuild(&mut store, agent.as_deref());
        asked_once(&store);
        match answer.get("err") {
            Some(name) => blob.push_str(&format!("!{}", name.as_str().unwrap())),
            None => blob.push_str(&serde_json::to_string(&answer).unwrap()),
        }
        blob.push('\n');
    }
    assert!(count > 0);
    assert_eq!(
        hex(&Sha256::digest(blob.as_bytes())),
        case["sha256"].as_str().expect("sha256"),
        "the sweep of {count} sequences"
    );
}

#[test]
fn every_case_of_recall_state() {
    let lines = jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "consts");
    check_header(header);
    let cases = &lines[1..];
    assert_eq!(header["n"].as_u64().unwrap() as usize, cases.len());
    let mut checked = 0;
    for case in cases {
        let k = case["k"].as_str().expect("a kind");
        match k {
            "settle" => {
                let got = match is_settlement(&event(case["e"].as_str().unwrap())) {
                    Ok(b) => json!(b),
                    Err(e) => json!({ "err": e.name() }),
                };
                assert_eq!(got, case["o"], "is_settlement {}", case["e"]);
            }
            "agent" => {
                let e = event(case["e"].as_str().unwrap());
                let got = agent_of(&e).map_or(Value::Null, |a| s(a));
                assert_eq!(got, case["o"], "agent_of {}", case["e"]);
            }
            "back" => {
                let got = taken_back(&event(case["e"].as_str().unwrap()));
                assert_eq!(json!(got), case["o"], "taken_back {}", case["e"]);
            }
            "sub" => {
                let id = text(&case["id"]);
                let ty = text(&case["type"]);
                let got = Subagent::from_payload(id.as_deref(), ty.as_deref()).map_or(
                    Value::Null,
                    |a| {
                        json!({
                            "id": s(&a.agent_id),
                            "type": a.agent_type.as_ref().map_or(Value::Null, |t| s(t)),
                            "by": s(&a.found_by),
                            "fields": a.fields().iter().map(|(k, v)| json!([s(k), s(v)])).collect::<Vec<_>>(),
                        })
                    },
                );
                assert_eq!(got, case["o"], "Subagent {case}");
            }
            "rebuild" => {
                let mut store = Rows::new(events(case["rows"].as_str().unwrap()));
                store.fail = case.get("fail").map(|n| n.as_u64().unwrap() as usize);
                let agent = text(&case["agent"]);
                let got = rebuild(&mut store, agent.as_deref());
                asked_once(&store);
                assert_eq!(got, case["o"], "rebuild_session: {}", case["name"]);
            }
            "sweep" => sweep(case),
            other => panic!("unknown case kind {other}"),
        }
        checked += 1;
    }
    assert_count("recall_state", checked, cases.len());
}
