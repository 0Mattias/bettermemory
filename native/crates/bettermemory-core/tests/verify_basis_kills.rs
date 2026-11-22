//! Cases a plausible wrong port of `verify_basis` and `verify::status`
//! fails: `bisect_left` for `bisect_right`, ordering by wall clock across
//! offsets, reading the give-up count after the walk, reading the dates
//! where the walk answered, the ellipsis at three claims, `claim_drift`
//! only on a positive count, a clean block keeping its drifted claims.

use std::collections::BTreeMap;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_history::{HistoryUnread, ReachableWalk, Unwalked};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::status::{
    claim_drift_recommendation, commit_drift_status, drift_recommendation, CommitDriftStatus,
    ResolvedCommitDrift,
};
use bettermemory_core::verify_basis::{
    count_since_verify, date_count_uncertainty, dates_since, unread_recommendation, AnchorWalk,
    BASIS_AUTHOR_DATE, BASIS_REACHABILITY,
};

fn at(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).expect("a datetime")
}

fn count(dates: &[DateTime], since: &str) -> usize {
    match dates_since(Some(Ok(dates)), &at(since)) {
        Ok(Some(Ok(n))) => n,
        other => panic!("{other:?}"),
    }
}

#[test]
fn an_equal_instant_is_no_drift() {
    let d = [
        at("2026-01-01T10:00:00+00:00"),
        at("2026-01-01T10:00:00+00:00"),
    ];
    assert_eq!(count(&d, "2026-01-01T10:00:00+00:00"), 0);
    assert_eq!(count(&d, "2026-01-01T09:59:59.999999+00:00"), 2);
}

#[test]
fn instants_order_across_offsets() {
    let d = [at("2026-01-01T12:00:00+02:00")];
    assert_eq!(count(&d, "2026-01-01T10:00:00+00:00"), 0);
    assert_eq!(count(&d, "2026-01-01T11:00:00+00:00"), 0);
    assert_eq!(count(&d, "2026-01-01T09:00:00+00:00"), 1);
}

#[test]
fn a_naive_stamp_raises_only_where_compared() {
    let naive = at("2026-01-01T10:00:00");
    assert!(dates_since(Some(Ok(&[])), &naive).is_ok());
    assert!(dates_since(Some(Ok(&[at("2026-01-01T10:00:00Z")])), &naive).is_err());
    assert_eq!(
        dates_since(Some(Err(HistoryUnread::Shallow)), &naive),
        Ok(Some(Err(HistoryUnread::Shallow)))
    );
}

#[test]
fn uncertainty_only_for_a_date_zero() {
    assert_eq!(
        date_count_uncertainty(0, BASIS_AUTHOR_DATE),
        Some(HistoryUnread::UnconfirmedCut)
    );
    assert_eq!(date_count_uncertainty(0, BASIS_REACHABILITY), None);
    assert_eq!(date_count_uncertainty(1, BASIS_AUTHOR_DATE), None);
}

#[test]
fn every_cause_has_its_own_text() {
    let all = [
        HistoryUnread::Timeout,
        HistoryUnread::OverBudget,
        HistoryUnread::Shallow,
        HistoryUnread::Error,
        HistoryUnread::UnconfirmedCut,
        HistoryUnread::CallBudget,
    ];
    let texts: std::collections::BTreeSet<&str> =
        all.iter().map(|w| unread_recommendation(*w)).collect();
    assert_eq!(texts.len(), 6);
    assert!(unread_recommendation(HistoryUnread::Shallow).contains("git fetch --unshallow"));
}

struct Walk {
    walk: Option<usize>,
    give: Option<HistoryUnread>,
}

impl AnchorWalk for Walk {
    fn toplevel_and_head(&mut self, _cwd: &[u32]) -> Option<(PyStr, PyStr)> {
        Some((cps("top"), cps("head")))
    }

    fn commits_since_anchor(
        &mut self,
        _cwd: &[u32],
        anchor: &[u32],
        _toplevel: &[u32],
        head: &[u32],
        unwalked: &mut Unwalked,
    ) -> Option<ReachableWalk> {
        if let Some(w) = self.give {
            unwalked.note(w);
        }
        self.walk.map(|n| ReachableWalk {
            anchor: anchor.to_vec(),
            head: head.to_vec(),
            commits: vec![cps("c"); n],
            touched: BTreeMap::new(),
            listed: true,
            timed_out: false,
        })
    }
}

#[test]
fn an_earlier_give_up_does_not_make_the_row_unread() {
    let mut unwalked = Unwalked::new();
    unwalked.note(HistoryUnread::Timeout);
    let mut origin = Walk {
        walk: None,
        give: None,
    };
    let got = count_since_verify(
        &mut origin,
        &mut unwalked,
        &cps("cwd"),
        &at("2026-01-01T10:00:00Z"),
        Some(&cps("a")),
        || Some(Ok(vec![at("2027-01-01T00:00:00Z")])),
    )
    .expect("no TypeError")
    .expect("applies")
    .expect("read");
    assert_eq!((got.count, got.basis), (1, BASIS_AUTHOR_DATE));
    assert_eq!(got.toplevel, Some(cps("top")));
}

#[test]
fn a_give_up_in_the_walk_is_unread_and_reads_no_dates() {
    let mut unwalked = Unwalked::new();
    let mut origin = Walk {
        walk: None,
        give: Some(HistoryUnread::Error),
    };
    let got = count_since_verify(
        &mut origin,
        &mut unwalked,
        &cps("cwd"),
        &at("2026-01-01T10:00:00Z"),
        Some(&cps("a")),
        || panic!("the dates are not read"),
    );
    assert_eq!(got, Ok(Some(Err(HistoryUnread::Error))));
}

#[test]
fn a_walk_that_answers_stands_and_reads_no_dates() {
    let mut unwalked = Unwalked::new();
    let mut origin = Walk {
        walk: Some(0),
        give: Some(HistoryUnread::Timeout),
    };
    let got = count_since_verify(
        &mut origin,
        &mut unwalked,
        &cps("cwd"),
        &at("2026-01-01T10:00:00Z"),
        Some(&cps("a")),
        || panic!("the dates are not read"),
    )
    .expect("no TypeError")
    .expect("applies")
    .expect("read");
    assert_eq!((got.count, got.basis), (0, BASIS_REACHABILITY));
}

fn texts(n: usize) -> Vec<PyStr> {
    (0..n).map(|i| cps(&format!("c{i}"))).collect()
}

#[test]
fn the_ellipsis_comes_past_three() {
    let three = claim_drift_recommendation(2, &texts(3));
    assert!(!three.contains(&0x2026));
    let four = claim_drift_recommendation(2, &texts(4));
    let text: String = four.iter().map(|&c| char::from_u32(c).unwrap()).collect();
    assert!(text.contains("(c0, c1, c2\u{2026})"));
    assert!(text.starts_with("2 commits since the last memory_verify implicate 4 declared claims"));
    let one: String = claim_drift_recommendation(1, &texts(1))
        .iter()
        .map(|&c| char::from_u32(c).unwrap())
        .collect();
    assert!(
        one.starts_with("1 commit since the last memory_verify implicates 1 declared claim (c0)")
    );
    assert!(drift_recommendation(0).starts_with("0 commits touching"));
    assert!(drift_recommendation(-1).starts_with("-1 commits touching"));
}

fn keys(v: &PyValue) -> usize {
    match v {
        PyValue::Dict(p) => p.len(),
        _ => panic!("a dict"),
    }
}

#[test]
fn claim_drift_rides_any_nonzero_check() {
    let mut s = CommitDriftStatus {
        status: "drift".into(),
        commits_since_verify: 1,
        recommendation: None,
        claims_checked: -1,
        claims_drifted: Vec::new(),
        basis: BASIS_AUTHOR_DATE.into(),
    };
    assert_eq!(keys(&s.to_dict()), 5);
    s.claims_checked = 0;
    assert_eq!(keys(&s.to_dict()), 4);
}

#[test]
fn a_clean_block_names_no_claim() {
    let r = ResolvedCommitDrift {
        count: 0,
        claims_checked: 2,
        claims_drifted: texts(2),
        basis: BASIS_REACHABILITY.into(),
    };
    let s = commit_drift_status(&r);
    assert_eq!(s.status, "clean");
    assert!(s.claims_drifted.is_empty());
    assert_eq!(s.claims_checked, 2);
    assert_eq!(s.recommendation, None);
    let d = commit_drift_status(&ResolvedCommitDrift { count: -3, ..r });
    assert_eq!(d.status, "drift");
    assert_eq!(d.claims_drifted.len(), 2);
}

#[test]
fn a_one_element_history_counts_on_both_sides() {
    // A midpoint rounded up probes past the end of a one-element history.
    let d = [at("2026-01-01T10:00:00+00:00")];
    assert_eq!(count(&d, "2026-01-01T09:00:00+00:00"), 1);
    assert_eq!(count(&d, "2026-01-01T10:00:00+00:00"), 0);
    assert_eq!(count(&d, "2026-01-01T11:00:00+00:00"), 0);
    let two = [
        at("2026-01-01T10:00:00+00:00"),
        at("2026-01-01T12:00:00+00:00"),
    ];
    assert_eq!(count(&two, "2026-01-01T11:00:00+00:00"), 1);
}
