//! Every case of `recall_choose.jsonl` (not gated) against
//! `recall::choose`: the header's constants, each `_seen_in_log` answer,
//! and each `_choose_hits` (and `_deliver_recall`) answer with every call
//! its fakes saw, in order. The spelling is the generator's
//! (`tests/contract/gen_recall_choose.py`).

mod common;

use std::collections::BTreeSet;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall::choose::{self as ch, ChooseError};
use bettermemory_core::recall::{RECALL_MAX_POINTERS, SEEN_KINDS};
use common::choose::{cps, dt, plain, run, text, T};
use serde_json::{json, Value};

fn seen_case(c: &Value) -> Value {
    let events: Vec<PyValue> = c["ev"].as_array().unwrap().iter().map(plain).collect();
    let sessions: BTreeSet<PyStr> = match c["ss"].as_array() {
        Some(a) => a.iter().map(|s| cps(s.as_str().unwrap())).collect(),
        None => [cps("S")].into_iter().collect(),
    };
    let wt = c["wt"].as_str().map(cps);
    let hs = c["hs"].as_str().map(cps);
    match ch::seen_in_log(
        &events,
        &dt(c["since"].as_str().unwrap_or(T)),
        &dt(c["own"].as_str().unwrap_or(T)),
        &sessions,
        wt.as_deref(),
        hs.as_deref(),
    ) {
        Ok(seen) => json!(seen.iter().map(|s| text(s)).collect::<Vec<_>>()),
        Err(ChooseError::TypeError) => json!({"err": "TypeError"}),
        Err(e) => panic!("{e:?}"),
    }
}

#[test]
fn recall_choose_every_case() {
    let lines = common::jsonl("recall_choose.jsonl");
    let header = &lines[0];
    assert_eq!(header["k"], "consts");
    assert_eq!(header["max_pointers"], json!(RECALL_MAX_POINTERS));
    assert_eq!(header["summary_chars"], json!(ch::SUMMARY_CHARS));
    let mut delivered = ch::DELIVERED_PROVENANCE.to_vec();
    delivered.sort_unstable();
    assert_eq!(header["delivered"], json!(delivered));
    let mut kinds = SEEN_KINDS.to_vec();
    kinds.sort_unstable();
    assert_eq!(header["seen_kinds"], json!(kinds));
    let rows = &lines[1..];
    assert_eq!(header["n"], json!(rows.len()));
    let mut checked = 0;
    for (i, row) in rows.iter().enumerate() {
        match row["k"].as_str().unwrap() {
            "log" => assert_eq!(seen_case(&row["c"]), row["o"], "log row {i}"),
            "choose" => {
                let (log, out) = run(&row["c"]);
                assert_eq!(out, row["o"], "choose row {i}");
                assert_eq!(log, plain(&row["log"]), "choose row {i}: the calls");
            }
            other => panic!("unknown row kind {other}"),
        }
        checked += 1;
    }
    assert!(checked > 0);
    assert_eq!(checked, rows.len());
}
