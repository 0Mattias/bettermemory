//! Cases a plausible wrong `membership_rows` fails, each against the
//! reference's answer (read off `membership_rows.py` and checked by the
//! golden file).

mod common;

use bettermemory_core::capture::Stamp;
use bettermemory_core::checkout_token::TOKEN_NAME;
use bettermemory_core::membership_lines::restated_row;
use bettermemory_core::membership_rows as rows;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;
use common::membership_rows::*;

fn d(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        pairs
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn i(n: i128) -> PyValue {
    PyValue::int(n)
}

#[test]
fn token_name_is_the_references() {
    assert_eq!(TOKEN_NAME, "bettermemory");
    assert_eq!(rows::TICKS_KEY, "ticks");
}

#[test]
fn stamps_keep_five_element_items_only_and_ints_as_text() {
    let raw = PyValue::List(vec![
        PyValue::List(vec![PyValue::str("a"), i(1), i(2), i(3)]),
        PyValue::Tuple(vec![
            PyValue::str("b"),
            PyValue::str(" 7 "),
            PyValue::Float(2.9),
            PyValue::Bool(true),
            PyValue::Int("1".to_string() + &"0".repeat(30)),
        ]),
        d(vec![("ticks", PyValue::Bool(false))]),
    ]);
    let got = rows::stamps_from(&raw).unwrap();
    assert_eq!(
        got,
        vec![Stamp {
            name: cps("b"),
            numbers: [
                "7".into(),
                "2".into(),
                "1".into(),
                "1".to_string() + &"0".repeat(30)
            ],
        }]
    );
    assert_eq!(rows::ticks_from(&raw).unwrap(), Some(false));
    // The first mapping whose value is a bool wins, not the first mapping.
    let raw = PyValue::List(vec![
        d(vec![("ticks", i(1))]),
        d(vec![("ticks", PyValue::Bool(true))]),
        d(vec![("ticks", PyValue::Bool(false))]),
    ]);
    assert_eq!(rows::ticks_from(&raw).unwrap(), Some(true));
    // A scalar is not iterable; a falsy one is no stamps.
    assert_eq!(rows::stamps_from(&i(5)).unwrap_err().class, "TypeError");
    assert!(rows::stamps_from(&i(0)).unwrap().is_empty());
    assert!(rows::stamps_from(&PyValue::str("abc")).unwrap().is_empty());
}

#[test]
fn evidence_keeps_the_first_key_and_the_last_value() {
    let row_of = |ev: PyValue| {
        let all = lines("membership_rows.jsonl");
        let mut row = dec(field(field(&all[0], "bases"), "proposal"));
        if let PyValue::Dict(items) = &mut row {
            for (k, v) in items.iter_mut() {
                if *k == PyValue::str("evidence_json") {
                    *v = ev.clone();
                }
            }
        }
        row
    };
    let ev = PyValue::List(vec![
        PyValue::List(vec![i(1), PyValue::str("x")]),
        PyValue::List(vec![PyValue::Bool(true), PyValue::str("y")]),
        PyValue::List(vec![PyValue::Float(1.0), PyValue::str("z")]),
        PyValue::List(vec![PyValue::Float(-0.0), PyValue::str("w")]),
        PyValue::List(vec![PyValue::Bool(false), PyValue::str("v")]),
    ]);
    let pr = rows::proposal_from(&row_of(ev)).unwrap();
    assert_eq!(
        pr.evidence,
        PyValue::Dict(vec![
            (i(1), PyValue::str("z")),
            (PyValue::Float(-0.0), PyValue::str("v")),
        ])
    );
    let bad = PyValue::List(vec![PyValue::List(vec![i(1)])]);
    let e = rows::proposal_from(&row_of(bad)).unwrap_err();
    assert_eq!(e.class, "ValueError");
    assert_eq!(
        e.message,
        "dictionary update sequence element #0 has length 1; 2 is required"
    );
}

#[test]
fn jsonable_collapses_equal_strs_first_position_last_value() {
    let v = PyValue::Dict(vec![
        (i(1), PyValue::str("a")),
        (PyValue::str("2"), PyValue::str("b")),
        (PyValue::str("1"), PyValue::Tuple(vec![i(3)])),
    ]);
    assert_eq!(
        rows::jsonable(&v).unwrap(),
        PyValue::Dict(vec![
            (PyValue::str("1"), PyValue::List(vec![i(3)])),
            (PyValue::str("2"), PyValue::str("b")),
        ])
    );
}

#[test]
fn views_sort_stably_and_typed_columns_refuse() {
    let all = lines("membership_rows.jsonl");
    let bases = field(&all[0], "bases");
    let row = dec(field(bases, "checkout"));
    let view = |m: &str, order: i64| bettermemory_core::membership_records::records::View {
        checkout: cps("K1"),
        machine: cps(m),
        path: cps("/p"),
        first_seen: cps("t"),
        last_seen: cps("t"),
        order,
    };
    let k = rows::checkout_from(&row, &[view("b", 2), view("a", 1), view("c", 2)]).unwrap();
    let names: Vec<_> = k.views.iter().map(|v| v.machine.clone()).collect();
    assert_eq!(names, vec![cps("a"), cps("b"), cps("c")]);
    let mut odd = row.clone();
    if let PyValue::Dict(items) = &mut odd {
        items
            .iter_mut()
            .find(|(c, _)| *c == PyValue::str("token"))
            .unwrap()
            .1 = i(5);
        items.retain(|(c, _)| *c != PyValue::str("registered"));
    }
    // A later KeyError is the reference's answer, not the refusal.
    assert_eq!(
        rows::checkout_from(&odd, &[]).unwrap_err().class,
        "KeyError"
    );
}

#[test]
fn restated_row_and_proposal_from_agree() {
    let all = lines("membership_rows.jsonl");
    let bases = field(&all[0], "bases");
    let mut n = 0;
    for case in &all[1..all.len() - 1] {
        if name(field(case, "f")) != "proposal" {
            continue;
        }
        let row = case_row(bases, case);
        let (a, b) = (restated_row(&row), rows::proposal_from(&row));
        match (a, b) {
            (Ok(r), Ok(p)) => {
                assert_eq!(
                    (r.id, r.kind, r.candidates, r.reason),
                    (p.id, p.kind, p.candidates, p.reason)
                );
            }
            (_, Err(e)) if e.class == rows::REFUSED => continue,
            (Err(x), Err(y)) => assert_eq!(x, y),
            (x, y) => panic!("disagree: {x:?} {y:?}"),
        }
        n += 1;
    }
    assert!(n > 50);
}

#[test]
fn signature_json_is_read_before_id() {
    let all = lines("membership_rows.jsonl");
    let mut row = dec(field(field(&all[0], "bases"), "proposal"));
    if let PyValue::Dict(items) = &mut row {
        items.retain(|(c, _)| *c != PyValue::str("signature_json") && *c != PyValue::str("id"));
    }
    let e = rows::proposal_from(&row).unwrap_err();
    assert_eq!(
        (e.class, e.message.as_str()),
        ("KeyError", "'signature_json'")
    );
}

#[test]
fn the_first_refused_column_is_named() {
    let all = lines("membership_rows.jsonl");
    let mut row = dec(field(field(&all[0], "bases"), "checkout"));
    if let PyValue::Dict(items) = &mut row {
        for (c, v) in items.iter_mut() {
            if *c == PyValue::str("git_dir") || *c == PyValue::str("token") {
                *v = i(5);
            }
        }
    }
    let e = rows::checkout_from(&row, &[]).unwrap_err();
    assert_eq!(e.message, "git_dir holds a int");
}
