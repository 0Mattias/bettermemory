//! Cases a plausible wrong reading of `review_counters`' chains fails:
//! each moves a chain into the wrong state, the wrong window or the wrong
//! count, or a rate or week into the wrong value. Every expected answer is
//! the reference's.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::review_counters::{
    build_chains, chains_in_window, iso_week, median, share, ChainState, ReviewChain, Tally,
};
use bettermemory_core::unicode::cps;

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn strs(items: &[&str]) -> PyValue {
    PyValue::List(items.iter().map(|t| s(t)).collect())
}

fn dict(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect())
}

/// 2026-11-21T10:00 UTC plus `m` minutes and `us` microseconds, as text.
fn at(m: i64, us: i64) -> String {
    let base = DateTime::new(2026, 11, 21, 10, 0, 0, 0, Some(0)).unwrap();
    base.minus_micros(-i128::from(m * 60_000_000 + us))
        .unwrap()
        .isoformat()
}

fn dt(m: i64, us: i64) -> DateTime {
    DateTime::from_isoformat(&cps(&at(m, us))).unwrap()
}

fn iss(rid: &str, ts: &str, extra: Vec<(&str, PyValue)>) -> PyValue {
    let mut pairs = vec![
        ("kind", s("review_issued")),
        ("review_id", s(rid)),
        ("ts", s(ts)),
        ("session", s("s1")),
        ("client", s("c1")),
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
        ("client", s("c1")),
    ];
    pairs.extend(extra);
    dict(pairs)
}

fn relation(keys: &[(&str, &str)]) -> (&'static str, PyValue) {
    (
        "review_answers",
        dict(vec![(
            "relation",
            dict(keys.iter().map(|(k, v)| (*k, s(v))).collect()),
        )]),
    )
}

fn pre() -> (&'static str, PyValue) {
    ("pre_answered", PyValue::Bool(true))
}

fn chains(events: &[PyValue]) -> Vec<ReviewChain> {
    build_chains(events, &dt(60, 0), None).unwrap()
}

fn find<'a>(cs: &'a [ReviewChain], key: &str) -> &'a ReviewChain {
    cs.iter().find(|c| c.key == cps(key)).expect("the chain")
}

/// An open chain `a` showing x and y, and a closing call `z` decided at
/// `dec_at` naming `keys`.
fn closing(dec_at: &str, keys: &[(&str, &str)]) -> Vec<PyValue> {
    vec![
        iss("a", &at(0, 0), vec![("related", strs(&["x", "y"]))]),
        iss("z", &at(1, 0), vec![pre()]),
        dec("z", dec_at, "committed", vec![relation(keys)]),
    ]
}

#[test]
fn an_answer_at_exactly_the_window_counts() {
    let cs = chains(&[
        iss("a", &at(0, 0), vec![]),
        dec("a", &at(30, 0), "committed", vec![]),
        iss("b", &at(1, 0), vec![]),
        dec("b", &at(31, 1), "committed", vec![]),
    ]);
    assert_eq!(find(&cs, "a").state, ChainState::Answered);
    assert_eq!(find(&cs, "b").state, ChainState::Unanswered);
}

#[test]
fn a_closing_call_at_the_open_rows_instant_does_not_close() {
    let cs = chains(&closing(&at(0, 0), &[("x", "unrelated"), ("y", "update")]));
    assert_eq!(find(&cs, "a").state, ChainState::Unanswered);
    assert!(!find(&cs, "z").absorbed);
    let cs = chains(&closing(&at(0, 1), &[("x", "unrelated"), ("y", "update")]));
    assert!(find(&cs, "a").closed);
    // The upper bound is inclusive, one microsecond past it is not.
    let cs = chains(&closing(&at(30, 0), &[("x", "a"), ("y", "b")]));
    assert!(find(&cs, "a").closed);
    let cs = chains(&closing(&at(30, 1), &[("x", "a"), ("y", "b")]));
    assert!(!find(&cs, "a").closed);
}

#[test]
fn the_window_runs_from_the_last_row_issued_before_the_answer() {
    let cs = chains(&[
        iss("r", &at(0, 0), vec![]),
        iss("r2", &at(20, 0), vec![("parent_review_id", s("r"))]),
        iss("r3", &at(60, 0), vec![("parent_review_id", s("r"))]),
        dec("r", &at(55, 0), "committed", vec![]),
        iss("q", &at(1, 0), vec![]),
        iss("q2", &at(20, 0), vec![("parent_review_id", s("q"))]),
        iss("q3", &at(70, 0), vec![("parent_review_id", s("q"))]),
        dec("q3", &at(45, 0), "committed", vec![]),
    ]);
    // Read from the chain's last row (r3, after the answer), r would be
    // answered; read from the root, q would not.
    assert_eq!(find(&cs, "r").state, ChainState::Pending);
    assert_eq!(find(&cs, "q").state, ChainState::Answered);
    let r = find(&cs, "r");
    assert_eq!(r.last_issued_before(&dt(55, 0)).review_id, cps("r2"));
    assert_eq!(r.last_issued_before(&dt(-1, 0)).review_id, cps("r"));
    // A row issued at the very instant asked counts as before it.
    assert_eq!(r.last_issued_before(&dt(20, 0)).review_id, cps("r2"));
    assert_eq!(r.last_issued_before(&dt(60, 0)).review_id, cps("r3"));
}

#[test]
fn a_standing_refusal_leaves_the_chain_open() {
    let cs = chains(&[
        iss("a", &at(0, 0), vec![]),
        dec("a", &at(5, 0), "refused_standing", vec![]),
        dec("a", &at(10, 0), "refused_standing", vec![]),
        dec("a", &at(15, 0), "committed", vec![]),
        dec("a", &at(20, 0), "refused_unsafe", vec![]),
    ]);
    let a = find(&cs, "a");
    assert_eq!(a.outcome(), Some(&cps("committed")));
    assert_eq!(a.standing_retries, 2);
    assert_eq!(a.calls(), 4);
    // Any other refusal is final.
    let cs = chains(&[
        iss("a", &at(0, 0), vec![]),
        dec("a", &at(5, 0), "refused_secret", vec![]),
        dec("a", &at(10, 0), "committed", vec![]),
    ]);
    assert_eq!(find(&cs, "a").outcome(), Some(&cps("refused_secret")));
}

#[test]
fn the_related_test_reads_the_relations_keys_as_a_superset() {
    let cs = chains(&closing(&at(10, 0), &[("k1", "x"), ("k2", "y")]));
    assert!(!find(&cs, "a").closed, "the values are not the keys");
    let cs = chains(&closing(&at(10, 0), &[("x", "u"), ("y", "u"), ("w", "u")]));
    assert!(find(&cs, "a").closed, "an extra key still closes");
    let cs = chains(&closing(&at(10, 0), &[("x", "u")]));
    assert!(!find(&cs, "a").closed, "a missing key does not");
}

#[test]
fn closers_are_tried_in_the_order_they_were_decided() {
    let cs = chains(&[
        iss("a", &at(0, 0), vec![("related", strs(&["x"]))]),
        iss("z2", &at(1, 0), vec![pre()]),
        iss("z1", &at(2, 0), vec![pre()]),
        dec("z1", &at(5, 0), "committed", vec![relation(&[("x", "u")])]),
        dec("z2", &at(8, 0), "committed", vec![relation(&[("x", "u")])]),
    ]);
    let a = find(&cs, "a");
    assert_eq!(a.decided.as_ref().unwrap().review_id, cps("z1"));
    assert!(find(&cs, "z1").absorbed);
    assert!(!find(&cs, "z2").absorbed);
}

#[test]
fn absorbed_calls_are_left_out_of_the_window() {
    let events = [
        iss("a", &at(0, 0), vec![]),
        iss("z", &at(1, 0), vec![pre()]),
        dec("z", &at(5, 0), "committed", vec![]),
    ];
    let cs = chains_in_window(&events, None, Some(&dt(10, 0)), &dt(60, 0)).unwrap();
    assert_eq!(cs.len(), 1);
    assert_eq!(cs[0].key, cps("a"));
    let mut tally = Tally::default();
    cs.iter().for_each(|c| tally.add(c));
    assert_eq!((tally.issued, tally.closed_by_call), (1, 1));
}

#[test]
fn refused_counts_no_standing_refusal() {
    let events = [
        iss("a", &at(0, 0), vec![]),
        dec("a", &at(1, 0), "refused_standing", vec![]),
        iss("b", &at(1, 0), vec![]),
        dec("b", &at(2, 0), "refused_secret", vec![]),
    ];
    let mut tally = Tally::default();
    for c in chains(&events) {
        tally.add(&c);
    }
    assert_eq!(tally.refused(), 1);
    assert_eq!(tally.refused_calls, 4);
    let names: Vec<_> = tally.actions.items.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(names.last(), Some(&cps("refused_standing")));
}

#[test]
fn rates_round_the_binary_quotient() {
    // 3 / 20000 is 0.000149999... in binary: 0.0001, where the decimal
    // 0.00015 rounds half to even to 0.0002.
    assert_eq!(share(3, 20000), Some(0.0001));
    assert_eq!(share(1, 32), Some(0.0312));
    assert_eq!(share(2, 3), Some(0.6667));
    let mut tally = Tally::default();
    tally.actions.add(&cps("committed"), 20000);
    tally.filings.add(&cps("review"), 203);
    let rates = tally.filings_per_100();
    // 1.015 is 1.01499... in binary.
    assert_eq!(rates.get("review"), Some(&PyValue::Float(1.01)));
    let mut tally = Tally::default();
    tally.actions.add(&cps("committed"), 800);
    tally.filings.add(&cps("review"), 1);
    assert_eq!(
        tally.filings_per_100().get("total"),
        Some(&PyValue::Float(0.12))
    );
}

#[test]
fn an_even_median_is_the_mean_of_the_middle_two() {
    let texts = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    assert_eq!(median(&texts(&["2", "1"])), Ok(Some(1.5)));
    assert_eq!(median(&texts(&["4", "1", "3", "2"])), Ok(Some(2.5)));
    assert_eq!(median(&texts(&["-2", "-1"])), Ok(Some(-1.5)));
    // Sorted as numbers, not as texts.
    assert_eq!(median(&texts(&["10", "9", "100"])), Ok(Some(10.0)));
}

#[test]
fn the_iso_week_is_the_utc_dates() {
    let week = |t: &str| iso_week(&DateTime::from_isoformat(&cps(t)).unwrap()).unwrap();
    assert_eq!(week("2021-01-04T00:30:00+01:00"), "2020-W53");
    assert_eq!(week("2021-01-03T23:30:00-01:00"), "2021-W01");
    assert_eq!(week("2024-12-30T00:00:00+00:00"), "2025-W01");
}
