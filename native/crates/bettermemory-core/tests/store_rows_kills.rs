//! Cases a plausible wrong `store::rows` fails: a descending sort that
//! reverses ties, nulls on the wrong end, one leading `-` stripped, a
//! TypeError naming its types the other way round, an int and a float
//! compared through a float, a refusal for a missing column raised after
//! a comparison, a payload's `ts` overriding the row's, a session of None
//! written, and the kind sets.

mod common;
#[path = "common/store_rows_leaves.rs"]
mod leaves;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::rows::Row;
use bettermemory_core::records::RecordError;
use bettermemory_core::store::rows::{
    self, event_from_columns, membership_column, membership_spec, sort_rows, Fail,
};
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode;

fn rows_of(values: &[SqlValue]) -> Vec<Row> {
    values
        .iter()
        .enumerate()
        .map(|(i, v)| Row {
            columns: vec![
                ("a".into(), v.clone()),
                ("n".into(), SqlValue::Integer(i as i64)),
            ],
        })
        .collect()
}

fn order(rows: &[Row]) -> Vec<i64> {
    rows.iter()
        .map(|r| match r.get("n") {
            Some(SqlValue::Integer(n)) => *n,
            _ => panic!(),
        })
        .collect()
}

fn sorted(values: &[SqlValue], by: &[&str]) -> Result<Vec<i64>, Fail> {
    let mut rows = rows_of(values);
    sort_rows(&mut rows, by).map(|()| order(&rows))
}

use SqlValue::{Integer as I, Null, Real as R, Text as T};

#[test]
fn descending_keeps_ties_in_order() {
    assert_eq!(
        sorted(&[I(1), I(2), I(1), I(2)], &["-a"]),
        Ok(vec![1, 3, 0, 2])
    );
}

#[test]
fn nulls_first_ascending_last_descending() {
    assert_eq!(sorted(&[I(1), Null, I(0)], &["a"]), Ok(vec![1, 2, 0]));
    assert_eq!(
        sorted(&[I(1), Null, I(0), Null], &["-a"]),
        Ok(vec![0, 2, 1, 3])
    );
    // A null sorts below a zero: the key is (False, 0) against (True, 0).
    assert_eq!(sorted(&[I(0), Null], &["a"]), Ok(vec![1, 0]));
}

#[test]
fn every_leading_dash_is_stripped() {
    assert_eq!(sorted(&[I(1), I(2)], &["---a"]), Ok(vec![1, 0]));
}

#[test]
fn type_error_names_the_types_as_cpython_compares() {
    let msg = |a: &str, b: &str| {
        Err(Fail::Caught(
            "TypeError",
            format!("'<' not supported between instances of '{a}' and '{b}'"),
        ))
    };
    assert_eq!(sorted(&[I(1), T("x".into())], &["a"]), msg("str", "int"));
    assert_eq!(sorted(&[T("x".into()), I(1)], &["a"]), msg("int", "str"));
    // Reversed first, then sorted: the other order again.
    assert_eq!(sorted(&[I(1), T("x".into())], &["-a"]), msg("int", "str"));
}

#[test]
fn int_and_float_compare_exactly() {
    let big = 1i64 << 53;
    assert_eq!(
        sorted(&[I(big + 1), R(big as f64), I(big)], &["a"]),
        Ok(vec![1, 2, 0])
    );
    assert_eq!(
        sorted(&[I(i64::MAX), R(9.223_372_036_854_776e18)], &["a"]),
        Ok(vec![0, 1])
    );
    assert_eq!(sorted(&[R(-0.5), I(-1), I(0)], &["a"]), Ok(vec![1, 0, 2]));
}

#[test]
fn a_missing_column_is_key_error_before_any_comparison() {
    let mut rows = rows_of(&[I(1), T("x".into())]);
    rows.push(Row { columns: vec![] });
    assert_eq!(
        sort_rows(&mut rows, &["a"]),
        Err(Fail::Caught("KeyError", "'a'".into()))
    );
}

#[test]
fn the_first_name_decides_first() {
    let mut rows: Vec<Row> = [(1, 2), (0, 9), (1, 1)]
        .iter()
        .enumerate()
        .map(|(i, (a, b))| Row {
            columns: vec![
                ("a".into(), I(*a)),
                ("b".into(), I(*b)),
                ("n".into(), I(i as i64)),
            ],
        })
        .collect();
    sort_rows(&mut rows, &["a", "b"]).unwrap();
    assert_eq!(order(&rows), vec![1, 2, 0]);
}

#[test]
fn spec_refusals_quote_as_repr() {
    assert_eq!(
        membership_spec(&unicode::cps("x'y")).err(),
        Some(Fail::Caught(
            "ValueError",
            "\"x'y\" is not a membership row kind".into()
        ))
    );
    let spec = membership_spec(&unicode::cps("machine")).unwrap();
    assert_eq!(spec.table, "machines");
    assert!(membership_column(spec, &unicode::cps("minted")).is_ok());
    assert_eq!(
        membership_column(spec, &unicode::cps("Minted")),
        Err(Fail::Caught(
            "ValueError",
            "machines has no column 'Minted'".into()
        ))
    );
}

fn event(session: Option<&str>, kind: &str, payload: &str) -> Result<Option<PyValue>, RecordError> {
    let session = session.map(unicode::cps);
    event_from_columns(
        &unicode::cps("T"),
        session.as_deref(),
        &unicode::cps(kind),
        &T(payload.into()),
    )
}

fn keys(v: &PyValue) -> Vec<String> {
    leaves::dict_keys(v)
}

#[test]
fn an_event_keeps_the_row_columns() {
    let e = event(
        Some("S"),
        "recall",
        r#"{"ts": "X", "z": 1, "kind": "k", "a": 2}"#,
    )
    .unwrap()
    .unwrap();
    assert_eq!(keys(&e), ["ts", "session", "kind", "z", "a"]);
    assert_eq!(e.get("ts"), Some(&PyValue::str("T")));
    let e = event(None, "recall", "{}").unwrap().unwrap();
    assert_eq!(keys(&e), ["ts", "kind"]);
}

#[test]
fn not_telemetry_is_none_before_the_payload_is_read() {
    for kind in [
        "memory_put",
        "native_member_delete",
        "rekey",
        "key_adopted",
        "store_forked",
    ] {
        assert_eq!(
            event_from_columns(&[], None, &unicode::cps(kind), &Null),
            Ok(None),
            "{kind}"
        );
    }
    assert_eq!(
        event_from_columns(&[], None, &unicode::cps("recall"), &Null),
        Err(RecordError::Raised("TypeError"))
    );
    assert_eq!(event(None, "recall", "{"), Ok(None));
    assert_eq!(event(None, "recall", "[]"), Ok(None));
}

#[test]
fn kind_sets() {
    assert!(rows::is_mutation("checkout_view_put"));
    assert!(!rows::is_mutation("memory_putt"));
    assert!(!rows::is_mutation("_put"));
    assert!(!rows::is_mutation("memories_put"));
    assert!(rows::is_membership_mutation("home_delete"));
    assert!(!rows::is_membership_mutation("memory_delete"));
    assert!(rows::not_telemetry("history_accepted"));
    assert!(!rows::not_telemetry("recall"));
    assert!(!rows::not_telemetry("store_restored_x"));
}

#[test]
fn upsert_leaves_the_key_out_of_the_update() {
    assert_eq!(
        rows::upsert("t", &["a", "k", "b"], "k"),
        "INSERT INTO t(a, k, b) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET \
         a = excluded.a, b = excluded.b"
    );
}

/// A float of exactly 2**63 is above every int64; a bound of `>` would
/// saturate it to i64::MAX and call the two equal.
#[test]
fn two_to_the_63_sorts_above_i64_max() {
    assert_eq!(
        sorted(&[R(9_223_372_036_854_775_808.0), I(i64::MAX)], &["a"]),
        Ok(vec![1, 0])
    );
    assert_eq!(
        sorted(&[I(i64::MIN), R(-9_223_372_036_854_775_808.0)], &["a"]),
        Ok(vec![0, 1])
    );
}

/// `_membership_values` takes a float only in a float column and only
/// when it is finite (the reference's words for inf and NaN).
#[test]
fn membership_floats_must_be_finite() {
    let spec = membership_spec(&unicode::cps("standing")).unwrap();
    let row = |p: PyValue| {
        PyValue::Dict(
            spec.columns
                .iter()
                .map(|c| {
                    let v = if *c == "probability" {
                        p.clone()
                    } else {
                        PyValue::Str(unicode::cps("x"))
                    };
                    (PyValue::Str(unicode::cps(c)), v)
                })
                .collect(),
        )
    };
    for bad in [f64::INFINITY, f64::NEG_INFINITY, f64::NAN] {
        assert_eq!(
            rows::membership_values(spec, &row(PyValue::Float(bad))).err(),
            Some(Fail::Caught(
                "TypeError",
                "standing.probability must be text, an integer or null".into()
            ))
        );
    }
    assert!(rows::membership_values(spec, &row(PyValue::Float(0.5))).is_ok());
}
