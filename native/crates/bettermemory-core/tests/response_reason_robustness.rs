//! Hostile input for `response_reason` and `response_trust`: long path
//! lists, deep nesting, integer bounds. Nothing panics, in debug or
//! release, and the reason grows with its input as the reference's does.

mod timing;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::response_reason::{
    check_reason, claim_at_index, verdict_and_reason, CheckReason, Raised,
};
use bettermemory_core::response_trust::{apply_trust, attach_provenance, hashable, TrustRows};
use bettermemory_core::store::provenance::TrustRow;

fn paths(n: usize) -> PyValue {
    PyValue::List((0..n).map(|i| PyValue::str(&format!("p{i}"))).collect())
}

fn nested(depth: usize) -> PyValue {
    let mut v = PyValue::List(Vec::new());
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

#[test]
fn deep_nesting_raises_without_overflowing() {
    let deep = nested(200_000);
    assert!(!hashable(&deep));
    let got = check_reason(&CheckReason {
        missing: PyValue::List(vec![deep.clone()]),
        anchored_missing: paths(1),
        ..Default::default()
    });
    assert_eq!(got, Err(Raised::TypeError));
    let got = check_reason(&CheckReason {
        contradicted_by: PyValue::List(vec![deep]),
        ..Default::default()
    });
    assert_eq!(got, Err(Raised::TypeError));
}

#[test]
fn integer_bounds() {
    let ev = vec![(PyValue::str("claim_excerpts"), paths(3))];
    assert_eq!(claim_at_index(&ev, i64::MAX), Ok(None));
    assert_eq!(claim_at_index(&ev, i64::MIN), Err(Raised::IndexError));
    let huge = PyValue::int_from_decimal(&"9".repeat(5_000)).unwrap();
    let got = check_reason(&CheckReason {
        commits: huge,
        ..Default::default()
    })
    .unwrap()
    .unwrap();
    assert_eq!(
        got.len(),
        5_000 + " commits since the last verify touched the paths it cites.".len()
    );
    let mut row = vec![(
        PyValue::str("last_verified_at"),
        PyValue::str("2026-01-01T00:00:00Z"),
    )];
    apply_trust(&mut row, Some("synced"), None, i64::MIN);
    apply_trust(&mut row, Some("synced"), None, i64::MAX);
}

#[test]
fn an_empty_or_odd_row_answers() {
    assert_eq!(verdict_and_reason(&[]), Err(Raised::KeyError));
    let row = vec![
        (PyValue::str("staleness_verdict"), PyValue::str("stale")),
        (
            PyValue::str("path_drift"),
            PyValue::Dict(vec![(
                PyValue::str("missing"),
                PyValue::str(&"x".repeat(100_000)),
            )]),
        ),
    ];
    let (_, reason) = verdict_and_reason(&row).unwrap();
    assert!(reason.unwrap().len() < 100);
}

struct Many;

impl TrustRows for Many {
    type Error = ();
    fn trust_rows(&self, ids: &[String]) -> Result<Option<Vec<(String, TrustRow)>>, ()> {
        Ok(Some(
            ids.iter()
                .map(|i| {
                    let t = TrustRow {
                        label: "local".into(),
                        verified_at: None,
                    };
                    (i.clone(), t)
                })
                .collect(),
        ))
    }
}

#[test]
fn many_rows_are_attached() {
    let mut rows: Vec<_> = (0..2_000)
        .map(|i| vec![(PyValue::str("id"), PyValue::str(&format!("m{i}")))])
        .collect();
    attach_provenance(&mut rows, &Many, 30).unwrap();
    assert!(rows.iter().all(|r| r.len() == 2));
}

#[test]
fn the_reason_grows_linearly_with_its_lists() {
    let run = |n: usize| {
        let entry = PyValue::Dict(vec![
            (PyValue::str("count_in_window"), PyValue::int(2)),
            (PyValue::str("most_recent_ts"), PyValue::str("2026-01-01")),
            (PyValue::str("outcome"), PyValue::str("ignored")),
        ]);
        let a = CheckReason {
            missing: paths(n),
            anchored_missing: paths(2),
            attested_absent_present: paths(n),
            contradicted_by: paths(n),
            rejections: PyValue::List(vec![entry; n / 10]),
            ..Default::default()
        };
        move || check_reason(&a).unwrap()
    };
    let mut small = run(5_000);
    let mut large = run(50_000);
    timing::grows_within(
        "check_reason: 5,000 and 50,000 paths",
        timing::TENFOLD,
        &mut || timing::once(&mut small),
        &mut || timing::once(&mut large),
    );
}

#[test]
fn a_long_contradicted_by_is_sliced_not_copied() {
    let run = |n: usize| {
        let list = CheckReason {
            contradicted_by: PyValue::List(vec![PyValue::str("x"); n]),
            ..Default::default()
        };
        let text = CheckReason {
            contradicted_by: PyValue::str(&"x".repeat(n)),
            ..Default::default()
        };
        move || {
            check_reason(&list).unwrap();
            check_reason(&text).unwrap()
        }
    };
    let mut small = run(20_000);
    let mut large = run(2_000_000);
    // A hundredfold input: the work before the slice is constant.
    timing::grows_within(
        "check_reason: contradicted_by of 20,000 and 2,000,000",
        timing::TENFOLD,
        &mut || timing::once(&mut small),
        &mut || timing::once(&mut large),
    );
}
