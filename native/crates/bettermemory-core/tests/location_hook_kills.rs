//! Inputs a plausible wrong `resolve_hook` answers differently from the
//! reference, each read from `location.py` 1893-1979 and checked against
//! it by hand: a key with an int past `str()`'s digits is no key (the
//! ValueError is caught), a cwd with one is a raise; `probe` is read for
//! its truth, not its type; `state` is compared exactly; host names are
//! compared whole; the exchange's keys keep their first place; the
//! token writer knows whether `answers` was posted as a mapping.

mod common;
mod location_hook_support;

use std::cell::RefCell;
use std::rc::Rc;

use bettermemory_core::location_resolve::Process;
use bettermemory_core::location_witness::Exchanges;
use bettermemory_core::pyvalue::PyValue;
use location_hook_support::*;
use serde_json::{json, Value};

fn st(t: &str) -> PyValue {
    PyValue::str(t)
}

fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(items.into_iter().map(|(k, v)| (st(k), v)).collect())
}

fn near(extra: Vec<(&str, PyValue)>) -> PyValue {
    let mut items = vec![
        ("v", PyValue::int_from_decimal("1").unwrap()),
        ("probe", PyValue::Bool(true)),
        ("cwd", st("/w")),
        ("cwd_object", PyValue::List(vec![st("1"), st("9"), st("b")])),
    ];
    for (k, v) in extra {
        match items.iter_mut().find(|(have, _)| *have == k) {
            Some(slot) => slot.1 = v,
            None => items.push((k, v)),
        }
    }
    dict(items)
}

/// `_probe(...)` of the generator: no object key.
fn probe(extra: Vec<(&str, PyValue)>) -> PyValue {
    let mut items = vec![
        ("v", PyValue::int_from_decimal("1").unwrap()),
        ("probe", PyValue::Bool(true)),
        ("cwd", st("/p")),
        ("state", st("indeterminate")),
    ];
    for (k, v) in extra {
        match items.iter_mut().find(|(have, _)| *have == k) {
            Some(slot) => slot.1 = v,
            None => items.push((k, v)),
        }
    }
    dict(items)
}

/// One round in a fresh process: (the calls' names, the outcome).
fn one(location: PyValue, mut rnd: Value) -> (Vec<String>, Value) {
    rnd["posted"] = to_neutral(&dict(vec![("location", location)]));
    let process = Process::default();
    let mut exchanges = Exchanges::new();
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let got = round(&process, &mut exchanges, &rnd, &log);
    let names = log
        .borrow()
        .iter()
        .map(|c| c[0].as_str().unwrap().to_string())
        .collect();
    (names, got)
}

fn huge() -> PyValue {
    PyValue::int_from_decimal(&format!("1{}", "0".repeat(5000))).unwrap()
}

#[test]
fn a_key_past_the_digit_limit_is_no_key() {
    let key = PyValue::List(vec![huge(), st("9"), PyValue::None]);
    let (calls, got) = one(
        near(vec![("cwd_object", key)]),
        json!({"here": ["1", "9", "b"]}),
    );
    assert!(calls.is_empty(), "{calls:?}");
    assert!(got.get("ask").is_some(), "{got}");
}

#[test]
fn a_probe_cwd_past_the_digit_limit_raises_after_the_model() {
    let (calls, got) = one(probe(vec![("cwd", huge())]), json!({"open": "m1"}));
    assert_eq!(calls, ["open", "model", "record"]);
    assert_eq!(got, json!({"raised": "ValueError"}));
}

#[test]
fn probe_is_read_for_its_truth() {
    let zero = PyValue::int_from_decimal("0").unwrap();
    let (calls, got) = one(probe(vec![("probe", zero)]), json!({"open": "m1"}));
    assert!(calls.is_empty(), "{calls:?}");
    assert!(got.get("ask").is_some());
    let (calls, got) = one(probe(vec![("probe", st("0"))]), json!({"open": "m1"}));
    assert_eq!(calls, ["open", "model", "record", "resolve"]);
    assert_eq!(got["res"]["p"], "p");
    let (calls, _) = one(
        probe(vec![("probe", PyValue::List(vec![]))]),
        json!({"open": "m1"}),
    );
    assert!(calls.is_empty(), "{calls:?}");
}

#[test]
fn state_is_compared_exactly() {
    let (calls, got) = one(
        probe(vec![("state", st("Indeterminate"))]),
        json!({"open": "m1"}),
    );
    assert!(calls.is_empty(), "{calls:?}");
    assert!(got.get("ask").is_some());
}

#[test]
fn host_names_compare_whole_code_points() {
    let local = to_neutral(&PyValue::Str(vec![1, 1]));
    let (calls, got) = one(
        near(vec![("hostname", PyValue::Str(vec![0x100]))]),
        json!({"here": ["1", "9", "b"], "local": local}),
    );
    assert_eq!(calls, ["key", "host"]);
    assert!(got.get("ask").is_some());
    let local = to_neutral(&st(""));
    let (calls, got) = one(
        near(vec![("hostname", st(""))]),
        json!({"here": ["1", "9", "b"], "local": local}),
    );
    assert_eq!(calls, ["key", "host", "hooked"]);
    assert_eq!(got["res"]["p"], "h");
    // U+0141 and U+0041 share their low byte: only a whole code point
    // tells them apart, and the reference asks.
    let local = to_neutral(&st("A"));
    let (calls, got) = one(
        near(vec![("hostname", st("\u{141}"))]),
        json!({"here": ["1", "9", "b"], "local": local}),
    );
    assert_eq!(calls, ["key", "host"]);
    assert!(got.get("ask").is_some());
}

/// The golden round whose client posts `answers` as an empty mapping,
/// run with the writer probed on a checkout with no token.
fn writer_said(answers: Option<PyValue>) -> Value {
    let lines = common::jsonl("location_hook.jsonl");
    let case = lines[1..]
        .iter()
        .find(|c| c["k"] == "need witnesses, an exchange key")
        .unwrap();
    let mut rnd = case["rounds"][0].clone();
    let given = common::neutral(&rnd["posted"]);
    let PyValue::Dict(held) = &given else {
        panic!("posted is a mapping");
    };
    let mut posted = held.clone();
    posted.retain(|(k, _)| *k != st("answers"));
    if let Some(a) = answers {
        posted.push((st("answers"), a));
    }
    rnd["posted"] = to_neutral(&PyValue::Dict(posted));
    rnd["probe_writer"] = json!(true);
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    round(&Process::default(), &mut Exchanges::new(), &rnd, &log);
    let said = log.borrow().iter().find(|c| c[0] == "writer").cloned();
    said.unwrap()[1].clone()
}

/// `AnsweredToken(answers, posted=isinstance(given, dict))`: a posted
/// mapping without `remint_ok` declines the re-mint (`__call__` answers
/// None); answers absent or no mapping leave the writer unposted, and it
/// asks.
#[test]
fn the_writer_knows_answers_were_posted() {
    assert_eq!(writer_said(Some(dict(vec![]))), "declined");
    assert_eq!(writer_said(None), "asked");
    assert_eq!(
        writer_said(Some(dict(vec![("remint_ok", PyValue::Bool(true))]))),
        "asked"
    );
}

/// Nested 12,000 deep, past the 9,999 levels `str()` spells: the
/// reference raises RecursionError (seen at 100,000 under 3.13.13).
/// The fixture's JSON and its reading recurse, so the round runs on a
/// thread with a large stack.
#[test]
fn a_deep_key_raises_recursion() {
    std::thread::Builder::new()
        .stack_size(1 << 30)
        .spawn(deep_key)
        .unwrap()
        .join()
        .unwrap();
}

fn deep_key() {
    let mut x = PyValue::List(vec![st("1"), st("9"), st("b")]);
    for _ in 0..12_000 {
        x = PyValue::List(vec![x]);
    }
    let key = PyValue::List(vec![x, st("9"), PyValue::None]);
    let (calls, got) = one(
        near(vec![("cwd_object", key)]),
        json!({"here": ["1", "9", "b"]}),
    );
    assert!(calls.is_empty());
    assert_eq!(got, json!({"raised": "RecursionError"}));
}
