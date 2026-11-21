//! Hostile input to `membership_records::records`: very many entries,
//! deep nesting, integer bounds. No panic in debug or release, and the
//! time grows in proportion to the input.

mod timing;

use bettermemory_core::capture::Manifest;
use bettermemory_core::membership_records::records::{
    manifest_from_json, manifest_match, Checkout, View,
};
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::unicode::cps;

fn raw(n: usize) -> PyValue {
    let entries = (0..n)
        .map(|i| {
            PyValue::List(vec![
                PyValue::Str(cps(&format!("p{i}"))),
                PyValue::int(i as i128),
            ])
        })
        .collect();
    PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::str("m")),
        (PyValue::str("entries"), PyValue::List(entries)),
    ])
}

fn manifest(n: usize, shift: usize) -> Manifest {
    Manifest {
        name: cps("m"),
        entries: (0..n)
            .map(|i| (cps(&format!("p{}", i + shift)), "1".to_owned()))
            .collect(),
    }
}

#[test]
fn many_entries_read_in_linear_time() {
    let (small, large) = (raw(20_000), raw(200_000));
    timing::grows_within(
        "from_json",
        timing::TENFOLD,
        &mut || timing::once(|| manifest_from_json(&small)),
        &mut || timing::once(|| manifest_from_json(&large)),
    );
    let m = manifest_from_json(&large).unwrap().unwrap();
    assert_eq!(m.entries.len(), 200_000);
}

#[test]
fn many_entries_match_in_linear_time() {
    let (a, b) = (manifest(20_000, 0), manifest(20_000, 10_000));
    let (c, d) = (manifest(200_000, 0), manifest(200_000, 100_000));
    timing::grows_within(
        "manifest_match",
        timing::TENFOLD,
        &mut || timing::once(|| manifest_match(Some(&a), Some(&b))),
        &mut || timing::once(|| manifest_match(Some(&c), Some(&d))),
    );
    assert_eq!(manifest_match(Some(&c), Some(&d)), 1.0 / 3.0);
}

#[test]
fn a_deeply_nested_path_or_size_does_not_panic() {
    let mut deep = PyValue::List(Vec::new());
    for _ in 0..100_000 {
        deep = PyValue::List(vec![deep]);
    }
    let as_path = PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::str("m")),
        (
            PyValue::str("entries"),
            PyValue::List(vec![PyValue::List(vec![deep.clone(), PyValue::int(1)])]),
        ),
    ]);
    assert_eq!(manifest_from_json(&as_path), Err(PyError::RecursionError));
    let as_size = PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::str("m")),
        (
            PyValue::str("entries"),
            PyValue::List(vec![PyValue::List(vec![PyValue::str("p"), deep])]),
        ),
    ]);
    assert_eq!(manifest_from_json(&as_size), Err(PyError::TypeError));
}

#[test]
fn str_of_the_path_raises_before_int_of_the_size() {
    let mut deep = PyValue::List(Vec::new());
    for _ in 0..100_000 {
        deep = PyValue::List(vec![deep]);
    }
    let both = PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::str("m")),
        (
            PyValue::str("entries"),
            PyValue::List(vec![PyValue::List(vec![deep, PyValue::str("x")])]),
        ),
    ]);
    assert_eq!(manifest_from_json(&both), Err(PyError::RecursionError));
}

fn with_entries(entries: PyValue) -> PyValue {
    PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::str("m")),
        (PyValue::str("entries"), entries),
    ])
}

/// The reference's iterator and unpacking stop at the first raise, after
/// at most three items of an element; the time does not follow the length
/// of what is left unread.
#[test]
fn a_malformed_read_stops_at_the_first_raise() {
    let text = |n: usize| with_entries(PyValue::Str(vec![u32::from('x'); n]));
    let (small, large) = (text(4_000), text(4_000_000));
    timing::grows_within(
        "entries as a str",
        timing::HOPELESS,
        &mut || timing::once(|| manifest_from_json(&small)),
        &mut || timing::once(|| manifest_from_json(&large)),
    );
    assert_eq!(manifest_from_json(&large), Err(PyError::ValueError));
    let list = |n: usize| {
        let mut v = vec![PyValue::int(1)];
        v.extend((0..n).map(|i| PyValue::List(vec![PyValue::str("p"), PyValue::int(i as i128)])));
        with_entries(PyValue::List(v))
    };
    let (small, large) = (list(4_000), list(4_000_000));
    timing::grows_within(
        "a bad first element",
        timing::HOPELESS,
        &mut || timing::once(|| manifest_from_json(&small)),
        &mut || timing::once(|| manifest_from_json(&large)),
    );
    assert_eq!(manifest_from_json(&large), Err(PyError::TypeError));
    let element = |n: usize| {
        with_entries(PyValue::List(vec![PyValue::List(
            (0..n).map(|i| PyValue::int(i as i128)).collect(),
        )]))
    };
    let (small, large) = (element(4_000), element(4_000_000));
    timing::grows_within(
        "a long element",
        timing::HOPELESS,
        &mut || timing::once(|| manifest_from_json(&small)),
        &mut || timing::once(|| manifest_from_json(&large)),
    );
    assert_eq!(manifest_from_json(&large), Err(PyError::ValueError));
}

#[test]
fn size_text_at_the_digit_limit() {
    let at = |digits: usize| {
        PyValue::Dict(vec![
            (PyValue::str("name"), PyValue::str("m")),
            (
                PyValue::str("entries"),
                PyValue::List(vec![PyValue::List(vec![
                    PyValue::str("p"),
                    PyValue::Str(cps(&"7".repeat(digits))),
                ])]),
            ),
        ])
    };
    let m = manifest_from_json(&at(4300)).unwrap().unwrap();
    assert_eq!(m.entries[0].1.len(), 4300);
    assert_eq!(manifest_from_json(&at(4301)), Err(PyError::ValueError));
    let huge = PyValue::Dict(vec![
        (PyValue::str("name"), PyValue::str("m")),
        (
            PyValue::str("entries"),
            PyValue::List(vec![PyValue::List(vec![
                PyValue::str("p"),
                PyValue::Float(f64::MAX),
            ])]),
        ),
    ]);
    let m = manifest_from_json(&huge).unwrap().unwrap();
    assert_eq!(m.entries[0].1.len(), 309);
}

#[test]
fn many_views_at_the_integer_bounds() {
    let mut views = Vec::new();
    for i in 0..100_000i64 {
        let order = [i64::MIN, i64::MAX, 0, -1][(i % 4) as usize];
        views.push(View {
            checkout: cps("k"),
            machine: cps(if i % 3 == 0 { "a" } else { "b" }),
            path: cps(&format!("/{i}")),
            first_seen: cps("t"),
            last_seen: cps("t"),
            order,
        });
    }
    let k = Checkout {
        id: cps("k"),
        project: cps("p"),
        machine: cps("a"),
        kind: cps("primary"),
        object_key: None,
        common_object_key: None,
        git_dir: None,
        token: None,
        token_status: cps("own"),
        stamps: Vec::new(),
        last_head: None,
        branch: None,
        remotes: Vec::new(),
        manifest: None,
        legacy: false,
        retired: false,
        registered: cps("t"),
        views,
        sessions: Vec::new(),
        replaced_by: None,
        ticks: None,
    };
    assert_eq!(k.newest_view().map(|v| v.path.clone()), Some(cps("/1")));
    let on = k.views_on(&cps("a"));
    assert_eq!(on.len(), 33_334);
    assert_eq!(on[0].order, i64::MIN);
    assert!(on.windows(2).all(|w| w[0].order <= w[1].order));
}
