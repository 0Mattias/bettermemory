//! Cases a plausible wrong `origin_history` fails, each with the
//! reference's answer (`gen_origin_history` pins them all too).

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use bettermemory_core::datetime::LAST_SECOND;
use bettermemory_core::origin_history::{
    author_dates, author_instant, log_stdin, read_lines, unanswered, HistoryUnread, Key,
    ReachableWalk, TimestampsMemo, Unwalked, FAILURE_SECONDS, MEMO_CAP, MEMO_SLOTS,
};
use bettermemory_core::pyvalue::PyStr;

fn t(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn k(h: &str) -> Key {
    (t("r"), t(h))
}

fn dates(n: usize) -> Arc<Vec<bettermemory_core::datetime::DateTime>> {
    Arc::new(vec![author_instant(0); n])
}

fn cut(items: &[&str]) -> BTreeSet<PyStr> {
    items.iter().map(|s| t(s)).collect()
}

#[test]
fn a_failure_under_another_guard_is_forgotten_not_kept() {
    let mut m: TimestampsMemo<u32> = TimestampsMemo::new();
    m.remember_failure(&k("h"), Some(1), HistoryUnread::Error, 0.0);
    assert_eq!(m.recall(&k("h"), Some(&2), None, 1.0), (None, None));
    // Asked again under the guard it was met under: gone.
    assert_eq!(m.recall(&k("h"), Some(&1), None, 1.0), (None, None));
}

#[test]
fn the_failure_window_is_half_open_and_the_clock_going_back_ends_it() {
    for (dt, kept) in [
        (FAILURE_SECONDS - 1e-9, true),
        (FAILURE_SECONDS, false),
        (-1e-9, false),
        (0.0, true),
    ] {
        let mut m: TimestampsMemo<u32> = TimestampsMemo::new();
        m.remember_failure(&k("h"), None, HistoryUnread::Timeout, 100.0);
        let got = m.recall(&k("h"), None, None, 100.0 + dt).1;
        assert_eq!(got.is_some(), kept, "dt {dt}");
    }
}

#[test]
fn a_call_budget_is_never_remembered() {
    let mut m: TimestampsMemo<u32> = TimestampsMemo::new();
    m.remember_failure(&k("h"), None, HistoryUnread::CallBudget, 0.0);
    assert!(m.failures().is_empty());
}

#[test]
fn a_cut_must_be_a_subset_and_none_never_serves() {
    let mut m: TimestampsMemo<u32> = TimestampsMemo::new();
    assert!(m.remember(&k("h"), dates(1), None, cut(&["a", "b"])));
    assert!(m.recall(&k("h"), None, Some(&cut(&["a"])), 0.0).0.is_some());
    assert!(m.recall(&k("h"), None, Some(&cut(&["c"])), 0.0).0.is_none());
    assert!(m.recall(&k("h"), None, None, 0.0).0.is_none());
}

#[test]
fn recall_touches_and_checked_cut_does_not() {
    let mut m: TimestampsMemo<u32> = TimestampsMemo::new();
    m.remember(&k("a"), dates(1), Some(1), cut(&[]));
    m.remember(&k("b"), dates(1), Some(1), cut(&[]));
    m.checked_cut(&k("a"));
    assert_eq!(m.base_for(&t("r"), Some(&1), &cut(&[])).unwrap().0, t("b"));
    m.recall(&k("a"), None, Some(&cut(&[])), 0.0);
    assert_eq!(m.base_for(&t("r"), Some(&1), &cut(&[])).unwrap().0, t("a"));
    // The guard and the cut must be equal, not merely related.
    assert!(m.base_for(&t("r"), Some(&1), &cut(&["x"])).is_none());
    assert!(m.base_for(&t("r"), None, &cut(&[])).is_none());
}

#[test]
fn recall_returns_the_stored_list_itself() {
    let mut m: TimestampsMemo<u32> = TimestampsMemo::new();
    let d = dates(3);
    m.remember(&k("h"), Arc::clone(&d), None, cut(&[]));
    assert!(Arc::ptr_eq(
        &m.recall(&k("h"), None, Some(&cut(&[])), 0.0).0.unwrap(),
        &d
    ));
}

#[test]
fn the_caps_evict_oldest_first() {
    let mut m: TimestampsMemo<u32> = TimestampsMemo::new();
    for i in 0..=MEMO_CAP {
        m.remember(&k(&i.to_string()), dates(1), None, cut(&[]));
    }
    assert_eq!(m.entries().len(), MEMO_CAP);
    assert_eq!(m.entries()[0].0, k("1"));
    assert!(m.remember(&k("big"), dates(MEMO_SLOTS), None, cut(&[])));
    assert_eq!(m.entries().len(), 1);
    m.remember_failure(&k("f"), None, HistoryUnread::Error, 0.0);
    assert!(!m.remember(&k("f"), dates(MEMO_SLOTS + 1), None, cut(&[])));
    assert_eq!(
        m.failures().len(),
        1,
        "a list over the slots leaves the failure"
    );
    for i in 0..=MEMO_CAP {
        m.remember_failure(&k(&format!("x{i}")), None, HistoryUnread::Error, 0.0);
    }
    assert_eq!(m.failures().len(), MEMO_CAP);
    assert_eq!(m.failures()[0].0, k("x1"));
}

#[test]
fn the_latest_date_keeps_its_microseconds() {
    assert_eq!(
        author_instant(i128::from(LAST_SECOND) + 1).isoformat(),
        "9999-12-31T23:59:59.999999+00:00"
    );
    assert_eq!(
        author_instant(i128::from(LAST_SECOND)).isoformat(),
        "9999-12-31T23:59:59+00:00"
    );
    assert_eq!(
        author_instant(-62_135_596_801).isoformat(),
        "0001-01-01T00:00:00+00:00"
    );
    let sorted: Vec<String> = author_dates(&[5, -1, 0])
        .iter()
        .map(|d| d.isoformat())
        .collect();
    assert_eq!(sorted[0], "1969-12-31T23:59:59+00:00");
}

#[test]
fn stdin_lines_follow_the_reference() {
    assert_eq!(log_stdin(&[], &[t("-p")]).unwrap(), b"--\n-p\n");
    assert_eq!(log_stdin(&[t("a\r")], &[]), None);
    assert_eq!(log_stdin(&[], &[t("")]), None);
    assert_eq!(log_stdin(&[vec![0xDC80]], &[]).unwrap(), b"\x80\n--\n");
    assert_eq!(log_stdin(&[vec![0xDC7F]], &[]), None);
    assert_eq!(log_stdin(&[t("a\0b")], &[]).unwrap(), b"a\0b\n--\n");
}

#[test]
fn lines_split_on_line_feeds_alone() {
    let r = read_lines(&t("1\r\n2\n"), |l| Some(l.len()));
    assert_eq!(r, Some(vec![2, 1]));
    assert_eq!(
        read_lines(&t("\n\n"), |l| (!l.is_empty()).then_some(())),
        None
    );
    assert_eq!(read_lines(&t(""), |_| None::<()>), Some(vec![]));
}

#[test]
fn shas_take_descendants_not_siblings() {
    let touched: BTreeMap<PyStr, BTreeSet<PyStr>> =
        [("a", "1"), ("a/b", "2"), ("ab", "3"), ("a.b", "4")]
            .iter()
            .map(|(p, c)| (t(p), cut(&[c])))
            .collect();
    let walk = ReachableWalk {
        anchor: t("x"),
        head: t("y"),
        commits: vec![t("9")],
        touched,
        listed: true,
        timed_out: false,
    };
    assert_eq!(walk.shas_touching(&[t("a//")]), cut(&["2"]));
    assert_eq!(walk.shas_touching(&[t("a")]), cut(&["1", "2"]));
    let unlisted = ReachableWalk {
        listed: false,
        ..walk
    };
    assert_eq!(unlisted.shas_touching(&[]), cut(&["9"]));
}

#[test]
fn unanswered_and_the_tally() {
    assert_eq!(unanswered((0, 0), (0, 0), false), None);
    assert_eq!(unanswered((0, 0), (0, 0), true), Some(HistoryUnread::Error));
    assert_eq!(
        unanswered((0, 0), (1, 1), false),
        Some(HistoryUnread::Timeout)
    );
    let mut u = Unwalked::new();
    assert_eq!(u.given_up_since(1), Some(HistoryUnread::Timeout));
    u.note(HistoryUnread::Error);
    assert_eq!(u.given_up_since(0), Some(HistoryUnread::Error));
    assert_eq!(u.given_up_since(1), None);
}
