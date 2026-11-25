//! Inputs on which a plausible wrong port of `eval_model` answers
//! otherwise, each with the reference's answer written out (read from
//! CPython 3.13.13) and, where the wrong port is a line of arithmetic, that
//! line run beside it to show it differs.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval_model::{self as em, Bound, EvalModelError};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

const DAY_US: i128 = 86_400_000_000;

fn utc(y: i64, mo: u32, d: u32, h: u32, us: u32) -> DateTime {
    DateTime::new(y, mo, d, h, 0, 0, us, Some(0)).expect("a datetime")
}

fn now() -> DateTime {
    utc(2026, 11, 25, 12, 0)
}

fn msg(e: EvalModelError) -> String {
    e.message()
        .iter()
        .map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
        .collect()
}

/// The interval with `p` and the steps as given, for the mutants.
fn wilson_with(p: f64, n: i64, z: f64, reorder: bool) -> (f64, f64) {
    let nf = n as f64;
    let denom = if reorder {
        1.0 + z * (z / nf)
    } else {
        1.0 + z * z / nf
    };
    let center = (p + z * z / (2.0 * nf)) / denom;
    let margin = z * (p * (1.0 - p) / nf + z * z / (4.0 * nf * nf)).sqrt() / denom;
    (center - margin, center + margin)
}

#[test]
fn rate_is_int_division_past_2_pow_53() {
    let (k, n) = ((1i64 << 53) + 1, (1i64 << 53) + 2);
    let r = em::RateCI::from_counts(k, n);
    assert_eq!(r.rate.map(f64::to_bits), Some(0x3fef_ffff_ffff_ffff));
    assert_ne!(((k as f64) / (n as f64)).to_bits(), 0x3fef_ffff_ffff_ffff);
}

#[test]
fn interval_p_is_int_division_past_2_pow_53() {
    let (k, n) = ((1i64 << 53) + 1, (1i64 << 53) + 3);
    let (lo, hi) = em::wilson_interval(k, n, em::WILSON_Z);
    assert_eq!(
        (lo.to_bits(), hi.to_bits()),
        (0x3fef_ffff_ffff_fff9, 0x3fef_ffff_ffff_ffff)
    );
    let (mlo, _) = wilson_with((k as f64) / (n as f64), n, em::WILSON_Z, false);
    assert_ne!(mlo.to_bits(), lo.to_bits());
}

#[test]
fn z_squared_over_n_in_the_reference_order() {
    let (lo, hi) = em::wilson_interval(1, 5, em::WILSON_Z);
    assert_eq!(
        (lo.to_bits(), hi.to_bits()),
        (0x3fa2_8bd7_9772_2a88, 0x3fe3_fbac_265c_6428)
    );
    let (_, mhi) = wilson_with(0.2, 5, em::WILSON_Z, true);
    assert_ne!(mhi.to_bits(), hi.to_bits());
}

#[test]
fn bounds_through_pythons_max_and_min() {
    // A NaN bound is 0.0 below and 1.0 above, as `max(0.0, nan)` and
    // `min(1.0, nan)` keep their first argument; a clamp lets it through.
    let (lo, hi) = em::wilson_interval(1, 3, f64::NAN);
    assert_eq!(
        (lo.to_bits(), hi.to_bits()),
        (0.0f64.to_bits(), 1.0f64.to_bits())
    );
    assert!(f64::NAN.clamp(0.0, 1.0).is_nan());
    let (lo, hi) = em::wilson_interval(0, 1, f64::INFINITY);
    assert_eq!((lo, hi), (0.0, 1.0));
    // z = -0.0: both bounds +0.0.
    let (lo, hi) = em::wilson_interval(0, 1, -0.0);
    assert_eq!((lo.to_bits(), hi.to_bits()), (0, 0));
    // A negative z swaps the sides; nothing reorders them.
    let (lo, hi) = em::wilson_interval(1, 3, -1.96);
    assert!(lo > hi);
}

#[test]
fn a_torn_read_clamps_the_rate() {
    let r = em::RateCI::from_counts(2, 1);
    assert_eq!(r.rate, Some(1.0));
    assert!(r.torn_read);
    assert_eq!(
        (r.lower.map(f64::to_bits), r.upper.map(f64::to_bits)),
        (Some(0x3fca_7002_b50b_5620), Some(0x3ff0_0000_0000_0000))
    );
    // A negative numerator is not torn: the rate divides, the interval
    // clamps k to 0.
    let r = em::RateCI::from_counts(-1, 3);
    assert_eq!(r.rate, Some(-1.0 / 3.0));
    assert!(!r.torn_read);
    assert_eq!(r.lower, Some(0.0));
}

#[test]
fn no_rate_for_a_denominator_not_positive() {
    for d in [0, -1, i64::MIN] {
        let r = em::RateCI::from_counts(5, d);
        assert_eq!(
            (r.rate, r.lower, r.upper, r.torn_read),
            (None, None, None, false)
        );
    }
}

#[test]
fn since_reads_unicode_digits() {
    let span = |s: &str| em::parse_since(Some(&cps(s)));
    assert_eq!(span("\u{663}\u{660}d"), Ok(Some(30 * DAY_US)));
    assert_eq!(span("3\u{660}d"), Ok(Some(30 * DAY_US)));
    assert_eq!(span("\u{ff13}\u{ff10}d"), Ok(Some(30 * DAY_US)));
    assert_eq!(span("\u{967}\u{966}\u{966}m"), Ok(Some(100 * 60_000_000)));
    // A superscript two is a digit to `isdigit`, not to `\d`.
    assert!(span("\u{b2}d").is_err());
}

#[test]
fn the_digit_limit_is_cpythons_own_text() {
    let spec: String = "1".repeat(4301) + "d";
    let e = em::parse_since(Some(&cps(&spec))).expect_err("past the limit");
    assert_eq!(e.name(), "ValueError");
    assert_eq!(
        msg(e),
        "Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; \
use sys.set_int_max_str_digits() to increase the limit"
    );
    // Leading zeros count; 4,300 digits are read.
    let zeros: String = "0".repeat(4301) + "d";
    assert!(msg(em::parse_since(Some(&cps(&zeros))).expect_err("limit")).starts_with("Exceeds"));
    let one: String = "0".repeat(4299) + "1d";
    assert_eq!(em::parse_since(Some(&cps(&one))), Ok(Some(DAY_US)));
    let nines: String = "9".repeat(4300) + "d";
    assert!(msg(em::parse_since(Some(&cps(&nines))).expect_err("range"))
        .starts_with("--since: value out of range"));
}

#[test]
fn the_span_limit_is_a_billion_days() {
    let span = |s: &str| em::parse_since(Some(&cps(s)));
    assert_eq!(span("999999999d"), Ok(Some(999_999_999 * DAY_US)));
    assert!(span("1000000000d").is_err());
    assert_eq!(
        span("86399999999999s"),
        Ok(Some(86_399_999_999_999 * 1_000_000))
    );
    assert!(span("86400000000000s").is_err());
    assert_eq!(
        msg(span("0000d").expect_err("zero")),
        "--since: value must be positive, got 0"
    );
}

#[test]
fn all_and_empty_are_checked_before_the_strip() {
    assert_eq!(em::parse_since(Some(&cps(""))), Ok(None));
    assert_eq!(em::parse_since(Some(&cps("all"))), Ok(None));
    assert!(em::parse_since(Some(&cps(" all"))).is_err());
    assert!(em::parse_since(Some(&cps("  "))).is_err());
    // The window bound strips first.
    assert_eq!(
        em::parse_window_bound(Some(&cps(" all ")), "--since", &now()),
        Ok(None)
    );
    assert_eq!(
        em::parse_window_bound(Some(&cps("\u{3000}")), "--since", &now()),
        Ok(None)
    );
}

#[test]
fn the_year_one_edge_to_the_microsecond() {
    // now - _EARLIEST_START is 63,931,118,400 s at 2026-11-25T12:00Z.
    let edge = "63931118400s";
    let past = "63931118401s";
    let bound = |s: &str, now: &DateTime| em::parse_window_bound(Some(&cps(s)), "--since", now);
    assert_eq!(
        bound(edge, &now()),
        Ok(Some(Bound::Span(63_931_118_400 * 1_000_000)))
    );
    assert!(bound(past, &now()).is_err());
    // One microsecond later still refuses a span one second longer, and
    // one microsecond earlier refuses the edge itself.
    assert!(bound(past, &utc(2026, 11, 25, 12, 999_999)).is_err());
    let before = DateTime::new(2026, 11, 25, 11, 59, 59, 999_999, Some(0)).expect("a datetime");
    assert!(bound(edge, &before).is_err());
}

#[test]
fn a_later_until_is_kept_and_a_later_since_refused() {
    let spec = cps("9999-12-31T23:59:59Z");
    let at = DateTime::new(9999, 12, 31, 23, 59, 59, 0, Some(0)).expect("a datetime");
    assert_eq!(
        em::parse_window_bound(Some(&spec), "--until", &now()),
        Ok(Some(Bound::At(at)))
    );
    assert!(em::parse_window_bound(Some(&spec), "--since", &now()).is_err());
    // Any flag but the literal `--since` keeps it.
    assert_eq!(
        em::parse_window_bound(Some(&spec), "--when", &now()),
        Ok(Some(Bound::At(at)))
    );
}

#[test]
fn later_than_now_compares_instants() {
    // 11:00:00-01:00 is now; 11:00:01-01:00 is a second later, though its
    // wall clock reads earlier.
    let b = |s: &str| em::parse_window_bound(Some(&cps(s)), "--since", &now());
    assert!(b("2026-11-25T11:00:00-01:00").is_ok());
    assert!(b("2026-11-25T11:00:01-01:00").is_err());
}

#[test]
fn a_span_error_names_since_under_until() {
    let e = em::parse_window_bound(Some(&cps("0d")), "--until", &now()).expect_err("zero");
    assert_eq!(msg(e), "--since: value must be positive, got 0");
}

#[test]
fn a_naive_now_raises_where_the_reference_meets_it() {
    let naive = DateTime::new(2026, 11, 25, 12, 0, 0, 0, None).expect("a datetime");
    let b = |s: &str, f: &str| em::parse_window_bound(Some(&cps(s)), f, &naive);
    assert_eq!(
        b("30d", "--until").map_err(msg),
        Err("can't subtract offset-naive and offset-aware datetimes".into())
    );
    assert_eq!(
        b("2026-10-01T00:00:00Z", "--since").map_err(msg),
        Err("can't compare offset-naive and offset-aware datetimes".into())
    );
    assert!(b("2026-10-01T00:00:00Z", "--until").is_ok());
}

#[test]
fn a_lowercase_z_and_the_shape_test() {
    let b = |s: &str| em::parse_window_bound(Some(&cps(s)), "--since", &now());
    assert!(b("2026-10-01T00:00:00z").is_ok());
    // `fromisoformat` reads the compact form; the shape test refuses it
    // for want of a '-'.
    assert!(b("20261001T000000Z").is_err());
    let naive = msg(b("2026-10-01").expect_err("naive"));
    assert_eq!(
        naive,
        "--since: '2026-10-01' has no UTC offset; add 'Z' (for example 2026-10-01Z)."
    );
}

fn event(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        pairs
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

#[test]
fn the_session_is_read_with_or() {
    for falsy in [
        PyValue::str(""),
        PyValue::int(0),
        PyValue::Bool(false),
        PyValue::List(vec![]),
        PyValue::Dict(vec![]),
        PyValue::Float(-0.0),
    ] {
        let ev = event(vec![
            ("ts", PyValue::str("t")),
            ("session_id", falsy),
            ("session", PyValue::str("s2")),
        ]);
        let c = em::silent_miss_from_event(&ev)
            .expect("a dict")
            .expect("a candidate");
        assert_eq!(c.session_id, cps("s2"));
    }
    // A truthy non-str is kept, and the event is not a candidate.
    let ev = event(vec![
        ("ts", PyValue::str("t")),
        ("session_id", PyValue::Bool(true)),
        ("session", PyValue::str("s2")),
    ]);
    assert_eq!(em::silent_miss_from_event(&ev), Ok(None));
}

#[test]
fn the_legacy_ids_for_an_empty_top_hits() {
    let ev = |top: PyValue| {
        event(vec![
            ("ts", PyValue::str("t")),
            ("session_id", PyValue::str("s")),
            ("top_hits", top),
            ("top_hit_ids", PyValue::List(vec![PyValue::str("l1")])),
        ])
    };
    let top = |e: &PyValue| {
        em::silent_miss_from_event(e)
            .expect("a dict")
            .expect("a candidate")
            .top_missed_id
    };
    assert_eq!(top(&ev(PyValue::List(vec![]))), Some(cps("l1")));
    assert_eq!(top(&ev(PyValue::None)), Some(cps("l1")));
    // A non-empty list whose first entry is not a dict reads neither.
    assert_eq!(top(&ev(PyValue::List(vec![PyValue::str("a")]))), None);
    // `_canonical_top_hit_id` never reads the legacy ids.
    assert_eq!(
        em::canonical_top_hit_id(&ev(PyValue::List(vec![]))),
        Ok(None)
    );
}

#[test]
fn a_bool_is_not_a_retrieval_count() {
    for (value, want) in [
        (PyValue::Bool(true), "0"),
        (PyValue::Bool(false), "0"),
        (PyValue::Float(3.0), "0"),
        (PyValue::str("3"), "0"),
        (PyValue::int(-1), "-1"),
        (
            PyValue::Int("1180591620717411303424".into()),
            "1180591620717411303424",
        ),
    ] {
        let ev = event(vec![
            ("ts", PyValue::str("t")),
            ("session_id", PyValue::str("s")),
            ("recent_retrieval_count", value),
        ]);
        let c = em::silent_miss_from_event(&ev)
            .expect("a dict")
            .expect("a candidate");
        assert_eq!(c.recent_retrieval_count, want);
    }
}

#[test]
fn the_window_is_half_open_on_instants() {
    let start = utc(2026, 1, 1, 0, 0);
    let end = utc(2026, 2, 1, 0, 0);
    let show = |ts: &str| event(vec![("ts", PyValue::str(ts))]);
    let w = |ts: &str| em::show_in_window(&show(ts), Some(&start), Some(&end));
    assert_eq!(w("2026-01-01T00:00:00Z"), Ok(true));
    assert_eq!(w("2026-02-01T00:00:00Z"), Ok(false));
    assert_eq!(w("2026-02-01T00:59:59+01:00"), Ok(true));
    assert_eq!(w("2026-01-15T00:00:00"), Ok(true));
    // No readable stamp: only an unbounded window holds it.
    assert_eq!(w("bad"), Ok(false));
    assert_eq!(em::show_in_window(&show("bad"), None, None), Ok(true));
    assert_eq!(
        em::show_in_window(&PyValue::List(vec![]), None, None).map_err(msg),
        Err("'list' object has no attribute 'get'".into())
    );
}

#[test]
fn resolve_moves_now_by_the_span() {
    let since = Bound::Span(30 * DAY_US);
    let (start, end) = em::resolve_window(Some(&since), None, &now()).expect("in range");
    assert_eq!(start, Some(utc(2026, 10, 26, 12, 0)));
    assert_eq!(end, None);
    let huge = Bound::Span(999_999_999 * DAY_US);
    assert_eq!(
        em::resolve_window(Some(&huge), None, &now()).map_err(msg),
        Err("date value out of range".into())
    );
}
