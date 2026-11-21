//! `Membership` (unit membership-1) over hostile stores: long merge and
//! parent chains, a merge cycle of every project, very long names and
//! commits, a payload nested past the recursion bound. No panic, and the
//! merge walk linear in the chain. `chain_of` and `named` over a chain
//! are quadratic in the reference too (`pid not in out`, a `_canon` per
//! project), so their chains are short.

mod common;
mod timing;

use std::collections::BTreeMap;

use bettermemory_core::membership::Error;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;
use common::membership_core::*;

fn project(id: &str, name: &str, merged: Option<&str>, parent: Option<&str>) -> PyValue {
    let o = |v: Option<&str>| v.map_or(PyValue::None, PyValue::str);
    PyValue::Dict(vec![
        (PyValue::str("id"), PyValue::str(id)),
        (PyValue::str("name"), PyValue::str(name)),
        (PyValue::str("merged_into"), o(merged)),
        (PyValue::str("provisional"), PyValue::None),
        (PyValue::str("parent"), o(parent)),
        (PyValue::str("tenant"), PyValue::None),
        (PyValue::str("folder"), PyValue::int(0)),
        (PyValue::str("boundary"), PyValue::int(0)),
        (PyValue::str("minted"), PyValue::str("t")),
    ])
}

fn over(tables: Vec<(&str, Vec<PyValue>)>) -> Model {
    let init = PyValue::Dict(vec![
        (
            PyValue::str("now"),
            PyValue::List(vec![PyValue::str("2026-01-01T00:00:00"), PyValue::int(0)]),
        ),
        (PyValue::str("session"), PyValue::None),
        (PyValue::str("given"), PyValue::Bool(false)),
    ]);
    let tables: BTreeMap<String, Vec<PyValue>> = tables
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect();
    model(&init, tables, None, false)
}

fn chain(n: usize, merged: bool, cycle: bool) -> Vec<PyValue> {
    (0..n)
        .map(|i| {
            let next = if i + 1 < n {
                format!("P{}", i + 1)
            } else if cycle {
                "P0".to_string()
            } else {
                String::new()
            };
            let link = (!next.is_empty()).then_some(next.as_str());
            if merged {
                project(&format!("P{i}"), "x", link, None)
            } else {
                project(&format!("P{i}"), "x", None, link)
            }
        })
        .collect()
}

#[test]
fn long_chains_and_cycles_end() {
    let mut m = over(vec![("projects", chain(2_000, true, true))]);
    assert_eq!(m.canonical(Some(&cps("P0"))).unwrap(), Some(cps("P0")));
    assert_eq!(m.canonical(Some(&cps("P5"))).unwrap(), Some(cps("P5")));
    let mut m = over(vec![("projects", chain(2_000, false, true))]);
    assert_eq!(m.chain_of(Some(&cps("P0"))).unwrap().len(), 2_000);
    let mut m = over(vec![("projects", chain(2_000, true, false))]);
    assert_eq!(m.canonical(Some(&cps("P0"))).unwrap(), Some(cps("P1999")));
    assert!(m.named(&[cps("projects:X")]).unwrap().len() == 1);
}

#[test]
fn long_names_commits_and_deep_payloads() {
    let long = "\u{df}".repeat(200_000);
    let commit = "a".repeat(1_000_000);
    let mut m = over(vec![
        ("projects", vec![project("P1", &long, None, None)]),
        (
            "project_witnesses",
            vec![PyValue::Dict(vec![
                (PyValue::str("project"), PyValue::str("P1")),
                (PyValue::str("commit_id"), PyValue::str(&commit)),
                (PyValue::str("arrival"), PyValue::int(1)),
            ])],
        ),
    ]);
    assert_eq!(
        m.named(&[cps(&format!("projects:{}", "ss".repeat(200_000)))])
            .unwrap()
            .len(),
        1
    );
    assert!(m.witness_commits(None).unwrap().is_empty());
    assert!(!m.has_witness(&cps("P1"), &cps(&commit)).unwrap());
    let mut deep = PyValue::int(1);
    for _ in 0..100_000 {
        deep = PyValue::List(vec![deep]);
    }
    let got = m.event("deep", vec![("x", deep)]);
    assert!(matches!(got, Err(Error::Raised(r)) if r.class == "RecursionError"));
}

#[test]
fn canonical_is_linear_in_the_merge_chain() {
    let run = |n: usize| {
        let mut m = over(vec![("projects", chain(n, true, false))]);
        m.project(&cps("P0")).unwrap();
        timing::once(|| m.canonical(Some(&cps("P0"))).unwrap())
    };
    timing::grows_within(
        "canonical: chains of 5,000 and 50,000 merges",
        timing::TENFOLD,
        &mut || run(5_000),
        &mut || run(50_000),
    );
}
