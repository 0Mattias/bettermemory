//! Hostile input to `audit::probe`: a hundred thousand events, a session
//! nested 100,000 deep, a megabyte message, integer bounds on every window
//! and on the v2 label, and the growth of the shield and the dedup from
//! 10,000 to 100,000 events.

mod common;
mod timing;

use bettermemory_core::audit::probe::{
    count_recent_retrievals, is_duplicate_audit, probe_for_miss, too_thin_to_probe, ProbeArgs,
    ProbeError,
};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::search::lane::relevance_label_v2;
use bettermemory_core::unicode::cps;
use common::audit_probe::FakeRanker;

fn now() -> DateTime {
    DateTime::from_isoformat(&cps("2026-03-01T12:00:00+00:00")).unwrap()
}

fn d(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        pairs
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn events(n: usize, kind: &str) -> Vec<PyValue> {
    (0..n)
        .map(|i| {
            d(vec![
                ("kind", PyValue::str(kind)),
                ("ts", PyValue::str("2026-03-01T11:59:30Z")),
                ("session", PyValue::str(&format!("s{i}"))),
                (
                    "returned",
                    PyValue::List(vec![PyValue::str(&format!("r{i}"))]),
                ),
            ])
        })
        .collect()
}

#[test]
fn a_session_nested_deep_is_refused_without_recursing() {
    let mut v = PyValue::str("S");
    for _ in 0..100_000 {
        v = PyValue::Tuple(vec![v]);
    }
    v = PyValue::Tuple(vec![v, PyValue::List(vec![])]);
    let e = [d(vec![
        ("kind", PyValue::str("search")),
        ("ts", PyValue::str("2026-03-01T11:59:30Z")),
        ("session", v),
    ])];
    assert_eq!(
        count_recent_retrievals(&e, &[cps("S")], &now(), 60, None, None),
        Err(ProbeError::TypeError)
    );
}

#[test]
fn window_bounds_never_panic() {
    for secs in [
        i64::MIN,
        i64::MIN + 1,
        -1,
        0,
        1,
        86_399_999_999_999,
        i64::MAX,
    ] {
        let _ = count_recent_retrievals(&[], &[], &now(), secs, None, None);
        let _ = is_duplicate_audit(&[], &cps("S"), None, &cps("q"), &now(), secs);
        let mut args = ProbeArgs::new(&cps("S"), now());
        args.lookback_seconds = secs;
        args.creation_shield_seconds = secs;
        let mut r = FakeRanker {
            hits: vec![],
            calls: vec![],
        };
        let _ = probe_for_miss(&[], &cps("deploy the backup"), &[], None, &mut r, &args);
    }
    assert_eq!(
        count_recent_retrievals(&[], &[], &now(), i64::MAX, None, None),
        Err(ProbeError::OverflowError)
    );
}

#[test]
fn the_v2_label_at_integer_bounds() {
    assert_eq!(relevance_label_v2(i64::MAX, i64::MAX), "high");
    assert_eq!(relevance_label_v2(i64::MIN, i64::MAX), "low");
    assert_eq!(relevance_label_v2(i64::MAX, 1), "high");
    assert_eq!(relevance_label_v2(3, i64::MAX), "low");
    assert_eq!(relevance_label_v2(0, i64::MIN), "low");
}

#[test]
fn a_megabyte_message_is_gated_in_linear_time() {
    let words = |n: usize| cps(&"deploy backup server ".repeat(n));
    let (small, large) = (words(5_000), words(50_000));
    assert!(!too_thin_to_probe(&large));
    timing::grows_within(
        "5,000 and 50,000 words",
        timing::TENFOLD,
        &mut || timing::once(|| too_thin_to_probe(&small)),
        &mut || timing::once(|| too_thin_to_probe(&large)),
    );
}

#[test]
fn the_shield_grows_linearly_in_events() {
    let (small, large) = (events(10_000, "search"), events(100_000, "search"));
    let sids = [cps("s5")];
    assert_eq!(
        count_recent_retrievals(&large, &sids, &now(), 60, None, None),
        Ok(1)
    );
    timing::grows_within(
        "10,000 and 100,000 events",
        timing::TENFOLD,
        &mut || timing::once(|| count_recent_retrievals(&small, &sids, &now(), 60, None, None)),
        &mut || timing::once(|| count_recent_retrievals(&large, &sids, &now(), 60, None, None)),
    );
}

#[test]
fn the_dedup_grows_linearly_in_events() {
    let (small, large) = (
        events(10_000, "turn_audited"),
        events(100_000, "turn_audited"),
    );
    let dup = |e: &[PyValue]| is_duplicate_audit(e, &cps("none"), None, &cps("q"), &now(), 3600);
    assert_eq!(dup(&large), Ok(false));
    timing::grows_within(
        "10,000 and 100,000 events",
        timing::TENFOLD,
        &mut || timing::once(|| dup(&small)),
        &mut || timing::once(|| dup(&large)),
    );
}
