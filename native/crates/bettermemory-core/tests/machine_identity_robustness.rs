//! The machine identity record over hostile input: very large texts and
//! containers, nesting far past the recursion bound, integers far past the
//! digit limit. Nothing panics, and reading is linear: sixteen times
//! the input costs under twenty-four times the time (the fastest of three),
//! so growth past about n^1.15 fails while a quadratic walk costs 256 times.

use std::time::{Duration, Instant};

use bettermemory_core::machine::{self as m, MachineFacts};
use bettermemory_core::pyvalue::{PyError, PyValue};

fn id_file(parts: Vec<PyValue>) -> PyValue {
    PyValue::Dict(vec![(PyValue::str("id_file"), PyValue::List(parts))])
}

fn timed(f: impl FnOnce()) -> Duration {
    let t = Instant::now();
    f();
    t.elapsed()
}

fn linear(name: &str, make: impl Fn(usize) -> PyValue) {
    let best = |n: usize| {
        let raw = make(n);
        (0..3)
            .map(|_| timed(|| drop(m::identity_from(&raw))))
            .min()
            .unwrap_or_default()
    };
    let small = best(25_000);
    let large = best(400_000);
    assert!(
        large <= small * 24 + Duration::from_millis(250),
        "{name}: {small:?} then {large:?}"
    );
}

fn identity(facts: PyValue) -> PyValue {
    PyValue::Dict(vec![
        (PyValue::str("id"), PyValue::str("A")),
        (PyValue::str("facts"), facts.clone()),
        (PyValue::str("recorded"), facts),
    ])
}

#[test]
fn large_values_read_in_linear_time() {
    linear("wide list", |n| {
        identity(id_file(vec![
            PyValue::List((0..n).map(|i| PyValue::int(i as i128)).collect()),
            PyValue::str(&"x".repeat(n)),
            PyValue::None,
        ]))
    });
    linear("wide dict", |n| {
        identity(PyValue::Dict(
            (0..n)
                .map(|i| (PyValue::str(&i.to_string()), PyValue::Float(i as f64)))
                .collect(),
        ))
    });
    linear("wide dict in a part", |n| {
        identity(id_file(vec![
            PyValue::Dict(
                (0..n)
                    .map(|i| (PyValue::int(i as i128), PyValue::str(&i.to_string())))
                    .collect(),
            ),
            PyValue::int(1),
            PyValue::None,
        ]))
    });
    linear("long text", |n| {
        identity(id_file(vec![
            PyValue::List(vec![PyValue::Str(vec![0xD800; n * 10])]),
            PyValue::int(1),
            PyValue::None,
        ]))
    });
}

#[test]
fn nesting_far_past_the_bound_raises_without_overflow() {
    let mut deep = PyValue::int(1);
    for _ in 0..1_000_000 {
        deep = PyValue::Tuple(vec![deep]);
    }
    let raw = id_file(vec![PyValue::int(1), PyValue::int(1), deep]);
    let t = Instant::now();
    assert_eq!(MachineFacts::from_dict(&raw), Err(PyError::RecursionError));
    assert!(t.elapsed() < Duration::from_secs(5));
}

#[test]
fn integers_far_past_the_limit() {
    let raw = id_file(vec![
        PyValue::Int(format!("-{}", "7".repeat(5_000_000))),
        PyValue::int(i128::MAX),
        PyValue::int(i128::MIN),
    ]);
    assert_eq!(MachineFacts::from_dict(&raw), Err(PyError::ValueError));
    let raw = id_file(vec![
        PyValue::int(i128::MIN),
        PyValue::int(i128::MAX),
        PyValue::Float(f64::MIN_POSITIVE),
    ]);
    let got = MachineFacts::from_dict(&raw)
        .unwrap()
        .unwrap()
        .id_file
        .unwrap();
    assert_eq!(got.0.len(), i128::MIN.to_string().len());
    for b in [[0u8; 17], [0xff; 17], [0x5a; 17]] {
        let id = m::new_machine_id(&b);
        assert_eq!(id.len(), 26);
    }
}
