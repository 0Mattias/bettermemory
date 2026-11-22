//! Hostile input for `mirror_format` and `native_text`: very long names,
//! deep nesting and wide mappings; no panic, and time grows linearly.

mod timing;

use bettermemory_core::mirror_format::{active_filename_for_tombstone, is_legacy_tombstone_name};
use bettermemory_core::native_text::{has_surrogates, origin_projection};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

fn name(n: usize) -> Vec<u32> {
    let mut s = vec![u32::from(b'.'); n];
    s.extend(cps("0123456789ABCDEFGHJKMNPQRS.tombstone.md"));
    s
}

#[test]
fn long_names_scan_linearly() {
    let (small, large) = (name(20_000), name(200_000));
    assert!(!is_legacy_tombstone_name(&large));
    timing::grows_within(
        "active_filename_for_tombstone",
        timing::TENFOLD,
        &mut || timing::once(|| active_filename_for_tombstone(&small)),
        &mut || timing::once(|| active_filename_for_tombstone(&large)),
    );
}

#[test]
fn deep_nesting_answers_without_overflow() {
    let mut v = PyValue::Str(vec![0xDC80]);
    for i in 0..200_000 {
        v = if i % 2 == 0 {
            PyValue::List(vec![v])
        } else {
            PyValue::Dict(vec![(PyValue::str("k"), v)])
        };
    }
    assert!(has_surrogates(&v));
}

#[test]
fn wide_origins_project_linearly() {
    let origin = |n: usize| -> Vec<(PyValue, PyValue)> {
        (0..n)
            .map(|i| {
                let v = if i % 7 == 0 {
                    PyValue::Str(vec![0xDC80])
                } else {
                    PyValue::str("v")
                };
                (PyValue::str(&format!("k{i}")), v)
            })
            .collect()
    };
    let (small, large) = (origin(2_000), origin(20_000));
    timing::grows_within(
        "origin_projection",
        timing::TENFOLD,
        &mut || timing::once(|| origin_projection(&small).unwrap()),
        &mut || timing::once(|| origin_projection(&large).unwrap()),
    );
}
