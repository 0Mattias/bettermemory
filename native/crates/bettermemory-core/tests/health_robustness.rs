//! Hostile input for `health_summary` and `health_roots`: very many
//! roots and sections, long names, many commas, integer bounds. No panic,
//! and time grows about linearly (sorting: n log n).

mod timing;

use std::hint::black_box;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::health_roots::{EstateProgress, LAST_CHECKED_CAP};
use bettermemory_core::health_summary::{health_section, health_summary};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

fn roots(n: usize) -> Vec<PyStr> {
    (0..n)
        .map(|i| cps(&format!("/r{}", (i * 7919) % n)))
        .collect()
}

#[test]
fn ordered_grows_about_linearly() {
    let p = EstateProgress {
        cursor: Some(cps("/r5")),
        ..EstateProgress::default()
    };
    let small = roots(20_000);
    let large = roots(200_000);
    assert_eq!(p.ordered(&large).len(), 200_000);
    timing::grows_within(
        "ordered",
        25,
        &mut || timing::once(|| black_box(p.ordered(&small))),
        &mut || timing::once(|| black_box(p.ordered(&large))),
    );
}

#[test]
fn attempts_past_the_cap_stay_bounded() {
    let mut p = EstateProgress::default();
    let when = DateTime::from_unix_micros_utc(0).unwrap();
    for i in 0..20_000 {
        p.attempt(cps(&format!("{}", i % 3000)), when);
    }
    assert_eq!(p.checked.len(), LAST_CHECKED_CAP);
    let huge = vec![0x10FFFF; 1 << 20];
    p.attempt(huge.clone(), when);
    assert_eq!(p.ordered(&[huge.clone(), vec![]]), vec![vec![], huge]);
}

fn report(n: usize) -> PyValue {
    PyValue::Dict(
        (0..n)
            .map(|i| (PyValue::str(&format!("s{i}")), PyValue::int(i as i128)))
            .collect(),
    )
}

#[test]
fn sections_and_commas_at_scale() {
    let r = report(2000);
    let summary = health_summary(&r).expect("a summary");
    assert!(matches!(summary, PyValue::Dict(_)));
    let name = cps(&",".repeat(1 << 20));
    assert_eq!(health_section(&r, &name).unwrap_err().name, "ValueError");
    let spaces = cps(&" \u{3000}".repeat(1 << 18));
    assert_eq!(health_section(&r, &spaces).unwrap_err().name, "ValueError");
    let big = PyValue::Dict(vec![(
        PyValue::str("n"),
        PyValue::Int(format!("-{}", "9".repeat(4000))),
    )]);
    assert!(health_summary(&big).is_ok());
    let int_max = PyValue::Dict(vec![(PyValue::str("n"), PyValue::int(i128::MAX))]);
    assert!(health_summary(&int_max).is_ok());
}

#[test]
fn section_lookup_grows_within_reason() {
    let small = report(200);
    let large = report(2000);
    let ask = |n: usize| {
        cps(&(0..n)
            .map(|i| format!("s{i}"))
            .collect::<Vec<_>>()
            .join(","))
    };
    let (qs, ql) = (ask(200), ask(2000));
    // Each asked name is looked up among the sections: quadratic, as the
    // reference's list membership is, so ten times the input may take a
    // hundred times as long.
    timing::grows_within(
        "health_section",
        150,
        &mut || timing::once(|| black_box(health_section(&small, &qs))),
        &mut || timing::once(|| black_box(health_section(&large, &ql))),
    );
}
