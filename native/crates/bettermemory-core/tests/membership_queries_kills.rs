//! Cases a plausible wrong read side of `Membership` (unit mqueries)
//! fails: `recorded` and `held` read as `int(ev.get(k) or 0)`, the old
//! first-sight suffix, the order of the suppression reasons, the event's
//! payload, an OverflowError that is not caught, the newest view, the
//! canonical read only once a binding is found.

mod common;

use std::collections::HashMap;

use bettermemory_core::membership::queries::{LEGACY_NO_LIVE_CHECKOUT, QUOTED_LINE};
use bettermemory_core::membership::Error;
use bettermemory_core::membership_lines::line_legacy;
use bettermemory_core::membership_records::paths::basename;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::caller::Resolution;
use bettermemory_core::unicode::cps;
use common::membership_core::{model, tables_of, Model};
use common::membership_queries::{proposal, reader};
use common::membership_rows::{field, lines, name};

fn header() -> PyValue {
    lines("membership_queries.jsonl").remove(0)
}

fn base_model() -> Model {
    let init = PyValue::Dict(vec![
        (
            PyValue::str("now"),
            PyValue::List(vec![PyValue::str("2026-03-04T05:06:07"), PyValue::int(0)]),
        ),
        (PyValue::str("session"), PyValue::None),
        (PyValue::str("given"), PyValue::Bool(false)),
    ]);
    model(&init, tables_of(field(&header(), "base")), None, false)
}

fn pr(items: Vec<(&str, PyValue)>) -> bettermemory_core::membership_records::records::Proposal {
    let over = PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    );
    proposal(field(&header(), "proposal"), &over)
}

fn legacy(ev: Vec<(&str, PyValue)>) -> bettermemory_core::membership_records::records::Proposal {
    let mut e = vec![
        (PyValue::str("path"), PyValue::str("/w/old/repo")),
        (PyValue::str("memories"), PyValue::int(3)),
        (PyValue::str("episodes"), PyValue::int(4)),
    ];
    e.extend(ev.into_iter().map(|(k, v)| (PyValue::str(k), v)));
    pr(vec![
        ("kind", PyValue::str("legacy")),
        ("reason", PyValue::str(LEGACY_NO_LIVE_CHECKOUT)),
        ("candidates_json", PyValue::List(vec![PyValue::str("P002")])),
        ("evidence_json", PyValue::Dict(e)),
    ])
}

fn expected(held: i64, recorded: Option<i64>) -> PyStr {
    let path = cps("/w/old/repo");
    line_legacy(
        &basename(&path),
        3,
        4,
        &path,
        held,
        &cps("alpha"),
        &cps("P002"),
        recorded,
        Some(&cps("Q1")),
    )
}

#[test]
fn recorded_missing_is_zero_not_memories() {
    let mut m = base_model();
    let got = m.written_afresh(&legacy(vec![])).unwrap().unwrap();
    assert_eq!(got, expected(0, Some(0)));
    assert_ne!(got, expected(0, None));
    let got = m
        .written_afresh(&legacy(vec![
            ("held", PyValue::str("")),
            ("recorded", PyValue::Bool(false)),
        ]))
        .unwrap()
        .unwrap();
    assert_eq!(got, expected(0, Some(0)));
    let got = m
        .written_afresh(&legacy(vec![
            ("held", PyValue::str(" 2 ")),
            ("recorded", PyValue::Float(5.9)),
        ]))
        .unwrap()
        .unwrap();
    assert_eq!(got, expected(2, Some(5)));
}

#[test]
fn overflow_is_not_caught() {
    let mut m = base_model();
    let files = HashMap::new();
    let file = reader(&files);
    let got = m.restated(
        &legacy(vec![("held", PyValue::Float(f64::INFINITY))]),
        &file,
    );
    match got {
        Err(Error::Raised(r)) => assert_eq!(r.class, "OverflowError"),
        other => panic!("{other:?}"),
    }
    let got = m.restated(&legacy(vec![("held", PyValue::Float(f64::NAN))]), &file);
    assert!(got.is_ok());
}

#[test]
fn old_first_sight_suffix_is_restated_and_quoted_kept() {
    let mut m = base_model();
    let files = HashMap::new();
    let file = reader(&files);
    let quoted = format!("{QUOTED_LINE}/w\" new");
    let old = format!("{quoted}; separate: bettermemory project init --new (a fresh project file)");
    let keep = pr(vec![("line", PyValue::str(&quoted))]);
    assert_eq!(m.restated(&keep, &file).unwrap().line, keep.line);
    let again = pr(vec![("line", PyValue::str(&old))]);
    assert_ne!(m.restated(&again, &file).unwrap().line, again.line);
    let other = pr(vec![
        ("line", PyValue::str(&old)),
        ("kind", PyValue::str("remote_diverged")),
    ]);
    assert_eq!(m.restated(&other, &file).unwrap().line, other.line);
    let contra = pr(vec![
        ("line", PyValue::str(&quoted)),
        ("kind", PyValue::str("file_contradiction")),
    ]);
    assert_ne!(m.restated(&contra, &file).unwrap().line, contra.line);
}

fn caller(project: &str) -> Resolution {
    let v = PyValue::Dict(vec![
        (PyValue::str("outcome"), PyValue::str("silent")),
        (PyValue::str("project"), PyValue::str(project)),
        (PyValue::str("checkout"), PyValue::None),
        (PyValue::str("rule"), PyValue::str("file")),
        (
            PyValue::str("chain"),
            PyValue::List(vec![PyValue::str(project)]),
        ),
    ]);
    Resolution::from_value(&v).unwrap()
}

#[test]
fn unresolved_is_named_before_the_relation_and_the_event_keeps_its_keys() {
    let mut m = base_model();
    let hits = [cps("R6"), cps("RZ"), cps("R7")];
    let got = m
        .admit_recall(&caller("P001"), &hits, &cps("prompt_recall"))
        .unwrap();
    let why: Vec<String> = got
        .suppressed
        .iter()
        .map(|(_, w)| name(&PyValue::Str(w.clone())))
        .collect();
    assert_eq!(why, ["unresolved", "no_home", "other_project"]);
    let st = m.store().0.borrow();
    let ev = st
        .tables
        .get("events")
        .and_then(|e| e.last())
        .expect("an event");
    let payload = field(ev, "payload");
    let PyValue::Dict(items) = payload else {
        panic!()
    };
    let keys: Vec<String> = items.iter().map(|(k, _)| name(k)).collect();
    assert_eq!(
        keys,
        [
            "surface",
            "admission",
            "checkout",
            "caller_rule",
            "suppressed"
        ]
    );
    drop(st);
    let got = m
        .admit_recall(&caller("P004"), &[cps("R6")], &cps("prompt_recall"))
        .unwrap();
    assert_eq!(got.delivered, [cps("R6")]);
}

#[test]
fn the_newest_view_decides_and_canonical_waits_for_a_binding() {
    let mut m = base_model();
    let mut files = HashMap::new();
    let id = |f: &str| PyValue::Dict(vec![(PyValue::str("id"), PyValue::str(f))]);
    files.insert(cps("/w/old"), id("F1"));
    files.insert(cps("/w/two"), id("F3"));
    let file = reader(&files);
    let k9 = pr(vec![("checkout", PyValue::str("K9"))]);
    assert!(!m.filed_elsewhere(&k9, &file).unwrap());
    let before = m.store().0.borrow().log.len();
    let k2 = pr(vec![("checkout", PyValue::str("K2"))]);
    assert!(!m.filed_elsewhere(&k2, &file).unwrap());
    let st = m.store().0.borrow();
    let tables: Vec<String> = st.log[before..]
        .iter()
        .map(|e| match e {
            PyValue::List(v) if v.len() > 1 => name(&v[1]),
            _ => String::new(),
        })
        .collect();
    assert!(tables.iter().any(|t| t == "project_files"), "{tables:?}");
    assert!(!tables.iter().any(|t| t == "projects"), "{tables:?}");
}

/// Python's dict keeps the last of a repeated JSON key, so a stored
/// `codes` that names the candidate twice is read by its last list.
#[test]
fn a_repeated_codes_key_reads_the_last() {
    let mut m = base_model();
    let none = HashMap::new();
    let file = reader(&none);
    let imp = |codes: Vec<(&str, &str)>| {
        let codes = codes
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), PyValue::List(vec![PyValue::str(v)])))
            .collect();
        pr(vec![
            ("kind", PyValue::str("import")),
            ("candidates_json", PyValue::List(vec![PyValue::str("P001")])),
            (
                "evidence_json",
                PyValue::Dict(vec![
                    (PyValue::str("imported_project"), PyValue::str("P003")),
                    (PyValue::str("source"), PyValue::str("/x/y.json")),
                    (PyValue::str("codes"), PyValue::Dict(codes)),
                ]),
            ),
        ])
    };
    let shown = |m: &mut Model, p| m.restated(&p, &file).unwrap().line;
    let twice = shown(&mut m, imp(vec![("P001", "first"), ("P001", "last")]));
    let last = shown(&mut m, imp(vec![("P001", "last")]));
    let first = shown(&mut m, imp(vec![("P001", "first")]));
    assert_eq!(twice, last);
    assert_ne!(twice, first);
}
