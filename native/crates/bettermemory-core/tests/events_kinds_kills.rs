//! Cases a plausible wrong `events` fails: `is True` read as truthiness,
//! a NaN or a path read as false, a near-miss kind matched, the raise on a
//! key that is not a `str` moved or dropped, the redaction applied where
//! the reference does not, items of a list kept that are not dicts.

use bettermemory_core::events as ev;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn audit(kind: &str, fields: Vec<(PyValue, PyValue)>) -> Result<bool, ev::EventsError> {
    ev::is_audit_row(&cps(kind), &fields)
}

fn no_redact(_: &[u32]) -> PyValue {
    panic!("redact called")
}

#[test]
fn forced_must_be_the_bool_true() {
    assert_eq!(
        audit("write", vec![(s("forced"), PyValue::int(1))]),
        Ok(false)
    );
    assert_eq!(
        audit("write", vec![(s("forced"), PyValue::Bool(true))]),
        Ok(true)
    );
    assert_eq!(
        audit("write", vec![(s("x_acknowledged"), PyValue::int(1))]),
        Ok(true)
    );
}

#[test]
fn truthiness_is_pythons() {
    let ack = |v: PyValue| audit("update", vec![(s("a_acknowledged"), v)]);
    assert_eq!(ack(PyValue::Float(f64::NAN)), Ok(true));
    assert_eq!(ack(PyValue::Float(-0.0)), Ok(false));
    assert_eq!(ack(PyValue::Path(cps(""))), Ok(true));
    assert_eq!(ack(PyValue::Tuple(vec![])), Ok(false));
    assert_eq!(ack(PyValue::Int("-0".into())), Ok(true));
    assert_eq!(ack(PyValue::int(0)), Ok(false));
}

#[test]
fn kinds_match_exactly() {
    let f = || vec![(s("forced"), PyValue::Bool(true))];
    assert_eq!(audit("Write", f()), Ok(false));
    assert_eq!(audit("write ", f()), Ok(false));
    assert_eq!(audit("standing_guard", vec![]), Ok(true));
    assert_eq!(ev::row_class(&cps("show ")), ev::RowClass::Measurement);
    assert!(!ev::prunable(&cps("episode_write")));
    assert!(ev::prunable(&cps("remove")));
}

#[test]
fn status_is_read_before_the_keys() {
    let int_key = (PyValue::int(1), PyValue::Bool(true));
    assert_eq!(
        audit("write", vec![int_key.clone()]),
        Err(ev::EventsError::AttributeError)
    );
    assert_eq!(
        audit(
            "write",
            vec![(s("status"), s("duplicate")), int_key.clone()]
        ),
        Ok(true)
    );
    assert_eq!(
        audit(
            "write",
            vec![(s("b_acknowledged"), PyValue::Bool(true)), int_key.clone()]
        ),
        Ok(true)
    );
    assert_eq!(
        audit(
            "write",
            vec![(s("b_acknowledged"), PyValue::Bool(false)), int_key.clone()]
        ),
        Err(ev::EventsError::AttributeError)
    );
    assert_eq!(audit("show", vec![int_key]), Ok(false));
    assert_eq!(audit("write", vec![(s("status"), s("stale"))]), Ok(false));
}

#[test]
fn state_fields_raise_only_with_evidence() {
    let fields = vec![(PyValue::int(7), s("x")), (s("id"), s("m"))];
    let got = ev::state_fields(&cps("show"), &fields, &mut no_redact).unwrap();
    assert!(got == vec![(s("id"), s("m"))]);
    assert_eq!(
        ev::state_fields(&cps("write"), &fields, &mut no_redact),
        Err(ev::EventsError::AttributeError)
    );
}

#[test]
fn redaction_only_for_a_kept_text() {
    let search = vec![(s("query"), s("q")), (s("returned"), s("r"))];
    let got = ev::state_fields(&cps("search"), &search, &mut no_redact).unwrap();
    assert!(got == vec![(s("returned"), s("r"))]);
    let dict = PyValue::Dict(vec![(s("hash"), s("h")), (s("preview"), s("p"))]);
    let got = ev::state_fields(
        &cps("turn_audited"),
        &[(s("probe_query"), dict)],
        &mut no_redact,
    )
    .unwrap();
    assert!(got == vec![(s("probe_query"), PyValue::Dict(vec![(s("hash"), s("h"))]))]);
    let mut called = 0;
    let mut redact = |_: &[u32]| {
        called += 1;
        PyValue::Dict(vec![(s("len"), PyValue::int(1)), (s("preview"), s("p"))])
    };
    let got = ev::state_fields(
        &cps("turn_audited"),
        &[(s("probe_query"), s("q"))],
        &mut redact,
    )
    .unwrap();
    assert_eq!(called, 1);
    assert!(
        got == vec![(
            s("probe_query"),
            PyValue::Dict(vec![(s("len"), PyValue::int(1))])
        )]
    );
}

#[test]
fn keys_of_shapes() {
    let keys: &[&str] = &["id"];
    assert!(ev::keys_of(&PyValue::Tuple(vec![]), keys) == PyValue::None);
    assert!(ev::keys_of(&s("id"), keys) == PyValue::None);
    let list = PyValue::List(vec![
        PyValue::Dict(vec![(s("id"), s("a")), (s("x"), s("b"))]),
        s("m"),
        PyValue::List(vec![]),
        PyValue::Dict(vec![(PyValue::int(1), s("c"))]),
    ]);
    let want = PyValue::List(vec![
        PyValue::Dict(vec![(s("id"), s("a"))]),
        PyValue::Dict(vec![]),
    ]);
    assert!(ev::keys_of(&list, keys) == want);
}

#[test]
fn output_keeps_the_input_order() {
    let fields = vec![
        (s("worktree_root"), s("w")),
        (s("z_acknowledged"), PyValue::None),
        (s("id"), s("m")),
        (s("attribution"), s("a")),
    ];
    let got = ev::state_fields(&cps("write"), &fields, &mut no_redact).unwrap();
    assert!(got == fields);
}

#[test]
fn status_must_be_a_str() {
    for kind in ["write", "update"] {
        assert_eq!(
            audit(kind, vec![(s("status"), PyValue::Path(cps("duplicate")))]),
            Ok(false)
        );
        assert_eq!(audit(kind, vec![(s("status"), s("duplicate"))]), Ok(true));
    }
}

#[test]
fn a_repeated_key_reads_its_last_value() {
    assert_eq!(
        audit(
            "write",
            vec![(s("status"), s("ok")), (s("status"), s("duplicate"))]
        ),
        Ok(true)
    );
    assert_eq!(
        audit(
            "write",
            vec![(s("status"), s("duplicate")), (s("status"), s("ok"))]
        ),
        Ok(false)
    );
}
