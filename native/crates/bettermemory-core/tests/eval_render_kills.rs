//! Cases a plausible wrong port of `eval_render` fails, each with the
//! reference's answer (CPython 3.13.13): `round` as `f64::round` in
//! `_bar`, Rust's `NaN`, `{}` for `'g'`, a decimal-text rounding for
//! `'0.2f'`, padding by bytes or display width, `by_model` sorted by
//! UTF-16 units or by the escaped text, `or` read as is-None, the share
//! printed for a NaN ratio, the review and surfaces dicts read with `.get`
//! where the reference subscripts, and `surfaces` tested for truthiness
//! where the reference tests `is None`.

use std::hint::black_box;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval::EvalReport;
use bettermemory_core::eval_model::{ColdEndorsementMemoriesRow, RateCI, SilentMissCandidate};
use bettermemory_core::eval_render::{self as er, RenderError};
use bettermemory_core::float_repr::{format_fixed, format_general};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::{cps, to_string};

fn report() -> EvalReport {
    EvalReport::new(DateTime::new(2026, 11, 25, 12, 0, 0, 0, Some(0)).expect("a datetime"))
}

fn render(r: &EvalReport) -> String {
    to_string(&er::render_text(r).expect("renders")).expect("UTF-8")
}

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn int(n: i64) -> PyValue {
    PyValue::int(n.into())
}

fn d(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect())
}

fn lines(v: Vec<PyStr>) -> Vec<String> {
    v.iter().map(|l| to_string(l).expect("UTF-8")).collect()
}

#[test]
fn bar_rounds_half_to_even() {
    // round(2.5) is 2 and round(4.5) is 4; f64::round gives 3 and 5.
    assert_eq!(to_string(&er::bar(0.25)).unwrap(), "▇▇▁▁▁▁▁▁▁▁");
    assert_eq!(to_string(&er::bar(0.45)).unwrap(), "▇▇▇▇▁▁▁▁▁▁");
    assert_eq!(to_string(&er::bar(0.35)).unwrap(), "▇▇▇▇▁▁▁▁▁▁");
    // min(1.0, nan) is 1.0: a full bar, where clamp(0, 1) keeps NaN.
    assert_eq!(to_string(&er::bar(f64::NAN)).unwrap(), "▇".repeat(10));
    assert_eq!(to_string(&er::bar(-1.0)).unwrap(), "▁".repeat(10));
}

/// `min(1.0, nan)` keeps 1.0 for every NaN, a signalling one too
/// (CPython 3.13.13's `_bar` of the bits through `struct.unpack`): a full
/// bar. An optimised arm64 build that folds the clamp into `fminnm`
/// passes a signalling NaN through to an empty bar; `black_box` keeps the
/// value from being folded at compile time, and this file runs in release
/// under the full battery.
#[test]
fn bar_of_a_signalling_nan_is_full() {
    let full = "▇".repeat(10);
    for bits in [
        0x7ff0_0000_0000_0001_u64,
        0xfff0_0000_0000_0001,
        0x7ff4_0000_0000_0000,
        0x7ff7_ffff_ffff_ffff,
        0x7ff8_0000_0000_0000,
        0xfff8_0000_0000_0000,
    ] {
        let rate = black_box(f64::from_bits(bits));
        assert_eq!(to_string(&er::bar(rate)).unwrap(), full, "{bits:016x}");
        let line = er::format_rate(
            &cps("x"),
            &RateCI {
                numerator: 1,
                denominator: 1,
                rate: Some(rate),
                lower: Some(0.0),
                upper: Some(1.0),
                torn_read: false,
            },
        );
        assert!(
            to_string(&line)
                .unwrap()
                .ends_with(&format!("nan [0.00, 1.00]   {full}   (k=1, n=1)")),
            "{bits:016x}"
        );
    }
}

#[test]
fn floats_spell_as_python() {
    assert_eq!(format_fixed(f64::NAN, 2).as_deref(), Ok("nan"));
    assert_eq!(format_fixed(f64::NEG_INFINITY, 2).as_deref(), Ok("-inf"));
    assert_eq!(format_fixed(-0.001, 2).as_deref(), Ok("-0.00"));
    // The binary value decides: 0.125 is a tie (even: 0.12), 2.675 is
    // below its text (2.67), 0.005 above (0.01), 0.015 below (0.01).
    assert_eq!(format_fixed(0.125, 2).as_deref(), Ok("0.12"));
    assert_eq!(format_fixed(2.675, 2).as_deref(), Ok("2.67"));
    assert_eq!(format_fixed(0.005, 2).as_deref(), Ok("0.01"));
    assert_eq!(format_fixed(0.015, 2).as_deref(), Ok("0.01"));
    assert_eq!(format_general(1e-05), "1e-05");
    assert_eq!(format_general(1234567.0), "1.23457e+06");
    assert_eq!(format_general(0.1 + 0.2), "0.3");
    assert_eq!(format_general(-0.0), "-0");
    assert_eq!(format_general(f64::NAN), "nan");
    let mut r = report();
    r.memory_helped_rate = RateCI {
        numerator: 1,
        denominator: 1,
        rate: Some(f64::NAN),
        lower: Some(f64::INFINITY),
        upper: Some(0.125),
        torn_read: false,
    };
    assert!(render(&r).contains("memory_helped_rate   nan [inf, 0.12]   ▇▇▇▇▇▇▇▇▇▇"));
}

fn cold(r: &mut EvalReport, ratio: f64) {
    r.cold_endorsement_ratio_threshold = ratio;
    r.cold_endorsement_memories_total = 1;
    r.cold_endorsement_memories_rows
        .push(ColdEndorsementMemoriesRow {
            id: cps("m1"),
            scopes: vec![],
            summary: cps("s"),
            retrieval_count: 40,
            auto_applied_count: 3,
            explicit_applied_count: 1,
        });
}

#[test]
fn ratio_rule_and_share() {
    let mut r = report();
    cold(&mut r, 1e-05);
    let text = render(&r);
    assert!(
        text.contains("(retrievals ≥ 30, explicit share < 1e-05): 1"),
        "{text}"
    );
    assert!(text.contains("(40 retrievals, explicit 1/4)"), "{text}");
    // A NaN ratio is not > 0: the strict rule, no share.
    let mut r = report();
    cold(&mut r, f64::NAN);
    let text = render(&r);
    assert!(text.contains("0 explicit applied"), "{text}");
    assert!(text.contains("(40 retrievals)"), "{text}");
    // No scopes: the dash.
    assert!(text.contains("  m1  —                   \"s\""), "{text}");
}

#[test]
fn padding_counts_code_points() {
    let mut r = report();
    r.by_model = vec![
        (cps("é"), vec![(cps("audited"), 1)]),
        (cps("記憶"), vec![(cps("audited"), 2)]),
        (cps("e\u{301}"), vec![]),
    ];
    let text = render(&r);
    // 27 spaces after one code point, 26 after two (whatever the bytes or
    // the columns).
    assert!(text.contains(&format!(
        "  é{} audited=1     misses=0    no_signal=0",
        " ".repeat(27)
    )));
    assert!(text.contains(&format!("  記憶{} audited=2     misses=0", " ".repeat(26))));
    assert!(text.contains(&format!("  e\u{301}{} audited=0", " ".repeat(26))));
    let label = er::format_rate(&cps("ééé"), &RateCI::from_counts(0, 0));
    assert_eq!(
        to_string(&label).unwrap(),
        format!("ééé{} n/a    (k=0, n=0)", " ".repeat(17))
    );
}

#[test]
fn by_model_sorts_by_code_point() {
    let mut r = report();
    // UTF-16 puts the astral character (D83D) before U+FF5E; code points
    // put it after. The escaped `\x1b` sorts after `A`; the raw one before.
    r.by_model = vec![
        (cps("\u{1F600}"), vec![]),
        (cps("\u{FF5E}"), vec![]),
        (cps("A"), vec![]),
        (cps("\x1b"), vec![]),
        (cps("a"), vec![]),
    ];
    let text = render(&r);
    let order: Vec<usize> = ["\\x1b", "  A ", "  a ", "\u{FF5E}", "\u{1F600}"]
        .iter()
        .map(|m| text.find(m).expect("a model line"))
        .collect();
    assert!(order.windows(2).all(|w| w[0] < w[1]), "{text}");
}

#[test]
fn empty_ids_read_as_missing() {
    let mut r = report();
    r.silent_miss_recent.push(SilentMissCandidate {
        ts: cps("2026-11-25T12:34:56Z"),
        session_id: cps("0123456789abcdef"),
        top_missed_id: Some(Vec::new()),
        top_missed_relevance: None,
        threshold_rule: None,
        recent_retrieval_count: "0".into(),
    });
    let text = render(&r);
    assert!(
        text.contains("  2026-11-25 12:34  session=0123456789ab…  missed=— relevance=—"),
        "{text}"
    );
}

fn review() -> PyValue {
    d(vec![
        (
            "chains",
            d(vec![
                ("issued", int(1)),
                ("answered", int(1)),
                ("unanswered", int(0)),
                ("pending", int(0)),
                ("closed_by_call", int(0)),
            ]),
        ),
        ("pre_answered", int(0)),
        ("pre_answered_zero", d(vec![("total", int(0))])),
        ("response_chars_p50", PyValue::None),
        ("calls_per_committed_write", PyValue::Float(1.5)),
        ("actions", d(vec![("accept", PyValue::Bool(true))])),
        ("filings_per_100_committed", d(vec![])),
        ("by_client", d(vec![])),
        ("grades", d(vec![("n", int(0))])),
    ])
}

#[test]
fn review_subscripts() {
    let got = lines(er::review_text_lines(Some(&review())).expect("renders"));
    assert_eq!(
        got[2],
        "  issued=1 answered=1 unanswered=0 pending=0 closed_by_call=0"
    );
    assert_eq!(
        got[3],
        "  pre_answered=0 pre_answered_zero=0 response_chars_p50=n/a calls_per_committed_write=1.5"
    );
    assert_eq!(got[4], "  actions accept=True");
    // A missing key raises; `.get` would print None or skip.
    let PyValue::Dict(pairs) = &review() else {
        unreachable!()
    };
    let pairs: Vec<(PyValue, PyValue)> = pairs
        .iter()
        .filter(|(k, _)| *k != s("grades"))
        .cloned()
        .collect();
    let missing = PyValue::Dict(pairs);
    assert_eq!(
        er::review_text_lines(Some(&missing)),
        Err(RenderError::KeyError)
    );
    // `not review`: an empty dict and a zero are silent; a list raises.
    assert_eq!(er::review_text_lines(Some(&d(vec![]))), Ok(vec![]));
    assert_eq!(er::review_text_lines(Some(&int(0))), Ok(vec![]));
    assert_eq!(
        er::review_text_lines(Some(&PyValue::List(vec![int(1)]))),
        Err(RenderError::TypeError)
    );
}

#[test]
fn surfaces_is_none_not_falsy() {
    // `surfaces is None`: an empty dict is read and its keys are missing.
    assert_eq!(er::surface_text_lines(None), Ok(vec![]));
    assert_eq!(er::surface_text_lines(Some(&PyValue::None)), Ok(vec![]));
    assert_eq!(
        er::surface_text_lines(Some(&d(vec![]))),
        Err(RenderError::KeyError)
    );
}

#[test]
fn rule_split_needs_a_dict() {
    // `by_rule` not a dict: no split and no membership delta either.
    let delta = d(vec![
        ("rows", int(1)),
        ("rows_differing", int(0)),
        ("delivered_not_rc3", int(0)),
        ("rc3_not_delivered", int(0)),
    ]);
    let surfaces = d(vec![
        ("by_rule", PyValue::List(vec![])),
        ("membership_delta", delta.clone()),
    ]);
    assert_eq!(er::rule_split_text_lines(&surfaces), Ok(vec![]));
    let surfaces = d(vec![("by_rule", d(vec![])), ("membership_delta", delta)]);
    assert_eq!(
        lines(er::rule_split_text_lines(&surfaces).unwrap()),
        [
            "  By rule:",
            "  membership_delta rows=1 differing=0 delivered_not_rc3=0 rc3_not_delivered=0"
        ]
    );
}

fn rate_dict(rate: PyValue, lower: PyValue) -> PyValue {
    d(vec![
        ("numerator", int(1)),
        ("denominator", int(2)),
        ("rate", rate),
        ("ci95_lower", lower),
        ("ci95_upper", PyValue::Float(0.5)),
        ("torn_read", PyValue::Bool(false)),
    ])
}

fn rate_of(v: &PyValue) -> Result<String, RenderError> {
    er::rate_from_dict(v)
        .and_then(|f| er::format_rate_fields(&cps("x"), &f))
        .map(|t| to_string(&t).unwrap())
}

#[test]
fn rate_dict_values() {
    // `_bar` runs first: a str rate is TypeError (a comparison), a str
    // bound is ValueError (the format code).
    assert_eq!(
        rate_of(&rate_dict(s("0.5"), PyValue::Float(0.1))),
        Err(RenderError::TypeError)
    );
    assert_eq!(
        rate_of(&rate_dict(PyValue::Float(0.5), s("0.1"))),
        Err(RenderError::ValueError)
    );
    // An int and a bool format through float(); an int compares exactly.
    assert_eq!(
        rate_of(&rate_dict(int(5), PyValue::Bool(true))).unwrap(),
        "x                    5.00 [1.00, 0.50]   ▇▇▇▇▇▇▇▇▇▇   (k=1, n=2)"
    );
    assert_eq!(
        rate_of(&rate_dict(int(-5), int(0))).unwrap(),
        "x                    -5.00 [0.00, 0.50]   ▁▁▁▁▁▁▁▁▁▁   (k=1, n=2)"
    );
    let huge = PyValue::int_from_decimal(&format!("1{}", "0".repeat(400))).unwrap();
    assert_eq!(
        rate_of(&rate_dict(huge, int(0))),
        Err(RenderError::OverflowError)
    );
    // A datetime's format is its strftime: the spec itself.
    let dt = PyValue::DateTime(DateTime::new(2026, 1, 1, 0, 0, 0, 0, None).unwrap());
    assert_eq!(
        rate_of(&rate_dict(PyValue::Float(0.5), dt)).unwrap(),
        "x                    0.50 [0.2f, 0.50]   ▇▇▇▇▇▁▁▁▁▁   (k=1, n=2)"
    );
    // torn_read is read though not printed.
    let PyValue::Dict(pairs) = &rate_dict(PyValue::Float(0.5), PyValue::Float(0.1)) else {
        unreachable!()
    };
    let pairs = pairs[..pairs.len() - 1].to_vec();
    assert_eq!(rate_of(&PyValue::Dict(pairs)), Err(RenderError::KeyError));
}

#[test]
fn humanize_floors() {
    assert_eq!(er::humanize_seconds(-86400), "-1d");
    assert_eq!(er::humanize_seconds(-90), "-90s");
    assert_eq!(er::humanize_seconds(-120), "-2m");
    assert_eq!(er::humanize_seconds(0), "0d");
    assert_eq!(er::humanize_seconds(5400), "90m");
}

#[test]
fn texts_cut_by_code_point() {
    let long: PyStr = std::iter::repeat_n(0x1F600, 51).collect();
    let cut = er::truncate(&long, 50);
    assert_eq!(cut.len(), 50);
    assert_eq!(cut[49], u32::from('…'));
    assert_eq!(er::truncate(&cps("abc"), 0), cps("ab…"));
    assert_eq!(
        er::short_ts(&cps("ééééééééééTabcdefg")),
        cps("éééééééééé abcde")
    );
    assert_eq!(
        er::short_ts(&cps("2026-11-25T12:3")),
        cps("2026-11-25T12:3")
    );
}

#[test]
fn to_dict_surfaces_and_reason() {
    let mut r = report();
    let keys = |v: &PyValue| match v {
        PyValue::Dict(pairs) => pairs.iter().map(|(k, _)| k.clone()).collect::<Vec<_>>(),
        _ => panic!("a dict"),
    };
    assert!(!keys(&r.to_dict()).contains(&s("surfaces")));
    r.surfaces = Some(PyValue::None);
    assert!(!keys(&r.to_dict()).contains(&s("surfaces")));
    r.surfaces = Some(d(vec![]));
    assert_eq!(keys(&r.to_dict()).last(), Some(&s("surfaces")));
    r.cold_endorsement_suppressed = true;
    let out = r.to_dict();
    let cold = out.get("cold_endorsement_memories").unwrap();
    assert!(matches!(
        cold.get("suppressed_reason"),
        Some(PyValue::Str(_))
    ));
}
