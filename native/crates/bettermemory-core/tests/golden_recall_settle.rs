//! Every case of `recall_settle.jsonl` (not gated) against the native
//! `recall_settle` and `events::event_id_items`: the header's constants,
//! each id-list reading, each `_used_in_segment` answer, and each
//! function's answer with every call its fakes saw, in order. The spelling
//! is the generator's (`tests/contract/gen_recall_settle.py`).

mod common;

use std::collections::BTreeSet;

use bettermemory_core::events as ev;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall_settle as rs;
use common::settle::{cps, dt, plain, run};
use serde_json::{json, Value};

fn check_consts(h: &Value) {
    assert_eq!(h["rule"], json!(rs::RULE));
    assert_eq!(h["default_harness"], json!(rs::DEFAULT_HARNESS));
    assert_eq!(h["state_idle_seconds"], json!(rs::STATE_IDLE_SECONDS));
    assert_eq!(h["review_unanswered"], json!(rs::REVIEW_UNANSWERED));
    assert_eq!(
        h["reaudit_dedup_window_seconds"],
        json!(rs::REAUDIT_DEDUP_WINDOW_SECONDS)
    );
    assert_eq!(h["review_issued"], json!(rs::REVIEW_ISSUED));
}

fn strs(v: &[PyStr]) -> PyValue {
    PyValue::List(v.iter().cloned().map(PyValue::Str).collect())
}

#[test]
fn recall_settle_every_case() {
    let lines = common::jsonl("recall_settle.jsonl");
    let header = &lines[0];
    assert_eq!(header["k"], "consts");
    check_consts(header);
    let used_ev: Vec<PyValue> = header["used_ev"]
        .as_array()
        .unwrap()
        .iter()
        .map(plain)
        .collect();
    let mut checked = 0;
    for r in &lines[1..] {
        match r["k"].as_str().unwrap() {
            "ids" => {
                let v = common::neutral(&r["v"]);
                let items: Vec<PyValue> = ev::event_id_items(&v)
                    .into_iter()
                    .map(|(i, s)| PyValue::List(vec![PyValue::int(i as i128), PyValue::Str(s)]))
                    .collect();
                assert!(
                    PyValue::List(items) == common::neutral(&r["o"]),
                    "items {r}"
                );
                assert!(
                    strs(&ev::event_id_list(&v)) == common::neutral(&r["l"]),
                    "list {r}"
                );
                assert!(ev::str_ids(&v) == ev::event_id_list(&v));
            }
            "used" => {
                let sessions: BTreeSet<PyStr> = r["ss"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|s| cps(s.as_str().unwrap()))
                    .collect();
                let got = rs::used_in_segment(
                    &used_ev,
                    &dt(r["since"].as_str().unwrap()),
                    &cps(r["hs"].as_str().unwrap()),
                    &sessions,
                );
                let got: Vec<PyStr> = got.into_iter().collect();
                assert!(strs(&got) == plain(&r["o"]), "used {r}");
            }
            kind => {
                let (answer, log) = run(kind, &r["c"]);
                assert!(
                    PyValue::List(log.clone()) == plain(&r["log"]),
                    "log of {r}\n got {log:?}"
                );
                assert!(answer == plain(&r["o"]), "answer of {r}\n got {answer:?}");
            }
        }
        checked += 1;
    }
    let in_file = header["count"].as_u64().unwrap() as usize;
    assert_eq!(in_file, lines.len() - 1);
    common::assert_count("recall_settle.jsonl", checked, in_file);
}
