//! Cases a plausible wrong `use_tokens` fails: the scan's early exit and
//! its laziness, `>=` at the mint time, the `or` fallbacks, the hash check
//! before the bridge, override ids before the scan, the expiry row's
//! oldest token and truncation, the alias, and one lock across the
//! auto-commit.

mod common;

use std::collections::BTreeSet;
use std::sync::Arc;
use std::thread;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::session::{PendingUseToken, SessionState};
use bettermemory_core::use_tokens::{
    advance_turn, already_recorded_pending_ids, attach_use_tokens, emit_expired_use_tokens,
    event_ts_epoch, hook_attributed_pending_ids, Row, UseTokenError,
};
use common::session::{cps, Fx};
use common::use_tokens::{pyv, Rec};
use serde_json::{json, Value};

fn fx(now: f64) -> Fx {
    Fx {
        now,
        n: 0,
        reads: 0,
        draws: 0,
    }
}

fn rows(v: Value) -> Vec<Row> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|r| match &pyv(r) {
            PyValue::Dict(items) => items.clone(),
            _ => unreachable!(),
        })
        .collect()
}

fn state_with(ids: &[&str], f: &mut Fx) -> SessionState {
    let st = SessionState::new(f);
    let ids: Vec<PyStr> = ids.iter().map(|s| cps(s)).collect();
    st.lock().issue_use_tokens(&ids, f);
    st
}

fn set(ids: &[&str]) -> BTreeSet<PyStr> {
    ids.iter().map(|s| cps(s)).collect()
}

#[test]
fn the_scan_stops_at_the_first_older_row_and_not_at_a_malformed_one() {
    let mut f = fx(1000.0);
    let st = state_with(&["a"], &mut f);
    let mut rec = Rec::new("S");
    rec.rows = rows(json!([
        {"kind": "use", "ts": "bad", "session": "S", "ids": ["x"]},
        {"kind": "use", "ts": "1970-01-01T00:16:39Z", "session": "S", "ids": ["a"]},
        {"kind": "use", "ts": "1970-01-01T00:16:41Z", "session": "S", "ids": ["a"]},
    ]));
    let got = already_recorded_pending_ids(&st, &mut rec, None, None).unwrap();
    assert!(got.is_empty());
    assert_eq!(rec.pl, 2);
    assert_eq!(rec.it, 1);
    assert_eq!(rec.q, 1);
}

#[test]
fn a_row_at_the_mint_time_settles() {
    let mut f = fx(1000.0);
    let st = state_with(&["a"], &mut f);
    let mut rec = Rec::new("S");
    rec.rows =
        rows(json!([{"kind": "use", "ts": "1970-01-01T00:16:40Z", "session": "S", "ids": ["a"]}]));
    assert_eq!(
        already_recorded_pending_ids(&st, &mut rec, None, None).unwrap(),
        set(&["a"])
    );
}

#[test]
fn falsy_values_fall_back_and_truthy_ones_do_not() {
    let mut f = fx(1000.0);
    let st = state_with(&["a", "b"], &mut f);
    let mut rec = Rec::new("S");
    rec.rows = rows(json!([
        {"kind": "use", "ts": "1970-01-01T00:16:41Z", "session": [], "session_id": "S", "ids": [], "memory_ids": ["a"]},
        {"kind": "use", "ts": "1970-01-01T00:16:41Z", "session": "S", "ids": ["x"], "memory_ids": ["b"]},
    ]));
    assert_eq!(
        hook_attributed_pending_ids(&st, &mut rec, None, None).unwrap(),
        set(&["a"])
    );
}

#[test]
fn an_unhashable_session_raises_before_the_tag_is_read() {
    let mut f = fx(1000.0);
    let st = state_with(&["a"], &mut f);
    let mut rec = Rec::new("S");
    rec.rows = rows(json!([{"kind": "use", "session": {"k": 1}}]));
    let e = already_recorded_pending_ids(&st, &mut rec, None, None).unwrap_err();
    assert_eq!(e, UseTokenError::Unhashable("dict"));
    assert_eq!(e.message(), "unhashable type: 'dict'");
    // A tuple holding a list raises for the list; a tuple of str does not.
    let deep = PyValue::Tuple(vec![
        PyValue::str("x"),
        PyValue::Tuple(vec![PyValue::List(vec![])]),
    ]);
    rec.rows = vec![vec![
        (PyValue::str("kind"), PyValue::str("use")),
        (PyValue::str("session"), deep),
    ]];
    assert_eq!(
        already_recorded_pending_ids(&st, &mut rec, None, None),
        Err(UseTokenError::Unhashable("list"))
    );
    // A tuple's hash visits its items in order: the list comes first.
    rec.rows = vec![vec![
        (PyValue::str("kind"), PyValue::str("use")),
        (
            PyValue::str("session"),
            PyValue::Tuple(vec![PyValue::List(vec![]), PyValue::Dict(vec![])]),
        ),
    ]];
    assert_eq!(
        already_recorded_pending_ids(&st, &mut rec, None, None),
        Err(UseTokenError::Unhashable("list"))
    );
}

#[test]
fn a_missing_hit_id_raises_even_after_a_declined_id() {
    let mut f = fx(1000.0);
    let st = state_with(&[], &mut f);
    let mut hits: Vec<Row> = vec![
        vec![(PyValue::str("id"), PyValue::Int("1".into()))],
        vec![(PyValue::str("x"), PyValue::Int("1".into()))],
    ];
    assert_eq!(
        attach_use_tokens(&mut hits, &st, &mut f),
        Err(UseTokenError::MissingId)
    );
    let mut one: Vec<Row> = vec![vec![(PyValue::str("id"), PyValue::Int("1".into()))]];
    assert_eq!(
        attach_use_tokens(&mut one, &st, &mut f),
        Err(UseTokenError::NotPorted)
    );
}

#[test]
fn the_bridge_needs_a_str_session_and_the_tag() {
    let mut f = fx(1000.0);
    let st = state_with(&["a", "b", "c"], &mut f);
    let mut rec = Rec::new("S");
    rec.rows = rows(json!([
        {"kind": "use", "ts": "1970-01-01T00:16:41Z", "session": "T", "ids": ["a"], "triggered_from": "stop_hook"},
        {"kind": "use", "ts": "1970-01-01T00:16:41Z", "session": "T", "ids": ["b"]},
        {"kind": "use", "ts": "1970-01-01T00:16:41Z", "session": 3, "ids": ["c"], "triggered_from": "stop_hook"},
    ]));
    assert_eq!(
        already_recorded_pending_ids(&st, &mut rec, None, None).unwrap(),
        set(&["a"])
    );
    assert_eq!(
        already_recorded_pending_ids(&st, &mut rec, Some(&set(&["T"])), None).unwrap(),
        set(&["a", "b"])
    );
}

#[test]
fn override_ids_leave_no_expiry_row_and_no_auto_commit() {
    let mut f = fx(1000.0);
    let st = state_with(&["a", "b"], &mut f);
    let mut rec = Rec::new("S");
    f.now = 2801.0;
    advance_turn(&st, &mut rec, Some(&set(&["a"])), &mut f).unwrap();
    assert_eq!(rec.w.len(), 1);
    assert!(rec.w[0][1].as_str().unwrap().contains("['b']"));
    assert_eq!(rec.w[0][0], "use_token_expired");
}

#[test]
fn the_expiry_row_measures_the_first_oldest_and_truncates_toward_zero() {
    let mut f = fx(1000.0);
    let st = SessionState::new(&mut f);
    let tok = |id: &str, at: f64, turn: i64| PendingUseToken {
        token: "t".into(),
        memory_id: cps(id),
        issued_at: at,
        issued_at_turn: turn,
    };
    let mut rec = Rec::new("S");
    let g = st.lock();
    emit_expired_use_tokens(
        &g,
        &mut rec,
        &[tok("a", 1000.5, 1), tok("b", 1000.5, 5)],
        &mut f,
    )
    .unwrap();
    assert_eq!(
        rec.w[0][1],
        "{'ids': ['a', 'b'], 'age_seconds': 0, 'turns_since_issue': -1, 'reason': 'wall_clock_ttl'}"
    );
    rec.w.clear();
    emit_expired_use_tokens(
        &g,
        &mut rec,
        &[tok("a", f64::NAN, 0), tok("b", 0.0, 0)],
        &mut f,
    )
    .unwrap_err();
    assert!(rec.w.is_empty());
}

#[test]
fn the_epoch_is_rounded_once() {
    // 2**53 + 1 microseconds past the epoch: dividing the double nearest
    // the count by 1e6 rounds twice.
    let v = event_ts_epoch(Some(&PyValue::str("2255-06-05T23:47:34.740993Z"))).unwrap();
    assert_eq!(v, 9_007_199_254.740_993);
    let v = event_ts_epoch(Some(&PyValue::str("1970-01-01T00:00:00.000001Z"))).unwrap();
    assert_eq!(v, 1e-6);
}

#[test]
fn one_lock_across_the_auto_commit() {
    for _ in 0..50 {
        let mut f = fx(1000.0);
        let st = Arc::new(state_with(&["a", "b"], &mut f));
        st.lock().turn_counter = 10;
        let mut handles = Vec::new();
        for _ in 0..2 {
            let st = Arc::clone(&st);
            handles.push(thread::spawn(move || {
                let mut f = fx(1700.0);
                let mut rec = Rec::new("S");
                advance_turn(&st, &mut rec, None, &mut f).unwrap();
                rec.w.len()
            }));
        }
        let rows: usize = handles.into_iter().map(|h| h.join().unwrap()).sum();
        assert_eq!(rows, 1);
    }
}
