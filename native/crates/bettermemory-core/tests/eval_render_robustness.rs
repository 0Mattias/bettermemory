//! Hostile input to `eval_render` and `EvalReport`: 100,000 cold rows and
//! `by_model` entries, megabyte ids, summaries and models, every float
//! extreme in each rate field and the ratio, counts at both ends of `i64`,
//! review and surfaces dicts nested 10,000 deep and 100,000 wide; growth
//! linear (`tests/timing`), no panic in debug or release.

mod timing;

use std::hint::black_box;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval::EvalReport;
use bettermemory_core::eval_model::{ColdEndorsementMemoriesRow, RateCI, SilentMissCandidate};
use bettermemory_core::eval_render::{self as er, RenderError};
use bettermemory_core::float_repr::{format_fixed, format_general, PrecisionTooBig, MAX_PRECISION};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

const FLOATS: [f64; 14] = [
    f64::NAN,
    f64::INFINITY,
    f64::NEG_INFINITY,
    f64::MAX,
    -f64::MAX,
    f64::MIN_POSITIVE,
    5e-324,
    -5e-324,
    0.0,
    -0.0,
    1.0,
    0.999_999_999_999_999_9,
    1e300,
    0.125,
];

fn report() -> EvalReport {
    EvalReport::new(DateTime::new(2026, 11, 25, 12, 0, 0, 0, Some(0)).expect("a datetime"))
}

fn row(i: usize, summary: &[u32]) -> ColdEndorsementMemoriesRow {
    ColdEndorsementMemoriesRow {
        id: cps(&format!("m{i}")),
        scopes: vec![cps("s")],
        summary: summary.to_vec(),
        retrieval_count: i as i64,
        auto_applied_count: i64::MAX,
        explicit_applied_count: i64::MAX,
    }
}

fn wide(n: usize) -> EvalReport {
    let mut r = report();
    r.cold_endorsement_ratio_threshold = 0.5;
    r.cold_endorsement_memories_total = n as i64;
    r.cold_endorsement_memories_rows = (0..n).map(|i| row(i, &cps("s"))).collect();
    r.by_model = (0..n)
        .map(|i| {
            (
                cps(&format!("model-{:06}", (i * 7919) % n)),
                vec![(cps("audited"), i as i64)],
            )
        })
        .collect();
    r
}

#[test]
fn wide_reports_grow_linearly() {
    let small = wide(10_000);
    let large = wide(100_000);
    let text = er::render_text(&large).expect("renders");
    assert!(text.len() > 100_000 * 40);
    timing::grows_within(
        "render_text over by_model entries",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(er::render_text(&small))),
        &mut || timing::once(|| black_box(er::render_text(&large))),
    );
    timing::grows_within(
        "to_dict over cold rows and by_model entries",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(small.to_dict())),
        &mut || timing::once(|| black_box(large.to_dict())),
    );
}

fn long_text(n: usize) -> PyStr {
    (0..n as u32)
        .map(|i| [0x41, 0x1b, 0xe9, 0x1F600, 0xD800, 0x301][(i % 6) as usize])
        .collect()
}

fn long_report(n: usize) -> EvalReport {
    let text = long_text(n);
    let mut r = report();
    r.scope_filter = Some(text.clone());
    r.threshold_rule = text.clone();
    r.cold_endorsement_memories_rows = vec![ColdEndorsementMemoriesRow {
        id: text.clone(),
        scopes: vec![text.clone(), text.clone()],
        summary: text.clone(),
        retrieval_count: 1,
        auto_applied_count: 0,
        explicit_applied_count: 0,
    }];
    r.by_model = vec![(text.clone(), vec![(text.clone(), 1)])];
    r.silent_miss_recent = vec![SilentMissCandidate {
        ts: text.clone(),
        session_id: text.clone(),
        top_missed_id: Some(text.clone()),
        top_missed_relevance: Some(text.clone()),
        threshold_rule: None,
        recent_retrieval_count: "1".into(),
    }];
    r
}

#[test]
fn megabyte_texts_grow_linearly() {
    let small = long_report(100_000);
    let large = long_report(1_000_000);
    let text = er::render_text(&large).expect("renders");
    // Every unprintable code point is escaped: nothing raw reaches the text.
    assert!(!text
        .iter()
        .any(|&c| c == 0x1b || (0xD800..0xE000).contains(&c)));
    timing::grows_within(
        "render_text over megabyte texts",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(er::render_text(&small))),
        &mut || timing::once(|| black_box(er::render_text(&large))),
    );
    let small = long_text(100_000);
    let large = long_text(1_000_000);
    assert_eq!(er::truncate(&large, 50).len(), 50);
    assert_eq!(er::short_ts(&large), large);
    timing::grows_within(
        "_truncate and _short_ts over a megabyte",
        timing::TENFOLD,
        &mut || timing::once(|| black_box((er::truncate(&small, 0), er::short_ts(&small)))),
        &mut || timing::once(|| black_box((er::truncate(&large, 0), er::short_ts(&large)))),
    );
}

#[test]
fn every_float_extreme() {
    for &a in &FLOATS {
        assert!(!format_general(a).is_empty());
        assert!(!format_fixed(a, 2).unwrap_or_default().is_empty());
        assert_eq!(er::bar(a).len(), 10);
        for &b in &FLOATS {
            for &c in &FLOATS {
                let rate = RateCI {
                    numerator: i64::MIN,
                    denominator: i64::MAX,
                    rate: Some(a),
                    lower: Some(b),
                    upper: Some(c),
                    torn_read: true,
                };
                let mut r = report();
                r.memory_helped_rate = rate;
                r.endorsement_rate = rate;
                r.silent_miss_rate = rate;
                r.cold_endorsement_ratio_threshold = a;
                r.cold_endorsement_memories_total = i64::MIN;
                r.cold_endorsement_memories_rows = vec![row(0, &cps("s"))];
                r.window_seconds = Some(i64::MIN);
                r.events_in_window = i64::MAX;
                let text = er::render_text(&r).expect("renders");
                assert!(!text.is_empty());
                black_box(r.to_dict());
            }
        }
    }
    // f64::MAX in full: 309 digits and two places.
    assert_eq!(format_fixed(f64::MAX, 2).map(|t| t.len()), Ok(312));
    for s in [i64::MIN, i64::MIN + 1, -1, 0, 1, i64::MAX] {
        black_box(er::humanize_seconds(s));
    }
}

/// The float extremes cost what an ordinary value does: the least
/// subnormal, the least normal and the greatest double through `'g'`, and
/// the two least through `'0.2f'`, 20,000 calls each against 20,000 of
/// 0.5 (a port that wrote out the full expansion took some 200 times as
/// long). `'0.2f'` of the greatest double writes its 309 digits, and
/// CPython 3.13.13 takes 150 times as long for it as for 0.5 (13 us a
/// call), so it is not held to this bound.
#[test]
fn float_extremes_cost_as_ordinary_values() {
    const CALLS: usize = 20_000;
    let general = |v: f64| {
        timing::once(|| {
            for _ in 0..CALLS {
                black_box(format_general(black_box(v)));
            }
        })
    };
    let fixed = |v: f64| {
        timing::once(|| {
            for _ in 0..CALLS {
                black_box(format_fixed(black_box(v), 2)).ok();
            }
        })
    };
    for extreme in [5e-324, f64::MIN_POSITIVE, f64::MAX] {
        timing::grows_within(
            &format!("format_general({extreme:e}) against 0.5"),
            10,
            &mut || general(0.5),
            &mut || general(extreme),
        );
    }
    for extreme in [5e-324, f64::MIN_POSITIVE] {
        timing::grows_within(
            &format!("format_fixed({extreme:e}, 2) against 0.5"),
            10,
            &mut || fixed(0.5),
            &mut || fixed(extreme),
        );
    }
}

/// A precision Python refuses (past `INT_MAX`, "precision too big") is
/// refused, never a panic or an allocation; one past 1,074 places writes
/// zeros.
#[test]
fn hostile_precisions_are_refused() {
    for precision in [
        usize::MAX,
        usize::MAX - 5,
        usize::MAX - 400,
        MAX_PRECISION + 1,
        1 << 40,
    ] {
        for value in [1.0, 1e300, f64::NAN, f64::INFINITY] {
            assert_eq!(format_fixed(value, precision), Err(PrecisionTooBig));
        }
    }
    let text = format_fixed(1e300, 1_000_000).expect("within INT_MAX");
    assert_eq!(text.len(), 301 + 1 + 1_000_000);
    assert!(text.ends_with(&"0".repeat(1_000_000)));
}

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn nested(depth: usize, leaf: PyValue) -> PyValue {
    (0..depth).fold(leaf, |v, i| {
        if i % 2 == 0 {
            PyValue::List(vec![v])
        } else {
            PyValue::Dict(vec![(s("k"), v)])
        }
    })
}

fn chains(issued: PyValue) -> PyValue {
    PyValue::Dict(vec![(
        s("chains"),
        PyValue::Dict(vec![
            (s("issued"), issued),
            (s("answered"), PyValue::int(0)),
            (s("unanswered"), PyValue::int(0)),
            (s("pending"), PyValue::int(0)),
            (s("closed_by_call"), PyValue::int(0)),
        ]),
    )])
}

#[test]
fn deep_dicts_raise_without_panic() {
    // A count nested 10,000 deep: `str()` of it meets the recursion limit.
    let review = chains(nested(10_000, PyValue::int(1)));
    assert_eq!(
        er::review_text_lines(Some(&review)),
        Err(RenderError::RecursionError)
    );
    // Nested 100 deep it spells, then the next key is missing.
    let review = chains(nested(100, PyValue::int(1)));
    assert_eq!(
        er::review_text_lines(Some(&review)),
        Err(RenderError::KeyError)
    );
    // The surfaces' rules nested 10,001 deep, a list outermost: its one
    // element is a dict, not a str.
    let surfaces = PyValue::Dict(vec![(s("rules"), nested(10_001, s("r")))]);
    assert_eq!(
        er::surface_text_lines(Some(&surfaces)),
        Err(RenderError::TypeError)
    );
    // Nested 10,000 deep, a dict outermost: `join` reads its one key, and
    // the next key of the section is missing.
    let surfaces = PyValue::Dict(vec![(s("rules"), nested(10_000, s("r")))]);
    assert_eq!(
        er::surface_text_lines(Some(&surfaces)),
        Err(RenderError::KeyError)
    );
    // A rate dict nested 10,000 deep in place of a tally.
    let surfaces = PyValue::Dict(vec![
        (s("rules"), PyValue::List(vec![])),
        (s("brief"), nested(10_000, PyValue::None)),
    ]);
    assert_eq!(
        er::surface_text_lines(Some(&surfaces)),
        Err(RenderError::KeyError)
    );
    // A rate whose numerator is nested 10,000 deep, in the n/a branch.
    let rate = PyValue::Dict(vec![
        (s("numerator"), nested(10_000, PyValue::int(1))),
        (s("denominator"), PyValue::int(0)),
        (s("rate"), PyValue::None),
        (s("ci95_lower"), PyValue::None),
        (s("ci95_upper"), PyValue::None),
        (s("torn_read"), PyValue::Bool(false)),
    ]);
    let fields = er::rate_from_dict(&rate).expect("six keys");
    assert_eq!(
        er::format_rate_fields(&cps("x"), &fields),
        Err(RenderError::RecursionError)
    );
    // A report carrying them renders the error and dumps without a panic.
    let mut r = report();
    r.review = Some(chains(nested(10_000, PyValue::int(1))));
    r.surfaces = Some(nested(10_000, PyValue::None));
    assert_eq!(er::render_text(&r), Err(RenderError::RecursionError));
    black_box(r.to_dict());
}

fn chain(n: usize) -> PyValue {
    (1..n).fold(PyValue::List(vec![]), |v, _| PyValue::List(vec![v]))
}

/// The depth bound against the int limit, in the order `repr` writes: a
/// count `[<n nested lists>, 10**4300]` raises RecursionError when the
/// innermost list is the 9,999th object spelled and ValueError when the
/// walk reaches the int. CPython 3.13.13 through `_review_text_lines`:
/// RecursionError at n = 9,998, ValueError at 9,997; lists alone 9,998
/// deep spell and 9,999 deep raise. The bound follows the release, so it
/// is pinned here and not in a golden file.
#[test]
fn depth_bound_meets_the_int_limit_in_repr_order() {
    let big = PyValue::Int(format!("1{}", "0".repeat(4300)));
    let pair = |n: usize| PyValue::List(vec![chain(n), big.clone()]);
    let answer = |issued: PyValue| er::review_text_lines(Some(&chains(issued)));
    assert_eq!(answer(pair(9_998)), Err(RenderError::RecursionError));
    assert_eq!(answer(pair(9_997)), Err(RenderError::ValueError));
    assert_eq!(answer(chain(9_999)), Err(RenderError::RecursionError));
    // 9,998 lists spell; the review's next key is missing.
    assert_eq!(answer(chain(9_998)), Err(RenderError::KeyError));
    // An int of 4,300 digits spells; one more digit is ValueError.
    let at_limit = PyValue::Int("9".repeat(4300));
    assert_eq!(
        answer(PyValue::List(vec![at_limit])),
        Err(RenderError::KeyError)
    );
    assert_eq!(
        answer(PyValue::Int(format!("-{}", "9".repeat(4300)))),
        Err(RenderError::KeyError)
    );
    assert_eq!(
        answer(PyValue::Int("9".repeat(4301))),
        Err(RenderError::ValueError)
    );
}

#[test]
fn wide_dicts_grow_linearly() {
    let actions = |n: usize| {
        let review = chains(PyValue::int(1));
        let PyValue::Dict(top) = &review else {
            unreachable!()
        };
        let mut top = top.to_vec();
        let wide: Vec<(PyValue, PyValue)> = (0..n)
            .map(|i| (s(&format!("a{i}")), PyValue::int(i as i128)))
            .collect();
        top.push((s("pre_answered"), PyValue::int(0)));
        top.push((
            s("pre_answered_zero"),
            PyValue::Dict(vec![(s("total"), PyValue::int(0))]),
        ));
        top.push((s("response_chars_p50"), PyValue::None));
        top.push((s("calls_per_committed_write"), PyValue::None));
        top.push((s("actions"), PyValue::Dict(wide.clone())));
        top.push((s("filings_per_100_committed"), PyValue::Dict(wide.clone())));
        top.push((s("by_client"), PyValue::Dict(vec![])));
        top.push((s("grades"), PyValue::Dict(vec![(s("n"), PyValue::int(0))])));
        PyValue::Dict(top)
    };
    let small = actions(10_000);
    let large = actions(100_000);
    let lines = er::review_text_lines(Some(&large)).expect("renders");
    assert!(lines[4].len() > 100_000 * 4);
    timing::grows_within(
        "_review_text_lines over 100,000 actions",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(er::review_text_lines(Some(&small)))),
        &mut || timing::once(|| black_box(er::review_text_lines(Some(&large)))),
    );
}
