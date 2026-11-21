//! Hostile input to `native_text`: deep nesting, long text, many keys.
//! No panic in debug or release, and time grows linearly.

mod timing;

use bettermemory_core::native_text as nt;
use bettermemory_core::pyvalue::PyValue;
use std::hint::black_box;

fn nested(depth: usize, leaf: PyValue) -> PyValue {
    let mut v = leaf;
    for i in 0..depth {
        v = if i % 2 == 0 {
            PyValue::List(vec![v])
        } else {
            PyValue::Dict(vec![(PyValue::str("k"), v)])
        };
    }
    v
}

#[test]
fn deep_nesting_does_not_exhaust_the_stack() {
    let deep = nested(200_000, PyValue::Str(vec![0xDC80]));
    assert!(nt::has_surrogates(&deep));
    let clean = nested(200_000, PyValue::str("x"));
    assert!(!nt::has_surrogates(&clean));
}

#[test]
fn utf8_text_of_extreme_code_points() {
    let text: Vec<u32> = vec![0, 0xD800, 0xDFFF, 0x10FFFF, 0x7F];
    assert_eq!(nt::utf8_text(&text), "\0\\ud800\\udfff\u{10FFFF}\u{7f}");
}

#[test]
fn utf8_text_grows_linearly() {
    let small: Vec<u32> = (0..20_000u32).map(|i| 0xD800 + (i % 0x800)).collect();
    let large: Vec<u32> = (0..200_000u32).map(|i| 0xD800 + (i % 0x800)).collect();
    timing::grows_within(
        "utf8_text",
        30,
        &mut || timing::once(|| black_box(nt::utf8_text(&small))),
        &mut || timing::once(|| black_box(nt::utf8_text(&large))),
    );
}

#[test]
fn projection_of_many_keys_grows_near_linearly() {
    let origin = |n: u32| -> Vec<(PyValue, PyValue)> {
        (0..n)
            .map(|i| {
                (
                    PyValue::str(&format!("k{i:08}")),
                    PyValue::Str(vec![0x61, 0xDC80 + (i % 64)]),
                )
            })
            .collect()
    };
    let (small, large) = (origin(2_000), origin(20_000));
    let (legacy, native) = nt::origin_projection(&large).unwrap();
    assert!(legacy.is_empty() && native.is_some());
    timing::grows_within(
        "origin_projection",
        40,
        &mut || timing::once(|| black_box(nt::origin_projection(&small))),
        &mut || timing::once(|| black_box(nt::origin_projection(&large))),
    );
}
