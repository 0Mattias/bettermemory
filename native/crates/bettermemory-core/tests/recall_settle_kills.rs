//! Cases a plausible wrong port of `recall_settle` and the id-list
//! readers fails, each with the reference's answer.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::events as ev;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall_settle as rs;
use common::settle::{cps, dt, plain, run, Fakes, Proxy, T};
use serde_json::{json, Value};

fn set(v: &[&str]) -> BTreeSet<PyStr> {
    v.iter().map(|s| cps(s)).collect()
}

fn events(v: Value) -> Vec<PyValue> {
    match &plain(&v) {
        PyValue::List(items) => items.clone(),
        _ => unreachable!(),
    }
}

fn ids(v: &[&str]) -> PyValue {
    PyValue::List(v.iter().map(|s| PyValue::str(s)).collect())
}

#[test]
fn id_items_keep_original_indices_and_read_a_string_whole() {
    let v = plain(&json!(["a", 1, null, "b"]));
    assert_eq!(ev::event_id_items(&v), vec![(0, cps("a")), (3, cps("b"))]);
    assert_eq!(ev::event_id_list(&plain(&json!("abc"))), vec![cps("abc")]);
    assert!(ev::event_id_list(&plain(&json!(""))).is_empty());
    assert!(ev::event_id_list(&PyValue::Tuple(vec![PyValue::str("a")])).is_empty());
}

#[test]
fn a_use_stamped_at_the_segment_start_counts() {
    let e = events(json!([{"kind": "use", "ts": T, "session": "S", "ids": ["a"]}]));
    assert_eq!(
        rs::used_in_segment(&e, &dt(T), &[], &set(&["S"])),
        set(&["a"])
    );
}

#[test]
fn an_empty_harness_session_never_joins_by_stamp() {
    let e = events(json!([{"kind": "use", "ts": T, "harness_session": "", "ids": ["a"]}]));
    assert!(rs::used_in_segment(&e, &dt(T), &[], &set(&["S"])).is_empty());
}

#[test]
fn ids_fall_back_to_memory_ids_only_when_ids_is_falsy() {
    let e = events(json!([
        {"kind": "use", "ts": T, "session": "S", "ids": [3], "memory_ids": ["b"]},
        {"kind": "use", "ts": T, "session": "S", "ids": [], "memory_ids": ["c"]}
    ]));
    assert_eq!(
        rs::used_in_segment(&e, &dt(T), &[], &set(&["S"])),
        set(&["c"])
    );
}

#[test]
fn the_first_of_equal_audit_instants_is_kept() {
    let case = json!({"ev": [
        {"kind": "turn_audited", "ts": "2026-01-01T12:00:00+01:00", "session": "S"},
        {"kind": "turn_audited", "ts": "2026-01-01T11:00:00Z", "session": "S"}
    ]});
    let (answer, _) = run("prev", &case);
    assert!(answer == PyValue::DateTime(dt("2026-01-01T12:00:00+01:00")));
}

#[test]
fn an_invalid_parent_falls_back_to_the_own_review_id() {
    let own = "01HZZZZZZZZZZZZZZZZZZZZZZA";
    let case = json!({"ev": [{"kind": "review_issued", "ts": T, "session": "S",
        "review_id": own, "parent_review_id": "01hzzzzzzzzzzzzzzzzzzzzzza"}]});
    assert!(run("review", &case).0 == ids(&[own]));
}

#[test]
fn an_unhashable_session_raises_but_a_falsy_one_reads_session_id() {
    let own = "01HZZZZZZZZZZZZZZZZZZZZZZA";
    let raise =
        json!({"ev": [{"kind": "review_issued", "ts": T, "session": ["S"], "review_id": own}]});
    assert!(run("review", &raise).0 == plain(&json!({"err": "TypeError"})));
    let falls = json!({"ev": [{"kind": "review_issued", "ts": T, "session": [],
        "session_id": "S", "review_id": own}]});
    assert!(run("review", &falls).0 == ids(&[own]));
}

#[test]
fn a_show_with_a_non_text_id_opens_nothing() {
    let case = json!({
        "closed": {"at": T, "n": 1, "by": {"m1": ["brief", null]}},
        "ev": [{"kind": "show", "ts": T, "session": "S", "id": ["m1"]}]
    });
    assert!(run("unused", &case).0 == ids(&["m1"]));
}

#[test]
fn an_empty_harness_reads_the_default_and_an_empty_prompt_id_is_left_out() {
    let body = "Run the integration suite with pytest fixtures for the database setup.";
    let case = json!({"seg": {"at": T, "n": 1, "by": {"m1": ["brief", "E"]}},
        "bodies": {"m1": body}, "h": "", "pid": ""});
    let (answer, log) = run("shown", &case);
    assert!(answer == ids(&["m1"]));
    let PyValue::List(rec) = &log[3] else {
        panic!("{log:?}")
    };
    let fields = &rec[2];
    assert!(fields.get("harness") == Some(&PyValue::str("claude-code")));
    assert!(fields.get("prompt_id").is_none());
    assert!(fields.get("client_model").is_none());
}

#[test]
fn a_show_stamped_at_the_closed_segment_start_opens_the_id() {
    let case = json!({
        "closed": {"at": T, "n": 1, "by": {"m1": ["brief", null]}},
        "ev": [{"kind": "show", "ts": T, "session": "S", "id": "m1"}]
    });
    assert!(run("unused", &case).0 == ids(&[]));
}

/// A store whose events are built in Rust, for values JSON cannot spell.
struct Rows(Vec<PyValue>);

impl rs::SettleStore for Rows {
    fn events_of_kinds(&mut self, _: &[&str], _: &DateTime) -> Vec<PyValue> {
        self.0.clone()
    }
    fn events_since(&mut self, _: &DateTime) -> Vec<PyValue> {
        self.0.clone()
    }
    fn load_one(&mut self, _: &[u32]) -> Result<PyStr, rs::LoadFailure> {
        unreachable!()
    }
}

#[test]
fn a_tuple_session_holding_a_list_raises() {
    let own = "01HZZZZZZZZZZZZZZZZZZZZZZA";
    let mut row = match plain(&json!({"kind": "review_issued", "ts": T, "review_id": own})) {
        PyValue::Dict(ref d) => d.clone(),
        _ => unreachable!(),
    };
    row.push((
        PyValue::str("session"),
        PyValue::Tuple(vec![PyValue::str("S"), PyValue::List(vec![])]),
    ));
    let mut store = Rows(vec![PyValue::Dict(row)]);
    let case = json!({});
    let shared = std::cell::RefCell::new(Fakes::new(&case));
    let out = rs::review_unanswered(
        &mut store,
        &mut Proxy(&shared),
        &mut Proxy(&shared),
        &cps("S"),
        &set(&["S"]),
        &dt(T),
        None,
        &dt(T),
        |_| (true, false),
    );
    assert!(matches!(out, Err(rs::SettleError::TypeError)));
}
