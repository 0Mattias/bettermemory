//! Cases a plausible wrong unit health_model-2 fails, each with the
//! reference's answer (read off CPython 3.13.13) and, where the wrong form
//! is a one-line rewrite, that form's answer beside it.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::health_model::{
    self as hm, ColdEndorsementMemories, CommitDriftDebt, CommitDriftRow, CrossRepoDrift,
    CrossRepoDriftGroup, HealthModelError, MemoryStats, VerificationDebt,
};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::origin_history::HistoryUnread as H;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::{self, cps};

fn d1() -> DateTime {
    DateTime::new(2026, 3, 1, 12, 0, 0, 0, Some(0)).expect("a datetime")
}

fn naive() -> DateTime {
    DateTime::new(2026, 3, 1, 12, 0, 0, 0, None).expect("a datetime")
}

fn memory() -> MemoryStats {
    MemoryStats::new(cps("m"), vec![cps("global")], cps("s"), d1(), d1())
}

/// A row whose `to_dict` raises TypeError: a contradiction ordered
/// against a naive `last_verified_at`.
fn raising() -> MemoryStats {
    let mut m = memory();
    m.last_contradicted_at = Some(d1());
    m.last_verified_at = Some(naive());
    m
}

fn dumps(v: &PyValue) -> String {
    let text = json::dumps(v, DumpOptions::DEFAULT).expect("dumps");
    unicode::to_string(&text).expect("text")
}

fn keys(v: &PyValue) -> Vec<String> {
    let PyValue::Dict(items) = v else {
        panic!("a dict")
    };
    items
        .iter()
        .map(|(k, _)| match k {
            PyValue::Str(s) => unicode::to_string(s).expect("text"),
            other => panic!("a str key: {other:?}"),
        })
        .collect()
}

fn ids(names: &[&str]) -> Vec<PyStr> {
    names.iter().map(|s| cps(s)).collect()
}

#[test]
fn unread_fields_sorts_the_causes_and_keeps_the_ids_in_insertion_order() {
    let m = vec![
        (cps("b"), H::Timeout),
        (cps("a"), H::Error),
        (cps("c"), H::CallBudget),
        (cps("d"), H::Error),
    ];
    assert_eq!(
        dumps(&hm::unread_fields(&m).to_dict()),
        r#"{"unread": 4, "unread_causes": {"call_budget": 1, "error": 2, "timeout": 1}, "unread_ids": ["b", "a", "c", "d"]}"#
    );
}

#[test]
fn unread_dict_keeps_the_causes_in_the_order_given() {
    // `dict(causes)`, not `dict(sorted(causes.items()))`.
    let causes = vec![(cps("timeout"), 1), (cps("error"), 1)];
    let d = PyValue::Dict(
        hm::unread_dict(2, &causes, &ids(&["b", "a"]))
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    );
    assert_eq!(
        dumps(&d),
        r#"{"unread": 2, "unread_causes": {"timeout": 1, "error": 1}, "unread_ids": ["b", "a"]}"#
    );
    // Nor are the ids capped there: 25 given, 25 written.
    let many: Vec<PyStr> = (0..25).map(|j| cps(&format!("i{j}"))).collect();
    let d = hm::unread_dict(25, &[], &many);
    let PyValue::List(listed) = &d[2].1 else {
        panic!("a list")
    };
    assert_eq!(listed.len(), 25);
}

#[test]
fn unread_ids_are_a_list_so_a_repeated_id_is_copied() {
    // `list(ids)`: the reference writes both, not a set of them.
    let mut d = CommitDriftDebt::new(Some(cps("r")), None);
    d.unread = 2;
    d.unread_ids = ids(&["a", "a"]);
    assert_eq!(
        dumps(&d.to_dict()),
        r#"{"current_repo": "r", "current_cwd": null, "total_drifted": 0, "rows": [], "unread": 2, "unread_causes": {}, "unread_ids": ["a", "a"]}"#
    );
}

// The causes are a dict and `unread_fields` reads a mapping, so their keys
// are distinct; a port given a repeated one refuses it in debug builds
// rather than write a value no reference dict holds (two entries, or the
// first or last of them alone).
#[cfg(debug_assertions)]
#[test]
#[should_panic(expected = "unread_causes holds a repeated key")]
fn a_repeated_cause_key_is_refused() {
    let causes = vec![(cps("timeout"), 1), (cps("timeout"), 2)];
    let _ = hm::unread_dict(3, &causes, &[]);
}

#[cfg(debug_assertions)]
#[test]
#[should_panic(expected = "unread_causes holds a repeated key")]
fn a_group_with_a_repeated_cause_key_is_refused() {
    let mut g = CrossRepoDriftGroup::new(cps("r"), cps("w"));
    g.unread = 2;
    g.unread_causes = vec![(cps("error"), 1), (cps("timeout"), 1), (cps("error"), 1)];
    let _ = g.to_dict();
}

#[cfg(debug_assertions)]
#[test]
#[should_panic(expected = "unread_fields holds a repeated id")]
fn a_repeated_unread_id_is_refused() {
    let m = vec![(cps("a"), H::Timeout), (cps("a"), H::Error)];
    let _ = hm::unread_fields(&m);
}

#[test]
fn unread_ids_are_capped_at_twenty_in_insertion_order() {
    // 21 ids counting down: the first twenty kept, i25 to i6, not the
    // twenty least after a sort and not all 21.
    let m: Vec<(PyStr, H)> = (0..21)
        .map(|j| (cps(&format!("i{}", 25 - j)), H::Shallow))
        .collect();
    let f = hm::unread_fields(&m);
    assert_eq!(f.unread, 21);
    assert_eq!(f.unread_ids.len(), hm::COMMIT_DRIFT_DEBT_CAP);
    assert_eq!(f.unread_ids.first(), Some(&cps("i25")));
    assert_eq!(f.unread_ids.last(), Some(&cps("i6")));
    assert_eq!(f.unread_causes, vec![(cps("shallow"), 21)]);
}

#[test]
fn unconfirmed_and_the_unread_keys_only_when_not_zero() {
    let mut d = CommitDriftDebt::new(Some(cps("r")), None);
    d.unread_causes = vec![(cps("x"), 1)];
    d.unread_ids = ids(&["x"]);
    assert_eq!(
        dumps(&d.to_dict()),
        r#"{"current_repo": "r", "current_cwd": null, "total_drifted": 0, "rows": []}"#
    );
    // A negative count is truthy: both written, the unread keys even with
    // nothing listed.
    let mut d = CommitDriftDebt::new(Some(cps("r")), None);
    d.unconfirmed = -1;
    d.unread = -1;
    assert_eq!(
        dumps(&d.to_dict()),
        r#"{"current_repo": "r", "current_cwd": null, "total_drifted": 0, "rows": [], "unconfirmed": -1, "unread": -1, "unread_causes": {}, "unread_ids": []}"#
    );
    // A group has no `unconfirmed`, and its unread keys follow
    // `last_checked_at`.
    let mut g = CrossRepoDriftGroup::new(cps("r"), cps("w"));
    g.unread = 1;
    assert_eq!(
        keys(&g.to_dict()),
        [
            "repo",
            "worktree_root",
            "total_drifted",
            "candidates",
            "rows",
            "last_checked_at",
            "unread",
            "unread_causes",
            "unread_ids"
        ]
    );
    g.unread = 0;
    assert_eq!(keys(&g.to_dict()).len(), 6);
}

#[test]
fn clamp_is_pythons_max_and_min_not_f64_clamp() {
    // max(0.0, -0.0) keeps its first argument: +0.0.
    assert_eq!(hm::clamp_ratio_threshold(-0.0).to_bits(), 0);
    assert_eq!((-0.0f64).clamp(0.0, 1.0).to_bits(), (-0.0f64).to_bits());
    // NaN is 0.0, where f64::clamp passes it through and f64::max drops it
    // only by the argument order.
    assert_eq!(hm::clamp_ratio_threshold(f64::NAN).to_bits(), 0);
    assert!(f64::NAN.clamp(0.0, 1.0).is_nan());
    assert_eq!(hm::clamp_ratio_threshold(-f64::NAN).to_bits(), 0);
    for (x, want) in [
        (f64::NEG_INFINITY, 0.0),
        (-1.0, 0.0),
        (-5e-324, 0.0),
        (5e-324, 5e-324),
        (0.5, 0.5),
        (1.0, 1.0),
        (1.5, 1.0),
        (f64::INFINITY, 1.0),
    ] {
        let got = hm::clamp_ratio_threshold(x);
        assert_eq!(got.to_bits(), f64::to_bits(want), "{x}");
    }
}

#[test]
fn weakly_endorsed_at_each_branch() {
    // No applies, or a negative count: not weakly endorsed, even with no
    // explicit one.
    assert!(!hm::weakly_endorsed(0, 0, 0.5));
    assert!(!hm::weakly_endorsed(-2, 0, 0.5));
    // Never explicit: weakly endorsed whatever the threshold, NaN too.
    assert!(hm::weakly_endorsed(3, 0, f64::NAN));
    assert!(hm::weakly_endorsed(3, 0, -1.0));
    // A NaN threshold is not <= 0.0, and ratio < NaN is false.
    assert!(!hm::weakly_endorsed(3, 1, f64::NAN));
    // -0.0 <= 0.0: off.
    assert!(!hm::weakly_endorsed(3, 1, -0.0));
    // Strictly below: the ratio equal to the threshold is not.
    assert!(!hm::weakly_endorsed(2, 1, 0.5));
    assert!(!hm::weakly_endorsed(10, 1, 0.1));
    assert!(hm::weakly_endorsed(3, 1, 0.5));
    assert!(!hm::weakly_endorsed(3, 3, 1.0));
    assert!(hm::weakly_endorsed(3, 3, f64::INFINITY));
    // A negative explicit count is a negative ratio.
    assert!(hm::weakly_endorsed(1, -1, 0.5));
    // `_is_weakly_endorsed` reads `applied_count`, not the auto plus
    // explicit sum.
    let mut m = memory();
    m.applied_count = 3;
    m.auto_applied_count = 100;
    m.explicit_applied_count = 1;
    assert!(hm::is_weakly_endorsed(&m, 0.5));
    m.applied_count = 2;
    assert!(!hm::is_weakly_endorsed(&m, 0.5));
}

#[test]
fn a_raising_row_raises_from_the_rollup() {
    let mut v = VerificationDebt::new(30);
    v.never_verified = vec![memory()];
    v.stale = vec![memory(), raising()];
    assert_eq!(v.to_dict(), Err(HealthModelError::TypeError));
    let mut v = VerificationDebt::new(30);
    v.never_verified = vec![raising()];
    assert_eq!(v.to_dict(), Err(HealthModelError::TypeError));
    let mut c = ColdEndorsementMemories::new(30);
    c.rows = vec![memory(), raising()];
    assert_eq!(c.to_dict(), Err(HealthModelError::TypeError));
    // A rollup with no such row answers.
    c.rows = vec![memory()];
    assert!(c.to_dict().is_ok());
}

#[test]
fn key_order_is_the_references_not_the_declarations() {
    let v = VerificationDebt::new(5).to_dict().expect("no rows");
    assert_eq!(
        keys(&v),
        [
            "stale_after_days",
            "never_verified_total",
            "never_verified_checkable",
            "stale_total",
            "stale_checkable",
            "fresh_count",
            "never_verified",
            "stale"
        ]
    );
    assert_eq!(
        keys(&CommitDriftDebt::new(None, None).to_dict()),
        ["current_repo", "current_cwd", "total_drifted", "rows"]
    );
    assert_eq!(
        keys(&CrossRepoDrift::default().to_dict()),
        ["total_drifted", "groups", "skipped", "unanchorable"]
    );
    assert_eq!(
        keys(&ColdEndorsementMemories::new(30).to_dict().expect("no rows")),
        ["min_retrievals", "total", "rows"]
    );
    assert_eq!(
        dumps(&hm::ActorSlices::default().to_dict()),
        r#"{"declared": [], "undeclared": {"client": null, "memories": 0, "memories_in_window": 0, "events": 0, "searches": 0, "applies": 0, "models": {}, "principals": {}}}"#
    );
}

#[test]
fn a_drift_rows_datetime_is_written_as_the_reference_writes_it() {
    // An offset of 30 seconds: isoformat_utc's "Z" and a tail; a naive
    // datetime without a zone; None only for None.
    let sec30 = DateTime::new(2026, 3, 1, 12, 0, 0, 0, Some(30_000_000)).expect("a datetime");
    let row = |dt: Option<DateTime>| CommitDriftRow::new(cps("i"), ids(&["s"]), cps("x"), dt, 1);
    assert_eq!(
        dumps(&row(Some(sec30)).to_dict()),
        r#"{"id": "i", "scopes": ["s"], "summary": "x", "last_verified_at": "2026-03-01T12:00:00Z:30", "commits_since_verify": 1, "basis": "author-date"}"#
    );
    assert_eq!(
        dumps(&row(Some(naive())).to_dict()),
        r#"{"id": "i", "scopes": ["s"], "summary": "x", "last_verified_at": "2026-03-01T12:00:00", "commits_since_verify": 1, "basis": "author-date"}"#
    );
    let mut g = CrossRepoDriftGroup::new(cps("r"), cps("w"));
    g.last_checked_at = Some(d1());
    assert!(dumps(&g.to_dict()).contains(r#""last_checked_at": "2026-03-01T12:00:00Z""#));
}

#[test]
fn the_caps() {
    assert_eq!(hm::VERIFICATION_DEBT_CAP, 20);
    assert_eq!(hm::COLD_ENDORSEMENT_MIN_RETRIEVALS, 30);
    assert_eq!(hm::COLD_ENDORSEMENT_CAP, 20);
    assert_eq!(hm::CROSS_REPO_MAX_ROOTS, 8);
    assert_eq!(hm::COMMIT_DRIFT_DEBT_CAP, 20);
    assert_eq!(hm::RECENT_SILENT_MISSES_CAP, 10);
}
