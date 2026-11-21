//! Cases a plausible wrong `Membership` writes unit (membership-2) fails:
//! the witness bound and the oldest it keeps, the blob cap, the full-id
//! test on the commit as given, the token and manifest a registration
//! keeps, the order of its ids, a boundary that moves only the previous
//! tenant's projects, and the common key a refresh takes.

mod common;

use std::collections::BTreeMap;

use bettermemory_core::capture::location::ObjectKey;
use bettermemory_core::capture::Kind;
use bettermemory_core::membership::vocab::WITNESS_BOUND;
use bettermemory_core::membership::writes::{BLOBS_PER_WITNESS, KEEP_OLDEST};
use bettermemory_core::membership::Error;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use common::membership_core::*;
use common::membership_writes::{location, History};

fn d(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn project(id: &str, parent: Option<&str>, tenant: Option<&str>, boundary: bool) -> PyValue {
    let o = |v: Option<&str>| v.map_or(PyValue::None, PyValue::str);
    d(vec![
        ("id", PyValue::str(id)),
        ("name", PyValue::str(id)),
        ("merged_into", PyValue::None),
        ("provisional", PyValue::None),
        ("parent", o(parent)),
        ("tenant", o(tenant)),
        ("folder", PyValue::int(0)),
        ("boundary", PyValue::int(i128::from(boundary))),
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

fn hex(i: usize) -> String {
    format!("{i:040x}")
}

fn witnesses(n: usize) -> Vec<PyValue> {
    (0..n)
        .map(|i| {
            d(vec![
                ("project", PyValue::str("P1")),
                ("commit_id", PyValue::str(&hex(100 + i))),
                ("arrival", PyValue::int(i as i128 + 1)),
            ])
        })
        .collect()
}

fn table(m: &Model, t: &str) -> Vec<PyValue> {
    m.store()
        .0
        .borrow()
        .tables
        .get(t)
        .cloned()
        .unwrap_or_default()
}

fn commits(m: &Model) -> Vec<PyStr> {
    table(m, "project_witnesses")
        .iter()
        .map(|r| match r.get("commit_id") {
            Some(PyValue::Str(s)) => s.clone(),
            _ => panic!("a commit"),
        })
        .collect()
}

#[test]
fn the_constants() {
    assert_eq!(BLOBS_PER_WITNESS, 8);
    assert_eq!(KEEP_OLDEST, 16);
    assert_eq!(WITNESS_BOUND, 64);
}

#[test]
fn the_bound_releases_the_seventeenth_oldest() {
    let mut m = over(vec![
        ("projects", vec![project("P1", None, None, false)]),
        ("project_witnesses", witnesses(64)),
    ]);
    m.add_witness(&cps("P1"), &cps(&hex(1)), &cps("s"), None)
        .unwrap();
    let c = commits(&m);
    assert_eq!(c.len(), 64);
    assert!(!c.contains(&cps(&hex(100 + KEEP_OLDEST))));
    assert!(c.contains(&cps(&hex(100))));
    assert!(c.contains(&cps(&hex(100 + KEEP_OLDEST - 1))));
}

#[test]
fn at_the_bound_nothing_is_released() {
    let mut m = over(vec![
        ("projects", vec![project("P1", None, None, false)]),
        ("project_witnesses", witnesses(63)),
    ]);
    m.add_witness(&cps("P1"), &cps(&hex(1)), &cps("s"), None)
        .unwrap();
    assert_eq!(commits(&m).len(), 64);
}

#[test]
fn at_most_eight_new_blobs_the_smallest() {
    let mut m = over(vec![("projects", vec![project("P1", None, None, false)])]);
    let blobs: Vec<PyStr> = ["k", "a", "j", "b", "i", "c", "h", "d", "g", "e", "f", "a"]
        .iter()
        .map(|s| cps(s))
        .collect();
    let h = History {
        fingerprint: Some(cps("fp")),
        blobs: [(cps(&hex(1)), blobs)].into_iter().collect(),
    };
    m.add_witness(&cps("P1"), &cps(&hex(1)), &cps("s"), Some(&h))
        .unwrap();
    let got: Vec<PyValue> = table(&m, "project_blobs")
        .iter()
        .map(|r| r.get("blob").unwrap().clone())
        .collect();
    let want: Vec<PyValue> = ["a", "b", "c", "d", "e", "f", "g", "h"]
        .iter()
        .map(|s| PyValue::str(s))
        .collect();
    assert_eq!(got, want);
}

#[test]
fn an_upper_case_commit_is_refused_and_writes_nothing() {
    let mut m = over(vec![("projects", vec![project("P1", None, None, false)])]);
    let e = m.add_witness(&cps("P1"), &cps(&hex(10).to_uppercase()), &cps("s"), None);
    assert!(matches!(e, Err(Error::Raised(r)) if r.class == "ValueError"));
    assert!(table(&m, "project_witnesses").is_empty());
}

fn key(inode: &str) -> ObjectKey {
    ObjectKey {
        device: cps("1"),
        inode: cps(inode),
        birth: Some(cps("b")),
    }
}

fn spec(items: Vec<(&str, PyValue)>) -> PyValue {
    d(items)
}

#[test]
fn an_empty_token_takes_the_written_one() {
    let mut m = over(vec![]);
    let loc = location(&spec(vec![
        ("root", PyValue::str("/r")),
        ("token", PyValue::str("")),
        ("token_written", PyValue::str("w")),
    ]));
    let k = m.register(&loc, &cps("P1"), &cps("x")).unwrap();
    assert_eq!(k.token, Some(cps("w")));
    assert_eq!(k.kind, cps("primary"));
}

#[test]
fn an_unborn_checkout_without_a_token_keeps_its_manifest() {
    let manifest = d(vec![
        ("name", PyValue::str("u")),
        ("entries", PyValue::List(vec![])),
    ]);
    let mut m = over(vec![]);
    let mut loc = location(&spec(vec![
        ("root", PyValue::str("/r")),
        ("manifest", manifest.clone()),
    ]));
    assert!(m
        .register(&loc, &cps("P1"), &cps("x"))
        .unwrap()
        .manifest
        .is_some());
    loc.head = Some(cps("h"));
    assert!(m
        .register(&loc, &cps("P1"), &cps("x"))
        .unwrap()
        .manifest
        .is_none());
    loc.kind = Some(Kind::Folder);
    assert!(m
        .register(&loc, &cps("P1"), &cps("x"))
        .unwrap()
        .manifest
        .is_some());
}

#[test]
fn the_id_is_minted_before_the_view_number() {
    let mut m = over(vec![]);
    let loc = location(&spec(vec![("root", PyValue::str("/r/"))]));
    let k = m.register(&loc, &cps("P1"), &cps("x")).unwrap();
    assert_eq!(k.views[0].path, cps("/r"));
    let log = m.store().0.borrow().log.clone();
    let first: Vec<String> = log
        .iter()
        .filter_map(|e| match e {
            PyValue::List(v) => match &v[0] {
                PyValue::Str(s) => {
                    Some(String::from_utf8(s.iter().map(|&c| c as u8).collect()).unwrap())
                }
                _ => None,
            },
            _ => None,
        })
        .collect();
    assert_eq!(first, ["next_id", "next_number", "put", "put", "record"]);
}

#[test]
fn a_boundary_moves_only_the_previous_tenants_projects() {
    let mut m = over(vec![(
        "projects",
        vec![
            project("P1", None, Some("T"), false),
            project("P2", Some("P1"), Some("T"), false),
            project("P3", Some("P2"), Some("P3"), true),
            project("P4", Some("P3"), Some("P3"), false),
        ],
    )]);
    m.make_boundary(&cps("P1"), None).unwrap();
    let tenant = |m: &mut Model, id: &str| m.project(&cps(id)).unwrap().tenant;
    assert_eq!(tenant(&mut m, "P1"), Some(cps("P1")));
    assert_eq!(tenant(&mut m, "P2"), Some(cps("P1")));
    assert_eq!(tenant(&mut m, "P3"), Some(cps("P3")));
    assert_eq!(tenant(&mut m, "P4"), Some(cps("P3")));
    m.lift_boundary(&cps("P1"), Some(&cps("OUT"))).unwrap();
    assert_eq!(tenant(&mut m, "P1"), Some(cps("OUT")));
    assert_eq!(tenant(&mut m, "P2"), Some(cps("OUT")));
    assert_eq!(tenant(&mut m, "P4"), Some(cps("P3")));
}

#[test]
fn a_refresh_takes_the_common_key_only_of_the_same_number() {
    let mut m = over(vec![]);
    let row = |common: &str| {
        let mut k = rows_checkout();
        k.object_key = Some(key("1"));
        k.common_object_key = Some(key(common));
        k
    };
    let mut loc = location(&spec(vec![]));
    loc.object_key = Some(ObjectKey {
        birth: Some(cps("c")),
        ..key("1")
    });
    loc.common_object_key = Some(key("9"));
    let k = m.refresh_object(row("1"), &loc).unwrap();
    assert_eq!(k.common_object_key, Some(key("9")));
    let k = m.refresh_object(row("2"), &loc).unwrap();
    assert_eq!(k.common_object_key, Some(key("2")));
    loc.common_object_key = None;
    let k = m.refresh_object(row("1"), &loc).unwrap();
    assert_eq!(k.common_object_key, Some(key("1")));
}

fn rows_checkout() -> bettermemory_core::membership_records::records::Checkout {
    let loc = location(&spec(vec![("root", PyValue::str("/r"))]));
    over(vec![]).register(&loc, &cps("P1"), &cps("x")).unwrap()
}
