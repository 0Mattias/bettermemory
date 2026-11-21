//! Every case of `history_detail.jsonl` against `history`'s third unit:
//! `changed`, `event_detail`, `record_row` over a walk of scripted rows
//! and `link_change_rows` over a shared `links_by_source`.

mod common;

use std::collections::HashMap;

use bettermemory_core::history_rows as hr;
use bettermemory_core::json;
use bettermemory_core::log::LogRow;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode;
use common::{jsonl, neutral};
use serde_json::Value;

fn load(text: &Value) -> PyValue {
    json::loads(&unicode::cps(text.as_str().expect("payload text"))).expect("a payload")
}

fn row(index: usize, kind: &str, payload: &str) -> LogRow {
    let seq = i64::try_from(index).expect("index") + 1;
    LogRow {
        seq,
        ts: format!("t{seq}"),
        session: (seq % 2 == 1).then(|| "s".to_string()),
        kind: kind.to_string(),
        payload: payload.to_string(),
        prev_mac: "pm".to_string(),
        mac: "m".to_string(),
    }
}

/// The stub verifier: True, False, None by seq modulo 3 (1, 2, 0).
fn verdict(seq: i64) -> Option<bool> {
    match seq % 3 {
        1 => Some(true),
        2 => Some(false),
        _ => None,
    }
}

fn strs(v: &[Vec<u32>]) -> PyValue {
    PyValue::List(v.iter().cloned().map(PyValue::Str).collect())
}

/// Values compared by their debug spelling, under which a `NaN` equals
/// itself and -0.0 differs from 0.0.
fn same(a: &PyValue, b: &PyValue) -> bool {
    format!("{a:?}") == format!("{b:?}")
}

fn check(case: &Value) {
    match case["f"].as_str().expect("f") {
        "changed" => {
            let got = hr::changed(&load(&case["a"]), &load(&case["b"]));
            assert!(
                same(
                    &strs(&got),
                    &neutral(&serde_json::json!({"t": "list", "v": case["r"]}))
                ),
                "{case}"
            );
        }
        "event" => {
            let got = PyValue::Dict(hr::event_detail(&load(&case["p"])));
            assert!(same(&got, &neutral(&case["r"])), "{case}");
        }
        "walk" => {
            let rows: Vec<(String, String, PyValue)> = case["rows"]
                .as_array()
                .expect("rows")
                .iter()
                .map(|r| {
                    let k = r[0].as_str().expect("kind").to_string();
                    (k, r[1].as_str().expect("p").to_string(), load(&r[1]))
                })
                .collect();
            let adjacent = |at: isize| -> Option<(&str, &PyValue)> {
                usize::try_from(at)
                    .ok()
                    .and_then(|at| rows.get(at))
                    .map(|(k, _, p)| (k.as_str(), p))
            };
            let mut walk = hr::RecordWalk::default();
            let want = case["r"].as_array().expect("r");
            assert_eq!(want.len(), rows.len());
            for (i, (kind, text, payload)) in rows.iter().enumerate() {
                let log_row = row(i, kind, text);
                let at = isize::try_from(i).expect("i");
                let got = hr::record_row(
                    &mut walk,
                    &log_row,
                    payload,
                    adjacent(at - 1),
                    adjacent(at + 1),
                    verdict(log_row.seq),
                );
                assert!(
                    same(&got.to_dict(true), &neutral(&want[i])),
                    "row {i} of {case}"
                );
            }
            let w = &case["w"];
            assert_eq!(walk.record_kind, w[0].as_str(), "{case}");
            assert_eq!(walk.previous_seq, w[1].as_i64(), "{case}");
            assert_eq!(walk.homed, w[2].as_bool().expect("homed"), "{case}");
            assert_eq!(walk.last_record_kind.as_deref(), w[3].as_str(), "{case}");
        }
        "links" => {
            let id = unicode::cps(case["id"].as_str().expect("id"));
            let mut by_source = HashMap::new();
            let want = case["r"].as_array().expect("r");
            for (i, r) in case["rows"].as_array().expect("rows").iter().enumerate() {
                let kind = r[0].as_str().expect("kind");
                let log_row = row(i, kind, r[1].as_str().expect("p"));
                let subject = unicode::cps(r[2].as_str().expect("subject"));
                let got = hr::link_change_rows(
                    &log_row,
                    &load(&r[1]),
                    &subject,
                    &id,
                    &mut by_source,
                    verdict(log_row.seq),
                )
                .expect("types");
                let got: Vec<PyValue> = got.iter().map(|x| x.to_dict(true)).collect();
                let want: Vec<PyValue> = want[i]
                    .as_array()
                    .expect("rows")
                    .iter()
                    .map(neutral)
                    .collect();
                assert!(
                    same(&PyValue::List(got), &PyValue::List(want)),
                    "row {i} of {case}"
                );
            }
        }
        other => panic!("unknown case {other}"),
    }
}

#[test]
fn every_case() {
    let lines = jsonl("history_detail.jsonl");
    let cases = &lines[1..];
    for case in cases {
        check(case);
    }
    assert!(!cases.is_empty());
    assert_eq!(Some(cases.len() as u64), lines[0]["cases"].as_u64());
}
