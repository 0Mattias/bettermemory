//! Hostile input to `eval_model::rules`: a million events through the
//! replayable-audit walk (linear, through a `tests/timing` growth claim),
//! scores past 2**1024 and of 4,300 digits, `top_hits` of 100,000
//! entries, spans at both ends of `i128`, `now` at years 1 and 9999; no
//! panic in debug or release.

mod timing;

use std::hint::black_box;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval_model::{self as em, ThresholdRule};
use bettermemory_core::pyvalue::PyValue;

fn now() -> DateTime {
    DateTime::new(2026, 11, 25, 12, 0, 0, 0, Some(0)).expect("a datetime")
}

fn all_rules() -> Vec<ThresholdRule> {
    em::THRESHOLD_RULES
        .iter()
        .chain(em::WIDENING_RULES.iter())
        .copied()
        .collect()
}

fn hit(rel: &str, score: PyValue) -> PyValue {
    PyValue::Dict(vec![
        (PyValue::str("relevance"), PyValue::str(rel)),
        (PyValue::str("relevance_v2"), PyValue::str("high")),
        (PyValue::str("score"), score),
    ])
}

fn int(text: &str) -> PyValue {
    PyValue::int_from_decimal(text).expect("decimal")
}

/// `n` events cycling through every kind the walk tells apart.
fn stream(n: usize) -> Vec<PyValue> {
    (0..n)
        .map(|i| {
            let day = 1 + i % 28;
            let ts = PyValue::str(&format!("2026-11-{day:02}T00:00:00Z"));
            match i % 7 {
                0 => PyValue::int(i as i128),
                1 => PyValue::Dict(vec![
                    (PyValue::str("kind"), PyValue::str("silent_miss_cutoff")),
                    (
                        PyValue::str("cutoff_ts"),
                        PyValue::str(&format!("2026-11-{:02}T00:00:00Z", 1 + i % 3)),
                    ),
                ]),
                _ => PyValue::Dict(vec![
                    (PyValue::str("kind"), PyValue::str("turn_audited")),
                    (PyValue::str("ts"), ts),
                    (PyValue::str("repeat"), PyValue::Bool(i % 7 == 2)),
                    (
                        PyValue::str("top_hits"),
                        PyValue::List(vec![hit("high", PyValue::int(60))]),
                    ),
                    (PyValue::str("recent_retrieval_count"), PyValue::int(0)),
                ]),
            }
        })
        .collect()
}

#[test]
fn a_million_events_walk_in_linear_time() {
    let big = stream(1_000_000);
    let small = stream(100_000);
    let w = em::collect_replayable_audits(&big, Some(30 * 86_400_000_000), &now()).expect("a walk");
    assert_eq!(w.total_events_scanned, 1_000_000 - 1_000_000 / 7 - 1);
    assert!(w.with_features > 0);
    timing::grows_within(
        "_collect_replayable_audits over events",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(em::collect_replayable_audits(&small, None, &now()))),
        &mut || timing::once(|| black_box(em::collect_replayable_audits(&big, None, &now()))),
    );
}

#[test]
fn scores_past_two_to_the_1024_and_of_4300_digits() {
    let huge = format!("9{}", "9".repeat(4299));
    let huge_less = format!("4{}", "9".repeat(4299));
    let two_1030 = {
        let mut t = "1".to_string();
        for _ in 0..1030 {
            let mut out = Vec::new();
            let mut carry = 0;
            for b in t.bytes().rev() {
                let v = (b - b'0') * 2 + carry;
                out.push(b'0' + v % 10);
                carry = v / 10;
            }
            if carry > 0 {
                out.push(b'0' + carry);
            }
            out.reverse();
            t = String::from_utf8(out).expect("ASCII");
        }
        t
    };
    let values = [
        int(&huge),
        int(&format!("-{huge}")),
        int(&huge_less),
        int(&two_1030),
        int(&format!("-{two_1030}")),
        PyValue::Float(f64::MAX),
        PyValue::Float(f64::MIN_POSITIVE),
        PyValue::Float(-f64::MAX),
        PyValue::Float(f64::INFINITY),
        PyValue::Float(f64::NAN),
        PyValue::Float(5e-324),
        PyValue::Bool(true),
        PyValue::int(i128::MAX),
        PyValue::int(i128::MIN),
    ];
    for top in &values {
        for second in &values {
            let hits = [hit("high", top.clone()), hit("low", second.clone())];
            for rule in all_rules() {
                for recent in ["0", "-1", &huge] {
                    let _ = black_box((rule.check)(&hits, recent));
                }
            }
        }
    }
    // 4,999...9 doubled is 9,999...8 < 9,999...9: exact at 4,300 digits.
    let hits = [hit("high", int(&huge)), hit("low", int(&huge_less))];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(true));
    let hits = [hit("high", int(&huge_less)), hit("low", int(&huge_less))];
    assert_eq!(em::rule_v3_top1_high_dominant(&hits, "0"), Ok(false));
}

#[test]
fn top_hits_of_100000_entries() {
    let hits: Vec<PyValue> = (0..100_000)
        .map(|i| hit("high", PyValue::int(100_000 - i)))
        .collect();
    for rule in all_rules() {
        assert_eq!(
            (rule.check)(&hits, "0"),
            Ok(rule.name != "v3_top1_high_dominant" && rule.name != "v4_top1_high_strict_combined")
        );
    }
    let event = PyValue::Dict(vec![
        (PyValue::str("kind"), PyValue::str("turn_audited")),
        (PyValue::str("top_hits"), PyValue::List(hits)),
    ]);
    let events = vec![event; 10];
    let w = em::collect_replayable_audits(&events, None, &now()).expect("a walk");
    assert_eq!(w.rows.len(), 10);
    assert_eq!(w.rows[0].top_hits.len(), 100_000);
}

#[test]
fn spans_and_clocks_at_their_ends() {
    let events = stream(50);
    let clocks = [
        now(),
        DateTime::new(1, 1, 1, 0, 0, 0, 0, Some(0)).expect("year 1"),
        DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, Some(0)).expect("year 9999"),
        DateTime::new(1, 1, 1, 0, 0, 0, 0, Some(86_399_999_999)).expect("an offset"),
        DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, Some(-86_399_999_999)).expect("an offset"),
        DateTime::new(2026, 11, 25, 12, 0, 0, 0, None).expect("naive"),
    ];
    let spans = [
        None,
        Some(0),
        Some(1),
        Some(-1),
        Some(i128::from(i64::MAX)),
        Some(i128::from(i64::MIN)),
        Some(i128::MAX),
        Some(i128::MIN),
        Some(86_399_999_999_999_999_999),
    ];
    for clock in &clocks {
        for span in spans {
            let _ = black_box(em::collect_replayable_audits(&events, span, clock));
        }
    }
}

#[test]
fn probe_query_display_of_every_shape() {
    let long = PyValue::str(&"\u{1d11e}".repeat(100_000));
    let (_, len, _) = em::probe_query_display(Some(&long));
    assert_eq!(len.as_deref(), Some("100000"));
    let shapes = [
        PyValue::None,
        PyValue::List(vec![long.clone(); 3]),
        PyValue::Dict(vec![
            (PyValue::str("len"), int(&"9".repeat(4300))),
            (PyValue::str("preview"), long),
        ]),
    ];
    for s in &shapes {
        let _ = black_box(em::probe_query_display(Some(s)));
    }
    let _ = black_box(em::probe_query_display(None));
}
