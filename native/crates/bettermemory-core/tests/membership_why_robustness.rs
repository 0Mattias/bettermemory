//! Hostile input to `why`: a log of 100,000 membership rows, a merge chain
//! and a parent chain of 1,000 projects, a chain of 10,000 retired
//! checkouts, a payload nested past `json::MAX_DEPTH`, a payload of very
//! many keys, integers past i64 and a sequence number at its bounds. No
//! panic, no stack overflow, and the time grows linearly with the log.

mod common;
mod timing;

use std::collections::BTreeMap;

use bettermemory_core::json::MAX_DEPTH;
use bettermemory_core::membership::Error;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::{cps, to_string};
use common::membership_core::Model;
use common::membership_why::{home, log_row, project, why_model, LOG_ROWS};

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn none() -> PyValue {
    PyValue::None
}

fn model(projects: Vec<PyValue>, homes: Vec<PyValue>, rows: Vec<PyValue>) -> Model {
    let tables = BTreeMap::from([
        ("projects".to_string(), projects),
        ("homes".to_string(), homes),
        ("proposals".to_string(), Vec::new()),
        (LOG_ROWS.to_string(), rows),
    ]);
    why_model(tables, None)
}

fn alpha() -> Vec<PyValue> {
    vec![project("P001", "alpha", &none(), &none())]
}

fn r1() -> Vec<PyValue> {
    vec![home("R1", "P001", &s("K1"), "file")]
}

/// A log of `n` rows, three in four of them decided for R1.
fn log(n: usize) -> Vec<PyValue> {
    (0..n)
        .map(|i| {
            let (kind, payload) = match i % 4 {
                0 => ("checkout_registered", r#"{"checkout": "K1", "rule": "r"}"#),
                1 => ("project_minted", r#"{"project": "P001"}"#),
                2 => (
                    "home_put",
                    r#"{"home": {"record_id": "R1", "checkout": "K1"}}"#,
                ),
                _ => ("checkout_moved", r#"{"checkout": "KX"}"#),
            };
            log_row(i as i64 + 1, kind, payload)
        })
        .collect()
}

fn class(m: &mut Model) -> &'static str {
    match m.why(&cps("R1")) {
        Err(Error::Raised(r)) => r.class,
        Err(Error::Store(_)) => "store",
        Ok(_) => "answered",
    }
}

#[test]
fn a_long_log_grows_linearly() {
    let mut small = model(alpha(), r1(), log(10_000));
    let mut large = model(alpha(), r1(), log(100_000));
    let w = large.why(&cps("R1")).expect("an answer");
    assert_eq!(w.rows.len(), 75_000);
    assert!(to_string(&w.text).expect("ASCII").ends_with("; 75000 rows"));
    timing::grows_within(
        "why over a long log",
        timing::TENFOLD,
        &mut || timing::once(|| small.why(&cps("R1")).expect("an answer")),
        &mut || timing::once(|| large.why(&cps("R1")).expect("an answer")),
    );
}

#[test]
fn a_merge_chain_of_a_thousand_projects() {
    // P0001 is merged into P0002, and so on to P1000: the record recorded
    // at the tail is homed at the head, and every project stands for it.
    let n = 1000;
    let id = |i: usize| format!("P{i:04}");
    let projects = (1..=n)
        .map(|i| {
            let into = if i < n { s(&id(i + 1)) } else { none() };
            project(&id(i), "chain", &into, &none())
        })
        .collect();
    let rows = (1..=n)
        .map(|i| {
            log_row(
                i as i64,
                "project_merged",
                &format!(r#"{{"project": "{}"}}"#, id(i)),
            )
        })
        .collect();
    let homes = vec![home("R1", &id(1), &s("K1"), "file")];
    let mut m = model(projects, homes, rows);
    let w = m.why(&cps("R1")).expect("an answer");
    assert_eq!(w.project, Some(cps(&id(n))));
    assert_eq!(w.rows.len(), n);
}

#[test]
fn a_parent_chain_of_a_thousand_projects() {
    let n = 1000;
    let id = |i: usize| format!("P{i:04}");
    let projects = (1..=n)
        .map(|i| {
            let parent = if i < n { s(&id(i + 1)) } else { none() };
            project(&id(i), "chain", &none(), &parent)
        })
        .collect();
    let rows = (1..=n)
        .map(|i| {
            log_row(
                i as i64,
                "project_minted",
                &format!(r#"{{"project": "{}"}}"#, id(i)),
            )
        })
        .collect();
    let homes = vec![home("R1", &id(1), &s("K1"), "file")];
    let mut m = model(projects, homes, rows);
    assert_eq!(m.why(&cps("R1")).expect("an answer").rows.len(), n);
}

/// `n` checkouts retired one into the next, the last into K1, and a row
/// naming each.
fn retired(n: usize) -> Model {
    let mut rows = Vec::new();
    for i in 0..n {
        let into = if i + 1 < n {
            format!("C{:06}", i + 1)
        } else {
            "K1".into()
        };
        let payload = format!(r#"{{"checkout": "C{i:06}", "into": "{into}"}}"#);
        rows.push(log_row(i as i64 + 1, "checkout_retired", &payload));
    }
    model(alpha(), r1(), rows)
}

#[test]
fn a_long_chain_of_retired_checkouts() {
    let mut small = retired(1_000);
    let mut large = retired(10_000);
    assert_eq!(large.why(&cps("R1")).expect("an answer").rows.len(), 10_000);
    timing::grows_within(
        "why over retired checkouts",
        timing::TENFOLD,
        &mut || timing::once(|| small.why(&cps("R1")).expect("an answer")),
        &mut || timing::once(|| large.why(&cps("R1")).expect("an answer")),
    );
}

#[test]
fn a_payload_nested_past_the_limit_is_recursion_error() {
    let deep = format!("{}{}", "[".repeat(MAX_DEPTH + 1), "]".repeat(MAX_DEPTH + 1));
    let mut m = model(alpha(), r1(), vec![log_row(1, "checkout_moved", &deep)]);
    assert_eq!(class(&mut m), "RecursionError");
    // In the folded checkouts' read too.
    let mut m = model(alpha(), r1(), vec![log_row(1, "checkout_retired", &deep)]);
    assert_eq!(class(&mut m), "RecursionError");
    // Within the limit, a deep value is read, compared and never hashed.
    let inner = MAX_DEPTH - 2;
    let nested = format!("{}{}", "[".repeat(inner), "]".repeat(inner));
    let rows = vec![
        log_row(1, "checkout_moved", &format!(r#"{{"checkout": {nested}}}"#)),
        log_row(
            2,
            "checkout_put",
            &format!(r#"{{"checkout": {{"id": {nested}}}}}"#),
        ),
    ];
    let mut m = model(alpha(), r1(), rows);
    assert_eq!(class(&mut m), "TypeError");
    // A deep rule is spelled by its repr.
    let rule = format!("{}{}", "[".repeat(5000), "]".repeat(5000));
    let rows = vec![log_row(
        1,
        "checkout_registered",
        &format!(r#"{{"checkout": "K1", "rule": {rule}}}"#),
    )];
    let mut m = model(alpha(), r1(), rows);
    let w = m.why(&cps("R1")).expect("an answer");
    assert_eq!(w.rows.len(), 1);
    assert!(to_string(&w.text)
        .expect("ASCII")
        .contains("joined by [[[["));
}

#[test]
fn a_payload_of_very_many_keys() {
    let mut payload = String::from("{");
    for i in 0..100_000 {
        payload.push_str(&format!(r#""k{i}": {i}, "#));
    }
    payload.push_str(r#""checkout": "K1"}"#);
    let mut m = model(alpha(), r1(), vec![log_row(1, "checkout_moved", &payload)]);
    assert_eq!(m.why(&cps("R1")).expect("an answer").rows.len(), 1);
}

#[test]
fn integers_past_i64_and_sequence_bounds() {
    let big = format!("1{}", "0".repeat(4000));
    let too_big = format!("1{}", "0".repeat(4300));
    let rows = vec![
        log_row(
            i64::MAX,
            "checkout_registered",
            &format!(r#"{{"checkout": "K1", "rule": {big}}}"#),
        ),
        log_row(
            i64::MIN,
            "checkout_moved",
            &format!(r#"{{"checkout": {too_big}}}"#),
        ),
        log_row(
            0,
            "checkout_moved",
            r#"{"checkout": 1e400, "proposal": -1e400}"#,
        ),
        log_row(
            -1,
            "home_put",
            r#"{"home": {"record_id": "R1", "checkout": 18446744073709551616}}"#,
        ),
    ];
    let mut m = model(alpha(), r1(), rows);
    let w = m.why(&cps("R1")).expect("an answer");
    assert_eq!(
        w.rows.iter().map(|r| r.seq).collect::<Vec<_>>(),
        vec![i64::MAX, -1]
    );
    let text = to_string(&w.text).expect("ASCII");
    assert!(
        text.contains("written in checkout 18446744073709551616 (homed now at K1)"),
        "{text}"
    );
    assert!(text.contains(&format!("joined by {big}; 2 rows")));
}
