//! Hostile input to the hook's leaves: a payload nested past the
//! decoder's budget, a megabyte of whitespace, a hundred thousand events,
//! ids at the code-point bounds, calendar ends. No case may panic, in
//! debug or release; the scan grows linearly.

mod timing;

use std::collections::BTreeSet;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::hook::{pending_retrievals, read_payload};
use bettermemory_core::json::LoadError;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

#[test]
fn deep_nesting_is_an_error_not_a_crash() {
    let text = format!("{}{}", "[".repeat(200_000), "]".repeat(200_000));
    assert_eq!(read_payload(&cps(&text)), Err(LoadError::Recursion));
    let text = format!("{}{}", "{\"a\": ".repeat(100_000), "}".repeat(100_000));
    assert_eq!(read_payload(&cps(&text)), Err(LoadError::Recursion));
}

#[test]
fn a_megabyte_of_space() {
    let mut text = vec![0x3000; 1 << 20];
    assert_eq!(read_payload(&text), Ok(PyValue::Dict(Vec::new())));
    text.extend(cps("{}"));
    assert_eq!(read_payload(&text), Ok(PyValue::Dict(Vec::new())));
}

fn event(i: usize) -> PyValue {
    let s = |t: &str| PyValue::str(t);
    PyValue::Dict(vec![
        (
            s("kind"),
            s(if i.is_multiple_of(3) { "use" } else { "search" }),
        ),
        (s("session"), s("S")),
        (s("ts"), s("2026-11-21T11:59:00.123456Z")),
        (s("returned"), PyValue::List(vec![s(&format!("m{i}"))])),
        (s("ids"), PyValue::List(vec![s(&format!("m{}", i + 1))])),
    ])
}

#[test]
fn the_scan_grows_linearly() {
    let now = DateTime::from_isoformat(&cps("2026-11-21T12:00:00+00:00")).expect("clock");
    let s = cps("S");
    let small: Vec<PyValue> = (0..10_000).map(event).collect();
    let large: Vec<PyValue> = (0..100_000).map(event).collect();
    let run = |evs: &[PyValue]| pending_retrievals(evs, &now, &s, &[&s], 600).expect("scan");
    assert_eq!(run(&small).len(), 3333);
    timing::grows_within(
        "pending_retrievals",
        timing::TENFOLD,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
}

#[test]
fn calendar_ends_and_odd_clocks() {
    let s = cps("S");
    let evs = vec![
        PyValue::Dict(vec![
            (PyValue::str("kind"), PyValue::str("show")),
            (PyValue::str("session"), PyValue::str("S")),
            (
                PyValue::str("ts"),
                PyValue::str("0001-01-01T00:00:00+23:59"),
            ),
            (PyValue::str("id"), PyValue::Str(vec![0x10FFFF, 0xDFFF])),
        ]),
        PyValue::Dict(vec![
            (PyValue::str("kind"), PyValue::str("show")),
            (PyValue::str("session"), PyValue::str("S")),
            (
                PyValue::str("ts"),
                PyValue::str("9999-12-31T23:59:59.999999-23:59"),
            ),
            (PyValue::str("id"), PyValue::str("late")),
        ]),
    ];
    for clock in [
        "0001-01-01T00:00:00+00:00",
        "9999-12-31T23:59:59.999999+00:00",
    ] {
        let now = DateTime::from_isoformat(&cps(clock)).expect("clock");
        // The reference answers the same at both clocks: nothing for the
        // most negative lookback, the late row for -1 and 0, both for the
        // largest.
        for (lookback, want) in [
            (i64::MIN, Vec::new()),
            (-1, vec![cps("late")]),
            (0, vec![cps("late")]),
            (i64::MAX, vec![cps("late"), vec![0x10FFFF, 0xDFFF]]),
        ] {
            let got = pending_retrievals(&evs, &now, &s, &[&s], lookback).expect("scan");
            let want: BTreeSet<PyStr> = want.into_iter().collect();
            assert_eq!(got, want, "clock {clock} lookback {lookback}");
        }
    }
}
