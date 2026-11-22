//! Hostile input to the migration's report and helpers: very long texts,
//! deep JSON, many values and roots, integer bounds. No panic, and time
//! that grows in proportion to the input.

mod timing;

use bettermemory_core::membership_migrate::{
    self as mm, MigrationReport, MigrationTotals, RootDecision,
};
use bettermemory_core::pyvalue::{PyError, PyValue};

#[test]
fn long_paths_and_keys() {
    let long: Vec<u32> = std::iter::repeat_n(u32::from(b'/'), 1_000_000).collect();
    assert_eq!(mm::basename_on(&long, false), long);
    assert_eq!(mm::basename_on(&long, true), long);
    assert_eq!(mm::repository_name(&long), long);
    let mut unc: Vec<u32> = "//?/UNC/".chars().map(u32::from).collect();
    unc.extend(std::iter::repeat_n(u32::from(b'a'), 1_000_000));
    assert!(mm::basename_on(&unc, true) == unc);
    let run = |n: usize| {
        let p: Vec<u32> = "/a".chars().map(u32::from).cycle().take(n).collect();
        assert!(!mm::basename_on(&p, true).is_empty());
        assert!(!mm::repository_name(&p).is_empty());
    };
    timing::grows_within(
        "basename and repository_name: 100,000 and 1,000,000 code points",
        timing::TENFOLD,
        &mut || timing::once(|| run(100_000)),
        &mut || timing::once(|| run(1_000_000)),
    );
}

#[test]
fn deep_and_wide_json() {
    for depth in [9998usize, 9999, 200_000] {
        let text = format!("{}{}", "[".repeat(depth), "]".repeat(depth));
        let got = mm::json_list(&PyValue::str(&text));
        assert_eq!(got.is_err(), depth > 9998);
        if depth > 9998 {
            assert_eq!(got, Err(PyError::RecursionError));
        }
        let unclosed = "{\"a\":".repeat(depth);
        assert!(matches!(
            mm::origin(&PyValue::str(&unclosed)),
            Ok(None) | Err(PyError::RecursionError)
        ));
    }
    let wide = format!("[{}0]", "0,".repeat(500_000));
    assert_eq!(
        mm::json_list(&PyValue::str(&wide)).map(|v| v.len()),
        Ok(500_001)
    );
}

#[test]
fn many_shas() {
    let vals = |n: usize| -> Vec<PyValue> {
        (0..n)
            .map(|i| PyValue::str(&format!(" {i:08X} ")))
            .collect()
    };
    let small = vals(20_000);
    let large = vals(200_000);
    assert_eq!(mm::shas(&large).len(), 200_000);
    timing::grows_within(
        "shas: 20,000 and 200,000 values",
        timing::TENFOLD,
        &mut || timing::once(|| assert_eq!(mm::shas(&small).len(), 20_000)),
        &mut || timing::once(|| assert_eq!(mm::shas(&large).len(), 200_000)),
    );
}

#[test]
fn many_roots_and_integer_bounds() {
    let report = |n: usize| MigrationReport {
        roots: (0..n)
            .map(|i| RootDecision {
                path: format!("/w/{i}").chars().map(u32::from).collect(),
                memories: i64::MAX,
                episodes: i64::MIN,
                held: (0..4)
                    .map(|k| (vec![u32::from(b'C'), k], i64::MAX))
                    .collect(),
                ..Default::default()
            })
            .collect(),
        totals: MigrationTotals {
            memory_homes: 1 << 60,
            tombstone_homes: 1 << 61,
            episode_homes: -(1 << 62),
            ..Default::default()
        },
        ..Default::default()
    };
    let big = report(1);
    assert_eq!(big.homed(), (1 << 60) + (1 << 61) - (1 << 62));
    let small = report(2_000);
    let large = report(20_000);
    assert_eq!(large.lines().len(), 20_000 + 7);
    timing::grows_within(
        "lines and to_dict: 2,000 and 20,000 roots",
        timing::TENFOLD,
        &mut || {
            timing::once(|| {
                small.lines();
                small.to_dict();
            })
        },
        &mut || {
            timing::once(|| {
                large.lines();
                large.to_dict();
            })
        },
    );
}
