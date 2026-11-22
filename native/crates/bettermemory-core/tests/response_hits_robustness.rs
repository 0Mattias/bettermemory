//! Hostile input for `response_rows`: very long path lists and texts,
//! integer bounds on the threshold and the counts, the far ends of the
//! calendar. No panic in debug or release, and time linear in the lists.

mod timing;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::records::summaries::MemoryHit;
use bettermemory_core::records::Confidence;
use bettermemory_core::response_rows::ResponseBuilder;
use bettermemory_core::unicode;

fn at(text: &str) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(text)).expect("a datetime")
}

fn hit(n: usize) -> MemoryHit {
    let mut h = MemoryHit::new(
        vec![0x41; 26],
        vec![vec![0x67]; n],
        Confidence::Medium,
        vec![0xdc80; n],
        f64::NAN,
        at("0001-01-01T00:00:00+00:00"),
        at("9999-12-31T23:59:59.999999+00:00"),
    );
    let ps: Vec<Vec<u32>> = (0..n).map(|i| unicode::cps(&format!("p{i}"))).collect();
    h.path_drift_checked_paths = ps.clone();
    h.path_drift_missing_paths = ps.clone();
    h.path_drift_verified_paths = ps.clone();
    h.path_drift_expected_absent_paths = ps.clone();
    h.path_drift_present_but_attested_absent = ps.clone();
    h.path_drift_dropped_as_route_paths = ps.clone();
    h.path_drift_claim_anchored_missing_paths = ps;
    h.path_drift_checked = i64::MIN;
    h.path_drift_missing = i64::MAX;
    h
}

#[test]
fn integer_and_calendar_bounds() {
    let now = at("9999-12-31T23:59:59.999999+00:00");
    for s in [i64::MIN, -1, 0, 1, i64::MAX] {
        for lv in [
            None,
            Some(at("0001-01-01T00:00:00+00:00")),
            Some(at("9999-12-31T23:59:59.999999+00:00")),
        ] {
            let mut h = hit(3);
            h.last_verified_at = lv;
            let out = ResponseBuilder::new(s).hit_to_dict(&h, now).expect("aware");
            assert!(out.get("path_drift").is_some());
            let _ = ResponseBuilder::new(s).hit_to_dict(&h, at("0001-01-01T00:00:00+00:00"));
        }
    }
}

#[test]
fn large_lists() {
    let out = ResponseBuilder::new(30)
        .hit_to_dict(&hit(200_000), at("2026-01-01T00:00:00+00:00"))
        .expect("ok");
    assert!(out.get("path_drift").is_some());
}

#[test]
fn hit_grows_linearly() {
    let small = hit(5_000);
    let large = hit(50_000);
    let b = ResponseBuilder::new(30);
    let now = at("2026-01-01T00:00:00+00:00");
    timing::grows_within(
        "hit_to_dict: 5,000 and 50,000 paths per bucket",
        timing::TENFOLD,
        &mut || timing::once(|| b.hit_to_dict(&small, now)),
        &mut || timing::once(|| b.hit_to_dict(&large, now)),
    );
}
