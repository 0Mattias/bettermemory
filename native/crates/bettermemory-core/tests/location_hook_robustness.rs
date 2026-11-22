//! Hostile input for `resolve_hook`: long probe paths, many keys in the
//! posted mapping, many folders read in an exchange, odd code points; no
//! panic, and time that grows with the input as the reference's does.

mod common;
mod location_hook_support;
mod timing;

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use bettermemory_core::location_resolve::Process;
use bettermemory_core::location_witness::Exchanges;
use bettermemory_core::pyvalue::PyValue;
use location_hook_support::*;
use serde_json::{json, Value};

fn st(t: &str) -> PyValue {
    PyValue::str(t)
}

fn run(rnd: &Value) -> Value {
    let process = Process::default();
    let mut exchanges = Exchanges::new();
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    round(&process, &mut exchanges, rnd, &log)
}

fn probe_with(cwd: PyValue, detail: PyValue) -> Value {
    let loc = PyValue::Dict(vec![
        (st("probe"), PyValue::Bool(true)),
        (st("state"), st("indeterminate")),
        (st("cwd"), cwd),
        (st("detail"), detail),
    ]);
    let posted = PyValue::Dict(vec![(st("location"), loc)]);
    json!({"posted": to_neutral(&posted), "open": "m1", "session": "s"})
}

#[test]
fn odd_code_points_pass_through() {
    let cwd = PyValue::Str(vec![0, 0x7f, 0x85, 0xd800, 0xdfff, 0xfeff, 0x10ffff]);
    let got = run(&probe_with(cwd, PyValue::Str(vec![0xdc80])));
    assert_eq!(got["res"]["p"], "p");
}

#[test]
fn a_posted_mapping_of_many_keys() {
    let mut items: Vec<(PyValue, PyValue)> = (0..20_000)
        .map(|i| (st(&format!("k{i}")), PyValue::Int(i.to_string())))
        .collect();
    items.push((st("cwd"), st("/w")));
    items.push((st("probe"), PyValue::Bool(true)));
    let posted = PyValue::Dict(vec![(st("location"), PyValue::Dict(items))]);
    let got = run(&json!({"posted": to_neutral(&posted)}));
    assert!(got.get("ask").is_some(), "{got}");
}

#[test]
fn a_long_probe_path_grows_linearly() {
    let case = |n: usize| probe_with(st(&"/a".repeat(n)), st("d"));
    let (small, large) = (case(20_000), case(200_000));
    assert_eq!(run(&small)["res"]["p"], "p");
    let time = |c: &Value| -> Duration {
        timing::once(|| {
            std::hint::black_box(run(c));
        })
    };
    timing::grows_within("hook probe path", 30, &mut || time(&small), &mut || {
        time(&large)
    });
}
