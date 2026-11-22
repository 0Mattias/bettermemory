//! Hostile input for the review's answers: a relation and a `named` list
//! of 200,000 ids, many repeated, a `said` of millions of spaces, unknown
//! keys by the hundred thousand, a review_id nested deep, ints at the
//! float bounds, and growth claims at ten times the input.

mod timing;

use std::collections::HashSet;

use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::review::*;
use bettermemory_core::unicode;

fn dict(items: Vec<(PyValue, PyValue)>) -> PyValue {
    PyValue::Dict(items)
}

fn zeros() -> Vec<(PyValue, PyValue)> {
    ["unsafe", "secret", "standing"]
        .iter()
        .map(|k| (PyValue::str(k), PyValue::int(0)))
        .collect()
}

fn relation(n: usize) -> PyValue {
    let mut items = zeros();
    let rel = (0..n)
        .map(|i| {
            (
                PyValue::str(&format!("id{}", i % (n / 2 + 1))),
                PyValue::str("update"),
            )
        })
        .collect();
    items.push((PyValue::str("relation"), PyValue::Dict(rel)));
    let named = (0..n)
        .map(|i| PyValue::str(&format!("id{}", i % 1000)))
        .collect();
    items.push((PyValue::str("named"), PyValue::List(named)));
    dict(items)
}

#[test]
fn a_large_relation_reads_and_checks() {
    let v = relation(200_000);
    let a = parse_answers(Some(&v)).unwrap().unwrap();
    assert_eq!(a.relation.len(), 100_001);
    assert_eq!(a.named.as_ref().unwrap().len(), 1000);
    let roster: HashSet<PyStr> = a.relation.iter().map(|(i, _)| i.clone()).collect();
    assert!(check_named(&a, &roster, &HashSet::new(), true).is_err());
    assert!(check_answered_ids(&a, &[]).is_err());
}

#[test]
fn relation_grows_linearly() {
    let (small, large) = (relation(20_000), relation(200_000));
    timing::grows_within(
        "parse_answers: 20,000 and 200,000 relation ids",
        timing::TENFOLD,
        &mut || timing::once(|| parse_answers(Some(&small))),
        &mut || timing::once(|| parse_answers(Some(&large))),
    );
}

fn unknown(n: usize) -> PyValue {
    dict(
        (0..n)
            .map(|i| (PyValue::str(&format!("k{i}")), PyValue::None))
            .collect(),
    )
}

#[test]
fn many_unknown_keys_grow_linearly() {
    let (small, large) = (unknown(20_000), unknown(200_000));
    timing::grows_within(
        "parse_answers: 20,000 and 200,000 unknown keys",
        timing::TENFOLD,
        &mut || timing::once(|| parse_answers(Some(&small))),
        &mut || timing::once(|| parse_answers(Some(&large))),
    );
}

#[test]
fn a_said_of_spaces_and_words() {
    let mut items = zeros();
    let said: PyStr = unicode::cps(&" a\u{3000}".repeat(1_000_000));
    items.push((PyValue::str("said"), PyValue::Str(said)));
    let a = parse_answers(Some(&dict(items))).unwrap().unwrap();
    assert_eq!(a.said.unwrap().len(), 2 * 1_000_000 - 1);
    let mut items = zeros();
    items.push((PyValue::str("said"), PyValue::Str(vec![0x20; 3_000_000])));
    assert!(matches!(
        parse_answers(Some(&dict(items))),
        Err(AnswerFault::Teach(_))
    ));
}

/// A review_id nested `depth` deep.
fn deep(depth: usize) -> PyValue {
    let mut v = PyValue::None;
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    dict(vec![(PyValue::str("review_id"), v)])
}

#[test]
fn a_deep_review_id_is_shown_or_a_recursion_error_never_a_crash() {
    // CPython 3.13's repr of a list 5,000 deep answers (its C recursion
    // guard, not the 1,000 frame limit, bounds it), so the teaching error
    // is raised; past `json::MAX_DEPTH` the repr is RecursionError.
    let m = match parse_answers(Some(&deep(5_000))) {
        Err(AnswerFault::Teach(m)) => m,
        other => panic!("{other:?}"),
    };
    assert!(m.len() > 10_000);
    assert_eq!(
        parse_answers(Some(&deep(20_000))),
        Err(AnswerFault::Py(PyError::RecursionError))
    );
}

#[test]
fn ints_at_the_float_bounds() {
    let score = |text: String| {
        let mut items = zeros();
        items[0].1 = PyValue::Int(text);
        parse_answers(Some(&dict(items)))
    };
    // 2**1024 - 2**970 rounds up to 2**1024 and overflows; one less fits.
    let max = "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497792";
    let below = "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497791";
    assert_eq!(
        score(max.to_owned()),
        Err(AnswerFault::Py(PyError::OverflowError))
    );
    assert!(matches!(
        score(below.to_owned()),
        Err(AnswerFault::Teach(_))
    ));
    assert!(matches!(
        score("-".to_owned() + &"9".repeat(5000)),
        Err(AnswerFault::Py(PyError::OverflowError))
    ));
    assert!(matches!(score("-1".to_owned()), Err(AnswerFault::Teach(_))));
    assert_eq!(score("1".to_owned()).unwrap().unwrap().r#unsafe, 1.0);
}
