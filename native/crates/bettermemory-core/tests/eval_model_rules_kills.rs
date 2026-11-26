//! Inputs on which a plausible wrong port of `eval_model::rules` answers
//! otherwise, each with the reference's answer written out (read from
//! CPython 3.13.13) and, where the wrong port is a line of arithmetic,
//! that line run beside it to show it differs.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval_model::{self as em, EvalModelError};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

fn now() -> DateTime {
    DateTime::new(2026, 11, 25, 12, 0, 0, 0, Some(0)).expect("a datetime")
}

/// A hit of relevance "high" with `score` when given.
fn hit(score: Option<PyValue>) -> PyValue {
    let mut items = vec![(PyValue::str("relevance"), PyValue::str("high"))];
    if let Some(s) = score {
        items.push((PyValue::str("score"), s));
    }
    PyValue::Dict(items)
}

fn int(text: &str) -> PyValue {
    PyValue::int_from_decimal(text).expect("decimal")
}

fn audit(ts: &str, extra: Vec<(&str, PyValue)>) -> PyValue {
    let mut items = vec![
        (PyValue::str("kind"), PyValue::str("turn_audited")),
        (PyValue::str("ts"), PyValue::str(ts)),
        (PyValue::str("verdict"), PyValue::str("miss")),
        (PyValue::str("top_hits"), PyValue::List(vec![hit(None)])),
        (PyValue::str("recent_retrieval_count"), PyValue::int(0)),
    ];
    items.extend(extra.into_iter().map(|(k, v)| (PyValue::str(k), v)));
    PyValue::Dict(items)
}

fn cutoff(ts: &str) -> PyValue {
    PyValue::Dict(vec![
        (PyValue::str("kind"), PyValue::str("silent_miss_cutoff")),
        (PyValue::str("cutoff_ts"), PyValue::str(ts)),
    ])
}

#[test]
fn an_int_score_is_not_read_through_f64() {
    // 2**53 + 3 against twice 2**52 + 2: exactly 2**53 + 3 < 2**53 + 4,
    // while the int read as a float rounds up to 2**53 + 4.
    let top = (1u64 << 53) + 3;
    let second = ((1u64 << 52) + 2) as f64;
    let hits = [
        hit(Some(int(&top.to_string()))),
        hit(Some(PyValue::Float(second))),
    ];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(false));
    assert!((top as f64) >= 2.0 * second, "the mutant flags it");
    // An int past 2**1024 stays an int: no overflow, no infinity.
    let big = format!("1{}", "0".repeat(400));
    assert_eq!(
        em::rule_v2_top1_high_score_50(&[hit(Some(int(&big)))], "0"),
        Ok(true)
    );
    let hits = [hit(Some(int(&big))), hit(Some(PyValue::Float(1e300)))];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(true));
    // Twice 1e308 is infinity, which no int reaches.
    let hits = [hit(Some(int(&big))), hit(Some(PyValue::Float(1e308)))];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(false));
    let hits = [hit(Some(PyValue::Float(f64::MAX))), hit(Some(int(&big)))];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(false));
}

#[test]
fn twice_an_int_second_score_is_exact() {
    let top = (1u64 << 54) + 1;
    let second = (1u64 << 53) + 1;
    let hits = [
        hit(Some(int(&top.to_string()))),
        hit(Some(int(&second.to_string()))),
    ];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(false));
    assert!((top as f64) >= 2.0 * (second as f64), "the mutant flags it");
    // Twice a float is a float: 2 * 1e308 is infinity.
    let hits = [
        hit(Some(PyValue::Float(f64::MAX))),
        hit(Some(PyValue::Float(1e308))),
    ];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(false));
}

#[test]
fn a_nan_is_not_a_number_that_compares() {
    let nan = PyValue::Float(f64::NAN);
    assert_eq!(
        em::rule_v2_top1_high_score_50(&[hit(Some(nan.clone()))], "0"),
        Ok(false)
    );
    // A NaN second score is not `<= 0`, so the dominance test runs and
    // fails: `60 >= nan` is false.
    let hits = [hit(Some(PyValue::int(60))), hit(Some(nan.clone()))];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(false));
    let second = f64::NAN;
    assert!(
        second.partial_cmp(&0.0).is_none(),
        "a mutant reading `not second > 0` flags it"
    );
    let hits = [hit(Some(nan)), hit(Some(PyValue::int(1)))];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(false));
}

#[test]
fn a_bool_score_is_a_number() {
    assert_eq!(
        em::rule_v3_top1_high_dominant(&[hit(Some(PyValue::Bool(true)))], "0"),
        Ok(true)
    );
    let hits = [hit(Some(PyValue::int(1))), hit(Some(PyValue::Bool(true)))];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(false));
    let hits = [hit(Some(PyValue::int(2))), hit(Some(PyValue::Bool(true)))];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(true));
}

#[test]
fn w2_asks_v1_before_its_own_guard() {
    let junk = [PyValue::str("junk")];
    assert_eq!(
        em::rule_w2_top1_v2_high_from_medium(&junk, "0"),
        Err(EvalModelError::AttributeError("str"))
    );
    assert_eq!(em::rule_w2_top1_v2_high_from_medium(&junk, "1"), Ok(false));
    // A huge negative count is not a recent retrieval.
    let big_negative = format!("-1{}", "0".repeat(30));
    assert_eq!(
        em::rule_w2_top1_v2_high_from_medium(&junk, &big_negative),
        Err(EvalModelError::AttributeError("str"))
    );
}

#[test]
fn the_latest_cutoff_wins_wherever_it_sits() {
    let events = [
        cutoff("2026-11-15T00:00:00Z"),
        audit("2026-11-10T00:00:00Z", vec![]),
        audit("2026-11-20T00:00:00Z", vec![]),
        cutoff("2026-11-01T00:00:00Z"),
    ];
    let w = em::collect_replayable_audits(&events, None, &now()).expect("a walk");
    assert_eq!(w.cutoff_retracted, 1);
    assert_eq!(w.rows.iter().map(|r| r.index).collect::<Vec<_>>(), [2]);
}

#[test]
fn an_unreadable_stamp_drops_under_a_cutoff() {
    let events = [
        cutoff("2026-01-01T00:00:00Z"),
        audit("junk", vec![]),
        audit("2026-11-20T00:00:00Z", vec![]),
    ];
    let w = em::collect_replayable_audits(&events, None, &now()).expect("a walk");
    assert_eq!((w.cutoff_retracted, w.with_features), (1, 1));
    // With no cutoff the same row is kept.
    let w = em::collect_replayable_audits(&events[1..], None, &now()).expect("a walk");
    assert_eq!((w.cutoff_retracted, w.with_features), (0, 2));
}

#[test]
fn a_value_that_is_not_a_dict_is_not_counted() {
    let events = [
        PyValue::None,
        PyValue::int(5),
        PyValue::str("x"),
        audit("2026-11-20T00:00:00Z", vec![]),
    ];
    let span = Some(30 * 86_400_000_000);
    let w = em::collect_replayable_audits(&events, span, &now()).expect("a walk");
    assert_eq!((w.total_events_scanned, w.events_in_window), (1, 1));
    let w = em::collect_replayable_audits(&events, None, &now()).expect("a walk");
    assert_eq!((w.total_events_scanned, w.events_in_window), (1, 1));
}

#[test]
fn repeat_is_read_for_truth() {
    let events = [
        audit("2026-11-20T00:00:00Z", vec![("repeat", PyValue::int(1))]),
        audit("2026-11-20T00:00:00Z", vec![("repeat", PyValue::str("y"))]),
        audit(
            "2026-11-20T00:00:00Z",
            vec![("repeat", PyValue::Float(0.0))],
        ),
    ];
    let w = em::collect_replayable_audits(&events, None, &now()).expect("a walk");
    assert_eq!((w.repeats_skipped, w.with_features), (2, 1));
}

#[test]
fn a_length_counts_code_points() {
    let value = PyValue::str("\u{e9}\u{1d11e}");
    let (preview, len, hash) = em::probe_query_display(Some(&value));
    assert_eq!(preview, Some(cps("\u{e9}\u{1d11e}")));
    assert_eq!(len.as_deref(), Some("2"));
    assert_eq!(hash, None);
    assert_eq!("\u{e9}\u{1d11e}".encode_utf16().count(), 3, "UTF-16 says 3");
    // A bool `len` is not a length.
    let d = PyValue::Dict(vec![(PyValue::str("len"), PyValue::Bool(true))]);
    assert_eq!(em::probe_query_display(Some(&d)).1, None);
}

#[test]
fn the_window_reaches_back_from_now() {
    // `now - since` past the year 1 raises before any event is read.
    // Microseconds from 0001-01-01T00:00 UTC to now.
    let edge = now().instant_us().expect("aware");
    assert!(em::collect_replayable_audits(&[], Some(edge), &now()).is_ok());
    assert_eq!(
        em::collect_replayable_audits(&[], Some(edge + 1), &now()),
        Err(EvalModelError::OverflowError)
    );
    // A naive `now` raises at the first readable stamp it is compared with.
    let naive = DateTime::new(2026, 11, 25, 12, 0, 0, 0, None).expect("naive");
    let events = [audit("junk", vec![]), audit("2026-11-20T00:00:00Z", vec![])];
    assert!(matches!(
        em::collect_replayable_audits(&events, Some(1), &naive),
        Err(EvalModelError::TypeError(_))
    ));
    assert!(em::collect_replayable_audits(&events[..1], Some(1), &naive).is_ok());
}
