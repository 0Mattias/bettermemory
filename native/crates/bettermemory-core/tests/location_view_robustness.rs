//! Hostile input for `continuity`: many and long stamps, deep tuples,
//! integers past `i128`, and huge texts. No panic; the stamp comparison
//! grows linearly with the stamps, as the reference's set intersection
//! does.

mod common;
mod timing;

use bettermemory_core::location_view::{continuity, py_eq, Asks, Declined, Side};
use bettermemory_core::pyvalue::PyValue;

fn stamps(n: usize, salt: &str) -> Vec<Vec<PyValue>> {
    (0..n)
        .map(|i| {
            vec![
                PyValue::Str(format!("{salt}{i}").chars().map(u32::from).collect()),
                PyValue::Int(format!("{}{i}", "9".repeat(60))),
                PyValue::Float(i as f64),
            ]
        })
        .collect()
}

fn run(mine: &[Vec<PyValue>], here: &[Vec<PyValue>]) -> bool {
    let a = Side {
        token: None,
        head: None,
        stamps: mine,
    };
    let b = Side {
        token: None,
        head: None,
        stamps: here,
    };
    continuity(
        a,
        b,
        false,
        Asks {
            holds: &mut |_| Ok(false),
            confirms: None,
            reminted: None,
        },
    )
    .unwrap()
    .0
}

#[test]
fn many_stamps_answer() {
    let a = stamps(300, "a");
    let b = stamps(300, "b");
    assert!(!run(&a, &b));
    assert!(run(&a, &a));
}

#[test]
fn big_integers_compare_exactly() {
    let big = PyValue::Int(format!("1{}", "0".repeat(300)));
    assert!(!py_eq(&big, &PyValue::Float(1e300)));
    assert!(py_eq(&big, &big.clone()));
    assert!(!py_eq(&PyValue::Float(f64::NAN), &PyValue::Float(f64::NAN)));
    assert!(!py_eq(
        &PyValue::Float(f64::INFINITY),
        &PyValue::Int("1".into())
    ));
    let max = PyValue::Int(i128::MAX.to_string());
    assert!(!py_eq(&max, &PyValue::Float(i128::MAX as f64)));
}

/// 200,000 nested tuples: the port walks them with its own stack and
/// answers. CPython 3.13 exhausts its C stack hashing such a tuple and
/// dies (exit 139), so the reference gives no answer to compare.
#[test]
fn very_deep_tuples_answer() {
    let mut v = PyValue::Int("1".into());
    for _ in 0..200_000 {
        v = PyValue::Tuple(vec![v]);
    }
    let s = vec![vec![v.clone()]];
    assert!(run(&s, &s));
    assert!(py_eq(&v, &v.clone()));
    let mut w = PyValue::List(vec![PyValue::Int("1".into())]);
    for _ in 0..200_000 {
        w = PyValue::Tuple(vec![w]);
    }
    let t = vec![vec![w]];
    let got = continuity(
        Side {
            token: None,
            head: None,
            stamps: &t,
        },
        Side {
            token: None,
            head: None,
            stamps: &s,
        },
        false,
        Asks {
            holds: &mut |_| Ok(false),
            confirms: None,
            reminted: None,
        },
    );
    assert_eq!(got, Err(Declined::Unhashable));
}

#[test]
fn deep_tuples_do_not_overflow() {
    let mut v = PyValue::Int("1".into());
    for _ in 0..2_000 {
        v = PyValue::Tuple(vec![v]);
    }
    let s = vec![vec![v.clone()]];
    assert!(run(&s, &s));
}

#[test]
fn huge_tokens_answer() {
    let t = "T".repeat(1 << 20);
    let (ok, _) = continuity(
        Side {
            token: Some(&t),
            head: None,
            stamps: &[],
        },
        Side {
            token: Some(&t),
            head: None,
            stamps: &[],
        },
        false,
        Asks {
            holds: &mut |_| Ok(false),
            confirms: None,
            reminted: None,
        },
    )
    .unwrap();
    assert!(ok);
}

#[test]
fn stamp_comparison_grows_linearly() {
    let small = (stamps(2_000, "a"), stamps(2_000, "b"));
    let large = (stamps(20_000, "a"), stamps(20_000, "b"));
    timing::grows_within(
        "continuity's stamps at ten times the stamps",
        30,
        &mut || timing::once(|| run(&small.0, &small.1)),
        &mut || timing::once(|| run(&large.0, &large.1)),
    );
}
