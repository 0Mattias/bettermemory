//! Hostile input to `health_model`: counts at the ends of `i64`, years 1
//! and 9999 at the widest offsets, timelines of 100,000 entries, deep
//! events; no panic in debug or release, `to_dict` linear in the
//! timeline and the predicates linear in an event's keys; `round_digits`
//! exact at 15 digits and refusing more.

mod timing;

use std::hint::black_box;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::health_model::{
    self as hm, ActorSlice, MarkerStats, MemoryStats, ScopeHealth,
};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::search::num::round_digits;
use bettermemory_core::unicode::cps;

const OFFSETS_US: [Option<i64>; 9] = [
    None,
    Some(0),
    Some(86_399_999_999),
    Some(-86_399_999_999),
    Some(86_340_000_000),
    Some(-86_340_000_000),
    Some(1),
    Some(-1),
    Some(30_000_000),
];

fn edges() -> Vec<DateTime> {
    let mut out = Vec::new();
    for off in OFFSETS_US {
        out.push(DateTime::new(1, 1, 1, 0, 0, 0, 0, off).expect("year 1"));
        out.push(DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, off).expect("year 9999"));
    }
    out
}

const COUNTS: [i64; 5] = [0, 1, -1, i64::MAX, i64::MIN];

#[test]
fn counts_at_the_ends_of_i64() {
    for &a in &COUNTS {
        for &b in &COUNTS {
            let m = MarkerStats {
                marker: cps("m"),
                fire_count: a,
                override_count: b,
            };
            let rate = m.override_rate();
            assert!(rate.is_finite());
            black_box(m.to_dict());
            if let Some(r) = hm::explicit_ratio(a, b) {
                assert!(r.is_finite());
            }
            let mut s = MemoryStats::new(cps("m"), vec![], cps(""), edges()[1], edges()[1]);
            s.applied_count = a;
            s.explicit_applied_count = b;
            s.model_applied_count = b;
            s.retrieval_count = a;
            let d = s.to_dict().expect("aware");
            black_box(json::dumps(&d, DumpOptions::DEFAULT).expect("dumps"));
            let dw = hm::DeadWeight {
                category: None,
                freshest_ts: 0.0,
                retrieval_count: a,
                applied_count: b,
                has_unresolved_contradiction: false,
                earliest_retrieval_ts: None,
                cutoff_ts: 1.0,
                grace_cutoff_ts: 1.0,
            };
            black_box(dw.is_dead_weight());
        }
    }
    let scope = ScopeHealth {
        scope: cps("s"),
        active: i64::MAX,
        dead: i64::MIN,
        ..ScopeHealth::default()
    };
    black_box(scope.to_dict());
    let actor = ActorSlice {
        memories: i64::MIN,
        models: vec![(cps("x"), i64::MAX)],
        ..ActorSlice::default()
    };
    black_box(actor.to_dict());
    assert_eq!(hm::explicit_ratio(i64::MAX, i64::MIN), Some(-1.0));
}

#[test]
fn years_one_and_9999_at_every_offset() {
    let all = edges();
    for a in &all {
        match hm::timestamp(a) {
            Ok(t) => assert!(t.is_finite() && a.offset_us.is_some()),
            Err(e) => assert_eq!(
                (e, a.offset_us),
                (hm::HealthModelError::NaiveTimestamp, None)
            ),
        }
        for b in &all {
            let _ = black_box(hm::freshest_touch_ts(a, b, Some(a), Some(b)));
            let _ = black_box(hm::has_unresolved_contradiction(Some(a), b, Some(a)));
            let mut m = MemoryStats::new(cps("m"), vec![], cps(""), *a, *b);
            m.last_contradicted_at = Some(*a);
            m.last_verified_at = Some(*b);
            m.last_used_at = Some(*a);
            let _ = black_box(m.to_dict());
        }
    }
    // The extremes' timestamps, exact.
    let first = DateTime::new(1, 1, 1, 0, 0, 0, 0, Some(86_399_999_999)).expect("dt");
    assert_eq!(hm::timestamp(&first), Ok(-62_135_683_199.999_999));
    let last = DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, Some(-86_399_999_999)).expect("dt");
    assert_eq!(hm::timestamp(&last), Ok(253_402_387_199.999_998));
}

#[test]
fn round_three_over_every_exponent() {
    for e in 0..2047u64 {
        for m in [0u64, 1, (1 << 52) - 1, 0x8_0000_0000_0000] {
            for sign in [0u64, 1] {
                let x = f64::from_bits((sign << 63) | (e << 52) | m);
                let r = round_digits(x, 3);
                if x.is_finite() {
                    assert!((r - x).abs() <= 0.0005 + x.abs() * 1e-15, "round({x:e}, 3)");
                } else {
                    assert_eq!(r.to_bits(), x.to_bits());
                }
            }
        }
    }
}

#[test]
fn round_at_fifteen_digits_is_python_s() {
    // CPython 3.13's round(x, 15) of each.
    let cases: [(f64, f64); 9] = [
        (0.1, 0.1),
        (0.5, 0.5),
        (2.675, 2.675),
        (1.000_000_000_000_000_4, 1.0),
        (0.123_456_789_012_345_68, 0.123_456_789_012_346),
        (5e-16, 1e-15),
        (1.5e-15, 1e-15),
        (2.5e-15, 2e-15),
        (-3.5e-15, -4e-15),
    ];
    for (x, want) in cases {
        assert_eq!(
            round_digits(x, 15).to_bits(),
            want.to_bits(),
            "round({x:e}, 15)"
        );
    }
}

// Python's round(0.5, 16) and round(0.1, 22) are 0.5 and 0.1; the port
// refuses a count it does not hold, in release as in debug.
#[test]
#[should_panic(expected = "round_digits holds 0 to 15 digits, not 16")]
fn round_refuses_sixteen_digits() {
    black_box(round_digits(black_box(0.5), black_box(16)));
}

#[test]
#[should_panic(expected = "round_digits holds 0 to 15 digits, not 22")]
fn round_refuses_twenty_two_digits() {
    black_box(round_digits(black_box(0.1), black_box(22)));
}

fn timeline(n: usize) -> MemoryStats {
    let mut m = MemoryStats::new(
        cps("m"),
        vec![cps("s"); 4],
        cps("x"),
        edges()[1],
        edges()[1],
    );
    m.resolution_timeline = (0..n)
        .map(|i| {
            PyValue::Dict(vec![
                (PyValue::str("kind"), PyValue::str("update")),
                (PyValue::str("ts"), PyValue::str("2026-03-01T12:00:00Z")),
                (PyValue::str("note"), PyValue::int(i as i128)),
            ])
        })
        .collect();
    m
}

#[test]
fn a_timeline_of_100000_entries() {
    let big = timeline(100_000);
    let d = big.to_dict().expect("aware");
    let PyValue::Dict(items) = &d else {
        panic!("a dict")
    };
    let PyValue::List(t) = &items[21].1 else {
        panic!("a list")
    };
    assert_eq!(t.len(), 100_000);
    let small = timeline(10_000);
    timing::grows_within(
        "to_dict over the timeline",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(small.to_dict())),
        &mut || timing::once(|| black_box(big.to_dict())),
    );
}

#[test]
fn deep_and_wide_events() {
    // A dict nested 100,000 deep under `auto`, and 100,000 keys before
    // `kind`: the predicates read three keys and never descend.
    let mut deep = PyValue::Bool(true);
    for _ in 0..100_000 {
        deep = PyValue::Dict(vec![(PyValue::str("auto"), deep)]);
    }
    let ev = PyValue::Dict(vec![
        (PyValue::str("kind"), PyValue::str("use")),
        (PyValue::str("outcome"), PyValue::str("applied")),
        (PyValue::str("auto"), deep),
    ]);
    assert_eq!(hm::endorsement_tier(&ev), Ok(Some(hm::AppliedTier::Model)));
    let mut wide: Vec<(PyValue, PyValue)> = (0..100_000)
        .map(|i| (PyValue::str(&format!("k{i}")), PyValue::None))
        .collect();
    wide.push((PyValue::str("kind"), PyValue::str("turn_audited")));
    wide.push((PyValue::str("triggered_from"), PyValue::str("stop_hook")));
    assert_eq!(hm::is_hook_telemetry_event(&PyValue::Dict(wide)), Ok(true));
}

fn wide_event(n: usize) -> PyValue {
    // The keys the predicates read come first, so the read from the end
    // passes every filler key before it finds them.
    let mut items = vec![
        (PyValue::str("kind"), PyValue::str("use")),
        (PyValue::str("outcome"), PyValue::str("applied")),
        (PyValue::str("auto"), PyValue::Bool(false)),
        (PyValue::str("attribution"), PyValue::str("hook")),
        (PyValue::str("triggered_from"), PyValue::str("stop_hook")),
    ];
    items.extend((0..n).map(|i| (PyValue::str(&format!("k{i}")), PyValue::int(i as i128))));
    PyValue::Dict(items)
}

#[test]
fn the_predicates_are_linear_in_the_event() {
    let read = |ev: &PyValue| {
        (
            hm::is_hook_telemetry_event(ev),
            hm::applied_tier(ev),
            hm::endorsement_tier(ev),
        )
    };
    let small = wide_event(20_000);
    let big = wide_event(200_000);
    assert_eq!(
        read(&big),
        (
            Ok(true),
            Ok(hm::AppliedTier::Hook),
            Ok(Some(hm::AppliedTier::Hook))
        )
    );
    timing::grows_within(
        "the three predicates over an event's keys",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(read(black_box(&small)))),
        &mut || timing::once(|| black_box(read(black_box(&big)))),
    );
}
