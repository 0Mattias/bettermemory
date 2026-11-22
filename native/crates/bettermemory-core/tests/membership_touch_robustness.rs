//! The sighting and token unit (membership-4a) over hostile input: a
//! checkout with many thousands of views, a path of a million
//! characters, a sighting stamp at the ends of the datetime range. No
//! panic in debug or release, and a sighting linear in the views.

mod common;
mod timing;

use std::collections::BTreeMap;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::membership_records::records::{Checkout, View};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;
use common::membership_core::Model;
use common::membership_touch::{location, model};

fn now() -> DateTime {
    let mut dt = DateTime::from_isoformat(&cps("2026-03-04T05:06:07")).unwrap();
    dt.offset_us = Some(0);
    dt
}

fn view(path: &str, order: i64, last: &str) -> View {
    View {
        checkout: cps("K1"),
        machine: cps("M1"),
        path: cps(path),
        first_seen: cps("t"),
        last_seen: cps(last),
        order,
    }
}

fn over(views: Vec<View>) -> (Model, Checkout) {
    let m = model(BTreeMap::new(), None, BTreeMap::new(), None, now());
    let row = |v: &View| {
        PyValue::Dict(vec![
            (PyValue::str("checkout"), PyValue::Str(v.checkout.clone())),
            (
                PyValue::str("seen_order"),
                PyValue::int(i128::from(v.order)),
            ),
        ])
    };
    m.store()
        .0
        .borrow_mut()
        .tables
        .insert("checkout_views".into(), views.iter().map(row).collect());
    let k = Checkout {
        id: cps("K1"),
        project: cps("P1"),
        machine: cps("M1"),
        kind: cps("primary"),
        object_key: None,
        common_object_key: None,
        git_dir: Some(cps("/g")),
        token: None,
        token_status: cps("n/a"),
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
    (m, k)
}

fn at(root: &str) -> PyValue {
    PyValue::Dict(vec![(PyValue::str("root"), PyValue::str(root))])
}

fn sight(n: usize) {
    let views = (0..n)
        .map(|i| view(&format!("/v/{i}"), i as i64, "x"))
        .collect();
    let (mut m, k) = over(views);
    let k = m.touch_view(k, &location(&at("/new"))).unwrap();
    assert_eq!(k.views.len(), n + 1);
}

#[test]
fn many_views_grow_linearly() {
    timing::grows_within(
        "touch_view: 2,000 and 20,000 views",
        timing::TENFOLD,
        &mut || timing::once(|| sight(2_000)),
        &mut || timing::once(|| sight(20_000)),
    );
}

#[test]
fn a_long_path_and_stamps_at_the_ends() {
    let long = "/".to_string() + &"a".repeat(1_000_000);
    for last in [
        "0001-01-01T00:00:00Z",
        "9999-12-31T23:59:59.999999Z",
        "9999-12-31T23:59:59-23:59",
        "",
        "Z",
    ] {
        let (mut m, k) = over(vec![view(&long, 5, last)]);
        let k = m
            .touch_view(k, &location(&at(&(long.clone() + "///"))))
            .unwrap();
        assert_eq!(k.views.len(), 1);
    }
}

#[test]
fn the_order_at_its_bounds() {
    // The store's next number is its own; the recording store's table is
    // emptied so its count does not pass i64::MAX.
    let (mut m, k) = over(vec![view("/a", i64::MIN, "x"), view("/b", i64::MAX, "x")]);
    m.store().0.borrow_mut().tables.clear();
    let k = m.touch_view(k, &location(&at("/a"))).unwrap();
    assert_eq!(k.views.len(), 2);
    // The view rewritten takes the store's next number (1 here): /b stays newest.
    assert_eq!(m.checkout_name(&k), cps("b"));
}
