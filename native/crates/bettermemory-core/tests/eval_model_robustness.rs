//! Hostile input to `eval_model`: specs of a megabyte of digits and of
//! whitespace (linear, through `tests/timing` growth claims), counts at
//! both ends of `i64`, extreme `z`, `now` at years 1 and 9999, every
//! numeric code point before a unit, wide and deep events; no panic in
//! debug or release.

mod timing;

use std::hint::black_box;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval_model::{self as em, Bound};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::{self, cps};

const COUNTS: [i64; 9] = [
    i64::MIN,
    i64::MIN + 1,
    -(1 << 53) - 1,
    -1,
    0,
    1,
    (1 << 53) + 1,
    i64::MAX - 1,
    i64::MAX,
];

fn now() -> DateTime {
    DateTime::new(2026, 11, 25, 12, 0, 0, 0, Some(0)).expect("a datetime")
}

fn digits(n: usize, tail: &str) -> Vec<u32> {
    let mut s = vec![u32::from(b'1'); n];
    s.extend(cps(tail));
    s
}

fn spaced(n: usize) -> Vec<u32> {
    let mut s = vec![0x3000; n];
    s.extend(cps("30d"));
    s.extend(std::iter::repeat_n(0x20, n));
    s
}

#[test]
fn counts_at_the_ends_of_i64() {
    for &n in &COUNTS {
        for &d in &COUNTS {
            let r = em::RateCI::from_counts(n, d);
            if d <= 0 {
                assert_eq!(r.rate, None);
                continue;
            }
            let (lo, hi) = (r.lower.expect("lower"), r.upper.expect("upper"));
            assert!(
                (0.0..=1.0).contains(&lo) && (0.0..=1.0).contains(&hi),
                "{n}/{d}"
            );
            assert!(lo <= hi, "{n}/{d}");
            assert_eq!(r.torn_read, n > d);
            black_box(r.to_dict());
        }
    }
}

#[test]
fn any_z() {
    let zs = [
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::MAX,
        f64::MIN_POSITIVE,
        5e-324,
        -0.0,
        0.0,
        -1e300,
    ];
    for z in zs {
        for &k in &COUNTS {
            for &n in &COUNTS {
                let (lo, hi) = em::wilson_interval(k, n, z);
                // Python's max and min never answer NaN here.
                assert!(!lo.is_nan() && !hi.is_nan(), "{k} {n} {z}");
            }
        }
    }
}

#[test]
fn a_megabyte_of_digits() {
    let big = digits(1_000_000, "d");
    let e = em::parse_since(Some(&big)).expect_err("past the digit limit");
    assert!(unicode::to_string(&e.message()).expect("ASCII").ends_with(
        "value has 1000000 digits; use sys.set_int_max_str_digits() to increase the limit"
    ));
    let small = digits(100_000, "d");
    timing::grows_within(
        "parse_since over digits",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(em::parse_since(Some(&small)))),
        &mut || timing::once(|| black_box(em::parse_since(Some(&big)))),
    );
    // No unit: the pattern backtracks once over the run, and the message
    // holds the whole spec.
    let big = digits(1_000_000, "");
    let e = em::parse_since(Some(&big)).expect_err("no unit");
    assert!(e.message().len() > 1_000_000);
    let small = digits(100_000, "");
    timing::grows_within(
        "parse_since over digits without a unit",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(em::parse_since(Some(&small)))),
        &mut || timing::once(|| black_box(em::parse_since(Some(&big)))),
    );
    // A digit and a '-': the timestamp reader refuses it.
    let big = digits(1_000_000, "-");
    assert!(em::parse_window_bound(Some(&big), "--since", &now()).is_err());
    let small = digits(100_000, "-");
    timing::grows_within(
        "parse_window_bound over digits and a dash",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(em::parse_window_bound(Some(&small), "--since", &now()))),
        &mut || timing::once(|| black_box(em::parse_window_bound(Some(&big), "--since", &now()))),
    );
}

#[test]
fn a_megabyte_of_whitespace() {
    let big = spaced(500_000);
    assert_eq!(
        em::parse_window_bound(Some(&big), "--since", &now()),
        Ok(Some(Bound::Span(30 * 86_400_000_000)))
    );
    assert_eq!(em::parse_since(Some(&big)), Ok(Some(30 * 86_400_000_000)));
    let small = spaced(50_000);
    timing::grows_within(
        "parse_window_bound over whitespace",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(em::parse_window_bound(Some(&small), "--since", &now()))),
        &mut || timing::once(|| black_box(em::parse_window_bound(Some(&big), "--since", &now()))),
    );
    let blank = vec![0x20u32; 1_000_000];
    assert_eq!(
        em::parse_window_bound(Some(&blank), "--until", &now()),
        Ok(None)
    );
    assert!(em::parse_since(Some(&blank)).is_err());
}

#[test]
fn every_numeric_code_point_before_a_unit() {
    // `\d` and `int()` read the same digits: a decimal digit gives a span
    // or the zero refusal, anything else numeric the shape refusal.
    let mut decimals = 0;
    for cp in 0..=0x10_FFFFu32 {
        if !unicode::is_numeric(cp) {
            continue;
        }
        let spec = vec![cp, u32::from(b'd')];
        let got = em::parse_since(Some(&spec));
        if unicode::is_decimal(cp) {
            decimals += 1;
            match got {
                Ok(Some(us)) => assert!(us > 0),
                Err(e) => assert!(unicode::to_string(&e.message())
                    .is_some_and(|m| m == "--since: value must be positive, got 0")),
                Ok(None) => panic!("U+{cp:04X}"),
            }
        } else {
            assert!(got.is_err(), "U+{cp:04X}");
        }
    }
    assert!(decimals > 600);
}

#[test]
fn now_at_years_1_and_9999() {
    let nows = [
        DateTime::new(1, 1, 1, 0, 0, 0, 0, Some(0)),
        DateTime::new(1, 1, 1, 0, 0, 0, 0, Some(86_399_999_999)),
        DateTime::new(1, 1, 2, 0, 0, 0, 0, Some(-86_399_999_999)),
        DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, Some(0)),
        DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, Some(-86_399_999_999)),
        DateTime::new(9999, 12, 31, 0, 0, 0, 0, None),
    ];
    let specs = [
        "1s",
        "999999999d",
        "3652058d",
        "0001-01-01T00:00:00Z",
        "9999-12-31T23:59:59.999999Z",
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59-23:59",
    ];
    for now in nows {
        let now = now.expect("a datetime");
        for spec in specs {
            for flag in ["--since", "--until"] {
                let b = em::parse_window_bound(Some(&cps(spec)), flag, &now);
                if let Ok(b) = b {
                    black_box(em::resolve_window(b.as_ref(), b.as_ref(), &now)).ok();
                }
            }
        }
        for us in [0, 1, i128::MAX, i128::MIN + 1, -1] {
            black_box(em::resolve_window(Some(&Bound::Span(us)), None, &now)).ok();
        }
    }
}

#[test]
fn wide_and_deep_events() {
    let mut deep = PyValue::str("leaf");
    for _ in 0..100_000 {
        deep = PyValue::List(vec![deep]);
    }
    let ev = PyValue::Dict(vec![
        (PyValue::str("ts"), PyValue::str("t")),
        (PyValue::str("session"), PyValue::str("s")),
        (PyValue::str("top_hits"), PyValue::List(vec![])),
        (PyValue::str("top_hit_ids"), deep),
    ]);
    let c = em::silent_miss_from_event(&ev)
        .expect("a dict")
        .expect("a candidate");
    assert_eq!(c.top_missed_id, None);
    let mut items: Vec<(PyValue, PyValue)> = (0..200_000)
        .map(|i| (PyValue::int(i), PyValue::None))
        .collect();
    items.push((PyValue::str("ts"), PyValue::str("2026-01-15T00:00:00Z")));
    let show = PyValue::Dict(items);
    assert_eq!(em::show_in_window(&show, None, None), Ok(true));
    assert_eq!(em::canonical_top_hit_id(&show), Ok(None));
}
