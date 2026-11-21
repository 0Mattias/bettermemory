//! Hostile inputs for the response leaves: large snapshots, many keys,
//! integer bounds; no panic, time linear in the input.

mod common;
mod timing;

use bettermemory_core::response_memo::DriftMemo;
use bettermemory_core::verdict_signals::{verdict_from_signals, VERDICT_REQUIRED};
use common::response_leaf::{answer, Snapshot};

fn star(n: u64) -> Snapshot {
    (1..=n)
        .map(|i| {
            (
                i,
                vec![("extends".to_string(), 0, Some(format!("{}", i % 7)))],
            )
        })
        .collect()
}

#[test]
fn links_one_hub_many_spokes() {
    let got = answer(&star(20_000), &[0]);
    assert_eq!(got[0].2.len(), 20_000);
    assert!(got[0].2.windows(2).all(|w| w[0].1 <= w[1].1));
}

#[test]
fn links_grow_linearly() {
    let small = star(2_000);
    let large = star(20_000);
    timing::grows_within(
        "candidate_link_rows: 2,000 and 20,000 links",
        timing::TENFOLD,
        &mut || timing::once(|| answer(&small, &[0])),
        &mut || timing::once(|| answer(&large, &[0])),
    );
}

fn churn(n: u64) {
    let m: DriftMemo<u64, u64> = DriftMemo::with_cap(1_000);
    for i in 0..n {
        m.remember(&[(i, i)], &[(i, i)], i as f64);
        let _ = m.recall(&(i / 2), i as f64);
    }
    assert_eq!(m.memo_keys().len() as u64, n.min(1_000));
}

#[test]
fn memo_churn_grows_linearly() {
    timing::grows_within(
        "DriftMemo: 2,000 and 20,000 remembers past the cap",
        timing::TENFOLD,
        &mut || timing::once(|| churn(2_000)),
        &mut || timing::once(|| churn(20_000)),
    );
}

#[test]
fn memo_odd_clocks_do_not_panic() {
    let m: DriftMemo<u8, u8> = DriftMemo::new();
    for now in [f64::INFINITY, f64::NEG_INFINITY, f64::NAN, f64::MAX, -0.0] {
        m.remember(&[], &[(1, 1)], now);
        for at in [f64::INFINITY, f64::NEG_INFINITY, f64::NAN, 0.0] {
            let _ = m.recall(&1, at);
        }
    }
}

#[test]
fn verdict_integer_bounds() {
    for v in [i64::MIN, -1, 0, 1, i64::MAX] {
        let _ = verdict_from_signals("fresh", v, Some(v), v, v);
    }
    assert_eq!(
        verdict_from_signals("never", i64::MAX, None, 0, 0),
        VERDICT_REQUIRED
    );
}
