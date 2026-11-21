//! Hostile input for `recall_settle` and the id-list readers: very long
//! event lists, deeply nested id fields and extreme stamps. Nothing
//! panics, and the time grows linearly with the events read.

mod common;
mod timing;

use std::collections::BTreeSet;

use bettermemory_core::events as ev;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall_settle as rs;
use common::settle::{cps, dt, plain, run, T};
use serde_json::json;

fn uses(n: usize) -> Vec<PyValue> {
    (0..n)
        .map(|i| {
            plain(&json!({"kind": "use", "ts": T, "session": "S",
                "ids": [format!("m{i}"), 7, ["x"]]}))
        })
        .collect()
}

#[test]
fn deeply_nested_and_huge_id_fields_do_not_panic() {
    let mut v = PyValue::str("a");
    for _ in 0..100_000 {
        v = PyValue::List(vec![v]);
    }
    assert!(ev::event_id_items(&v).is_empty());
    let big = PyValue::List((0..200_000).map(PyValue::int).collect());
    assert!(ev::event_id_list(&big).is_empty());
}

#[test]
fn extreme_stamps_and_clocks_do_not_panic() {
    for now in [
        "0001-01-01T00:00:00+00:00",
        "9999-12-31T23:59:59.999999+00:00",
    ] {
        let case = json!({"now": now, "ev": [
            {"kind": "turn_audited", "ts": "0001-01-01T00:00:00-23:59", "session": "S"},
            {"kind": "turn_audited", "ts": "9999-12-31T23:59:59+23:59", "session": "S"}]});
        let _ = run("prev", &case);
        let case = json!({"now": now, "closed": {"at": T, "by": {"m": ["b", null]}}});
        let _ = run("unused", &case);
    }
}

#[test]
fn used_in_segment_grows_linearly() {
    let sessions: BTreeSet<PyStr> = [cps("S")].into_iter().collect();
    let small = uses(2_000);
    let large = uses(20_000);
    let since = dt(T);
    timing::grows_within(
        "used_in_segment",
        timing::TENFOLD,
        &mut || timing::once(|| rs::used_in_segment(&small, &since, &[], &sessions)),
        &mut || timing::once(|| rs::used_in_segment(&large, &since, &[], &sessions)),
    );
}

fn shown_case(n: usize) -> serde_json::Value {
    let by: serde_json::Map<String, serde_json::Value> = (0..n)
        .map(|i| (format!("m{i:07}"), json!(["brief", null])))
        .collect();
    let ex: Vec<String> = (0..n).map(|i| format!("x{i:07}")).collect();
    json!({"seg": {"at": T, "n": 1, "by": by}, "bodies": {}, "ex": ex})
}

#[test]
fn settle_shown_excludes_in_linear_time() {
    let small = shown_case(2_000);
    let large = shown_case(20_000);
    timing::grows_within(
        "settle_shown exclude",
        timing::TENFOLD,
        &mut || timing::once(|| run("shown", &small)),
        &mut || timing::once(|| run("shown", &large)),
    );
}
