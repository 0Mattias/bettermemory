//! Cases a plausible wrong `health_model` fails, each with the reference's
//! answer (read off CPython 3.13.13) and, where the wrong form is a
//! one-line rewrite, that form's answer beside it.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::health_model::{
    self as hm, AppliedTier, DeadWeight, HealthModelError, MarkerStats, MemoryStats,
};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Category;
use bettermemory_core::search::num::{round4, round_digits};
use bettermemory_core::unicode::cps;

fn utc(y: i64, mo: u32, d: u32, h: u32, mi: u32, s: u32, us: u32) -> DateTime {
    DateTime::new(y, mo, d, h, mi, s, us, Some(0)).expect("a datetime")
}

fn at(offset_minutes: i64, dt: DateTime) -> DateTime {
    // The same instant at another offset: the wall clock moved by it.
    let off = offset_minutes * 60_000_000;
    let moved = dt.minus_micros(-i128::from(off)).expect("in range");
    DateTime {
        offset_us: Some(off),
        ..moved
    }
}

fn naive(dt: DateTime) -> DateTime {
    DateTime {
        offset_us: None,
        ..dt
    }
}

fn ev(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn d1() -> DateTime {
    utc(2026, 3, 1, 12, 0, 0, 0)
}

fn memory() -> MemoryStats {
    MemoryStats::new(cps("m"), vec![cps("global")], cps("s"), d1(), d1())
}

#[test]
fn round_is_half_even_on_the_binary_value_not_half_away() {
    // (x * 1000).round() / 1000 rounds half away from zero, after a
    // multiplication that itself rounds.
    let away = |x: f64| (x * 1000.0).round() / 1000.0;
    for (x, want) in [
        (0.0625, 0.062),
        (0.3125, 0.312),
        (2.0625, 2.062),
        (1.0005, 1.0),
    ] {
        assert_eq!(round_digits(x, 3), want, "round({x}, 3)");
        assert_ne!(away(x), want, "half away from zero matches at {x}");
    }
}

#[test]
fn round_reads_the_binary_value_not_the_scaled_product_or_the_repr() {
    // round_ties_even(x * 1000) / 1000 sees 0.0025 * 1000 == 2.5 and goes
    // to even; the binary 0.0025 is above the half, and so is 0.0005.
    let scaled_even = |x: f64| (x * 1000.0).round_ties_even() / 1000.0;
    for (x, want) in [(0.0025, 0.003), (0.0005, 0.001)] {
        assert_eq!(round_digits(x, 3), want);
        assert_ne!(scaled_even(x), want);
    }
    // A decimal half-even on the repr gives 0.002 for 0.0025 too.
    assert_eq!(round_digits(0.0015, 3), 0.002);
    assert_eq!(round_digits(-0.0005, 3), -0.001);
    let z = round_digits(-0.0001, 3);
    assert!(
        z == 0.0 && z.is_sign_negative(),
        "a negative rounds to -0.0"
    );
}

#[test]
fn round4_keeps_its_answers() {
    assert_eq!(round4(0.03125), 0.0312);
    assert_eq!(round4(0.09375), 0.0938);
    assert_eq!(round4(1.00005), 1.0001);
    assert_eq!(round4(6.238051680925874), 6.2381);
    assert_eq!(round_digits(0.03125, 4), round4(0.03125));
}

#[test]
fn override_rate_to_dict_rounds_to_three_places() {
    let m = MarkerStats {
        marker: cps("m"),
        fire_count: 15,
        override_count: 1,
    };
    assert_eq!(m.override_rate(), 0.0625);
    let PyValue::Dict(ref items) = m.to_dict() else {
        panic!("a dict")
    };
    assert!(matches!(items[3].1, PyValue::Float(f) if f == 0.062));
}

#[test]
fn override_rate_total_does_not_wrap() {
    // fire + override at i64::MAX each wraps to -2 in i64, and a total
    // that is not positive answers 0.0.
    let m = MarkerStats {
        marker: cps("m"),
        fire_count: i64::MAX,
        override_count: i64::MAX,
    };
    assert_eq!(m.total(), 2 * i128::from(i64::MAX));
    assert_eq!(m.override_rate(), 0.5);
    let negative = MarkerStats {
        marker: cps("m"),
        fire_count: -5,
        override_count: 1,
    };
    assert_eq!(negative.override_rate(), 0.0);
}

#[test]
fn auto_is_the_bool_true_only() {
    for v in [
        PyValue::int(1),
        PyValue::Float(1.0),
        PyValue::str("true"),
        PyValue::str("True"),
        PyValue::List(vec![PyValue::Bool(true)]),
    ] {
        let e = ev(vec![
            ("kind", PyValue::str("use")),
            ("outcome", PyValue::str("applied")),
            ("auto", v),
        ]);
        assert_eq!(hm::applied_tier(&e), Ok(AppliedTier::Model));
        assert_eq!(hm::endorsement_tier(&e), Ok(Some(AppliedTier::Model)));
    }
    let e = ev(vec![
        ("auto", PyValue::Bool(true)),
        ("attribution", PyValue::str("hook")),
    ]);
    assert_eq!(hm::applied_tier(&e), Ok(AppliedTier::Auto));
}

#[test]
fn equality_is_against_a_str_only() {
    // An int, a bool, a path or a datetime never equals the literal, even
    // where its text would.
    for v in [
        PyValue::Path(cps("stop_hook")),
        PyValue::int(1),
        PyValue::Bool(true),
        PyValue::List(vec![PyValue::str("stop_hook")]),
    ] {
        let e = ev(vec![
            ("kind", PyValue::str("turn_audited")),
            ("triggered_from", v),
        ]);
        assert_eq!(hm::is_hook_telemetry_event(&e), Ok(false));
    }
    let e = ev(vec![
        ("kind", PyValue::Path(cps("use"))),
        ("attribution", PyValue::str("hook")),
    ]);
    assert_eq!(hm::is_hook_telemetry_event(&e), Ok(false));
    // A use whose attribution is hook is telemetry; the same with "Hook"
    // or a trailing space is not.
    let hook = |a: &str| {
        ev(vec![
            ("kind", PyValue::str("use")),
            ("attribution", PyValue::str(a)),
        ])
    };
    assert_eq!(hm::is_hook_telemetry_event(&hook("hook")), Ok(true));
    assert_eq!(hm::is_hook_telemetry_event(&hook("Hook")), Ok(false));
    assert_eq!(hm::is_hook_telemetry_event(&hook("hook ")), Ok(false));
}

#[test]
fn a_repeated_key_reads_its_last_value_and_a_near_key_none() {
    // A dict built with a key set twice holds the later value. Read first
    // to last, this event is a turn_audited from the stop hook (True), an
    // auto use (auto) and an ignored one (None).
    let e = ev(vec![
        ("kind", PyValue::str("turn_audited")),
        ("triggered_from", PyValue::str("stop_hook")),
        ("attribution", PyValue::str("hook")),
        ("auto", PyValue::Bool(true)),
        ("outcome", PyValue::str("ignored")),
        ("kind", PyValue::str("use")),
        ("triggered_from", PyValue::str("x")),
        ("attribution", PyValue::str("model")),
        ("auto", PyValue::Bool(false)),
        ("outcome", PyValue::str("applied")),
    ]);
    assert_eq!(hm::is_hook_telemetry_event(&e), Ok(false));
    assert_eq!(hm::applied_tier(&e), Ok(AppliedTier::Model));
    assert_eq!(hm::endorsement_tier(&e), Ok(Some(AppliedTier::Model)));
    // Without `auto` the read meets every pair, the earlier values too,
    // and still keeps each key's last.
    let PyValue::Dict(items) = &e else {
        panic!("a dict")
    };
    let e = PyValue::Dict(
        items
            .iter()
            .filter(|(k, _)| *k != PyValue::str("auto"))
            .cloned()
            .collect(),
    );
    assert_eq!(hm::is_hook_telemetry_event(&e), Ok(false));
    assert_eq!(hm::applied_tier(&e), Ok(AppliedTier::Model));
    assert_eq!(hm::endorsement_tier(&e), Ok(Some(AppliedTier::Model)));
    // A key one code point short or long, or a path spelled as the key,
    // is another key: `ev.get("attribution")` is "model" here.
    let mut items = vec![
        (PyValue::str("kind"), PyValue::str("use")),
        (PyValue::str("outcome"), PyValue::str("applied")),
        (PyValue::str("attribution"), PyValue::str("model")),
    ];
    for k in ["attributio", "attributions", "kin", "kinds"] {
        items.push((PyValue::str(k), PyValue::str("hook")));
    }
    items.push((PyValue::Path(cps("attribution")), PyValue::str("hook")));
    let e = PyValue::Dict(items);
    assert_eq!(hm::is_hook_telemetry_event(&e), Ok(false));
    assert_eq!(hm::applied_tier(&e), Ok(AppliedTier::Model));
    assert_eq!(hm::endorsement_tier(&e), Ok(Some(AppliedTier::Model)));
}

#[test]
fn a_value_that_is_not_a_dict_has_no_get() {
    for v in [PyValue::None, PyValue::str("use"), PyValue::List(vec![])] {
        assert_eq!(
            hm::is_hook_telemetry_event(&v),
            Err(HealthModelError::AttributeError)
        );
        assert_eq!(hm::applied_tier(&v), Err(HealthModelError::AttributeError));
        assert_eq!(
            hm::endorsement_tier(&v),
            Err(HealthModelError::AttributeError)
        );
    }
}

fn base() -> DeadWeight {
    DeadWeight {
        category: None,
        freshest_ts: 100.0,
        retrieval_count: 1,
        applied_count: 0,
        has_unresolved_contradiction: false,
        earliest_retrieval_ts: None,
        cutoff_ts: 200.0,
        grace_cutoff_ts: 300.0,
    }
}

#[test]
fn each_dead_weight_gate_alone_and_at_its_bound() {
    assert!(base().is_dead_weight());
    let not = |f: &dyn Fn(&mut DeadWeight)| {
        let mut d = base();
        f(&mut d);
        d.is_dead_weight()
    };
    assert!(!not(&|d| d.category = Some(Category::Ambient)));
    assert!(not(&|d| d.category = Some(Category::Fact)));
    assert!(not(&|d| d.category = Some(Category::UserInference)));
    // The window's bound excludes; one unit below it does not.
    assert!(!not(&|d| d.freshest_ts = 200.0));
    assert!(not(
        &|d| d.freshest_ts = f64::from_bits(200f64.to_bits() - 1)
    ));
    assert!(!not(&|d| d.retrieval_count = 0));
    assert!(not(&|d| d.retrieval_count = -1));
    assert!(!not(&|d| d.applied_count = 1));
    // `applied_count > 0`, not `!= 0`.
    assert!(not(&|d| d.applied_count = -1));
    assert!(!not(&|d| d.has_unresolved_contradiction = true));
    // The grace's bound excludes; one unit below it does not; None counts
    // as old.
    assert!(!not(&|d| d.earliest_retrieval_ts = Some(300.0)));
    assert!(not(
        &|d| d.earliest_retrieval_ts = Some(f64::from_bits(300f64.to_bits() - 1))
    ));
    // A NaN compares false: it excludes nothing.
    assert!(not(&|d| d.freshest_ts = f64::NAN));
    assert!(not(&|d| d.earliest_retrieval_ts = Some(f64::NAN)));
}

#[test]
fn timestamp_divides_the_microseconds_exactly() {
    let d = utc(5530, 3, 3, 8, 26, 42, 691_225);
    let want = 112348024002.69122;
    assert_eq!(hm::timestamp(&d), Ok(want));
    let us: i64 = 112_348_024_002_691_225;
    assert_ne!(us as f64 / 1e6, want, "the f64 micros route rounds twice");
    assert_eq!(
        hm::freshest_touch_ts(&utc(1, 1, 1, 0, 0, 0, 0), &d, None, None),
        Ok(want)
    );
}

#[test]
fn naive_datetimes_are_declined_or_refused() {
    let n = naive(d1());
    assert_eq!(hm::timestamp(&n), Err(HealthModelError::NaiveTimestamp));
    assert_eq!(
        hm::freshest_touch_ts(&d1(), &d1(), Some(&n), None),
        Err(HealthModelError::NaiveTimestamp)
    );
    // Naive against aware is TypeError at whichever comparison meets it.
    assert_eq!(
        hm::has_unresolved_contradiction(Some(&n), &d1(), None),
        Err(HealthModelError::TypeError)
    );
    assert_eq!(
        hm::has_unresolved_contradiction(Some(&d1()), &d1(), Some(&n)),
        Err(HealthModelError::TypeError)
    );
    // No contradiction: nothing is compared.
    assert_eq!(
        hm::has_unresolved_contradiction(None, &n, Some(&d1())),
        Ok(false)
    );
    // Naive against naive orders by the wall clock.
    let later = naive(utc(2026, 3, 1, 12, 0, 1, 0));
    assert_eq!(
        hm::has_unresolved_contradiction(Some(&later), &n, None),
        Ok(true)
    );
    let mut m = memory();
    m.last_contradicted_at = Some(n);
    assert_eq!(m.to_dict(), Err(HealthModelError::TypeError));
}

#[test]
fn aware_datetimes_compare_as_instants() {
    // One instant at +23:59 and -23:59: the wall clocks differ by almost
    // two days, the instants not at all.
    let east = at(1439, d1());
    let west = at(-1439, d1());
    assert_eq!(
        hm::has_unresolved_contradiction(Some(&east), &west, None),
        Ok(false)
    );
    assert_eq!(
        hm::has_unresolved_contradiction(Some(&west), &east, None),
        Ok(false)
    );
    let a_bit_later = west.minus_micros(-1).expect("in range");
    assert_eq!(
        hm::has_unresolved_contradiction(Some(&a_bit_later), &east, None),
        Ok(true)
    );
}

#[test]
fn ratios_divide_the_integers_exactly() {
    let a: i64 = (1 << 53) + 1;
    let e: i64 = (1 << 53) + 3;
    let want = 1.0000000000000002;
    assert_eq!(hm::explicit_ratio(a, e), Some(want));
    assert_ne!(e as f64 / a as f64, want, "f64 division of the counts");
    assert_eq!(hm::explicit_ratio(0, 5), None);
    assert_eq!(hm::explicit_ratio(-1, 1), None);
}

#[test]
fn model_endorsement_ratio_is_none_only_at_zero() {
    let mut m = memory();
    assert_eq!(m.model_endorsement_ratio(), None);
    m.applied_count = -3;
    m.model_applied_count = 0;
    // 0 / -3 is -0.0 in Python; explicit_ratio's `<= 0` would say None.
    let r = m.model_endorsement_ratio().expect("a ratio");
    assert!(r == 0.0 && r.is_sign_negative());
    assert_eq!(m.endorsement_ratio(), None);
    m.model_applied_count = 1;
    assert_eq!(m.model_endorsement_ratio(), Some(-1.0 / 3.0));
}

#[test]
fn to_dict_keeps_the_reference_key_order() {
    let PyValue::Dict(ref items) = memory().to_dict().expect("a dict") else {
        panic!("a dict")
    };
    let keys: Vec<String> = items
        .iter()
        .map(|(k, _)| match k {
            PyValue::Str(s) => s.iter().map(|&c| char::from_u32(c).unwrap()).collect(),
            _ => panic!("a str key"),
        })
        .collect();
    assert_eq!(
        keys,
        [
            "id",
            "scopes",
            "summary",
            "created",
            "updated",
            "retrieval_count",
            "show_count",
            "applied_count",
            "auto_applied_count",
            "explicit_applied_count",
            "hook_applied_count",
            "model_applied_count",
            "endorsement_ratio",
            "model_endorsement_ratio",
            "ignored_count",
            "contradicted_count",
            "corrected_count",
            "last_used_at",
            "last_verified_at",
            "category",
            "has_unresolved_contradiction",
            "resolution_timeline",
        ]
    );
}

#[test]
fn iso_replaces_every_utc_offset_spelling() {
    assert_eq!(hm::iso(None), None);
    assert_eq!(
        hm::iso(Some(&d1())).as_deref(),
        Some("2026-03-01T12:00:00Z")
    );
    let thirty = DateTime::new(2026, 3, 1, 12, 0, 0, 0, Some(30_000_000)).expect("dt");
    assert_eq!(
        hm::iso(Some(&thirty)).as_deref(),
        Some("2026-03-01T12:00:00Z:30")
    );
}

#[test]
fn hookless_reason_is_the_reference_sentence() {
    assert_eq!(hm::ENDORSEMENT_GRACE_DAYS, 2);
    assert!(hm::HOOKLESS_REASON.starts_with(
        "no Stop-hook settlement telemetry in the event log \u{2014} the only settlement"
    ));
    assert!(hm::HOOKLESS_REASON.ends_with("hooks.json carries the shape), then re-check."));
    assert_eq!(hm::HOOKLESS_REASON.chars().count(), 359);
}
