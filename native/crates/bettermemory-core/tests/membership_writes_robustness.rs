//! The writes unit (membership-2) over hostile input: a commit of a
//! million characters, a project with thousands of witnesses and blobs,
//! a long parent chain made a boundary. No panic in debug or release,
//! and the witness write linear in the rows it reads. Each blob row names
//! its own witness: the recording store deletes by a scan of the table,
//! where the store's index finds the rows, so a released witness with
//! many blobs would time the fake, not the model.

mod common;
mod timing;

use std::collections::BTreeMap;

use bettermemory_core::membership::Error;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use common::membership_core::*;
use common::membership_writes::History;

fn d(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn project(id: &str, parent: Option<&str>) -> PyValue {
    let o = |v: Option<&str>| v.map_or(PyValue::None, PyValue::str);
    d(vec![
        ("id", PyValue::str(id)),
        ("name", PyValue::str(id)),
        ("merged_into", PyValue::None),
        ("provisional", PyValue::None),
        ("parent", o(parent)),
        ("tenant", PyValue::None),
        ("folder", PyValue::int(0)),
        ("boundary", PyValue::int(0)),
        ("minted", PyValue::str("t")),
    ])
}

fn over(tables: Vec<(&str, Vec<PyValue>)>) -> Model {
    let init = d(vec![
        (
            "now",
            PyValue::List(vec![PyValue::str("2026-01-01T00:00:00"), PyValue::int(0)]),
        ),
        ("session", PyValue::None),
        ("given", PyValue::Bool(true)),
    ]);
    let tables: BTreeMap<String, Vec<PyValue>> = tables
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect();
    model(&init, tables, None, false)
}

#[test]
fn a_huge_commit_is_refused_with_seventy_two_shown() {
    let mut m = over(vec![]);
    let commit = cps(&"\u{e9}".repeat(1_000_000));
    match m.add_witness(&cps("P1"), &commit, &cps("s"), None) {
        Err(Error::Raised(r)) => {
            assert_eq!(r.class, "ValueError");
            assert_eq!(r.message.chars().filter(|&c| c == '\u{e9}').count(), 72);
        }
        other => panic!("{other:?}"),
    }
}

fn witnesses(n: usize) -> Vec<PyValue> {
    (0..n)
        .map(|i| {
            d(vec![
                ("project", PyValue::str("P1")),
                ("commit_id", PyValue::str(&format!("{:040x}", 1000 + i))),
                ("arrival", PyValue::int(i as i128 + 1)),
            ])
        })
        .collect()
}

fn blobs(n: usize) -> Vec<PyValue> {
    (0..n)
        .map(|i| {
            d(vec![
                ("project", PyValue::str("P1")),
                ("blob", PyValue::str(&format!("b{i}"))),
                ("witness", PyValue::str(&format!("{:040x}", 1000 + i))),
            ])
        })
        .collect()
}

fn add(n: usize) {
    let mut m = over(vec![
        ("projects", vec![project("P1", None)]),
        ("project_witnesses", witnesses(n)),
        ("project_blobs", blobs(n)),
    ]);
    let many: Vec<PyStr> = (0..n).map(|i| cps(&format!("n{i}"))).collect();
    let h = History {
        fingerprint: None,
        blobs: [(cps(&format!("{:040x}", 1)), many)].into_iter().collect(),
    };
    m.add_witness(
        &cps("P1"),
        &cps(&format!("{:040x}", 1)),
        &cps("s"),
        Some(&h),
    )
    .unwrap();
}

#[test]
fn many_witnesses_and_blobs() {
    add(20_000);
}

#[test]
fn the_witness_write_grows_linearly() {
    timing::grows_within(
        "add_witness: 2,000 and 20,000 rows",
        timing::TENFOLD,
        &mut || timing::once(|| add(2_000)),
        &mut || timing::once(|| add(20_000)),
    );
}

#[test]
fn a_long_chain_made_a_boundary() {
    let n = 300;
    let mut ps = vec![project("P0", None)];
    for i in 1..n {
        ps.push(project(&format!("P{i}"), Some(&format!("P{}", i - 1))));
    }
    let mut m = over(vec![("projects", ps)]);
    m.make_boundary(&cps("P0"), Some(&cps("/"))).unwrap();
    let last = m.project(&cps(&format!("P{}", n - 1))).unwrap();
    assert_eq!(last.tenant, Some(cps("P0")));
    m.lift_boundary(&cps("P0"), None).unwrap();
    assert_eq!(m.project(&cps("P5")).unwrap().tenant, None);
}
