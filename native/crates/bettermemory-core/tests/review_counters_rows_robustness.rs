//! Hostile rows through `review_counters`' readers: 100,000 ids, relation
//! dicts of 100,000 pairs, actor blocks nested 10,000 deep (read one
//! level only), scores at every float extreme; no panic, and time linear
//! in the row's size.

mod timing;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::review_counters::{
    bucket_value, is_review_row, score, session_of, CountersError, DecidedRow, GradeRow, IssuedRow,
};
use bettermemory_core::unicode::cps;

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn dict(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect())
}

/// A row with `n` ids in each list, `n` relation pairs (every third key
/// repeated, every fifth value not a `str`) and `n` filings.
fn wide(n: usize) -> PyValue {
    let ids = PyValue::List((0..n).map(|i| s(&format!("id-{i}"))).collect());
    let relation = PyValue::Dict(
        (0..n)
            .map(|i| {
                let key = if i % 3 == 2 {
                    format!("k-{}", i - 2)
                } else {
                    format!("k-{i}")
                };
                let value = if i % 5 == 4 {
                    PyValue::int(i as i128)
                } else {
                    s("unrelated")
                };
                (s(&key), value)
            })
            .collect(),
    );
    let filings = PyValue::List(
        (0..n)
            .map(|i| {
                dict(vec![
                    ("id", s(&format!("t-{i}"))),
                    ("detector", s("review")),
                ])
            })
            .collect(),
    );
    dict(vec![
        ("ts", s("2026-11-21T10:00:00Z")),
        ("kind", s("write")),
        ("review_id", s("w1")),
        ("status", s("committed")),
        (
            "review_answers",
            dict(vec![
                ("relation", relation),
                ("unsafe", PyValue::int(0)),
                ("secret", PyValue::Float(-0.0)),
            ]),
        ),
        ("review_filings", filings),
        ("related", ids.clone()),
        ("roster", ids.clone()),
        ("supersedes_reviewed", ids.clone()),
        ("update_suggested", ids.clone()),
        ("credential_kinds", ids.clone()),
        ("matches", ids),
    ])
}

#[test]
fn rows_of_100000_ids_and_pairs_read_whole_and_grow_linearly() {
    let row = DecidedRow::from_event(&wide(100_000)).unwrap().unwrap();
    assert_eq!(row.matches.len(), 100_000);
    assert_eq!(row.filings.len(), 100_000);
    // Of each three keys the third repeats the first: two kept per three,
    // the repeated key's value the last one.
    let n = 100_000;
    let kept = (0..n)
        .filter(|i| i % 3 != 2)
        .filter(|&i| {
            let last = if i % 3 == 0 && i + 2 < n { i + 2 } else { i };
            last % 5 != 4
        })
        .count();
    assert_eq!(row.relation.len(), kept);
    assert_eq!(row.relation[0], (cps("k-0"), cps("unrelated")));
    assert!(row.zero_attestation());
    let issued = IssuedRow::from_event(&wide(100_000)).unwrap().unwrap();
    assert_eq!(issued.related.len(), 100_000);
    let small = wide(10_000);
    let large = wide(100_000);
    let run =
        |ev: &PyValue| timing::once(|| (DecidedRow::from_event(ev), IssuedRow::from_event(ev)));
    timing::grows_within(
        "review counters rows",
        timing::TENFOLD,
        &mut || run(&small),
        &mut || run(&large),
    );
}

#[test]
fn an_actor_nested_10000_deep_is_read_one_level() {
    let mut actor = dict(vec![("session", s("deepest")), ("client", s("deepest"))]);
    for _ in 0..10_000 {
        actor = dict(vec![("actor", actor)]);
    }
    let ev = dict(vec![
        ("ts", s("2026-11-21T10:00:00Z")),
        ("kind", s("review_issued")),
        ("review_id", s("r1")),
        ("actor", actor),
    ]);
    assert_eq!(session_of(&ev), Ok(None));
    assert_eq!(bucket_value(&ev, "client"), Ok(cps("none")));
    let row = IssuedRow::from_event(&ev).unwrap().unwrap();
    assert_eq!(row.client, cps("none"));
    assert_eq!(is_review_row(&ev), Ok(true));
}

#[test]
fn scores_at_every_float_extreme() {
    let floats = [
        0.0,
        -0.0,
        f64::MIN_POSITIVE,
        5e-324,
        -5e-324,
        f64::MAX,
        f64::MIN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NAN,
    ];
    for x in floats {
        let got = score(Some(&PyValue::Float(x))).unwrap().unwrap();
        assert_eq!(got.to_bits(), x.to_bits());
    }
    let ints = [
        ("0", Ok(Some(0.0))),
        // float(0) is +0.0: an int has no negative zero.
        ("-0", Ok(Some(0.0))),
        ("9007199254740993", Ok(Some(9_007_199_254_740_992.0))),
        (
            "-9223372036854775808",
            Ok(Some(-9_223_372_036_854_775_808.0)),
        ),
        (
            "170141183460469231731687303715884105728",
            Ok(Some(1.7014118346046923e38)),
        ),
    ];
    for (text, want) in ints {
        let got = score(Some(&PyValue::Int(text.into())));
        let bits = |a: Result<Option<f64>, CountersError>| a.map(|x| x.map(f64::to_bits));
        assert_eq!(bits(got), bits(want), "{text}");
    }
    for digits in [309, 400, 5000, 100_000] {
        let big = PyValue::Int(format!("-9{}", "9".repeat(digits)));
        assert_eq!(score(Some(&big)), Err(CountersError::OverflowError));
    }
    for x in floats {
        let answers = dict(vec![
            ("unsafe", PyValue::Float(x)),
            ("secret", PyValue::Float(x)),
        ]);
        let ev = dict(vec![
            ("ts", s("2026-11-21T10:00:00Z")),
            ("review_id", s("w1")),
            ("status", s("committed")),
            ("review_answers", answers),
        ]);
        let row = DecidedRow::from_event(&ev).unwrap().unwrap();
        assert_eq!(row.zero_attestation(), x == 0.0);
    }
}

#[test]
fn long_and_odd_texts_read_without_panic() {
    let long = PyValue::Str(vec![0xd800; 200_000]);
    let ev = dict(vec![
        ("ts", PyValue::Str(vec![0x10ffff; 100_000])),
        ("review_id", long.clone()),
        ("right_answers", PyValue::Bool(true)),
        ("right_action", PyValue::Bool(true)),
    ]);
    assert_eq!(GradeRow::from_event(&ev), Ok(None));
    let ev = dict(vec![
        ("ts", s("2026-11-21T10:00:00Z")),
        ("review_id", long),
        ("right_answers", PyValue::Bool(true)),
        ("right_action", PyValue::Bool(true)),
        ("sample_ts", s(&"9".repeat(100_000))),
    ]);
    let row = GradeRow::from_event(&ev).unwrap().unwrap();
    assert_eq!(row.review_id.len(), 200_000);
    assert_eq!(row.sample_ts, None);
    for value in [
        PyValue::None,
        PyValue::Int("5".into()),
        s("x"),
        PyValue::Tuple(vec![]),
    ] {
        assert!(matches!(
            DecidedRow::from_event(&value),
            Err(CountersError::AttributeError(_))
        ));
    }
}
