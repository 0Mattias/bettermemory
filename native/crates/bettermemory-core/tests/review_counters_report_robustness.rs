//! Hostile input for `compute_review_report`: many chains over many
//! clients and models, a week map of 10,000 weeks, response sizes past the
//! float range, stamps at the calendar's end and values that are not rows.
//! No panic in debug or release, and growth within linear bounds.

mod timing;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::review_counters::{compute_review_report, CountersError};
use bettermemory_core::unicode::cps;

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn dict(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect())
}

fn base() -> DateTime {
    DateTime::new(2026, 11, 21, 10, 0, 0, 0, Some(0)).unwrap()
}

/// The base time plus `us` microseconds, as text.
fn at(us: i64) -> String {
    base().minus_micros(-i128::from(us)).unwrap().isoformat()
}

fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).unwrap()
}

/// `n` chains a microsecond apart over `n / 100` clients and models, each
/// with a response size, every other one committed with a filing and
/// every tenth graded.
fn many_chains(n: usize) -> Vec<PyValue> {
    let buckets = (n / 100).max(1);
    let mut events = Vec::with_capacity(n * 2);
    for i in 0..n {
        let rid = format!("r{i}");
        let ts = at(i as i64);
        events.push(dict(vec![
            ("kind", s("review_issued")),
            ("review_id", s(&rid)),
            ("ts", s(&ts)),
            ("session", s(&format!("s{i}"))),
            ("client", s(&format!("c{}", i % buckets))),
            ("model", s(&format!("m{}", (i * 7) % buckets))),
            ("response_chars", PyValue::int((i % 997) as i128)),
        ]));
        if i % 2 == 0 {
            events.push(dict(vec![
                ("kind", s("write")),
                ("review_id", s(&rid)),
                ("ts", s(&at(i as i64 + 1_000_000))),
                ("status", s("committed")),
                ("review_action", s("committed")),
                ("session", s(&format!("s{i}"))),
                (
                    "review_filings",
                    PyValue::List(vec![dict(vec![
                        ("id", s("t")),
                        ("detector", s(&format!("d{}", i % 13))),
                    ])]),
                ),
            ]));
        }
        if i % 10 == 0 {
            events.push(dict(vec![
                ("kind", s("review_grade")),
                ("review_id", s(&rid)),
                ("ts", s(&ts)),
                ("sample_ts", s(&ts)),
                ("right_answers", PyValue::Bool(true)),
                ("right_action", PyValue::Bool(i % 20 == 0)),
            ]));
        }
    }
    events
}

#[test]
fn a_hundred_thousand_chains_over_a_thousand_clients_and_models() {
    let now = dt(&at(3_600_000_000));
    let events = many_chains(100_000);
    let report = compute_review_report(&events, None, None, &now).unwrap();
    assert_eq!(report.total.issued, 100_000);
    assert_eq!(report.total.committed(), 50_000);
    assert_eq!(report.by_client.items.len(), 1_000);
    assert_eq!(report.by_model.items.len(), 1_000);
    assert_eq!(report.grades.n, 10_000);
    assert_eq!(report.grades.right_both, 5_000);
    let dict = report.to_dict().unwrap();
    assert_eq!(dict.get("response_chars_n"), Some(&PyValue::int(100_000)));
    let (small, large) = (many_chains(10_000), many_chains(100_000));
    let run = |ev: &[PyValue]| {
        timing::once(|| {
            compute_review_report(ev, None, None, &now)
                .and_then(|r| r.to_dict())
                .unwrap()
        })
    };
    timing::grows_within(
        "a report over many chains",
        timing::TENFOLD,
        &mut || run(&small),
        &mut || run(&large),
    );
}

#[test]
fn a_week_map_of_ten_thousand_weeks() {
    let mut events = Vec::new();
    let start = DateTime::new(1830, 1, 4, 12, 0, 0, 0, Some(0)).unwrap();
    let week_us: i128 = 7 * 86_400 * 1_000_000;
    for i in 0..10_000 {
        let issued = start.minus_micros(-week_us * i).unwrap();
        let decided = issued.minus_micros(-60_000_000).unwrap();
        let rid = format!("w{i}");
        events.push(dict(vec![
            ("kind", s("review_issued")),
            ("review_id", s(&rid)),
            ("ts", s(&issued.isoformat())),
        ]));
        events.push(dict(vec![
            ("kind", s("write")),
            ("review_id", s(&rid)),
            ("ts", s(&decided.isoformat())),
            ("status", s("committed")),
            ("review_action", s("committed")),
            (
                "review_filings",
                PyValue::List(vec![dict(vec![("id", s("t")), ("detector", s("x"))])]),
            ),
        ]));
    }
    let now = DateTime::new(2100, 1, 1, 0, 0, 0, 0, Some(0)).unwrap();
    let report = compute_review_report(&events, None, None, &now).unwrap();
    assert_eq!(report.filings_by_week.items.len(), 10_000);
    let dict = report.to_dict().unwrap();
    let PyValue::Dict(weeks) = dict.get("filings_by_week").unwrap() else {
        panic!("a dict")
    };
    assert_eq!(weeks.len(), 10_000);
    let names: Vec<&PyValue> = weeks.iter().map(|(k, _)| k).collect();
    assert_eq!(names[0], &s("1830-W01"));
    assert!(names.windows(2).all(|w| match (w[0], w[1]) {
        (PyValue::Str(a), PyValue::Str(b)) => a < b,
        _ => false,
    }));
}

#[test]
fn sizes_past_the_float_range_and_the_calendar_end() {
    let mut big = String::from("1");
    big.push_str(&"0".repeat(400));
    let issued = |rid: &str, ts: &str, chars: PyValue| {
        dict(vec![
            ("kind", s("review_issued")),
            ("review_id", s(rid)),
            ("ts", s(ts)),
            ("response_chars", chars),
        ])
    };
    let events = vec![
        issued("a", &at(0), PyValue::Int(big.clone())),
        issued("b", &at(1), PyValue::Int(format!("-{big}"))),
        issued("c", &at(2), PyValue::int(i128::MAX)),
        issued("d", &at(3), PyValue::Int(big.clone())),
    ];
    let now = dt(&at(3_600_000_000));
    let report = compute_review_report(&events, None, None, &now).unwrap();
    assert_eq!(report.to_dict(), Err(CountersError::OverflowError));
    let late = vec![issued("z", "9999-12-31T23:00:00+00:00", PyValue::int(1))];
    let end = dt("9999-12-31T23:29:59.999999+00:00");
    let now = dt("9999-12-31T23:59:59.999999+00:00");
    let report = compute_review_report(&late, None, Some(&end), &now).unwrap();
    assert_eq!(report.judged_through, now);
    let end = dt("9999-12-31T23:30:00+00:00");
    assert_eq!(
        compute_review_report(&late, None, Some(&end), &now).err(),
        Some(CountersError::OverflowError)
    );
}

#[test]
fn values_that_are_not_rows_and_deep_values_are_skipped() {
    let mut deep = PyValue::List(Vec::new());
    for _ in 0..10_000 {
        deep = PyValue::List(vec![deep]);
    }
    let events = vec![
        PyValue::None,
        s("text"),
        PyValue::int(5),
        PyValue::List(vec![s("kind")]),
        dict(vec![("kind", PyValue::List(vec![]))]),
        dict(vec![
            ("kind", s("review_issued")),
            ("review_id", s("a")),
            ("ts", s(&at(0))),
            ("roster", deep.clone()),
            ("response_chars", deep),
        ]),
        dict(vec![("kind", s("review_grade")), ("review_id", s("g"))]),
    ];
    let now = dt(&at(3_600_000_000));
    let report = compute_review_report(&events, None, None, &now).unwrap();
    assert_eq!(report.total.issued, 1);
    assert_eq!(report.grades.n, 0);
    assert!(report.total.response_chars.is_empty());
}
