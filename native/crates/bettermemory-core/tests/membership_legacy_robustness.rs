//! Hostile input to the legacy questions, the roots and the record
//! counts: many open questions, a directory of very many parts, many
//! projects asked at once, a rule nested deep. No panic, and the time
//! grows linearly with the input.

mod common;
mod timing;

use std::collections::BTreeMap;

use bettermemory_core::fsdata::Flavour;
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

fn project(id: &str, name: &str) -> PyValue {
    d(vec![
        ("id", s(id)),
        ("name", s(name)),
        ("merged_into", PyValue::None),
        ("provisional", PyValue::None),
        ("parent", PyValue::None),
        ("tenant", PyValue::None),
        ("folder", PyValue::int(0)),
        ("boundary", PyValue::int(0)),
        ("minted", s("2026-01-01T00:00:00")),
    ])
}

fn question(i: usize, rule: PyValue) -> PyValue {
    d(vec![
        ("id", s(&format!("Q{i:06}"))),
        ("checkout", PyValue::None),
        ("kind", s("legacy")),
        ("candidates_json", PyValue::List(vec![s("C1")])),
        (
            "evidence_json",
            d(vec![("unresolved_project", s("L1")), ("rule", rule)]),
        ),
        ("reason", s("legacy_no_live_checkout")),
        ("line", s("l")),
        ("state", s("open")),
        ("answer", PyValue::None),
        ("answered_by", PyValue::None),
        ("signature_json", PyValue::None),
        ("opened", s("t")),
    ])
}

fn questions(n: usize) -> Model {
    let qs = (0..n).map(|i| question(i, s("ambiguous"))).collect();
    over(vec![
        ("projects", vec![project("L1", "old"), project("C1", "old")]),
        ("proposals", qs),
    ])
}

#[test]
fn settle_grows_linearly_with_open_questions() {
    let mut small = || {
        let mut m = questions(300);
        timing::once(|| assert!(m.settle_legacy_questions().expect("ok").is_empty()))
    };
    let mut large = || {
        let mut m = questions(3000);
        timing::once(|| assert!(m.settle_legacy_questions().expect("ok").is_empty()))
    };
    timing::grows_within("settle", 30, &mut small, &mut large);
}

#[test]
fn a_rule_nested_deep_does_not_panic() {
    let mut rule = s("none");
    for _ in 0..100_000 {
        rule = PyValue::List(vec![rule]);
    }
    let mut m = over(vec![
        ("projects", vec![project("L1", "old"), project("C1", "new")]),
        ("proposals", vec![question(0, rule)]),
    ]);
    // Either closed (the rule is truthy and not a text legacy_asks reads
    // as asking) or a raised RecursionError; never a panic or an
    // overflowed stack, and the same in debug and release.
    match m.settle_legacy_questions() {
        Ok(got) => {
            assert_eq!(got.len(), 1);
            assert_eq!(got[0].id, cps("Q000000"));
        }
        Err(bettermemory_core::membership::Error::Raised(r)) => {
            assert_eq!(r.class, "RecursionError");
        }
        Err(e) => panic!("neither closed nor RecursionError: {e:?}"),
    }
}

fn view_model() -> Model {
    over(vec![
        ("projects", vec![project("P1", "a")]),
        (
            "checkouts",
            vec![d(vec![
                ("id", s("K1")),
                ("project", s("P1")),
                ("machine", s("M1")),
                ("kind", s("primary")),
                ("object_device", PyValue::None),
                ("object_inode", PyValue::None),
                ("object_birth", PyValue::None),
                ("common_device", PyValue::None),
                ("common_inode", PyValue::None),
                ("common_birth", PyValue::None),
                ("git_dir", PyValue::None),
                ("token", PyValue::None),
                ("token_status", PyValue::None),
                ("stamps_json", PyValue::None),
                ("last_head", PyValue::None),
                ("branch", PyValue::None),
                ("remotes_json", PyValue::List(vec![])),
                ("manifest_json", PyValue::None),
                ("legacy", PyValue::int(0)),
                ("retired", PyValue::int(0)),
                ("registered", s("t")),
                ("unresolved_json", PyValue::None),
            ])],
        ),
        (
            "checkout_views",
            vec![d(vec![
                ("checkout", s("K1")),
                ("machine", s("M1")),
                ("path", s("/w")),
                ("first_seen", s("t")),
                ("last_seen", s("t")),
                ("seen_order", PyValue::int(1)),
            ])],
        ),
    ])
}

#[test]
fn below_root_grows_linearly_with_the_directory() {
    let m = view_model();
    let k = m.checkout(&cps("K1")).expect("a checkout");
    let dir = |n: usize| cps(&format!("/w{}", "/a/..".repeat(n)));
    let (d1, d2) = (dir(5_000), dir(50_000));
    for f in [Flavour::Posix, Flavour::Windows] {
        assert_eq!(m.below_root_in(f, &k, &d1, None), Ok(Some(false)));
    }
    let mut small = || timing::once(|| m.below_root_in(Flavour::Posix, &k, &d1, None));
    let mut large = || timing::once(|| m.below_root_in(Flavour::Posix, &k, &d2, None));
    timing::grows_within("below_root", 30, &mut small, &mut large);
}

fn homes(n: usize) -> (Model, Vec<Vec<u32>>) {
    let projects = (0..n).map(|i| project(&format!("P{i:06}"), "p")).collect();
    let homes = (0..n)
        .step_by(n / 10)
        .map(|i| {
            d(vec![
                ("record_id", s(&format!("R{i}"))),
                ("kind", s(if i % 2 == 0 { "memory" } else { "episode" })),
                ("project", s(&format!("P{i:06}"))),
            ])
        })
        .collect();
    let ids = (0..n).map(|i| cps(&format!("P{i:06}"))).collect();
    (over(vec![("projects", projects), ("homes", homes)]), ids)
}

#[test]
fn records_by_project_grows_linearly_with_the_projects() {
    // Ten homes whatever the size: the fake store's column_in scans the
    // asked list once per row, so the timed part is the port's own.
    let (mut m, ids) = homes(4000);
    let got = m.records_by_project(&ids).expect("counts");
    assert_eq!(got.len(), 4000);
    assert_eq!(got[0].1, (1, 0));
    assert_eq!(got[1].1, (0, 0));
    let mut small = || {
        let (mut m, ids) = homes(200);
        timing::once(|| m.records_by_project(&ids).expect("counts"))
    };
    let mut large = || {
        let (mut m, ids) = homes(2000);
        timing::once(|| m.records_by_project(&ids).expect("counts"))
    };
    timing::grows_within("records_by_project", 30, &mut small, &mut large);
}
