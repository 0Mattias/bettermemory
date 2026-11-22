//! Hostile inputs for `verify_basis` and `verify::status`: long histories,
//! many claims, integer bounds; no panic, time logarithmic or linear in
//! the input.

mod timing;

use bettermemory_core::claims::{Claim, Kind};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::status::{
    claim_drift_recommendation, commit_drift_status, drift_recommendation, gated_claims,
    unreadable, ResolvedCommitDrift,
};
use bettermemory_core::verify_basis::dates_since;

fn history(n: i64) -> Vec<DateTime> {
    (0..n)
        .map(|i| DateTime::from_unix_micros_utc(i * 1_000_000).expect("an instant"))
        .collect()
}

#[test]
fn a_million_dates() {
    let h = history(1_000_000);
    let mid = DateTime::from_unix_micros_utc(500_000 * 1_000_000).expect("an instant");
    assert_eq!(dates_since(Some(Ok(&h)), &mid), Ok(Some(Ok(499_999))));
    let low = DateTime::new(1, 1, 1, 0, 0, 0, 0, Some(-86_399_999_999)).expect("min");
    assert_eq!(dates_since(Some(Ok(&h)), &low), Ok(Some(Ok(1_000_000))));
    let high = DateTime::new(9999, 12, 31, 23, 59, 59, 999_999, Some(86_399_999_999)).expect("max");
    assert_eq!(dates_since(Some(Ok(&h)), &high), Ok(Some(Ok(0))));
}

#[test]
fn the_count_does_not_grow_with_the_history() {
    let small = history(10_000);
    let large = history(100_000);
    let since = DateTime::from_unix_micros_utc(5_000 * 1_000_000).expect("an instant");
    timing::grows_within(
        "dates_since: 10,000 and 100,000 dates",
        timing::TENFOLD,
        &mut || timing::once(|| dates_since(Some(Ok(&small)), &since)),
        &mut || timing::once(|| dates_since(Some(Ok(&large)), &since)),
    );
}

#[test]
fn integer_bounds() {
    for c in [i64::MIN, i64::MAX, 0, 1, -1] {
        assert!(!drift_recommendation(c).is_empty());
        let r = ResolvedCommitDrift {
            count: c,
            claims_checked: c,
            claims_drifted: vec![cps("a")],
            basis: String::new(),
        };
        let _ = commit_drift_status(&r).to_dict();
    }
}

fn many(n: usize) -> Vec<Vec<u32>> {
    (0..n).map(|i| cps(&format!("p/{i}/../x.py"))).collect()
}

#[test]
fn many_claims() {
    let drifted = many(200_000);
    assert!(claim_drift_recommendation(2, &drifted).len() < 400);
    let claims: Vec<Claim> = drifted
        .iter()
        .map(|p| Claim {
            kind: Kind::Path,
            rel_path: p.clone(),
            name: vec![],
            value: vec![],
        })
        .collect();
    assert!(!unreadable(&claims));
    assert_eq!(gated_claims(&claims).len(), 200_000);
    let deep: Vec<u32> = std::iter::repeat_n(cps("../"), 100_000).flatten().collect();
    let one = [Claim {
        kind: Kind::Path,
        rel_path: deep,
        name: vec![],
        value: vec![],
    }];
    assert!(gated_claims(&one).is_empty());
}

#[test]
fn the_recommendation_grows_linearly_in_claims() {
    let small = many(20_000);
    let large = many(200_000);
    let claims = |v: &[Vec<u32>]| -> Vec<Claim> {
        v.iter()
            .map(|p| Claim {
                kind: Kind::Path,
                rel_path: p.clone(),
                name: vec![],
                value: vec![],
            })
            .collect()
    };
    let (cs, cl) = (claims(&small), claims(&large));
    timing::grows_within(
        "gated_claims: 20,000 and 200,000 claims",
        timing::TENFOLD,
        &mut || timing::once(|| gated_claims(&cs)),
        &mut || timing::once(|| gated_claims(&cl)),
    );
}
