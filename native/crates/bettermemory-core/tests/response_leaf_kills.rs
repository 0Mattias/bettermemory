//! Cases a plausible wrong port of the response leaves fails.

mod common;

use bettermemory_core::response_memo::{DriftMemo, TIMED_OUT_SECONDS};
use bettermemory_core::verdict_signals::{
    commit_leg_escalates, verdict_from_signals, VERDICT_FRESH, VERDICT_RECOMMENDED,
    VERDICT_REQUIRED,
};
use common::response_leaf::{answer, Snapshot};

fn l(kind: &str, target: u64, note: Option<&str>) -> (String, u64, Option<String>) {
    (kind.to_string(), target, note.map(str::to_string))
}

#[test]
fn links_sort_by_type_before_id() {
    let snap: Snapshot = vec![(1, vec![l("supersedes", 2, None), l("extends", 9, None)])];
    let got = answer(&snap, &[1]);
    assert_eq!(
        got[0].1,
        vec![l("extends", 9, None), l("supersedes", 2, None)]
    );
}

#[test]
fn links_ties_keep_snapshot_order_not_note_order() {
    let snap: Snapshot = vec![(
        1,
        vec![
            l("extends", 2, Some("z")),
            l("extends", 2, None),
            l("extends", 2, Some("a")),
        ],
    )];
    let got = answer(&snap, &[1]);
    assert_eq!(
        got[0].1,
        vec![
            l("extends", 2, Some("z")),
            l("extends", 2, None),
            l("extends", 2, Some("a"))
        ]
    );
}

#[test]
fn links_duplicates_collapse_on_the_note_too() {
    let snap: Snapshot = vec![
        (
            1,
            vec![l("extends", 2, Some("a")), l("extends", 2, Some("a"))],
        ),
        (
            1,
            vec![l("extends", 2, Some("a")), l("extends", 2, Some("b"))],
        ),
    ];
    let got = answer(&snap, &[2]);
    assert_eq!(
        got[0].2,
        vec![l("extends", 1, Some("a")), l("extends", 1, Some("b"))]
    );
}

#[test]
fn links_rows_follow_the_wanted_order_not_a_hash() {
    let wanted: Vec<u64> = (0..200).rev().chain([5, 7, 5]).collect();
    let got = answer(&Vec::new(), &wanted);
    let ids: Vec<u64> = got.iter().map(|r| r.0).collect();
    assert_eq!(ids, (0..200).rev().collect::<Vec<_>>());
}

#[test]
fn links_self_link_is_both_ways() {
    let snap: Snapshot = vec![(4, vec![l("depends_on", 4, None)])];
    let got = answer(&snap, &[4]);
    assert_eq!(got[0].1, vec![l("depends_on", 4, None)]);
    assert_eq!(got[0].2, vec![l("depends_on", 4, None)]);
}

fn memo() -> DriftMemo<&'static str, i32> {
    DriftMemo::with_cap(2)
}

#[test]
fn memo_setting_a_held_key_keeps_its_place() {
    let m = memo();
    m.remember(&[("a", 1), ("b", 2)], &[], 0.0);
    m.remember(&[("a", 3), ("c", 4)], &[], 0.0);
    assert_eq!(m.memo_keys(), vec!["b", "c"]);
    assert_eq!(m.recall(&"a", 0.0), None);
}

#[test]
fn memo_a_hit_moves_to_the_end() {
    let m = memo();
    m.remember(&[("a", 1), ("b", 2)], &[], 0.0);
    assert_eq!(m.recall(&"a", 0.0), Some(1));
    m.remember(&[("c", 3)], &[], 0.0);
    assert_eq!(m.memo_keys(), vec!["a", "c"]);
}

#[test]
fn memo_timed_out_window_is_half_open() {
    let m = memo();
    m.remember(&[], &[("t", 1)], 10.0);
    assert_eq!(m.recall(&"t", 10.0), Some(1));
    assert_eq!(m.recall(&"t", 10.0 + TIMED_OUT_SECONDS - 1e-9), Some(1));
    assert_eq!(m.recall(&"t", 10.0 + TIMED_OUT_SECONDS), None);
    assert!(m.timed_out_keys().is_empty());
}

#[test]
fn memo_timed_out_hit_does_not_move_but_a_remember_restamps() {
    let m = memo();
    m.remember(&[], &[("a", 1), ("b", 2)], 0.0);
    assert_eq!(m.recall(&"a", 1.0), Some(1));
    assert_eq!(m.timed_out_keys(), vec!["a", "b"]);
    m.remember(&[], &[("a", 5)], 50.0);
    assert_eq!(m.timed_out_keys(), vec!["b", "a"]);
    assert_eq!(m.recall(&"a", 100.0), Some(5));
    assert_eq!(m.recall(&"b", 100.0), None);
}

#[test]
fn memo_clock_back_and_nan_remove() {
    let m = memo();
    m.remember(&[], &[("a", 1), ("b", 2)], 5.0);
    assert_eq!(m.recall(&"a", 4.999), None);
    assert_eq!(m.recall(&"b", f64::NAN), None);
    assert!(m.timed_out_keys().is_empty());
}

#[test]
fn memo_answers_before_timed_out() {
    let m = memo();
    m.remember(&[("k", 1)], &[("k", 2)], 0.0);
    assert_eq!(m.recall(&"k", 1e9), Some(1));
}

#[test]
fn verdict_none_and_non_positive_counts_never_escalate() {
    assert!(!commit_leg_escalates(None));
    assert!(!commit_leg_escalates(Some(0)));
    assert!(!commit_leg_escalates(Some(i64::MIN)));
    assert!(commit_leg_escalates(Some(1)));
    assert_eq!(verdict_from_signals("never", 0, None, 0, 0), VERDICT_FRESH);
    assert_eq!(
        verdict_from_signals("stale", -1, Some(-1), -1, -1),
        VERDICT_FRESH
    );
}

#[test]
fn verdict_remote_alone_is_required() {
    assert_eq!(
        verdict_from_signals("remote", 0, None, 0, 0),
        VERDICT_REQUIRED
    );
}

#[test]
fn verdict_raise_is_exact_text() {
    assert_eq!(
        verdict_from_signals("Never", 1, None, 0, 0),
        VERDICT_RECOMMENDED
    );
    assert_eq!(
        verdict_from_signals("stale ", 1, None, 0, 0),
        VERDICT_RECOMMENDED
    );
    assert_eq!(
        verdict_from_signals("stale", 0, None, 0, 1),
        VERDICT_REQUIRED
    );
}

#[test]
fn verdict_cited_missing_never_raises_to_required() {
    assert_eq!(
        verdict_from_signals("never", 0, None, 5, 0),
        VERDICT_RECOMMENDED
    );
}
