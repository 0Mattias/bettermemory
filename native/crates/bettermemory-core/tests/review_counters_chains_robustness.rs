//! Hostile logs for `review_counters::chains`: a chain of 100,000 rows
//! answered 10,000 times, 10,000 chains against 10,000 closing calls,
//! stamps at years 1 and 9999, `int`s past the float range for the median,
//! and 50,000 distinct actions and 50,000 detectors over 100,000
//! decisions. Hostile values set by hand: counts at `u64::MAX`, dates
//! outside `date`'s range for `iso_year_week`, and median texts that are
//! not canonical. No panic in debug or release, except the two stated
//! ones, alike in both: a count added past `u64::MAX`, and a median text
//! no `int` has.
//!
//! The reference is quadratic in two places: `last_issued_before` scans a
//! chain's rows for each decision (rows times decisions), and the closing
//! rule walks every closer for every open chain (open chains times
//! closers). The port bisects the rows, and reads only the closers of the
//! chain's session and client decided after its last row, stopping past
//! the window: linearithmic in both families except when every closer of
//! one session falls inside one chain's window and fails its related test,
//! which stays quadratic as in the reference.

mod timing;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::review_counters::{
    build_chains, chains_in_window, iso_week, iso_year_week, median, ChainState, CountersError,
    Counts, Tally,
};
use bettermemory_core::unicode::cps;

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn dict(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect())
}

/// 2026-11-21T10:00 UTC plus `us` microseconds, as text.
fn at(us: i64) -> String {
    DateTime::new(2026, 11, 21, 10, 0, 0, 0, Some(0))
        .unwrap()
        .minus_micros(-i128::from(us))
        .unwrap()
        .isoformat()
}

fn now() -> DateTime {
    DateTime::new(2030, 1, 1, 0, 0, 0, 0, Some(0)).unwrap()
}

fn iss(rid: &str, ts: &str, extra: Vec<(&str, PyValue)>) -> PyValue {
    let mut pairs = vec![
        ("kind", s("review_issued")),
        ("review_id", s(rid)),
        ("ts", s(ts)),
        ("session", s("s1")),
    ];
    pairs.extend(extra);
    dict(pairs)
}

fn dec(rid: &str, ts: &str, action: &str, extra: Vec<(&str, PyValue)>) -> PyValue {
    let mut pairs = vec![
        ("kind", s("write")),
        ("review_id", s(rid)),
        ("ts", s(ts)),
        ("status", s("committed")),
        ("review_action", s(action)),
        ("session", s("s1")),
    ];
    pairs.extend(extra);
    dict(pairs)
}

fn relation(key: &str) -> (&'static str, PyValue) {
    (
        "review_answers",
        dict(vec![("relation", dict(vec![(key, s("unrelated"))]))]),
    )
}

/// One chain of `rows` rows a second apart, refused for a standing record
/// `answers` times at even steps: every answer is a retry.
fn long_chain(rows: usize, answers: usize) -> Vec<PyValue> {
    let mut events = vec![iss("r", &at(0), vec![])];
    for k in 1..rows {
        let id = format!("r{k}");
        events.push(iss(
            &id,
            &at(k as i64 * 1_000_000),
            vec![("parent_review_id", s("r"))],
        ));
    }
    let step = rows / answers;
    for j in 0..answers {
        let ts = at((j * step) as i64 * 1_000_000 + 500_000);
        events.push(dec(
            &format!("r{}", j * step + 1),
            &ts,
            "refused_standing",
            vec![],
        ));
    }
    events
}

#[test]
fn a_chain_of_100000_rows_answered_10000_times() {
    let events = long_chain(100_000, 10_000);
    let chains = build_chains(&events, &now(), None).unwrap();
    assert_eq!(chains.len(), 1);
    let c = &chains[0];
    assert_eq!(c.issued.len(), 100_000);
    assert_eq!(c.standing_retries, 9_999);
    assert_eq!(c.calls(), 110_000);
    assert_eq!(c.outcome(), Some(&cps("refused_standing")));
    let (small, large) = (long_chain(10_000, 1_000), long_chain(100_000, 10_000));
    let run = |ev: &[PyValue]| timing::once(|| build_chains(ev, &now(), None));
    timing::grows_within(
        "one long chain",
        timing::TENFOLD,
        &mut || run(&small),
        &mut || run(&large),
    );
}

/// `n` open chains and `n` closing calls. `pair`: closer i closes chain
/// i; else every closer is of another session and closes none.
fn closing_log(n: usize, pair: bool) -> Vec<PyValue> {
    let mut events = Vec::new();
    let session = if pair { "s1" } else { "s2" };
    for i in 0..n {
        let base = i as i64 * 60_000_000;
        events.push(iss(
            &format!("a{i}"),
            &at(base),
            vec![("related", PyValue::List(vec![s("x")]))],
        ));
        events.push(iss(
            &format!("z{i}"),
            &at(base + 1),
            vec![
                ("pre_answered", PyValue::Bool(true)),
                ("session", s(session)),
            ],
        ));
        events.push(dec(
            &format!("z{i}"),
            &at(base + 2),
            "committed",
            vec![relation("x")],
        ));
    }
    events
}

#[test]
fn ten_thousand_chains_against_ten_thousand_closers() {
    for pair in [true, false] {
        let events = closing_log(10_000, pair);
        let chains = build_chains(&events, &now(), None).unwrap();
        assert_eq!(chains.len(), 20_000);
        let closed = chains.iter().filter(|c| c.closed).count();
        let absorbed = chains.iter().filter(|c| c.absorbed).count();
        assert_eq!(
            (closed, absorbed),
            if pair { (10_000, 10_000) } else { (0, 0) }
        );
        let open = chains
            .iter()
            .filter(|c| c.state == ChainState::Unanswered)
            .count();
        assert_eq!(open, if pair { 0 } else { 10_000 });
        let (small, large) = (closing_log(1_000, pair), closing_log(10_000, pair));
        let run = |ev: &[PyValue]| timing::once(|| build_chains(ev, &now(), None));
        timing::grows_within("closers", timing::TENFOLD, &mut || run(&small), &mut || {
            run(&large)
        });
    }
}

/// `n` open chains a microsecond apart and `n` closers inside every one's
/// window, none naming the chains' related id: each chain reads every
/// closer, as in the reference.
fn crowded_log(n: usize) -> Vec<PyValue> {
    let mut events = Vec::new();
    for i in 0..n {
        events.push(iss(
            &format!("a{i}"),
            &at(i as i64),
            vec![("related", PyValue::List(vec![s("x")]))],
        ));
        events.push(iss(
            &format!("z{i}"),
            &at(60_000_000 + i as i64),
            vec![("pre_answered", PyValue::Bool(true))],
        ));
        events.push(dec(
            &format!("z{i}"),
            &at(60_000_000 + i as i64),
            "committed",
            vec![relation("y")],
        ));
    }
    events
}

#[test]
fn closers_crowding_one_window_stay_within_quadratic_growth() {
    let chains = build_chains(&crowded_log(3_000), &now(), None).unwrap();
    assert_eq!(chains.iter().filter(|c| c.closed).count(), 0);
    let (small, large) = (crowded_log(300), crowded_log(3_000));
    let run = |ev: &[PyValue]| timing::once(|| build_chains(ev, &now(), None));
    // Quadratic: a hundred times for ten times the input, with the room
    // TENFOLD leaves linear code.
    timing::grows_within(
        "crowded closers",
        timing::TENFOLD * 6,
        &mut || run(&small),
        &mut || run(&large),
    );
}

#[test]
fn stamps_at_years_1_and_9999() {
    let early = [
        iss("a", "0001-01-01T00:00:00Z", vec![]),
        iss("b", "0001-01-01T00:00:00+23:59", vec![]),
        dec("b", "0001-01-01T00:20:00+23:59", "committed", vec![]),
    ];
    let chains = build_chains(&early, &now(), None).unwrap();
    assert_eq!(chains[0].key, cps("b"));
    assert_eq!(chains[0].state, ChainState::Answered);
    assert_eq!(
        iso_week(&chains[0].issued[0].ts),
        Err(CountersError::OverflowError)
    );
    let start = DateTime::new(1, 1, 1, 0, 0, 0, 0, Some(0)).unwrap();
    let got = chains_in_window(&early, Some(&start), None, &now()).unwrap();
    assert_eq!(got.len(), 1, "b's root falls before year 1 as an instant");
    let late = [
        iss("a", "9999-12-31T23:59:59.999999Z", vec![]),
        dec("a", "9999-12-31T23:59:59.999999Z", "committed", vec![]),
    ];
    let chains = build_chains(&late, &now(), None).unwrap();
    assert_eq!(chains[0].state, ChainState::Answered);
    let open = [iss("a", "9999-12-31T23:59:59.999999-23:59", vec![])];
    assert_eq!(
        build_chains(&open, &now(), None),
        Err(CountersError::OverflowError)
    );
    let end = DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, Some(0)).unwrap();
    assert_eq!(
        chains_in_window(&[], None, Some(&end), &now()),
        Err(CountersError::OverflowError)
    );
    assert_eq!(iso_week(&end), Ok("9999-W52".to_string()));
}

#[test]
fn ints_past_the_float_range_in_the_median() {
    let huge = format!("1{}", "0".repeat(100_000));
    let neg = format!("-{huge}");
    assert_eq!(
        median(std::slice::from_ref(&huge)),
        Err(CountersError::OverflowError)
    );
    assert_eq!(median(&[huge.clone(), neg.clone()]), Ok(Some(0.0)));
    assert_eq!(
        median(&[huge.clone(), huge.clone()]),
        Err(CountersError::OverflowError)
    );
    let many: Vec<String> = (0..1_001)
        .map(|i| {
            if i % 2 == 0 {
                huge.clone()
            } else {
                neg.clone()
            }
        })
        .collect();
    assert_eq!(median(&many), Err(CountersError::OverflowError));
    let tally = Tally {
        response_chars: vec![huge.clone()],
        ..Tally::default()
    };
    assert_eq!(tally.to_dict(), Err(CountersError::OverflowError));
    let small: Vec<String> = (0..10_000)
        .map(|i| format!("{i}{}", "7".repeat(30)))
        .collect();
    let large: Vec<String> = (0..100_000)
        .map(|i| format!("{i}{}", "7".repeat(30)))
        .collect();
    timing::grows_within(
        "median",
        timing::TENFOLD,
        &mut || timing::once(|| median(&small)),
        &mut || timing::once(|| median(&large)),
    );
}

fn distinct_actions(n: usize) -> Vec<PyValue> {
    let mut events = Vec::new();
    for i in 0..n {
        let id = format!("a{i}");
        let ts = at(i as i64 * 1_000);
        events.push(iss(&id, &ts, vec![]));
        let filing = dict(vec![("id", s("t")), ("detector", s(&format!("d{i}")))]);
        events.push(dec(
            &id,
            &ts,
            if i % 2 == 0 { "committed" } else { &id },
            vec![("review_filings", PyValue::List(vec![filing]))],
        ));
    }
    events
}

#[test]
fn a_hundred_thousand_distinct_actions_and_detectors() {
    let tally_of = |events: &[PyValue]| {
        let mut tally = Tally::default();
        for c in build_chains(events, &now(), None).unwrap() {
            tally.add(&c);
        }
        tally
    };
    let tally = tally_of(&distinct_actions(100_000));
    assert_eq!(tally.actions.items.len(), 4 + 50_000);
    assert_eq!(tally.filings.items.len(), 2 + 50_000);
    assert_eq!(tally.committed(), 50_000);
    let rates = tally.filings_per_100();
    let PyValue::Dict(rates) = &rates else {
        panic!("a dict")
    };
    assert_eq!(rates.len(), 50_003);
    assert_eq!(rates[2].1, PyValue::Float(0.0));
    let (small, large) = (distinct_actions(10_000), distinct_actions(100_000));
    timing::grows_within(
        "distinct actions",
        timing::TENFOLD,
        &mut || timing::once(|| tally_of(&small).to_dict()),
        &mut || timing::once(|| tally_of(&large).to_dict()),
    );
}

#[test]
fn values_that_are_not_rows_are_skipped() {
    let mut nested = PyValue::List(vec![]);
    for _ in 0..10_000 {
        nested = PyValue::List(vec![nested]);
    }
    let events = [
        nested,
        PyValue::None,
        s("review_issued"),
        dict(vec![("kind", PyValue::List(vec![s("write")]))]),
        iss("a", &at(0), vec![("related", s("x"))]),
    ];
    let chains = build_chains(&events, &now(), None).unwrap();
    assert_eq!(chains.len(), 1);
    assert!(chains[0].related().is_empty());
}

fn texts(values: &[&str]) -> Vec<String> {
    values.iter().map(|v| v.to_string()).collect()
}

fn rates(tally: &Tally) -> Vec<(String, PyValue)> {
    let rates = tally.filings_per_100();
    let PyValue::Dict(rates) = &rates else {
        panic!("a dict")
    };
    rates
        .iter()
        .map(|(k, v)| match k {
            PyValue::Str(k) => (
                k.iter().map(|&c| char::from_u32(c).unwrap()).collect(),
                v.clone(),
            ),
            _ => panic!("a str key"),
        })
        .collect()
}

#[test]
fn counts_at_the_u64_bound_answer_as_the_reference() {
    // The reference's answers, from `_Tally` with these counts set.
    let mut tally = Tally::default();
    tally.actions.add(&cps("committed"), 1);
    tally.filings.add(&cps("review"), u64::MAX / 50);
    assert_eq!(
        rates(&tally),
        vec![
            ("review".to_string(), PyValue::Float(3.6893488147419103e19)),
            ("review-disagrees".to_string(), PyValue::Float(0.0)),
            ("total".to_string(), PyValue::Float(3.6893488147419103e19)),
        ]
    );
    let mut tally = Tally::default();
    tally.actions.add(&cps("committed"), 1);
    tally.filings.add(&cps("review"), u64::MAX);
    tally.filings.add(&cps("zz"), 5);
    assert_eq!(
        rates(&tally),
        vec![
            ("review".to_string(), PyValue::Float(1.8446744073709552e21)),
            ("review-disagrees".to_string(), PyValue::Float(0.0)),
            ("zz".to_string(), PyValue::Float(500.0)),
            ("total".to_string(), PyValue::Float(1.8446744073709552e21)),
        ]
    );
    let mut tally = Tally {
        answered: u64::MAX,
        unanswered: u64::MAX,
        committed_calls: u64::MAX,
        ..Tally::default()
    };
    for action in ["committed", "refused_secret", "refused_unsafe", "restates"] {
        tally.actions.add(&cps(action), u64::MAX);
    }
    assert_eq!(tally.refused(), 3 * u128::from(u64::MAX));
    let dict = tally.to_dict().unwrap();
    assert_eq!(dict.get("unanswered_share"), Some(&PyValue::Float(0.5)));
    assert_eq!(
        dict.get("refused"),
        Some(&PyValue::Int("55340232221128654845".to_string()))
    );
    assert_eq!(
        dict.get("committed"),
        Some(&PyValue::Int(u64::MAX.to_string()))
    );
    assert_eq!(
        dict.get("calls_per_committed_write"),
        Some(&PyValue::Float(1.0))
    );
}

#[test]
#[should_panic(expected = "a count past u64::MAX")]
fn a_count_added_past_the_u64_bound_panics() {
    let mut counts = Counts::from_keys(&["review"]);
    counts.add(&cps("review"), u64::MAX);
    counts.add(&cps("review"), 1);
}

#[test]
#[should_panic(expected = "a count past u64::MAX")]
fn a_tally_count_added_past_the_u64_bound_panics() {
    let chains = build_chains(&[iss("a", &at(0), vec![])], &now(), None).unwrap();
    let mut tally = Tally {
        issued: u64::MAX,
        ..Tally::default()
    };
    tally.add(&chains[0]);
}

#[test]
fn iso_year_week_holds_dates_date_holds_only() {
    assert_eq!(iso_year_week(1, 1, 1), Some((1, 1)));
    assert_eq!(iso_year_week(9999, 12, 31), Some((9999, 52)));
    assert_eq!(iso_year_week(2024, 2, 29), Some((2024, 9)));
    for (year, month, day) in [
        (i64::MAX, 1, 1),
        (i64::MIN, 1, 1),
        (0, 1, 1),
        (10_000, 1, 1),
        (2026, 0, 1),
        (2026, 13, 1),
        (2026, u32::MAX, 1),
        (2026, 1, 0),
        (2026, 2, 29),
        (2026, 4, 31),
        (2026, 1, u32::MAX),
    ] {
        assert_eq!(
            iso_year_week(year, month, day),
            None,
            "{year}-{month}-{day}"
        );
    }
}

#[test]
fn median_texts_read_as_int_reads_them() {
    // The reference's `_median([1, 2])`, `([3, 10, 12])` and `([0, 7])`.
    assert_eq!(median(&texts(&["+1", "2"])), Ok(Some(1.5)));
    assert_eq!(
        median(&texts(&[" 3\n", "1_0", "\u{661}\u{662}"])),
        Ok(Some(10.0))
    );
    assert_eq!(median(&texts(&["-0", "007"])), Ok(Some(3.5)));
    assert_eq!(median(&texts(&["+0", "-00"])), Ok(Some(0.0)));
}

#[test]
fn median_texts_no_int_has_panic() {
    let long = format!("+1{}", "0".repeat(5_000));
    for bad in [
        "",
        "-",
        "+",
        "1e5",
        "1__0",
        "_1",
        "1_",
        "0x10",
        " ",
        long.as_str(),
    ] {
        let values = texts(&["1", bad]);
        let answer = std::panic::catch_unwind(|| median(&values));
        assert!(answer.is_err(), "{bad:?}");
    }
}
