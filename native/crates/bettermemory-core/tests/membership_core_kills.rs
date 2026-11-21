//! Cases a plausible wrong `Membership` (unit membership-1) fails: the
//! full-id test at its bounds, a call's rollback, the cache of projects
//! and of the members index, merge cycles, KeyError's text, the stamp.

mod common;

use std::collections::BTreeMap;

use bettermemory_core::membership::reads::full_sha;
use bettermemory_core::membership::vocab::{self, WITNESS_BOUND};
use bettermemory_core::membership::Error;
use bettermemory_core::membership_lines::Raised;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use common::membership_core::*;

fn init(now: &str, minutes: i128) -> PyValue {
    PyValue::Dict(vec![
        (
            PyValue::str("now"),
            PyValue::List(vec![PyValue::str(now), PyValue::int(minutes)]),
        ),
        (PyValue::str("session"), PyValue::None),
        (PyValue::str("given"), PyValue::Bool(false)),
    ])
}

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

fn over(projects: Vec<PyValue>) -> Model {
    let tables: BTreeMap<String, Vec<PyValue>> =
        [("projects".to_string(), projects)].into_iter().collect();
    model(&init("2026-01-01T00:00:00", 0), tables, None, false)
}

fn ids(v: &[&str]) -> Vec<PyStr> {
    v.iter().map(|s| cps(s)).collect()
}

#[test]
fn full_sha_bounds() {
    let hex = "0123456789abcdef".repeat(5);
    for n in [0, 1, 39, 41, 63, 65, 80] {
        assert!(!full_sha(&cps(&hex[..n])), "{n}");
    }
    assert!(full_sha(&cps(&hex[..40])));
    assert!(full_sha(&cps(&hex[..64])));
    assert!(!full_sha(&cps(&hex[..40].to_uppercase())));
    assert!(!full_sha(&cps(&format!("{}g", &hex[..39]))));
    assert!(!full_sha(&cps(&format!("{}\n", &hex[..39]))));
    assert_eq!(WITNESS_BOUND, 64);
    assert_eq!(vocab::PROJECT_SCOPE, "projects:");
}

#[test]
fn a_failed_call_leaves_no_partial_puts() {
    let mut m = over(vec![project("P001", "a", None, None)]);
    let before = m.store().0.borrow().tables.clone();
    let got: Result<(), _> = m.call(|m| {
        m.mint_project(&cps("x"), &cps("new"), None, None, None, false, false)?;
        m.put_project(
            bettermemory_core::membership_rows::project_from(&project("P009", "y", None, None))
                .unwrap(),
        )?;
        Err(Error::Raised(Raised {
            class: "_Boom",
            message: String::new(),
        }))
    });
    assert!(got.is_err());
    assert_eq!(m.store().0.borrow().tables, before);
    // The reference keeps the rows it collected and its read of the projects.
    assert_eq!(m.rows(), &[1, 2, 3][..]);
    assert!(m.project(&cps("P009")).is_ok());
}

#[test]
fn projects_are_read_once_a_call() {
    let mut m = over(vec![project("P001", "a", None, None)]);
    m.project(&cps("P001")).unwrap();
    m.project(&cps("P001")).unwrap();
    let reads = |m: &Model| m.store().0.borrow().log.len();
    assert_eq!(reads(&m), 1);
    m.call(|m| m.project(&cps("P001")).map(|_| ())).unwrap();
    assert_eq!(reads(&m), 4);
}

#[test]
fn members_index_follows_a_put_project() {
    let mut m = over(vec![
        project("P001", "a", None, None),
        project("P002", "b", Some("P001"), None),
    ]);
    assert_eq!(m.members(&cps("P001")).unwrap(), ids(&["P001", "P002"]));
    let p = bettermemory_core::membership_rows::project_from(&project("P002", "b", None, None))
        .unwrap();
    m.put_project(p).unwrap();
    assert_eq!(m.members(&cps("P001")).unwrap(), ids(&["P001"]));
    assert_eq!(m.members(&cps("P002")).unwrap(), ids(&["P002"]));
}

#[test]
fn cycles_and_global_end() {
    let mut m = over(vec![
        project("P001", "a", Some("P002"), None),
        project("P002", "b", Some("P001"), None),
        project("P003", "c", None, Some("P003")),
        project("P004", "d", Some("global"), None),
    ]);
    assert_eq!(m.canonical(Some(&cps("P001"))).unwrap(), Some(cps("P001")));
    assert_eq!(m.canonical(Some(&cps("P002"))).unwrap(), Some(cps("P002")));
    assert_eq!(
        m.canonical(Some(&cps("P004"))).unwrap(),
        Some(cps("global"))
    );
    assert_eq!(m.canonical(None).unwrap(), None);
    assert_eq!(m.chain_of(Some(&cps("P003"))).unwrap(), ids(&["P003"]));
    assert_eq!(m.chain_of(Some(&cps("P004"))).unwrap(), Vec::<PyStr>::new());
    assert_eq!(m.canon(&cps("PX")).unwrap(), cps("PX"));
}

#[test]
fn key_error_text_is_the_repr() {
    let mut m = over(vec![]);
    let text = |e: Error<PyStr>| match e {
        Error::Raised(r) => (r.class, r.message),
        Error::Store(_) => panic!("a store error"),
    };
    assert_eq!(
        text(m.project(&cps("PX")).unwrap_err()),
        ("KeyError", "'PX'".to_string())
    );
    assert_eq!(
        text(m.project(&cps("a'b")).unwrap_err()),
        ("KeyError", "\"a'b\"".to_string())
    );
    assert_eq!(
        text(m.checkout(&cps("K'1")).unwrap_err()),
        ("KeyError", "\"K'1\"".to_string())
    );
}

#[test]
fn named_needs_a_name_after_the_prefix() {
    let mut m = over(vec![
        project("P001", "", None, None),
        project("P002", "A", None, None),
    ]);
    assert!(m.named(&ids(&["projects:"])).unwrap().is_empty());
    assert!(m.named(&ids(&["Projects:a"])).unwrap().is_empty());
    assert_eq!(
        m.named(&ids(&["projects:a"]))
            .unwrap()
            .into_iter()
            .collect::<Vec<_>>(),
        ids(&["P002"])
    );
}

#[test]
fn stamp_in_utc_and_its_refusals() {
    let m = model(
        &init("2026-03-04T00:30:00", 90),
        BTreeMap::new(),
        None,
        false,
    );
    assert_eq!(m.stamp().unwrap(), cps("2026-03-03T23:00:00Z"));
    let m = model(
        &init("0001-01-01T00:30:00", 60),
        BTreeMap::new(),
        None,
        false,
    );
    assert!(matches!(m.stamp(), Err(Error::Raised(r)) if r.class == "OverflowError"));
}

#[test]
fn reads_here_only_with_the_location_liveness() {
    let m = over(vec![]);
    assert!(m.reads_here());
    let mut i = init("2026-01-01T00:00:00", 0);
    if let PyValue::Dict(items) = &mut i {
        items[2].1 = PyValue::Bool(true);
    }
    let m = model(&i, BTreeMap::new(), None, false);
    assert!(!m.reads_here());
}

#[test]
fn members_index_is_rebuilt_after_a_rolled_back_call() {
    let mut m = over(vec![project("P001", "a", None, None)]);
    let minted: Result<(), _> = m.call(|m| {
        let p = m.mint_project(&cps("x"), &cps("new"), None, None, None, false, false)?;
        assert_eq!(m.members(&p.id)?, vec![p.id.clone()]);
        Err(Error::Raised(Raised {
            class: "_Boom",
            message: String::new(),
        }))
    });
    assert!(minted.is_err());
    // The reference reads a new `_projects` dict, so its index is rebuilt.
    let got = m.call(|m| m.members(&cps("P002"))).unwrap();
    assert_eq!(got, Vec::<PyStr>::new());
}

#[test]
fn a_global_row_ends_canonical_and_chain_of() {
    let mut m = over(vec![
        project("global", "g", Some("P001"), Some("P001")),
        project("P001", "a", None, None),
    ]);
    assert_eq!(
        m.canonical(Some(&cps("global"))).unwrap(),
        Some(cps("global"))
    );
    assert_eq!(
        m.chain_of(Some(&cps("global"))).unwrap(),
        Vec::<PyStr>::new()
    );
}

#[test]
fn members_and_projects_sort_rows_given_out_of_order() {
    let mut m = over(vec![
        project("P002", "b", Some("P001"), None),
        project("P001", "a", None, None),
    ]);
    assert_eq!(m.members(&cps("P001")).unwrap(), ids(&["P001", "P002"]));
    let mut m = over(vec![
        project("P002", "b", None, None),
        project("P001", "a", None, None),
    ]);
    let got: Vec<PyStr> = m.projects().unwrap().into_iter().map(|p| p.id).collect();
    assert_eq!(got, ids(&["P001", "P002"]));
}

#[test]
fn stamp_overflow_text() {
    let m = model(
        &init("0001-01-01T00:30:00", 60),
        BTreeMap::new(),
        None,
        false,
    );
    match m.stamp() {
        Err(Error::Raised(r)) => assert_eq!(
            (r.class, r.message.as_str()),
            ("OverflowError", "date value out of range")
        ),
        _ => panic!("no OverflowError"),
    }
}

#[test]
fn file_binding_takes_the_first_row() {
    let row = |p: &str| {
        PyValue::Dict(vec![
            (PyValue::str("file_id"), PyValue::str("F")),
            (PyValue::str("project"), PyValue::str(p)),
        ])
    };
    let tables: BTreeMap<String, Vec<PyValue>> = [
        (
            "projects".to_string(),
            vec![
                project("P001", "a", None, None),
                project("P002", "b", None, None),
            ],
        ),
        ("project_files".to_string(), vec![row("P001"), row("P002")]),
    ]
    .into_iter()
    .collect();
    let mut m = model(&init("2026-01-01T00:00:00", 0), tables, None, false);
    assert_eq!(m.file_binding(&cps("F")).unwrap(), Some(cps("P001")));
}
