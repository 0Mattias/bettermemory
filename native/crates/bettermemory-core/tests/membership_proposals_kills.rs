//! Cases a plausible wrong port of the machine rows and the proposals
//! fails: equality by structure where the reference compares by Python's
//! `==`, the id minted after the stamp, the stored checkout named on an
//! answer, a missing evidence key read as unequal to None.

mod common;

use std::collections::BTreeMap;

use bettermemory_core::machine::{MachineFacts, MachineIdentity};
use bettermemory_core::membership::proposals::{Line, Opening};
use bettermemory_core::membership::Error;
use bettermemory_core::membership_records::records::OPEN;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use common::membership_core::{model, Model};

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn d(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(items.into_iter().map(|(k, v)| (s(k), v)).collect())
}

fn init(now: &str) -> PyValue {
    d(vec![
        ("now", PyValue::List(vec![s(now), PyValue::int(0)])),
        ("session", PyValue::None),
        ("given", PyValue::Bool(true)),
    ])
}

fn over(tables: Vec<(&str, Vec<PyValue>)>, now: &str) -> Model {
    let t: BTreeMap<String, Vec<PyValue>> = tables
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect();
    model(&init(now), t, None, false)
}

fn log_ops(m: &Model) -> Vec<String> {
    m.store()
        .0
        .borrow()
        .log
        .iter()
        .map(|e| match e {
            PyValue::List(x) => match &x[0] {
                PyValue::Str(t) => String::from_utf8(t.iter().map(|&c| c as u8).collect()).unwrap(),
                _ => String::new(),
            },
            _ => String::new(),
        })
        .collect()
}

fn facts() -> MachineFacts {
    MachineFacts {
        hardware: Some(cps("HW")),
        hostname: None,
        id_file: None,
    }
}

fn identity() -> MachineIdentity {
    MachineIdentity {
        id: cps("M1"),
        facts: facts(),
        predecessor: None,
        minted: false,
        cloned: false,
        facts_updated: false,
        recorded: None,
    }
}

#[test]
fn stored_facts_in_another_key_order_are_equal() {
    let row = d(vec![
        ("id", s("M1")),
        (
            "facts_json",
            d(vec![
                ("id_file", PyValue::None),
                ("hostname", PyValue::None),
                ("hardware", s("HW")),
            ]),
        ),
        ("predecessor", PyValue::None),
        ("minted", s("x")),
    ]);
    let mut m = over(vec![("machines", vec![row])], "2026-01-01T00:00:00");
    m.record_machine(&identity()).unwrap();
    assert!(!log_ops(&m).contains(&"put".to_string()));
}

#[test]
fn a_tuple_of_facts_is_not_a_list() {
    let row = d(vec![
        ("id", s("M1")),
        (
            "facts_json",
            d(vec![
                ("hardware", s("HW")),
                ("hostname", PyValue::None),
                ("id_file", PyValue::Tuple(vec![])),
            ]),
        ),
        ("predecessor", PyValue::None),
        ("minted", s("x")),
    ]);
    let mut id = identity();
    id.facts.id_file = None;
    let mut m = over(vec![("machines", vec![row])], "2026-01-01T00:00:00");
    m.record_machine(&id).unwrap();
    assert!(log_ops(&m).contains(&"put".to_string()));
}

fn opening(
    checkout: Option<&str>,
    evidence: Vec<(PyValue, PyValue)>,
    distinct: Option<&str>,
) -> Opening<'static> {
    Opening {
        checkout: checkout.map(cps),
        kind: cps("legacy"),
        candidates: vec![cps("P1")],
        evidence,
        reason: cps("r"),
        line: Line::Text(cps("l")),
        distinct: distinct.map(cps),
    }
}

#[test]
fn the_id_is_minted_before_the_stamp_fails() {
    // Year 1 at +01:00 is before the first instant UTC can spell.
    let init = d(vec![
        (
            "now",
            PyValue::List(vec![s("0001-01-01T00:00:00"), PyValue::int(60)]),
        ),
        ("session", PyValue::None),
        ("given", PyValue::Bool(true)),
    ]);
    let mut m = model(&init, BTreeMap::new(), None, false);
    let got = m.open_proposal(&opening(Some("K1"), vec![], None));
    assert!(matches!(got, Err(Error::Raised(r)) if r.class == "OverflowError"));
    assert!(log_ops(&m).contains(&"next_id".to_string()));
}

fn q_row(id: &str, checkout: &str, evidence: PyValue) -> PyValue {
    d(vec![
        ("id", s(id)),
        ("checkout", s(checkout)),
        ("kind", s("legacy")),
        ("candidates_json", PyValue::List(vec![])),
        ("evidence_json", evidence),
        ("reason", s("r")),
        ("line", s("l")),
        ("state", s(OPEN)),
        ("answer", PyValue::None),
        ("answered_by", PyValue::None),
        ("signature_json", PyValue::None),
        ("opened", s("o")),
    ])
}

#[test]
fn a_missing_key_equals_a_none_value() {
    let mut m = over(
        vec![("proposals", vec![q_row("Q001", "K1", d(vec![]))])],
        "2026-01-01T00:00:00",
    );
    let got = m
        .open_proposal(&opening(
            Some("K1"),
            vec![(s("p"), PyValue::None)],
            Some("p"),
        ))
        .unwrap();
    assert_eq!(got.id, cps("Q001"));
}

#[test]
fn a_float_equal_to_an_int_matches_the_distinct_value() {
    let mut m = over(
        vec![(
            "proposals",
            vec![q_row("Q001", "K1", d(vec![("n", PyValue::int(1))]))],
        )],
        "2026-01-01T00:00:00",
    );
    let got = m
        .open_proposal(&opening(
            Some("K1"),
            vec![(s("n"), PyValue::Float(1.0))],
            Some("n"),
        ))
        .unwrap();
    assert_eq!(got.id, cps("Q001"));
}

#[test]
fn a_restatement_equal_by_value_puts_nothing_and_answers_the_new_order() {
    let ev = d(vec![("a", PyValue::int(1)), ("b", PyValue::int(2))]);
    let mut m = over(
        vec![("proposals", vec![q_row("Q001", "K1", ev)])],
        "2026-01-01T00:00:00",
    );
    let given = vec![(s("b"), PyValue::Float(2.0)), (s("a"), PyValue::Bool(true))];
    let got = m.restate_proposal(&cps("Q001"), &cps("l"), &given).unwrap();
    assert!(!log_ops(&m).contains(&"put".to_string()));
    assert_eq!(got.evidence, PyValue::Dict(given));
}

#[test]
fn an_answer_names_the_given_proposals_checkout() {
    let mut m = over(
        vec![("proposals", vec![q_row("Q001", "K1", d(vec![]))])],
        "2026-01-01T00:00:00",
    );
    let mut pr = m.proposal(&cps("Q001")).unwrap();
    pr.checkout = Some(cps("K9"));
    m.close(&pr, &cps("P1"), &cps("u"), None).unwrap();
    let log = m.store().0.borrow().log.clone();
    let put = format!("{:?}", log[2]);
    let rec = format!("{:?}", log[3]);
    let k1: PyStr = cps("K1");
    let k9: PyStr = cps("K9");
    assert!(put.contains(&format!("{k1:?}")) && !put.contains(&format!("{k9:?}")));
    assert!(rec.contains(&format!("{k9:?}")));
}

#[test]
fn a_proposal_not_open_as_given_is_left_alone() {
    let mut m = over(
        vec![("proposals", vec![q_row("Q001", "K1", d(vec![]))])],
        "2026-01-01T00:00:00",
    );
    let mut pr = m.proposal(&cps("Q001")).unwrap();
    pr.state = cps("answered");
    m.close(&pr, &cps("P1"), &cps("u"), None).unwrap();
    assert_eq!(log_ops(&m), vec!["rows".to_string()]);
}

#[test]
fn a_restatement_keeps_a_signature_holding_nan_and_puts_nothing() {
    let mut row = q_row("Q001", "K1", d(vec![("n", PyValue::int(2))]));
    if let PyValue::Dict(items) = &mut row {
        for (k, v) in items.iter_mut() {
            if *k == s("signature_json") {
                *v = PyValue::List(vec![s("h"), PyValue::Float(f64::NAN)]);
            } else if *k == s("line") {
                *v = s("a line");
            }
        }
    }
    let mut m = over(vec![("proposals", vec![row])], "2026-01-01T00:00:00");
    m.restate_proposal(&cps("Q001"), &cps("a line"), &[(s("n"), PyValue::int(2))])
        .unwrap();
    assert_eq!(log_ops(&m), vec!["batch", "rows", "commit"]);
}

#[test]
fn the_opened_event_carries_the_plain_evidence() {
    let mut m = over(vec![], "2026-01-01T00:00:00");
    m.open_proposal(&opening(
        Some("K1"),
        vec![(
            PyValue::int(3),
            PyValue::Tuple(vec![PyValue::int(1), PyValue::int(2)]),
        )],
        None,
    ))
    .unwrap();
    let events = m.store().0.borrow().tables["events"].clone();
    let want = d(vec![(
        "3",
        PyValue::List(vec![PyValue::int(1), PyValue::int(2)]),
    )]);
    let PyValue::Dict(ev) = &events[0] else {
        panic!("an event")
    };
    let PyValue::Dict(payload) = &ev[1].1 else {
        panic!("a payload")
    };
    let got = payload
        .iter()
        .find(|(k, _)| *k == s("evidence"))
        .map(|(_, v)| v);
    assert_eq!(got, Some(&want));
}

#[test]
fn a_predecessor_that_is_not_a_str_declines() {
    let row = d(vec![
        ("id", s("M1")),
        ("facts_json", d(vec![])),
        ("predecessor", PyValue::int(5)),
        ("minted", s("x")),
    ]);
    let m = over(vec![("machines", vec![row])], "2026-01-01T00:00:00");
    let got = m.machines();
    assert!(matches!(got, Err(Error::Raised(r)) if r.class == "NotPorted"));
}

#[test]
fn the_last_of_duplicate_given_keys_is_the_distinct_value() {
    let mut m = over(
        vec![(
            "proposals",
            vec![q_row("Q001", "K1", d(vec![("p", PyValue::int(2))]))],
        )],
        "2026-01-01T00:00:00",
    );
    let got = m
        .open_proposal(&opening(
            Some("K1"),
            vec![(s("p"), PyValue::int(1)), (s("p"), PyValue::int(2))],
            Some("p"),
        ))
        .unwrap();
    assert_eq!(got.id, cps("Q001"));
}

#[test]
fn the_first_of_two_rows_with_one_id_is_the_proposal() {
    let mut second = q_row("Q001", "K1", d(vec![]));
    if let PyValue::Dict(items) = &mut second {
        for (k, v) in items.iter_mut() {
            if *k == s("line") {
                *v = s("second");
            }
        }
    }
    let m = over(
        vec![("proposals", vec![q_row("Q001", "K1", d(vec![])), second])],
        "2026-01-01T00:00:00",
    );
    assert_eq!(m.proposal(&cps("Q001")).unwrap().line, cps("l"));
}
