//! Hostile rows for `membership_rows`: very large, deeply nested and at
//! the integer bounds. No panic in debug or release; the dict readers and
//! `_jsonable` grow linearly, as the reference's dicts do.

mod common;
mod timing;

use bettermemory_core::membership_rows as rows;
use bettermemory_core::pyvalue::PyValue;
use common::membership_rows::*;

fn base(f: &str) -> PyValue {
    let all = lines("membership_rows.jsonl");
    dec(field(field(&all[0], "bases"), f))
}

fn with(mut row: PyValue, col: &str, v: PyValue) -> PyValue {
    if let PyValue::Dict(items) = &mut row {
        items
            .iter_mut()
            .find(|(c, _)| *c == PyValue::str(col))
            .unwrap()
            .1 = v;
    }
    row
}

fn deep(n: usize) -> PyValue {
    let mut v = PyValue::int(1);
    for _ in 0..n {
        v = PyValue::List(vec![v]);
    }
    v
}

#[test]
fn deep_values_answer_without_a_panic() {
    let row = with(
        base("proposal"),
        "evidence_json",
        PyValue::Dict(vec![(PyValue::str("k"), deep(100_000))]),
    );
    let pr = rows::proposal_from(&row).unwrap();
    assert_eq!(rows::proposal_row(&pr).unwrap_err().class, "RecursionError");
    let row = with(base("checkout"), "id", deep(100_000));
    assert_eq!(
        rows::checkout_from(&row, &[]).unwrap_err().class,
        "RecursionError"
    );
    let row = with(
        base("checkout"),
        "stamps_json",
        PyValue::List(vec![deep(100_000)]),
    );
    assert!(rows::checkout_from(&row, &[]).is_ok());
}

#[test]
fn integer_bounds() {
    for (text, ok) in [
        ("9223372036854775807", true),
        ("-9223372036854775808", true),
        ("9223372036854775808", false),
        ("-9223372036854775809", false),
    ] {
        let row = with(base("view"), "seen_order", PyValue::Int(text.into()));
        assert_eq!(rows::view_from(&row).is_ok(), ok, "{text}");
    }
    let huge = PyValue::Str(vec![u32::from(b'9'); 100_000]);
    let row = with(base("view"), "seen_order", huge);
    assert_eq!(rows::view_from(&row).unwrap_err().class, "ValueError");
}

fn evidence(n: usize) -> PyValue {
    let pairs = (0..n)
        .map(|k| {
            PyValue::List(vec![
                PyValue::int(k as i128 % (n as i128 / 2)),
                PyValue::str("v"),
            ])
        })
        .collect();
    with(base("proposal"), "evidence_json", PyValue::List(pairs))
}

#[test]
fn dict_and_jsonable_grow_linearly() {
    let (small, large) = (evidence(2_000), evidence(20_000));
    timing::grows_within(
        "evidence: 2,000 and 20,000 pairs",
        timing::TENFOLD,
        &mut || timing::once(|| rows::proposal_row(&rows::proposal_from(&small).unwrap()).unwrap()),
        &mut || timing::once(|| rows::proposal_row(&rows::proposal_from(&large).unwrap()).unwrap()),
    );
}

#[test]
fn deeply_nested_tuple_keys_answer_without_a_panic() {
    // The reference's dict() hashes a key 50,000 tuples deep.
    let mut k = PyValue::int(1);
    for _ in 0..50_000 {
        k = PyValue::Tuple(vec![k]);
    }
    let pairs = PyValue::List(vec![PyValue::Tuple(vec![k, PyValue::int(2)])]);
    let row = with(base("proposal"), "evidence_json", pairs);
    assert!(rows::proposal_from(&row).is_ok());
}

#[test]
fn jsonable_depth_bound_is_the_references() {
    // JSONABLE_DEPTH (900) nested containers fit; one more does not.
    assert_eq!(rows::JSONABLE_DEPTH, 900);
    assert!(rows::jsonable(&deep(900)).is_ok());
    assert_eq!(
        rows::jsonable(&deep(901)).unwrap_err().class,
        "RecursionError"
    );
}
