//! Cases a plausible wrong reading of `review_counters`' rows fails: each
//! puts a row into the wrong action, bucket, chain or verdict.

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

fn write(extra: Vec<(&str, PyValue)>) -> PyValue {
    let mut pairs = vec![
        ("ts", s("2026-11-21T10:00:00Z")),
        ("kind", s("write")),
        ("review_id", s("w1")),
    ];
    pairs.extend(extra);
    dict(pairs)
}

fn decided(ev: &PyValue) -> DecidedRow {
    DecidedRow::from_event(ev)
        .expect("no raise")
        .expect("a row")
}

#[test]
fn an_empty_review_action_falls_to_the_status_map() {
    let row = decided(&write(vec![
        ("status", s("credential_warning")),
        ("review_action", s("")),
    ]));
    assert_eq!(row.action, cps("refused_secret"));
    let row = decided(&write(vec![
        ("status", s("other")),
        ("review_action", s("")),
    ]));
    assert_eq!(row.action, cps("other"));
    let row = decided(&write(vec![
        ("status", s("duplicate")),
        ("review_action", PyValue::int(1)),
    ]));
    assert_eq!(row.action, cps("restates"));
    assert!(!row.committed());
}

#[test]
fn a_bool_score_is_none_and_breaks_the_zero_attestation() {
    let answers = dict(vec![
        ("unsafe", PyValue::Bool(false)),
        ("secret", PyValue::int(0)),
    ]);
    let row = decided(&write(vec![
        ("status", s("committed")),
        ("review_answers", answers),
    ]));
    assert_eq!(row.r#unsafe, None);
    assert_eq!(row.secret, Some(0.0));
    assert!(!row.zero_attestation());
    assert_eq!(score(Some(&PyValue::Bool(true))), Ok(None));
}

#[test]
fn negative_zero_attests_and_nan_does_not() {
    let answers = |u: f64| {
        dict(vec![
            ("unsafe", PyValue::Float(u)),
            ("secret", PyValue::Float(0.0)),
        ])
    };
    let row = decided(&write(vec![
        ("status", s("committed")),
        ("review_answers", answers(-0.0)),
    ]));
    assert!(row.zero_attestation());
    let row = decided(&write(vec![
        ("status", s("committed")),
        ("review_answers", answers(f64::NAN)),
    ]));
    assert!(!row.zero_attestation());
}

#[test]
fn a_huge_int_score_raises_rather_than_reading_as_inf() {
    let big = PyValue::Int(format!("1{}", "0".repeat(400)));
    let ev = write(vec![
        ("status", s("committed")),
        ("review_answers", dict(vec![("unsafe", big.clone())])),
    ]);
    assert_eq!(
        DecidedRow::from_event(&ev),
        Err(CountersError::OverflowError)
    );
    // 2**1024 - 2**970 overflows; one less rounds to the largest float.
    let edge = "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497792";
    assert_eq!(
        score(Some(&PyValue::Int(edge.into()))),
        Err(CountersError::OverflowError)
    );
    let below = "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497791";
    assert_eq!(score(Some(&PyValue::Int(below.into()))), Ok(Some(f64::MAX)));
    // The guards come first: no status, no raise.
    let ev = dict(vec![
        ("ts", s("2026-11-21T10:00:00Z")),
        ("review_id", s("w1")),
        ("review_answers", dict(vec![("unsafe", big)])),
    ]);
    assert_eq!(DecidedRow::from_event(&ev), Ok(None));
}

#[test]
fn an_int_verdict_is_not_a_grade() {
    let grade = |ra: PyValue| {
        dict(vec![
            ("ts", s("2026-11-21T10:00:00Z")),
            ("review_id", s("g1")),
            ("right_answers", ra),
            ("right_action", PyValue::Bool(true)),
            ("pre_answered", PyValue::int(1)),
        ])
    };
    assert_eq!(GradeRow::from_event(&grade(PyValue::int(1))), Ok(None));
    let row = GradeRow::from_event(&grade(PyValue::Bool(false)))
        .unwrap()
        .unwrap();
    assert!(!row.right_answers && row.right_action);
    assert!(!row.pre_answered, "pre_answered 1 is not True");
}

#[test]
fn the_rows_own_field_comes_before_the_actors() {
    let ev = dict(vec![
        ("client", s("row")),
        ("session", s("row")),
        (
            "actor",
            dict(vec![("client", s("actor")), ("session", s("actor"))]),
        ),
    ]);
    assert_eq!(bucket_value(&ev, "client"), Ok(cps("row")));
    // The session reads the other way round: the actor's first.
    assert_eq!(session_of(&ev), Ok(Some(cps("actor"))));
    let ev = dict(vec![
        ("client", s("")),
        ("actor", dict(vec![("client", s("actor"))])),
    ]);
    assert_eq!(bucket_value(&ev, "client"), Ok(cps("actor")));
    let ev = dict(vec![("actor", PyValue::List(vec![s("client")]))]);
    assert_eq!(bucket_value(&ev, "model"), Ok(cps("none")));
}

#[test]
fn a_false_or_zero_door_is_present() {
    for door in [PyValue::Bool(false), PyValue::int(0), s("")] {
        let ev = dict(vec![
            ("ts", s("2026-11-21T10:00:00Z")),
            ("review_id", s("r1")),
            ("door", door),
        ]);
        assert_eq!(IssuedRow::from_event(&ev), Ok(None));
    }
    let ev = dict(vec![
        ("ts", s("2026-11-21T10:00:00Z")),
        ("review_id", s("r1")),
        ("door", PyValue::None),
        ("response_chars", PyValue::Bool(true)),
        ("pre_answered", PyValue::int(1)),
    ]);
    let row = IssuedRow::from_event(&ev).unwrap().unwrap();
    assert_eq!(row.response_chars, None);
    assert!(!row.pre_answered);
    assert_eq!(row.chain_key(), &cps("r1"));
}

#[test]
fn a_list_kind_answers_false_and_an_empty_review_id_counts() {
    let ev = dict(vec![("kind", PyValue::List(vec![s("review_issued")]))]);
    assert_eq!(is_review_row(&ev), Ok(false));
    let ev = dict(vec![("kind", s("write")), ("review_id", s(""))]);
    assert_eq!(is_review_row(&ev), Ok(true));
    let ev = dict(vec![("kind", s("write")), ("review_id", PyValue::int(1))]);
    assert_eq!(is_review_row(&ev), Ok(false));
    assert_eq!(
        is_review_row(&PyValue::List(vec![])),
        Err(CountersError::AttributeError("list"))
    );
}

#[test]
fn a_tuple_of_ids_reads_as_none() {
    let ev = write(vec![
        ("status", s("committed")),
        ("matches", PyValue::Tuple(vec![s("a")])),
        (
            "credential_kinds",
            PyValue::List(vec![s("a"), s(""), PyValue::int(2), s("b")]),
        ),
    ]);
    let row = decided(&ev);
    assert!(row.matches.is_empty());
    assert_eq!(row.credential_kinds, vec![cps("a"), cps("b")]);
}

#[test]
fn a_repeated_relation_key_keeps_its_first_place_and_last_value() {
    let relation = PyValue::Dict(vec![
        (s("a"), s("update")),
        (s("b"), s("unrelated")),
        (s("a"), s("unrelated")),
        (PyValue::int(1), s("update")),
    ]);
    let answers = dict(vec![
        ("relation", relation),
        ("unsafe", PyValue::int(0)),
        ("secret", PyValue::int(0)),
    ]);
    let row = decided(&write(vec![
        ("status", s("committed")),
        ("review_answers", answers),
    ]));
    assert_eq!(
        row.relation,
        vec![(cps("a"), cps("unrelated")), (cps("b"), cps("unrelated"))]
    );
    assert!(row.zero_attestation());
}

#[test]
fn one_label_not_unrelated_breaks_the_zero_attestation() {
    let answers = dict(vec![
        (
            "relation",
            dict(vec![("a", s("update")), ("b", s("unrelated"))]),
        ),
        ("unsafe", PyValue::int(0)),
        ("secret", PyValue::int(0)),
    ]);
    let row = decided(&write(vec![
        ("status", s("committed")),
        ("review_answers", answers),
    ]));
    assert!(!row.zero_attestation());
    // An int zero scores +0.0, as float(0) does.
    assert_eq!(row.r#unsafe.map(f64::to_bits), Some(0.0f64.to_bits()));
    assert_eq!(
        score(Some(&PyValue::Int("-0".into()))).map(|x| x.map(f64::to_bits)),
        Ok(Some(0.0f64.to_bits()))
    );
}
