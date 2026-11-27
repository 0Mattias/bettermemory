//! Cases a plausible wrong reading of `compute_review_report` fails: each
//! moves a key of the report, a response size, a grade or the judged
//! instant. Every expected answer is the reference's.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::review_counters::{compute_review_report, ReviewReport};
use bettermemory_core::unicode::{cps, to_string};

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn dict(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect())
}

/// 2026-11-21T10:00 UTC plus `m` minutes, as text.
fn at(m: i64) -> String {
    let base = DateTime::new(2026, 11, 21, 10, 0, 0, 0, Some(0)).unwrap();
    base.minus_micros(-i128::from(m * 60_000_000))
        .unwrap()
        .isoformat()
}

fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).unwrap()
}

fn iss(rid: &str, ts: &str, client: &str, model: &str, extra: Vec<(&str, PyValue)>) -> PyValue {
    let mut pairs = vec![
        ("kind", s("review_issued")),
        ("review_id", s(rid)),
        ("ts", s(ts)),
        ("session", s("s1")),
        ("client", s(client)),
        ("model", s(model)),
    ];
    pairs.extend(extra);
    dict(pairs)
}

fn commit(rid: &str, ts: &str, detectors: &[&str]) -> PyValue {
    let filings = detectors
        .iter()
        .map(|d| dict(vec![("id", s("t")), ("detector", s(d))]))
        .collect();
    dict(vec![
        ("kind", s("write")),
        ("review_id", s(rid)),
        ("ts", s(ts)),
        ("status", s("committed")),
        ("review_action", s("committed")),
        ("session", s("s1")),
        ("client", s("c1")),
        ("review_filings", PyValue::List(filings)),
    ])
}

fn grade(rid: &str, ts: &str, sample_ts: &str, right: bool) -> PyValue {
    dict(vec![
        ("kind", s("review_grade")),
        ("review_id", s(rid)),
        ("ts", s(ts)),
        ("sample_ts", s(sample_ts)),
        ("right_answers", PyValue::Bool(right)),
        ("right_action", PyValue::Bool(true)),
    ])
}

fn chars(n: i128) -> (&'static str, PyValue) {
    ("response_chars", PyValue::int(n))
}

fn report(events: &[PyValue], start: Option<&str>, end: Option<&str>, now: &str) -> PyValue {
    let (start, end) = (start.map(dt), end.map(dt));
    compute_review_report(events, start.as_ref(), end.as_ref(), &dt(now))
        .and_then(|r: ReviewReport| r.to_dict())
        .unwrap()
}

fn keys(v: &PyValue) -> Vec<String> {
    let PyValue::Dict(items) = v else {
        panic!("a dict")
    };
    items
        .iter()
        .map(|(k, _)| match k {
            PyValue::Str(s) => to_string(s).unwrap(),
            other => panic!("a str key: {other:?}"),
        })
        .collect()
}

fn path<'a>(v: &'a PyValue, keys: &[&str]) -> &'a PyValue {
    keys.iter()
        .fold(v, |v, k| v.get(k).unwrap_or_else(|| panic!("no key {k}")))
}

fn float(v: &PyValue) -> f64 {
    match v {
        PyValue::Float(x) => *x,
        other => panic!("a float: {other:?}"),
    }
}

#[test]
fn by_client_and_by_model_are_sorted_in_code_point_order() {
    let events = vec![
        iss("a", &at(0), "c2", "\u{1F600}", vec![]),
        iss("b", &at(1), "\u{FF21}", "Z", vec![]),
        iss("c", &at(2), "a", "\u{FF21}", vec![]),
        iss("d", &at(3), "\u{1F600}", "a", vec![]),
        iss("e", &at(4), "Z", "c2", vec![]),
    ];
    let r = report(&events, None, None, &at(60));
    let want = ["Z", "a", "c2", "\u{FF21}", "\u{1F600}"];
    assert_eq!(keys(path(&r, &["by_client"])), want);
    assert_eq!(keys(path(&r, &["by_model"])), want);
    assert_eq!(keys(path(&r, &["pre_answered_zero", "by_client"])), want);
    assert_eq!(keys(path(&r, &["pre_answered_zero", "by_model"])), want);
}

#[test]
fn weeks_are_sorted_as_texts_and_keep_a_foreign_detector() {
    let mut events = Vec::new();
    for (i, (issued, decided)) in [
        ("2027-01-04T08:55:00+00:00", "2027-01-04T09:00:00+00:00"),
        ("0999-06-01T08:55:00+00:00", "0999-06-01T09:00:00+00:00"),
        ("2026-12-31T08:55:00+00:00", "2026-12-31T09:00:00+00:00"),
    ]
    .into_iter()
    .enumerate()
    {
        let rid = format!("w{i}");
        events.push(iss(&rid, issued, "c1", "m1", vec![]));
        events.push(commit(&rid, decided, &["zz", "review", "zz"]));
    }
    let r = report(&events, None, None, "2027-02-01T00:00:00+00:00");
    let weeks = path(&r, &["filings_by_week"]);
    assert_eq!(keys(weeks), ["2026-W53", "2027-W01", "999-W22"]);
    let week = path(weeks, &["2027-W01"]);
    assert_eq!(keys(week), ["review", "review-disagrees", "zz"]);
    assert_eq!(path(week, &["zz"]), &PyValue::int(2));
    assert_eq!(path(week, &["review-disagrees"]), &PyValue::int(0));
}

#[test]
fn a_roster_page_and_a_pre_answered_call_leave_the_sizes() {
    let events = vec![
        iss("r1", &at(0), "c1", "m1", vec![chars(10)]),
        iss("r2", &at(1), "c1", "m1", vec![chars(10)]),
        iss(
            "r3",
            &at(2),
            "c1",
            "m1",
            vec![chars(99_999), ("roster", PyValue::List(vec![s("m")]))],
        ),
        iss(
            "r4",
            &at(3),
            "c1",
            "m1",
            vec![chars(99_999), ("pre_answered", PyValue::Bool(true))],
        ),
    ];
    let r = report(&events, None, None, &at(60));
    assert_eq!(float(path(&r, &["response_chars_p50"])), 10.0);
    assert_eq!(path(&r, &["response_chars_n"]), &PyValue::int(2));
}

#[test]
fn sizes_are_read_off_each_issued_row_under_its_own_client() {
    let events = vec![
        iss("root", &at(-10), "c1", "m1", vec![chars(5)]),
        iss(
            "follow",
            &at(2),
            "c9",
            "m9",
            vec![chars(70), ("parent_review_id", s("root"))],
        ),
    ];
    let r = report(&events, Some(&at(0)), Some(&at(45)), &at(120));
    assert_eq!(path(&r, &["chains", "issued"]), &PyValue::int(0));
    assert_eq!(float(path(&r, &["response_chars_p50"])), 70.0);
    assert_eq!(keys(path(&r, &["by_client"])), ["c9"]);
    let c9 = path(&r, &["by_client", "c9"]);
    assert_eq!(path(c9, &["issued"]), &PyValue::int(0));
    assert_eq!(path(c9, &["response_chars_n"]), &PyValue::int(1));
}

#[test]
fn a_later_grade_at_the_same_instant_replaces_the_first() {
    let events = vec![
        grade("g1", &at(100), &at(1), true),
        grade("g1", "2026-11-21T12:40:00+01:00", &at(1), false),
        grade("g2", &at(200), &at(2), true),
        grade("g2", &at(150), &at(2), false),
    ];
    let r = report(&events, Some(&at(0)), Some(&at(45)), &at(400));
    assert_eq!(path(&r, &["grades", "n"]), &PyValue::int(2));
    assert_eq!(path(&r, &["grades", "right_answers"]), &PyValue::int(1));
}

#[test]
fn the_grade_window_is_read_on_sample_ts() {
    let events = vec![
        grade("in", &at(-30), &at(10), true),
        grade("out", &at(10), &at(-30), true),
        grade("end", &at(10), &at(45), true),
    ];
    let r = report(&events, Some(&at(0)), Some(&at(45)), &at(400));
    assert_eq!(path(&r, &["grades", "n"]), &PyValue::int(1));
}

#[test]
fn judged_through_is_the_earlier_and_now_on_a_tie() {
    let judged = |end: &str, now: &str| {
        let r = report(&[], None, Some(end), now);
        path(&r, &["judged_through"]).clone()
    };
    assert_eq!(judged(&at(45), &at(60)), s("2026-11-21T11:00:00Z"));
    assert_eq!(judged(&at(45), &at(120)), s("2026-11-21T11:15:00Z"));
    assert_eq!(
        judged("2026-11-21T11:00:00+01:00", &at(30)),
        s("2026-11-21T10:30:00Z")
    );
    assert_eq!(
        judged("2026-11-21T11:00:00+01:00", &at(31)),
        s("2026-11-21T11:30:00+01:00")
    );
    let r = report(&[], None, None, "2026-11-21T15:30:00+05:30");
    assert_eq!(
        path(&r, &["judged_through"]),
        &s("2026-11-21T15:30:00+05:30")
    );
}
