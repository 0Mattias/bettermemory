//! `location_witness` over hostile answers: ids nested past `str()`'s
//! depth, very long lists, many exchanges, extreme clocks. No panic, and
//! the readers are built in time that grows with the answers as the
//! reference's does (ten times the ids under sixty times the time).

mod timing;

use bettermemory_core::location_witness::{
    independent_answer, AnsweredGit, AnsweredHistory, ExchangeReads, Exchanges, NeedWitnesses,
    EXCHANGES_CAP,
};
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::unicode::cps;

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn d(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect())
}

fn nested(depth: usize) -> PyValue {
    let mut v = PyValue::int(1);
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

#[test]
fn ids_nested_past_the_depth_raise_recursion_error() {
    for key in ["asked", "held", "ancestry_of"] {
        let a = d(vec![(key, PyValue::List(vec![nested(20_000)]))]);
        assert_eq!(
            AnsweredHistory::new(&a).unwrap_err(),
            PyError::RecursionError,
            "{key}"
        );
    }
    let a = d(vec![(
        "independent",
        PyValue::List(vec![d(vec![("of", PyValue::List(vec![nested(20_000)]))])]),
    )]);
    assert_eq!(
        AnsweredGit::new(&a, 256).unwrap_err(),
        PyError::RecursionError
    );
    let q = d(vec![("independent", PyValue::List(vec![nested(20_000)]))]);
    assert_eq!(
        independent_answer(&q, &d(vec![]), None).unwrap_err(),
        PyError::RecursionError
    );
    // A shallow nesting is spelled.
    let a = d(vec![("asked", PyValue::List(vec![nested(50)]))]);
    assert!(AnsweredHistory::new(&a).is_ok());
}

#[test]
fn joined_of_many_questions_does_not_panic() {
    let needs: Vec<NeedWitnesses> = (0..2_000)
        .map(|i| NeedWitnesses {
            question: d(vec![
                ("held", PyValue::List(vec![s(&format!("{i}")), s("x")])),
                ("walk", PyValue::Bool(i % 2 == 0)),
                ("deep", PyValue::List(vec![nested(5_000)])),
            ]),
        })
        .collect();
    let j = NeedWitnesses::joined(&needs).unwrap();
    let PyValue::Dict(items) = &j.question else {
        panic!()
    };
    let PyValue::List(held) = &items[0].1 else {
        panic!()
    };
    assert_eq!(held.len(), 2_001);
    assert_eq!(items[1].1, PyValue::Bool(true));
}

#[test]
fn many_exchanges_and_extreme_clocks() {
    let mut x = Exchanges::new();
    let a = d(vec![("exchange", PyValue::None)]);
    for (i, now) in [0.0, f64::MAX, f64::MIN, f64::INFINITY, f64::NAN, -0.0]
        .iter()
        .cycle()
        .take(3_000)
        .enumerate()
    {
        let f = (i as u128).to_be_bytes();
        let mut e = ExchangeReads::new(&a, None, cps("m"), &mut x, *now, f, 256).unwrap();
        e.set_read(cps("f"), None);
        e.carried(&mut x, *now);
        assert!(x.entries().len() <= EXCHANGES_CAP);
    }
}

#[test]
fn reading_answers_is_linear() {
    let answers = |n: usize| {
        let ids: Vec<PyValue> = (0..n).map(|i| s(&format!("{i:040X}"))).collect();
        d(vec![
            ("asked", PyValue::List(ids.clone())),
            ("held", PyValue::List(ids.clone())),
            ("ancestry_of", PyValue::List(ids)),
        ])
    };
    let (small, large) = (answers(5_000), answers(50_000));
    timing::grows_within(
        "AnsweredHistory: 5,000 and 50,000 ids",
        timing::TENFOLD,
        &mut || timing::once(|| AnsweredHistory::new(&small).unwrap()),
        &mut || timing::once(|| AnsweredHistory::new(&large).unwrap()),
    );
}

#[test]
fn reading_independent_answers_is_linear() {
    let answers = |n: usize| {
        let entries: Vec<PyValue> = (0..n)
            .map(|i| {
                d(vec![
                    (
                        "of",
                        PyValue::List(vec![s(&format!("{i:040x}")), s(&format!("{:040x}", i + 1))]),
                    ),
                    ("top", PyValue::List(vec![s(&format!("{i:040x}"))])),
                ])
            })
            .collect();
        d(vec![("independent", PyValue::List(entries))])
    };
    let (small, large) = (answers(2_000), answers(20_000));
    timing::grows_within(
        "AnsweredGit: 2,000 and 20,000 independent entries",
        timing::TENFOLD,
        &mut || timing::once(|| AnsweredGit::new(&small, 256).unwrap()),
        &mut || timing::once(|| AnsweredGit::new(&large, 256).unwrap()),
    );
}

#[test]
fn joining_distinct_keys_is_linear() {
    let needs = |n: usize| -> Vec<NeedWitnesses> {
        (0..n)
            .map(|i| NeedWitnesses {
                question: d(vec![(
                    format!("k{i}").as_str(),
                    PyValue::List(vec![PyValue::int(i as i128)]),
                )]),
            })
            .collect()
    };
    let (small, large) = (needs(2_000), needs(20_000));
    timing::grows_within(
        "NeedWitnesses::joined: 2,000 and 20,000 distinct keys",
        timing::TENFOLD,
        &mut || timing::once(|| NeedWitnesses::joined(&small).unwrap()),
        &mut || timing::once(|| NeedWitnesses::joined(&large).unwrap()),
    );
    let j = NeedWitnesses::joined(&large).unwrap();
    let PyValue::Dict(items) = &j.question else {
        panic!()
    };
    assert_eq!(items.len(), 20_000);
    assert_eq!(items[19_999].0, s("k19999"));
}
