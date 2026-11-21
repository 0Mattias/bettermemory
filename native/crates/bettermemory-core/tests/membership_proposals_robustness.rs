//! Hostile input to the machine rows and the proposals: deeply nested
//! evidence and facts, many open proposals on one checkout. No panic, and
//! the time grows linearly with the number of open proposals and with
//! the size of the evidence a restatement compares.

mod common;
mod timing;

use std::collections::BTreeMap;

use bettermemory_core::membership::proposals::{Line, Opening};
use bettermemory_core::membership::Error;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;
use common::membership_core::{model, Model};

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn d(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(items.into_iter().map(|(k, v)| (s(k), v)).collect())
}

fn over(tables: Vec<(&str, Vec<PyValue>)>) -> Model {
    let init = d(vec![
        (
            "now",
            PyValue::List(vec![s("2026-01-01T00:00:00"), PyValue::int(0)]),
        ),
        ("session", PyValue::None),
        ("given", PyValue::Bool(true)),
    ]);
    let t: BTreeMap<String, Vec<PyValue>> = tables
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect();
    model(&init, t, None, false)
}

fn nested(depth: usize) -> PyValue {
    let mut v = PyValue::None;
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

fn q_row(i: usize) -> PyValue {
    d(vec![
        ("id", PyValue::Str(cps(&format!("Q{i:05}")))),
        ("checkout", s("K1")),
        ("kind", s("first_sight")),
        ("candidates_json", PyValue::List(vec![])),
        ("evidence_json", d(vec![("p", PyValue::int(i as i128))])),
        ("reason", s("r")),
        ("line", s("l")),
        ("state", s("open")),
        ("answer", PyValue::None),
        ("answered_by", PyValue::None),
        ("signature_json", PyValue::None),
        ("opened", s("o")),
    ])
}

fn opening(evidence: Vec<(PyValue, PyValue)>) -> Opening<'static> {
    Opening {
        checkout: Some(cps("K1")),
        kind: cps("legacy"),
        candidates: vec![],
        evidence,
        reason: cps("r"),
        line: Line::Text(cps("l")),
        distinct: Some(cps("p")),
    }
}

#[test]
fn deeply_nested_evidence_raises_or_answers_without_panic() {
    for depth in [10, 900, 100_000] {
        let mut m = over(vec![]);
        match m.open_proposal(&opening(vec![(s("p"), nested(depth))])) {
            Ok(pr) => assert_eq!(pr.id, cps("Q001")),
            Err(Error::Raised(r)) => assert_eq!(r.class, "RecursionError"),
            Err(e) => panic!("{e:?}"),
        }
    }
}

#[test]
fn deeply_nested_stored_facts_compare_without_panic() {
    let row = d(vec![
        ("id", s("M1")),
        ("facts_json", nested(200_000)),
        ("predecessor", PyValue::None),
        ("minted", s("x")),
    ]);
    let mut m = over(vec![("machines", vec![row])]);
    let id = bettermemory_core::machine::MachineIdentity {
        id: cps("M1"),
        facts: bettermemory_core::machine::MachineFacts {
            hardware: None,
            hostname: None,
            id_file: None,
        },
        predecessor: None,
        minted: false,
        cloned: false,
        facts_updated: false,
        recorded: None,
    };
    let _ = m.record_machine(&id);
    let _ = m.machines();
}

#[test]
fn many_open_proposals_are_scanned_in_linear_time() {
    let run = |n: usize| {
        let mut m = over(vec![("proposals", (0..n).map(q_row).collect())]);
        timing::once(|| {
            let got = m
                .open_proposal(&opening(vec![(s("p"), PyValue::int(-1))]))
                .expect("opened");
            assert_eq!(got.state, cps("open"));
        })
    };
    timing::grows_within(
        "open_proposal over 300 and 3,000 open proposals",
        timing::TENFOLD,
        &mut || run(300),
        &mut || run(3_000),
    );
}

#[test]
fn a_restatement_compares_large_evidence_in_linear_time() {
    let run = |n: usize| {
        let stored = PyValue::Dict(
            (0..n)
                .map(|i| (PyValue::Str(cps(&format!("k{i}"))), PyValue::int(i as i128)))
                .collect(),
        );
        let mut row = q_row(0);
        if let PyValue::Dict(items) = &mut row {
            for (k, v) in items.iter_mut() {
                if *k == s("evidence_json") {
                    *v = stored.clone();
                }
            }
        }
        let given: Vec<(PyValue, PyValue)> = match &stored {
            PyValue::Dict(items) => items.iter().rev().cloned().collect(),
            _ => unreachable!(),
        };
        let mut m = over(vec![("proposals", vec![row])]);
        timing::once(|| {
            let got = m
                .restate_proposal(&cps("Q00000"), &cps("l"), &given)
                .expect("restated");
            assert_eq!(got.state, cps("open"));
        })
    };
    timing::grows_within(
        "restate_proposal over 2,000 and 20,000 evidence keys",
        timing::TENFOLD,
        &mut || run(2_000),
        &mut || run(20_000),
    );
}

#[test]
fn a_restatement_compares_large_nested_evidence_in_linear_time() {
    let run = |n: usize| {
        let inner: Vec<(PyValue, PyValue)> = (0..n)
            .map(|i| (PyValue::Str(cps(&format!("k{i}"))), PyValue::int(i as i128)))
            .collect();
        let stored = PyValue::Dict(vec![(s("a"), PyValue::Dict(inner.clone()))]);
        let mut row = q_row(0);
        if let PyValue::Dict(items) = &mut row {
            for (k, v) in items.iter_mut() {
                if *k == s("evidence_json") {
                    *v = stored.clone();
                }
            }
        }
        let given = vec![(s("a"), PyValue::Dict(inner.iter().rev().cloned().collect()))];
        let mut m = over(vec![("proposals", vec![row])]);
        timing::once(|| {
            let got = m
                .restate_proposal(&cps("Q00000"), &cps("l"), &given)
                .expect("restated");
            assert_eq!(got.state, cps("open"));
        })
    };
    timing::grows_within(
        "restate_proposal over 2,000 and 20,000 keys of a nested evidence dict",
        timing::TENFOLD,
        &mut || run(2_000),
        &mut || run(20_000),
    );
}
