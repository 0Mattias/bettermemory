//! Hostile rows through `events`: very many fields, a long kind, a long
//! list kept by keys, deep values. No panic, and the time grows linearly.

use std::time::{Duration, Instant};

use bettermemory_core::events as ev;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

fn fields(n: usize) -> Vec<(PyValue, PyValue)> {
    (0..n)
        .map(|i| {
            (
                PyValue::str(&format!("f{i}_acknowledge")),
                PyValue::int(i as i128),
            )
        })
        .collect()
}

fn hits(n: usize) -> PyValue {
    PyValue::List(
        (0..n)
            .map(|i| {
                PyValue::Dict(vec![
                    (PyValue::str("id"), PyValue::int(i as i128)),
                    (PyValue::str("s"), PyValue::None),
                ])
            })
            .collect(),
    )
}

fn fastest(mut run: impl FnMut()) -> Duration {
    (0..3)
        .map(|_| {
            let t = Instant::now();
            run();
            t.elapsed()
        })
        .min()
        .unwrap()
}

fn deep(depth: usize) -> PyValue {
    let mut v = PyValue::None;
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

#[test]
fn many_fields_grow_linearly() {
    let kind = cps("write");
    let small = fields(100_000);
    let large = fields(200_000);
    let mut never = |_: &[u32]| PyValue::None;
    let t1 = fastest(|| {
        assert_eq!(ev::is_audit_row(&kind, &small), Ok(false));
        ev::state_fields(&kind, &small, &mut never).unwrap();
    });
    let t2 = fastest(|| {
        assert_eq!(ev::is_audit_row(&kind, &large), Ok(false));
        ev::state_fields(&kind, &large, &mut never).unwrap();
    });
    assert!(
        t2 < t1 * 4 + Duration::from_millis(50),
        "{t1:?} then {t2:?}"
    );
}

#[test]
fn long_list_kept_by_keys() {
    let kind = cps("search_miss");
    let mut never = |_: &[u32]| PyValue::None;
    let t1 = fastest(|| {
        ev::state_fields(
            &kind,
            &[(PyValue::str("top_hits"), hits(100_000))],
            &mut never,
        )
        .unwrap();
    });
    let t2 = fastest(|| {
        ev::state_fields(
            &kind,
            &[(PyValue::str("top_hits"), hits(200_000))],
            &mut never,
        )
        .unwrap();
    });
    assert!(
        t2 < t1 * 4 + Duration::from_millis(50),
        "{t1:?} then {t2:?}"
    );
}

#[test]
fn long_and_odd_kinds() {
    let long: Vec<u32> = std::iter::repeat_n(u32::from('w'), 1 << 20).collect();
    assert_eq!(ev::row_class(&long), ev::RowClass::Measurement);
    assert_eq!(ev::is_audit_row(&long, &fields(10)), Ok(false));
    for kind in [vec![], vec![0xD800], vec![0x10FFFF], vec![0]] {
        assert!(ev::prunable(&kind));
    }
}

#[test]
fn deep_values_are_kept_without_recursion() {
    let value = deep(200_000);
    let mut never = |_: &[u32]| PyValue::None;
    let out = ev::state_fields(
        &cps("show"),
        &[
            (PyValue::str("id"), value),
            (PyValue::str("session_id"), deep(10)),
        ],
        &mut never,
    )
    .unwrap();
    assert_eq!(out.len(), 2);
    let kept = ev::keys_of(
        &PyValue::List(vec![
            deep(100_000),
            PyValue::Dict(vec![(PyValue::str("id"), deep(100_000))]),
        ]),
        &["id"],
    );
    assert!(matches!(kept, PyValue::List(ref v) if v.len() == 1));
    assert!(ev::is_audit_row(
        &cps("write"),
        &[(PyValue::str("a_acknowledged"), deep(100_000))]
    )
    .unwrap());
}
