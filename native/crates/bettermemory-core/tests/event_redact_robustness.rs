//! Hostile input for `event_redact`: texts long and pathological for the
//! five patterns' backtracking, a hundred thousand detector spans, values
//! nested 200,000 deep in every named field shape, and an event of
//! 100,000 fields. No panic, and the time grows linearly.

mod timing;

use std::time::Duration;

use bettermemory_core::event_redact as er;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::rows;
use bettermemory_core::unicode::cps;

fn none(_: &[u32]) -> Vec<(usize, usize, PyStr)> {
    Vec::new()
}

fn repeat(piece: &str, n: usize) -> PyStr {
    cps(&piece.repeat(n))
}

fn redact_time(text: &PyStr) -> Duration {
    timing::once(|| er::redact_query(text, &mut none))
}

/// Each pattern's backtracking shape: a long body after the prefix that
/// ends where `\b` fails, and prefixes packed back to back.
#[test]
fn pattern_shapes_grow_linearly() {
    let shapes: [(&str, &str, &str); 6] = [
        ("sk-ant-", "-", "\u{e9}"),
        ("sk-", "a", "\u{e9}"),
        ("ghp_", "a", "_"),
        ("github_pat_", "_", "\u{e9}"),
        ("", "sk-", ""),
        ("", "AKIAAKIA", "Z"),
    ];
    for (head, body, tail) in shapes {
        let make = |n: usize| {
            let mut t = cps(head);
            t.extend(repeat(body, n));
            t.extend(cps(tail));
            t
        };
        let (small, large) = (make(20_000), make(200_000));
        timing::grows_within(
            &format!("{head}{body}{tail}"),
            20,
            &mut || redact_time(&small),
            &mut || redact_time(&large),
        );
    }
}

/// Many spans are joined once: 10,000 against 100,000.
#[test]
fn many_spans_join_once() {
    let run = |n: usize| {
        let text = repeat("tok ", n);
        move || {
            timing::once(|| {
                er::strip_known_secrets(&text, &mut |t: &[u32]| {
                    (0..t.len() / 4)
                        .map(|i| (4 * i, 4 * i + 3, cps("k")))
                        .collect()
                })
            })
        }
    };
    let (mut small, mut large) = (run(10_000), run(100_000));
    timing::grows_within("spans", 20, &mut small, &mut large);
    let out = er::strip_known_secrets(&repeat("tok ", 3), &mut |_: &[u32]| {
        vec![(0, 3, cps("k")), (4, 7, cps("k")), (8, 12, cps("k"))]
    });
    assert_eq!(out, cps("[REDACTED:k] [REDACTED:k] [REDACTED:k]"));
}

/// Spans out of range or out of order are sliced as Python slices them.
#[test]
fn odd_spans_do_not_panic() {
    let text = cps("abc");
    let out = er::strip_known_secrets(&text, &mut |_: &[u32]| {
        vec![
            (5, 9, cps("x")),
            (1, 0, cps("y")),
            (usize::MAX, usize::MAX, cps("z")),
        ]
    });
    assert_eq!(out, cps("abc[REDACTED:x][REDACTED:y]abc[REDACTED:z]"));
}

fn deep(depth: usize) -> PyValue {
    let mut v = PyValue::str("<|im_start|>");
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

#[test]
fn deep_values_in_every_shape() {
    let snippet = |v: PyValue| PyValue::Dict(vec![(PyValue::str("snippet"), v)]);
    let said = |v: PyValue| PyValue::Dict(vec![(PyValue::str("said"), v)]);
    let fields = vec![
        (PyValue::str("note"), deep(200_000)),
        (
            PyValue::str("top_hits"),
            PyValue::List(vec![snippet(deep(100_000))]),
        ),
        (PyValue::str("review_answers"), said(deep(100_000))),
        (PyValue::str("query"), PyValue::str("q")),
    ];
    let out = er::redact_event_fields(&fields, &mut none);
    assert_eq!(out.len(), 4);
    assert!(out[0].1 != fields[0].1);
}

/// An event of n fields against 10n.
#[test]
fn wide_event_grows_linearly() {
    let make = |n: usize| {
        let mut e: Vec<(PyValue, PyValue)> = (0..n)
            .map(|i| (PyValue::str(&format!("f{i}")), PyValue::int(i as i128)))
            .collect();
        e.push((PyValue::str("kind"), PyValue::str("search")));
        e.push((
            PyValue::str("query"),
            PyValue::str("sk-aaaaaaaaaaaaaaaaaaaaaaaa"),
        ));
        e
    };
    let (small, large) = (make(10_000), make(100_000));
    let time = |e: &Vec<(PyValue, PyValue)>| {
        timing::once(|| rows::event_import_payload(e, &cps("v8"), &mut none).unwrap())
    };
    timing::grows_within("wide event", 20, &mut || time(&small), &mut || time(&large));
}

#[test]
fn a_million_code_points() {
    let mut text: PyStr = (0..1_000_000u32).map(|i| 0xD7F0 + i % 32).collect();
    text.extend(cps("sk-ant-aaaaaaaaaaaaaaaaaaaaaaaaaa"));
    let PyValue::Dict(ref d) = er::redact_query(&text, &mut none) else {
        panic!("a dict")
    };
    assert_eq!(d[2].1, PyValue::int(1_000_033));
}
