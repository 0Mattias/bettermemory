//! Cases a plausible wrong reading of the review's answers fails: a bool
//! read as a number, JSON null read as an object, the order of the
//! checks, the dedup orders, the whitespace `said` folds past ASCII and
//! the float conversion of a large int.

use std::collections::HashSet;

use bettermemory_core::json;
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::review::*;
use bettermemory_core::unicode;

fn raw(text: &str) -> PyValue {
    json::loads(&unicode::cps(text)).expect("json")
}

fn p(text: &str) -> Result<Option<ReviewAnswers>, AnswerFault> {
    parse_answers(Some(&raw(text)))
}

fn teach(r: Result<Option<ReviewAnswers>, AnswerFault>) -> String {
    match r {
        Err(AnswerFault::Teach(m)) => unicode::to_string(&m).expect("text"),
        other => panic!("not a teaching error: {other:?}"),
    }
}

fn ok(text: &str) -> ReviewAnswers {
    p(text).expect("read").expect("answers")
}

fn s(t: &str) -> PyStr {
    unicode::cps(t)
}

const ZEROS: &str = r#""unsafe": 0, "secret": 0, "standing": 0"#;

#[test]
fn a_bool_is_not_a_number() {
    let m = teach(p(r#"{"unsafe": true, "secret": 0, "standing": 0}"#));
    assert!(m.starts_with("review.unsafe must be a number from 0 to 1, got bool."));
}

#[test]
fn json_null_and_an_empty_object_are_a_first_call() {
    assert_eq!(parse_answers(Some(&PyValue::None)), Ok(None));
    assert_eq!(parse_update_answers(Some(&raw("{}"))), Ok(None));
    assert_eq!(parse_answers(None), Ok(None));
}

#[test]
fn a_tuple_is_not_a_mapping_but_names_may_be_a_tuple() {
    let r = parse_answers(Some(&PyValue::Tuple(vec![])));
    assert!(teach(r).starts_with("review must be an object."));
    let named = PyValue::Tuple(vec![
        PyValue::str("b"),
        PyValue::str("a"),
        PyValue::str("b"),
    ]);
    let v = PyValue::Dict(vec![(PyValue::str("named"), named)]);
    let a = parse_update_answers(Some(&v)).unwrap().unwrap();
    assert_eq!(a.named, Some(vec![s("b"), s("a")]));
}

#[test]
fn a_path_score_names_its_class() {
    let v = PyValue::Dict(vec![(PyValue::str("unsafe"), PyValue::Path(s("x")))]);
    let m = teach(parse_answers(Some(&v)));
    assert!(m.contains("got PurePosixPath."), "{m}");
}

#[test]
fn negative_zero_is_in_range_and_kept() {
    let a = ok(r#"{"unsafe": -0.0, "secret": 0, "standing": 0}"#);
    assert!(a.r#unsafe.is_sign_negative());
    assert!(a.is_zero_attestation());
}

#[test]
fn just_past_one_is_out_of_range() {
    let m = teach(p(
        r#"{"unsafe": 1.0000000000000002, "secret": 0, "standing": 0}"#,
    ));
    assert!(m.contains("got 1.0000000000000002."), "{m}");
}

#[test]
fn an_int_past_float_range_is_overflow_and_one_inside_is_shown_whole() {
    let big = format!(
        r#"{{"unsafe": 1{}, "secret": 0, "standing": 0}}"#,
        "0".repeat(400)
    );
    assert_eq!(p(&big), Err(AnswerFault::Py(PyError::OverflowError)));
    let inside = format!(
        r#"{{"unsafe": 1{}, "secret": 0, "standing": 0}}"#,
        "0".repeat(308)
    );
    let m = teach(p(&inside));
    assert!(m.contains(&format!("got 1{}.", "0".repeat(308))), "{m}");
}

#[test]
fn checks_run_in_the_reference_order() {
    // Unknown keys first, then review_id, relation, named, said, the scores.
    let m = teach(p(r#"{"zz": 1, "review_id": 5}"#));
    assert!(m.starts_with("review carries unknown keys ['zz']"));
    let m = teach(p(r#"{"review_id": 5, "relation": 1}"#));
    assert!(m.starts_with("review.review_id must be"));
    let m = teach(p(r#"{"relation": 1, "named": 1}"#));
    assert!(m.starts_with("review.relation must be an object"));
    let m = teach(p(r#"{"named": 1, "said": 1}"#));
    assert!(m.starts_with("review.named is a list"));
    let m = teach(p(r#"{"said": 1, "unsafe": "x"}"#));
    assert!(m.starts_with("review.said quotes"));
    let m = teach(p(r#"{"standing": "x", "secret": "x"}"#));
    assert!(m.starts_with("review.unsafe is missing"));
    let m = teach(p(r#"{"standing": "x", "secret": "x", "unsafe": 0}"#));
    assert!(m.starts_with("review.secret must be a number"));
}

#[test]
fn unknown_keys_sort_by_code_point() {
    let m = teach(p(r#"{"b": 1, "B": 1, "a": 1}"#));
    assert!(
        m.starts_with("review carries unknown keys ['B', 'a', 'b']."),
        "{m}"
    );
}

// json::loads folds a repeated key as a dict does, so these two cases
// build the Dict by hand to reach the folds of the readers themselves.
#[test]
fn a_hand_built_repeated_unknown_key_is_listed_once() {
    let v = PyValue::Dict(vec![
        (PyValue::str("x"), PyValue::None),
        (PyValue::str("x"), PyValue::None),
    ]);
    let m = teach(parse_answers(Some(&v)));
    assert!(m.starts_with("review carries unknown keys ['x']."), "{m}");
}

#[test]
fn a_hand_built_repeated_relation_id_keeps_its_last_label() {
    let rel = PyValue::Dict(vec![
        (PyValue::str("a"), PyValue::str("unrelated")),
        (PyValue::str("b"), PyValue::str("extends")),
        (PyValue::str("a"), PyValue::str("update")),
    ]);
    let v = PyValue::Dict(vec![
        (PyValue::str("relation"), rel),
        (PyValue::str("unsafe"), PyValue::int(0)),
        (PyValue::str("secret"), PyValue::int(0)),
        (PyValue::str("standing"), PyValue::int(0)),
    ]);
    let a = parse_answers(Some(&v)).expect("read").expect("answers");
    assert_eq!(
        a.relation,
        vec![(s("a"), s("update")), (s("b"), s("extends"))]
    );
}

#[test]
fn unknown_keys_of_mixed_types_refuse_to_sort() {
    let v = PyValue::Dict(vec![
        (PyValue::str("x"), PyValue::None),
        (PyValue::int(1), PyValue::None),
    ]);
    assert_eq!(
        parse_answers(Some(&v)),
        Err(AnswerFault::Py(PyError::TypeError))
    );
}

#[test]
fn a_repeated_relation_id_keeps_its_place_and_its_last_label() {
    let a = ok(&format!(
        r#"{{"relation": {{"a": "update", "b": "extends", "a": "unrelated"}}, {ZEROS}}}"#
    ));
    assert_eq!(
        a.relation,
        vec![(s("a"), s("unrelated")), (s("b"), s("extends"))]
    );
    assert!(!a.is_zero_attestation());
}

#[test]
fn named_keeps_first_appearance() {
    let a = ok(&format!(
        r#"{{"named": ["c", "a", "c", "b", "a"], {ZEROS}}}"#
    ));
    assert_eq!(a.named, Some(vec![s("c"), s("a"), s("b")]));
}

#[test]
fn said_folds_unicode_whitespace_but_not_a_zero_width_space() {
    let a = ok(&format!(r#"{{"said": "　a \u0085b​c ", {ZEROS}}}"#));
    assert_eq!(a.said, Some(unicode::cps("a b\u{200b}c")));
    let m = teach(p(&format!(r#"{{"said": "　 ", {ZEROS}}}"#)));
    assert!(m.starts_with("review.said quotes"));
}

#[test]
fn a_lowercase_ulid_is_refused() {
    let m = teach(p(r#"{"review_id": "01j00000000000000000000000"}"#));
    assert!(m.starts_with("review.review_id must be the review_id a review returned, got '01j"));
}

#[test]
fn update_reads_zero_scores_and_no_standing() {
    let a = parse_update_answers(Some(&raw(r#"{"relation": {"a": "unrelated"}}"#)))
        .unwrap()
        .unwrap();
    assert_eq!((a.r#unsafe, a.secret, a.standing), (0.0, 0.0, None));
    assert!(a.is_zero_attestation());
    let ev = json::dumps(&a.to_event(), json::DumpOptions::DEFAULT).unwrap();
    assert_eq!(
        unicode::to_string(&ev).unwrap(),
        r#"{"relation": {"a": "unrelated"}, "unsafe": 0.0, "secret": 0.0}"#
    );
}

#[test]
fn check_named_shown_ids_need_no_name_and_not_required_names_nothing() {
    let a = ok(&format!(r#"{{"relation": {{"a": "update"}}, {ZEROS}}}"#));
    let roster: HashSet<PyStr> = [s("a")].into();
    let shown: HashSet<PyStr> = [s("a")].into();
    assert_eq!(check_named(&a, &roster, &shown, false), Ok(vec![]));
    let m = match check_named(&a, &roster, &HashSet::new(), false) {
        Err(AnswerFault::Teach(m)) => unicode::to_string(&m).unwrap(),
        other => panic!("{other:?}"),
    };
    assert!(m.starts_with("review.relation answers roster ids not named: ['a']."));
}

#[test]
fn check_answered_ids_sorts_the_foreign_ids() {
    let a = ok(&format!(
        r#"{{"relation": {{"z": "update", "a": "update"}}, {ZEROS}}}"#
    ));
    let m = match check_answered_ids(&a, &[]) {
        Err(AnswerFault::Teach(m)) => unicode::to_string(&m).unwrap(),
        other => panic!("{other:?}"),
    };
    assert!(m.starts_with("review.relation names ids no review of this write showed: ['a', 'z']."));
}
