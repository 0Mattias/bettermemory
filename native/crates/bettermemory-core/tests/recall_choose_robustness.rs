//! Hostile input for `recall::choose`: large logs, many hits, deep values
//! in an event, extreme stamps. No panic, and time grows linearly.

mod common;
mod timing;

use std::collections::BTreeSet;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall::choose as ch;
use common::choose::{cps, dt, run, T};
use serde_json::{json, Value};

fn event(i: usize) -> PyValue {
    PyValue::Dict(vec![
        (PyValue::str("kind"), PyValue::str("search")),
        (PyValue::str("ts"), PyValue::str("2026-01-01T12:00:05.000Z")),
        (PyValue::str("session"), PyValue::str("S")),
        (
            PyValue::str("returned"),
            PyValue::List(vec![PyValue::str(&format!("m{i}"))]),
        ),
    ])
}

fn sessions() -> BTreeSet<PyStr> {
    [cps("S")].into_iter().collect()
}

#[test]
fn seen_in_log_is_linear_in_the_events() {
    let small: Vec<PyValue> = (0..5_000).map(event).collect();
    let large: Vec<PyValue> = (0..50_000).map(event).collect();
    let (t, s) = (dt(T), sessions());
    assert_eq!(
        ch::seen_in_log(&large, &t, &t, &s, None, None)
            .unwrap()
            .len(),
        50_000
    );
    timing::grows_within(
        "seen_in_log",
        timing::TENFOLD,
        &mut || timing::once(|| ch::seen_in_log(&small, &t, &t, &s, None, None)),
        &mut || timing::once(|| ch::seen_in_log(&large, &t, &t, &s, None, None)),
    );
}

#[test]
fn deep_values_and_extreme_stamps_answer() {
    let mut deep = PyValue::List(Vec::new());
    for _ in 0..100_000 {
        deep = PyValue::List(vec![deep]);
    }
    let events = vec![
        PyValue::Dict(vec![
            (PyValue::str("kind"), PyValue::str("search")),
            (
                PyValue::str("ts"),
                PyValue::str("9999-12-31T23:59:59.999999+00:00"),
            ),
            (PyValue::str("session"), PyValue::str("S")),
            (PyValue::str("returned"), deep),
        ]),
        PyValue::Dict(vec![
            (PyValue::str("kind"), PyValue::str("show")),
            (PyValue::str("ts"), PyValue::str("0001-01-01T00:00:00Z")),
            (PyValue::str("session"), PyValue::str("S")),
            (PyValue::str("id"), PyValue::Str(vec![0xD800; 1 << 20])),
        ]),
    ];
    let t = dt(T);
    let seen = ch::seen_in_log(
        &events,
        &dt("0001-01-01T00:00:00+00:00"),
        &t,
        &sessions(),
        None,
        None,
    );
    assert_eq!(seen.unwrap().len(), 0);
}

fn many(n: usize) -> Value {
    let hits: Vec<Value> = (0..n).map(|i| json!([format!("m{i}"), "s"])).collect();
    let mut mem = serde_json::Map::new();
    let mut trust = serde_json::Map::new();
    let mut contra: Vec<Value> = Vec::new();
    for i in 0..n {
        mem.insert(format!("m{i}"), json!(["body", T, null]));
        trust.insert(
            format!("m{i}"),
            json!(if i % 2 == 0 { "remote" } else { "local" }),
        );
        contra.push(json!([format!("m{i}"), "2025-01-01T00:00:00+00:00"]));
    }
    json!({"hits": hits, "mem": mem, "trust": trust, "contra": contra, "deliver": true})
}

#[test]
fn many_hits_choose_three() {
    let (_, out) = run(&many(3_000));
    assert_eq!(out["chosen"].as_array().unwrap().len(), 3);
    assert_eq!(out["held"].as_array().unwrap().len(), 1_500);
}
