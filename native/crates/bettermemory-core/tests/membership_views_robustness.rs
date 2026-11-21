//! Hostile inputs to membership-3 and `location.continuity`: init stamps
//! by the hundred thousand (the sets meet in linear time), a history
//! that cycles and fans out (the walk stops at its bound), a liveness
//! span over many views (each read once), deep merge chains, empty and
//! huge commit ids. No panic, and time grows with the input.

mod common;
mod timing;

use std::collections::{BTreeMap, BTreeSet};
use std::rc::Rc;

use bettermemory_core::location_continuity::{continuity, Readers};
use bettermemory_core::membership_records::records::HistoryReader;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use common::membership_views::{location, History, Run};

fn stamps(n: usize, offset: usize) -> Vec<(u64, u64)> {
    (0..n).map(|i| ((i + offset) as u64, 7)).collect()
}

fn meet(n: usize) -> bool {
    let (a, b) = (stamps(n, 0), stamps(n, n - 1));
    let mut holds = |_: &[u32]| -> Result<bool, ()> { Ok(false) };
    continuity(
        None,
        None,
        &a,
        None,
        None,
        &b,
        false,
        Readers {
            holds: &mut holds,
            confirms: None,
            reminted: None,
        },
    )
    .unwrap()
    .0
}

#[test]
fn many_init_stamps_meet_in_near_linear_time() {
    assert!(meet(10));
    timing::grows_within(
        "init stamps: 20,000 and 200,000",
        timing::TENFOLD,
        &mut || timing::once(|| meet(20_000)),
        &mut || timing::once(|| meet(200_000)),
    );
}

fn d(items: Vec<(PyValue, PyValue)>) -> PyValue {
    PyValue::Dict(items)
}

fn witness(project: &str, commit: &str, i: i128) -> PyValue {
    d(vec![
        (PyValue::str("project"), PyValue::str(project)),
        (PyValue::str("commit_id"), PyValue::str(commit)),
        (PyValue::str("arrival"), PyValue::int(i)),
        (PyValue::str("fingerprint"), PyValue::str("f")),
    ])
}

#[test]
fn a_cycling_fanning_history_stops_at_the_bound() {
    let n = 5_000;
    let fp = (0..n)
        .map(|i| (PyValue::str(&format!("c{i}")), PyValue::str("f")))
        .collect();
    let parents = (0..n)
        .map(|i| {
            let p = (0..8)
                .map(|j| PyValue::str(&format!("c{}", (i * 7 + j) % n)))
                .collect();
            (PyValue::str(&format!("c{i}")), PyValue::List(p))
        })
        .collect();
    let spec = d(vec![
        (PyValue::str("fp"), d(fp)),
        (PyValue::str("parents"), d(parents)),
    ]);
    let h = History::of(&spec, Rc::default()).unwrap();
    let tables: BTreeMap<String, Vec<PyValue>> = [(
        "project_witnesses".to_string(),
        vec![witness("P1", &"e".repeat(40), 1)],
    )]
    .into();
    let mut r = Run::new(tables, &d(Vec::new()));
    let loc = location(&d(vec![(PyValue::str("head"), PyValue::str("c0"))]));
    let got =
        r.m.rewritten(&loc, Some(&h as &dyn HistoryReader), None)
            .unwrap();
    assert_eq!(got.get(&cps("P1")), Some(&512));
}

#[test]
fn tied_witnesses_by_the_thousand() {
    let tables = BTreeMap::new();
    let r = Run::new(tables, &d(Vec::new()));
    let held: BTreeMap<PyStr, BTreeSet<PyStr>> = (0..2_000)
        .map(|i| (cps(&format!("P{i}")), [cps(&format!("{i:040x}"))].into()))
        .collect();
    let spec = d(vec![(PyValue::str("reach"), d(Vec::new()))]);
    let h = History::of(&spec, Rc::default()).unwrap();
    let got =
        r.m.specific(Some(&h as &dyn HistoryReader), None, &held)
            .unwrap();
    assert_eq!(got.best.len(), 2_000);
    assert!(got.bounded);
    let huge = cps(&"A".repeat(1 << 20));
    assert_eq!(
        r.m.independent(None, std::slice::from_ref(&huge)).unwrap(),
        Some([huge].into())
    );
    assert_eq!(r.m.independent(None, &[]).unwrap(), Some(BTreeSet::new()));
}

fn span(n: usize) -> usize {
    let views: Vec<PyValue> = (0..n)
        .map(|i| {
            d(vec![
                (PyValue::str("checkout"), PyValue::str("K1")),
                (PyValue::str("machine"), PyValue::str("M1")),
                (PyValue::str("path"), PyValue::str(&format!("/v/{i}"))),
                (PyValue::str("first_seen"), PyValue::str("t")),
                (PyValue::str("last_seen"), PyValue::str("t")),
                (PyValue::str("seen_order"), PyValue::int(i as i128)),
            ])
        })
        .collect();
    let k = common::membership_views::checkout_row("K1");
    let tables: BTreeMap<String, Vec<PyValue>> = [
        ("checkouts".to_string(), vec![k]),
        ("checkout_views".to_string(), views),
    ]
    .into();
    let mut r = Run::new(tables, &d(Vec::new()));
    let k = r.m.checkout(&cps("K1")).unwrap();
    r.m.liveness_held(|m| {
        assert_eq!(m.live_view(&k), None);
        assert_eq!(m.live_view(&k), None);
    });
    let n = r.lv.borrow().len();
    n
}

#[test]
fn a_span_reads_each_view_once() {
    assert_eq!(span(3_000), 3_000);
}
